"""Player-network service: async jobs that assemble PRD section 5 results.

A job walks through stages (target page -> current team -> teammates ->
previous teams), spending one budget unit per fresh page fetch. Cached pages
are free, which is what makes budget-exhausted jobs cheap to resume.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

from app.config import Settings
from app.liquipedia import LiquipediaClient, ProviderError, RateLimited, page_url
from app.models import (
    CareerResult, CareerStint, Envelope, ErrorInfo, JobStatus, KinshipRecord,
    NetworkResult, OmittedRecords, OverlapWindow, Player, PreviousTeamGroup,
    RelationConfidence, RelationshipResult, RosterMember, RosterRelation,
    SharedTeamRecord, Source, StaffMember, TeamInfo, TeamRosterResult,
    TeamStatus, Teammate,
)
from app.parsing import (
    SquadEntry, classify_page, date_bounds, find_kinship, first_active_squad,
    name_key, other_names_for, overlap_verdict, parse_career, parse_player,
    parse_squads, parse_team_history, recent_previous_teams,
)
from app.store import Store


class _BudgetExhausted(Exception):
    """Raised internally when a job hits its page budget."""


class _Paused(Exception):
    """Raised when upstream throttling refuses to let the job continue."""


class InvalidResource(Exception):
    """The page exists but is not the kind of resource this tool needs.

    Tools turn this into an MCP ``isError`` result with a code and a
    concrete alternative, so an agent never receives a silent empty answer.
    """

    def __init__(self, given: str, resolved_as: str, expected: str, suggestion: str):
        self.given = given
        self.resolved_as = resolved_as
        self.expected = expected
        self.suggestion = suggestion
        if resolved_as == "unknown":
            message = f"could not confirm '{given}' is a {expected} page"
        else:
            message = f"'{given}' is a {resolved_as} page; expected a {expected} page"
        super().__init__(message)

    def as_error(self) -> ErrorInfo:
        return ErrorInfo(
            code="invalid_resource_type", message=str(self), given=self.given,
            resolved_as=self.resolved_as, suggestion=self.suggestion,
        )


_STAFF_HINTS = ("coach", "manager", "analyst", "owner", "ceo")


def _staff_identity(text: str) -> bool:
    return any(hint in text for hint in _STAFF_HINTS)


class PlayerNetworkService:
    """Owns the single background worker that processes lookup jobs."""

    def __init__(self, settings: Settings, store: Store, client: LiquipediaClient) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._missing_pages: set[str] = set()
        # Jobs this process owns (queued or being processed). A job with an
        # old heartbeat that is *not* in here was orphaned by a restart.
        self._known_jobs: set[str] = set()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # A restart leaves jobs in queued/running state. Re-queue them so
        # cached pages can finish the work instead of polling forever.
        if self._worker is not None and not self._worker.done():
            return
        for job in self._store.unfinished_jobs():
            stages = job["stages"]
            warnings = stages.setdefault("warnings", [])
            if "resumed after server restart" not in warnings:
                warnings.append("resumed after server restart")
            self._store.save_job(
                job["job_id"], job["player_id"], JobStatus.QUEUED,
                stages, job["budget_used"],
            )
            self._known_jobs.add(job["job_id"])
            self._queue.put_nowait(job["job_id"])
        self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def _run_worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            self._known_jobs.add(job_id)
            try:
                await self._process(job_id)
            except Exception as exc:  # defensive: a bug must not kill the worker
                self._fail(job_id, f"internal error: {exc}", code="internal_error")
            finally:
                self._known_jobs.discard(job_id)
                self._queue.task_done()

    # -- public API (used by MCP tools) --------------------------------------

    async def lookup(self, player_id: str) -> Envelope:
        """Return a cached result, or hand the player to the job queue."""
        player_id = player_id.strip()

        # Validate the page kind first, even when a job result is cached:
        # stale results for team pages must never be served as player data.
        try:
            page = await self._fetch(player_id, None)
        except RateLimited as exc:
            return Envelope(
                status=JobStatus.FAILED, player_id=player_id,
                error=ErrorInfo(code="rate_limited", message=str(exc), given=player_id),
            )
        except ProviderError as exc:
            return Envelope(
                status=JobStatus.FAILED, player_id=player_id,
                error=ErrorInfo(code=exc.code, message=str(exc), given=player_id),
            )
        self._require_player(page, player_id)

        existing = self._store.latest_job_for_player(player_id)
        if existing and existing["status"] == JobStatus.COMPLETE:
            age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                existing["updated_at"]
            ).timestamp()
            if age <= self._settings.cache_ttl_seconds:
                return self._envelope(existing, served_from_cache=True)

        if existing and existing["status"] in (JobStatus.QUEUED, JobStatus.RUNNING):
            checked = await self.result(existing["job_id"])
            if checked is not None and checked.status in (
                JobStatus.QUEUED, JobStatus.RUNNING
            ):
                return checked
            # The job was orphaned and has just been marked failed: start over.

        # Only partial/paused jobs are resumed; an expired complete or failed
        # job starts from a clean slate so stale stage data is never reused.
        if existing and existing["status"] in (JobStatus.PARTIAL, JobStatus.PAUSED):
            stages = existing["stages"]
            stages.setdefault("warnings", []).append(
                "resumed from a previous incomplete job"
            )
        else:
            stages = {"warnings": []}
            if existing and existing["status"] == JobStatus.COMPLETE:
                stages["warnings"].append("refreshing an expired cached result")
        job_id = uuid.uuid4().hex[:12]
        self._store.save_job(job_id, player_id, JobStatus.QUEUED, stages, 0)
        self._known_jobs.add(job_id)
        self._queue.put_nowait(job_id)
        return Envelope(
            status=JobStatus.QUEUED, job_id=job_id, player_id=player_id,
            progress="job queued; poll get_lookup_result",
        )

    async def result(self, job_id: str) -> Envelope | None:
        job = self._store.get_job(job_id)
        if not job:
            return None
        if job["status"] in (JobStatus.QUEUED, JobStatus.RUNNING) and (
            job_id not in self._known_jobs
        ):
            heartbeat = job.get("heartbeat_at") or job.get("updated_at")
            age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                heartbeat
            ).timestamp() if heartbeat else 0
            if age > self._settings.job_stall_seconds:
                self._store.save_job(
                    job_id, job["player_id"], JobStatus.FAILED, job["stages"],
                    job["budget_used"],
                    error=ErrorInfo(
                        code="job_stalled",
                        message=(
                            f"job made no progress for {int(age)}s; "
                            "call get_player_network again to start a fresh lookup"
                        ),
                    ),
                )
                job = self._store.get_job(job_id)
        return self._envelope(job) if job else None

    async def wait(self, job_id: str, wait_ms: int) -> Envelope | None:
        """Block until the job settles or ``wait_ms`` elapses."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, wait_ms) / 1000
        while True:
            envelope = await self.result(job_id)
            if envelope is None or envelope.status not in (
                JobStatus.QUEUED, JobStatus.RUNNING
            ):
                return envelope
            remaining = deadline - loop.time()
            if remaining <= 0:
                return envelope
            await asyncio.sleep(min(0.25, remaining))

    async def career(self, player_id: str) -> Envelope:
        """Full recorded team history of one player (PRD feature 1).

        Costs a single page fetch (cache-served when fresh); no job queue.
        """
        player_id = player_id.strip()
        try:
            page = await self._fetch(player_id, None)
        except RateLimited as exc:
            return Envelope(
                status=JobStatus.FAILED, player_id=player_id,
                error=ErrorInfo(code="rate_limited", message=str(exc), given=player_id),
            )
        except ProviderError as exc:
            return Envelope(
                status=JobStatus.FAILED, player_id=player_id,
                error=ErrorInfo(code=exc.code, message=str(exc), given=player_id),
            )
        self._require_player(page, player_id)
        player = parse_player(
            page.wikitext, title=page.title, page_id=page.page_id,
            settings=self._settings, revision_id=page.revision_id,
            fetched_at=page.fetched_at,
        )
        stints = parse_career(page.wikitext, self._settings)
        warnings = [] if stints else ["no team history recorded on the page"]
        for stint in stints:
            self._stamp_canonical(stint)
        return Envelope(
            status=JobStatus.COMPLETE, player_id=page.title,
            result=CareerResult(player=player, stints=stints),
            fetched_at=page.fetched_at, sources=self._sources_of(player),
            warnings=warnings,
        )

    async def roster(self, team_id: str, include_staff: bool = True) -> Envelope:
        """Current active roster of one team (PRD 2026-10-26 issue-fix P0-01).

        One team page plus one batched member-page request (cache-served when
        fresh). Staff are kept apart from the playing roster.
        """
        team_id = team_id.strip()
        try:
            page = await self._fetch(team_id, None)
        except RateLimited as exc:
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(code="rate_limited", message=str(exc), given=team_id),
            )
        except ProviderError as exc:
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(code=exc.code, message=str(exc), given=team_id),
            )
        self._require_team(page, team_id)

        rows = parse_squads(page.wikitext)
        active = first_active_squad(rows)
        players = [r for r in active if not r.staff and r.nickname]
        active_staff = [r for r in rows if r.kind == "staff" and r.section == "active"]
        staff_rows: list[SquadEntry] = []
        seen_staff: set[str] = set()
        for row in [r for r in active if r.staff] + active_staff:
            key = name_key(row.nickname or row.real_name or "")
            if not key or key in seen_staff:
                continue
            seen_staff.add(key)
            staff_rows.append(row)

        warnings: list[str] = []
        if not active:
            warnings.append("no active player squad found on the team page")

        titles = list(dict.fromkeys(self._row_title(r) for r in players))
        titles = [t for t in titles if t]
        try:
            batch = await self._client.get_wikitext_many(titles) if titles else None
        except RateLimited as exc:
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(code="rate_limited", message=str(exc), given=team_id),
            )
        except ProviderError as exc:
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(code=exc.code, message=str(exc), given=team_id),
            )
        pages = batch.pages if batch else {}
        missing = batch.missing if batch else set()

        roster: list[RosterMember] = []
        for row in players:
            title = self._row_title(row)
            member_page = pages.get(title)
            profile = None
            if member_page is not None:
                profile = parse_player(
                    member_page.wikitext, title=member_page.title,
                    page_id=member_page.page_id, settings=self._settings,
                    revision_id=member_page.revision_id,
                    fetched_at=member_page.fetched_at,
                )
            elif title in missing:
                warnings.append(f"profile page unavailable for {row.nickname}")
            roles = list(dict.fromkeys(
                [*row.roles, *(profile.roles if profile else [])]
            ))
            roster.append(RosterMember(
                player_id=member_page.title if member_page else (title or row.nickname),
                nickname=row.nickname,
                real_name=(profile.real_name if profile else None) or row.real_name,
                nationality=(profile.nationality if profile else None) or row.nationality,
                age=profile.age if profile else None,
                roles=roles,
                joined_at=row.join,
                relation=(
                    RosterRelation.SUBSTITUTE if row.substitute
                    else RosterRelation.CONFIRMED
                ),
                source=profile.sources[0] if profile and profile.sources else None,
            ))

        staff = [
            StaffMember(
                nickname=row.nickname or row.real_name or "unknown",
                real_name=row.real_name,
                nationality=row.nationality,
                role=row.role_label or (", ".join(row.roles) or None),
                joined_at=row.join,
            )
            for row in staff_rows
        ] if include_staff else []

        sources = [self._source_of_page(page)]
        seen_sources = {page.title}
        for member_page in pages.values():
            if member_page.title not in seen_sources:
                seen_sources.add(member_page.title)
                sources.append(self._source_of_page(member_page))
        return Envelope(
            status=JobStatus.COMPLETE,
            result=TeamRosterResult(
                team=TeamInfo(
                    name=page.title, page_id=page.page_id,
                    url=page_url(self._settings, page.title),
                ),
                roster=roster, staff=staff,
            ),
            fetched_at=page.fetched_at, sources=sources, warnings=warnings,
        )

    async def relationship(
        self, player_a: str, player_b: str, include_non_overlapping: bool = False,
    ) -> Envelope:
        """Shared-tenure and kinship judgment between two players (PRD feature 2).

        Costs one batched request for both pages when cold. Non-overlapping
        shared stints are hidden by default and summarised in ``omitted``.
        """
        a_id, b_id = player_a.strip(), player_b.strip()
        if name_key(a_id) == name_key(b_id):
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(code="same_player",
                                message="player_a and player_b are the same"),
            )
        try:
            batch = await self._client.get_wikitext_many([a_id, b_id])
        except RateLimited as exc:
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(code="rate_limited", message=str(exc)),
            )
        except ProviderError as exc:
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(code=exc.code, message=str(exc)),
            )
        missing = [t for t in (a_id, b_id) if t in batch.missing]
        if missing:
            return Envelope(
                status=JobStatus.FAILED,
                error=ErrorInfo(
                    code="page_missing",
                    message=f"player page not found: {', '.join(missing)}",
                    given=", ".join(missing),
                ),
            )
        a_page, b_page = batch.pages[a_id], batch.pages[b_id]
        self._require_player(a_page, a_id)
        self._require_player(b_page, b_id)
        a_player = parse_player(a_page.wikitext, title=a_page.title, page_id=a_page.page_id,
                                settings=self._settings, revision_id=a_page.revision_id,
                                fetched_at=a_page.fetched_at)
        b_player = parse_player(b_page.wikitext, title=b_page.title, page_id=b_page.page_id,
                                settings=self._settings, revision_id=b_page.revision_id,
                                fetched_at=b_page.fetched_at)
        a_stints = parse_career(a_page.wikitext, self._settings)
        b_stints = parse_career(b_page.wikitext, self._settings)

        records: list[SharedTeamRecord] = []
        for a_stint in a_stints:
            self._stamp_canonical(a_stint)
            a_key = name_key(self._store.canonical_title(a_stint.team.name))
            for b_stint in b_stints:
                self._stamp_canonical(b_stint)
                b_key = name_key(self._store.canonical_title(b_stint.team.name))
                if a_key != b_key or not a_key:
                    continue
                overlap = self._overlap_window(a_stint, b_stint)
                playing = self._playing_together(
                    a_player, b_player, a_stint, b_stint
                ) and overlap.verdict != "none"
                records.append(SharedTeamRecord(
                    team=a_stint.team,
                    canonical_team=(a_stint.canonical_team or b_stint.canonical_team),
                    a_stint=a_stint, b_stint=b_stint,
                    overlap=overlap, playing_together=playing,
                    games=sorted({g for g in (a_stint.game, b_stint.game) if g != "unknown"}),
                ))
        records.sort(key=lambda r: (name_key(r.team.name),
                                    r.a_stint.start.raw if r.a_stint.start else ""))

        omitted = [r for r in records if r.overlap.verdict == "none"]
        visible = records if include_non_overlapping else [
            r for r in records if r.overlap.verdict != "none"
        ]

        kinship = find_kinship(
            a_page.wikitext, other_names_for(b_player, b_page.title), a_page.title,
        ) or find_kinship(
            b_page.wikitext, other_names_for(a_player, a_page.title), b_page.title,
        )
        confirmed = sum(1 for r in visible if r.overlap.verdict == "confirmed")
        unconfirmed = sum(1 for r in visible if r.overlap.verdict == "unconfirmed")
        warnings = []
        if not a_stints:
            warnings.append(f"no team history recorded for {a_player.nickname}")
        if not b_stints:
            warnings.append(f"no team history recorded for {b_player.nickname}")
        if not visible and not kinship:
            warnings.append("no shared team records found; this is not proof of no relation")
        if confirmed:
            summary = (
                f"{confirmed} confirmed shared stint(s) at "
                f"{len({name_key(r.team.name) for r in visible if r.overlap.verdict == 'confirmed'})} team(s)"
            )
        elif unconfirmed:
            summary = (
                f"no confirmed shared stints; {unconfirmed} stint(s) with "
                f"unconfirmed overlap at {len({name_key(r.team.name) for r in visible})} team(s)"
            )
        elif kinship:
            summary = "no shared team records found; an explicit kinship relation is recorded"
        else:
            summary = "no overlapping shared stints found"
        fetched = max(x.fetched_at for x in (a_page, b_page))
        return Envelope(
            status=JobStatus.COMPLETE,
            result=RelationshipResult(
                player_a=a_player, player_b=b_player,
                shared_teams=visible,
                omitted=(
                    None if include_non_overlapping or not omitted
                    else OmittedRecords(
                        count=len(omitted),
                        reason="shared team(s) without an overlapping stint",
                    )
                ),
                kinship=kinship, summary=summary,
            ),
            fetched_at=fetched,
            sources=self._sources_of(a_player) + self._sources_of(b_player),
            warnings=warnings,
        )

    # -- job execution --------------------------------------------------------

    async def _process(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        if not job:
            return
        player_id = job["player_id"]
        stages = job["stages"]
        budget = self._Budget(self._settings.page_budget_per_job, job["budget_used"])
        warnings: list[str] = stages.get("warnings", [])
        sources: dict[str, dict] = stages.get("sources", {})
        self._set_status(job_id, player_id, JobStatus.RUNNING, stages, budget.used)
        heartbeat = asyncio.create_task(self._heartbeat(job_id))

        try:
            player = await self._load_target(job_id, player_id, stages, budget, sources)
            current = await self._current_teammates(
                player, stages, budget, sources, warnings
            )
            previous = await self._previous_teams(
                player, stages, budget, sources, warnings
            )
        except _BudgetExhausted:
            result = self._assemble(stages)
            stages["result"] = result.model_dump(mode="json")
            self._store.save_job(
                job_id, player_id, JobStatus.PARTIAL, stages, budget.used,
                error=ErrorInfo(
                    code="page_budget_exhausted",
                    message="page budget exhausted; call get_player_network again to resume",
                ),
            )
            return
        except _Paused as exc:
            self._store.save_job(
                job_id, player_id, JobStatus.PAUSED, stages, budget.used,
                error=ErrorInfo(code="rate_limited", message=str(exc)),
            )
            return
        except InvalidResource as exc:
            self._store.save_job(
                job_id, player_id, JobStatus.FAILED, stages, budget.used,
                error=exc.as_error(),
            )
            return
        except ProviderError as exc:
            self._fail(job_id, f"upstream error: {exc}", code=exc.code)
            return
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

        result = NetworkResult(player=player, current_teammates=current,
                               previous_teams=previous)
        stages["result"] = result.model_dump(mode="json")
        stages["sources"] = sources
        stages["warnings"] = warnings
        status = JobStatus.PARTIAL if budget.exhausted else JobStatus.COMPLETE
        self._store.save_job(job_id, player_id, status, stages, budget.used)

    async def _heartbeat(self, job_id: str) -> None:
        """Keep a running job's heartbeat fresh during long batch phases."""
        while True:
            await asyncio.sleep(10)
            self._store.touch_job(job_id)

    # -- stages ----------------------------------------------------------------

    async def _load_target(self, job_id, player_id, stages, budget, sources) -> Player:
        if "player" in stages:
            return Player.model_validate(stages["player"])
        page = await self._fetch(player_id, budget)
        self._require_player(page, player_id)
        self._remember_source(sources, page)
        player = parse_player(
            page.wikitext, title=page.title, page_id=page.page_id,
            settings=self._settings, revision_id=page.revision_id,
            fetched_at=page.fetched_at,
        )
        if player.team_status == TeamStatus.UNKNOWN and not player.current_team:
            pass  # page simply does not state a team; that stays 'unknown'
        stages["player"] = player.model_dump(mode="json")
        stages["player_title"] = page.title
        self._set_status(job_id, player_id, JobStatus.RUNNING, stages, budget.used)
        return player

    async def _current_teammates(self, player, stages, budget, sources, warnings):
        if "current_teammates" in stages:
            return [Teammate.model_validate(t) for t in stages["current_teammates"]]

        team = player.current_team
        rows: list[SquadEntry] = []
        if team and team.name:
            page = await self._fetch(team.name, budget, required=False)
            if page is None:
                warnings.append(f"current team page not found: {team.name}")
            else:
                self._remember_source(sources, page)
                team.page_id = page.page_id
                # Only the first "active" squad: that is the current-game tab.
                rows = first_active_squad(parse_squads(page.wikitext))
        rows = [
            r for r in rows
            if not r.staff and r.nickname and r.nickname != player.nickname
        ]
        pages = await self._fetch_batch([self._row_title(r) for r in rows], budget)
        for page in pages.values():
            self._remember_source(sources, page)
        teammates = [
            self._teammate_from_row(
                row, team, pages.get(self._row_title(row)),
                relation=RelationConfidence.CONFIRMED,
            )
            for row in rows
        ]
        stages["current_teammates"] = [t.model_dump(mode="json") for t in teammates]
        return teammates

    async def _previous_teams(self, player, stages, budget, sources, warnings):
        done: list[PreviousTeamGroup] = [
            PreviousTeamGroup.model_validate(g) for g in stages.get("previous_teams", [])
        ]
        if "history_done" in stages:
            return done

        player_title = stages.get("player_title", "")
        history = []
        if player_title:
            target_page = await self._fetch(player_title, budget)  # cache hit, free
            history = parse_team_history(target_page.wikitext, self._settings)
        if not history:
            warnings.append("team history not readable from the player page")
            stages["history_done"] = True
            return done

        current_name = player.current_team.name if player.current_team else None
        picks = recent_previous_teams(history, current_name, limit=3)
        team_pages = await self._fetch_batch(
            [tenure.team.name for tenure in picks], budget
        )
        for page in team_pages.values():
            self._remember_source(sources, page)

        # Parse every team first, then batch-load all member pages at once.
        groups: list[PreviousTeamGroup] = []
        member_rows: dict[str, list[tuple[SquadEntry, RelationConfidence, TeamRef]]] = {}
        for tenure in picks:
            page = team_pages.get(tenure.team.name)
            if page is None:
                warnings.append(f"previous team page not found: {tenure.team.name}")
                groups.append(PreviousTeamGroup(
                    tenure=tenure, note="team page missing; roster history unavailable"
                ))
                continue
            tenure.team.page_id = page.page_id
            target_window = (tenure.start, tenure.end)
            confirmed: list[tuple[SquadEntry, RelationConfidence, TeamRef]] = []
            unconfirmed: list[tuple[SquadEntry, RelationConfidence, TeamRef]] = []
            seen: set[str] = set()
            for row in parse_squads(page.wikitext):
                if row.staff or not row.nickname or row.nickname == player.nickname:
                    continue
                if row.nickname in seen:
                    continue
                verdict = overlap_verdict(target_window, (row.join, row.leave))
                if verdict == "none":
                    continue
                seen.add(row.nickname)
                bucket = confirmed if verdict == "confirmed" else unconfirmed
                bucket.append((
                    row,
                    RelationConfidence.CONFIRMED if verdict == "confirmed"
                    else RelationConfidence.UNCONFIRMED,
                    tenure.team,
                ))
            member_rows[tenure.team.name] = confirmed + unconfirmed
            groups.append(PreviousTeamGroup(tenure=tenure))

        all_rows = [entry for rows in member_rows.values() for entry in rows]
        member_pages = await self._fetch_batch(
            [self._row_title(entry[0]) for entry in all_rows], budget
        )
        for page in member_pages.values():
            self._remember_source(sources, page)
        for group in groups:
            for row, relation, team in member_rows.get(group.tenure.team.name, []):
                teammate = self._teammate_from_row(
                    row, team, member_pages.get(self._row_title(row)),
                    relation=relation,
                )
                if relation == RelationConfidence.CONFIRMED:
                    group.teammates.append(teammate)
                else:
                    group.unconfirmed.append(teammate)
        done.extend(groups)
        stages["previous_teams"] = [g.model_dump(mode="json") for g in done]
        stages["history_done"] = True
        return done

    def _teammate_from_row(
        self, row: SquadEntry, team, page, relation: RelationConfidence,
    ) -> Teammate:
        """Build a teammate from a roster row; ``page`` is the profile page
        when one was fetched, otherwise the row itself is all we know."""
        if page is not None:
            profile = parse_player(
                page.wikitext, title=page.title, page_id=page.page_id,
                settings=self._settings, revision_id=page.revision_id,
                fetched_at=page.fetched_at,
            )
            note = "substitute" if row.substitute else None
        else:
            profile = Player(
                nickname=row.nickname, real_name=row.real_name,
                nationality=row.nationality, roles=list(row.roles) or [],
                team_status=TeamStatus.UNKNOWN,
                missing=["profile page unavailable; data from roster row only"],
            )
            note = "substitute" if row.substitute else "profile not fetched (missing page)"
        for role in row.roles:
            if role not in profile.roles:
                profile.roles.append(role)
        if row.substitute:
            profile.missing.append("marked as substitute in the roster row")
        return Teammate(player=profile, team=team, relation=relation, note=note)

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _overlap_window(a_stint: CareerStint, b_stint: CareerStint) -> OverlapWindow:
        """Verdict plus the best-estimate overlap interval of two stints."""
        verdict = overlap_verdict(
            (a_stint.start, a_stint.end), (b_stint.start, b_stint.end)
        )
        if verdict == "none":
            return OverlapWindow(verdict="none", reason="stints do not overlap in time")
        # Overlap window: later start to earlier end (open end = ongoing).
        a_lo = date_bounds(a_stint.start, is_start=True)[0] if a_stint.start else 0
        b_lo = date_bounds(b_stint.start, is_start=True)[0] if b_stint.start else 0
        later_start = a_stint.start if a_lo >= b_lo else b_stint.start
        if a_stint.end is None:
            earlier_end = b_stint.end
        elif b_stint.end is None:
            earlier_end = a_stint.end
        elif date_bounds(a_stint.end, is_start=False)[0] <= date_bounds(b_stint.end, is_start=False)[0]:
            earlier_end = a_stint.end
        else:
            earlier_end = b_stint.end
        return OverlapWindow(
            verdict=verdict, start=later_start, end=earlier_end,
            reason=None if verdict == "confirmed"
            else "date precision on the pages is insufficient to confirm the overlap",
        )

    @staticmethod
    def _playing_together(a: Player, b: Player, a_stint: CareerStint, b_stint: CareerStint) -> bool:
        """False when either side was staff / benched / stand-in at the time.

        Player pages themselves assert 'player', so True is the default; stint
        annotations and page-level staff roles can only overturn it.
        """
        for stint in (a_stint, b_stint):
            identity = (stint.identity or "").lower()
            if identity in {"inactive", "trial", "benched", "substitute", "sub",
                            "stand-in", "standin"} or _staff_identity(identity):
                return False
        for player in (a, b):
            if _staff_identity(" ".join(player.roles).lower()):
                return False
        return True

    @staticmethod
    def _sources_of(player: Player) -> list[Source]:
        return list(player.sources)

    def _source_of_page(self, page) -> Source:
        return Source(
            page=page.title, page_id=page.page_id,
            url=page_url(self._settings, page.title),
            revision_id=page.revision_id, fetched_at=page.fetched_at,
        )

    def _require_player(self, page, given: str) -> None:
        kind = classify_page(page.wikitext)
        if kind == "player":
            return
        if kind == "team":
            suggestion = f'use get_team_roster(team_id="{page.title}")'
        elif kind in ("tournament", "disambiguation"):
            suggestion = (
                f'use search_players(query="{given}") to find a player page '
                f'(this is a {kind} page)'
            )
        else:
            suggestion = (
                f'use search_players(query="{given}") to resolve the page title first'
            )
        raise InvalidResource(given, kind, "player", suggestion)

    def _require_team(self, page, given: str) -> None:
        kind = classify_page(page.wikitext)
        if kind == "team":
            return
        if kind == "player":
            suggestion = (
                f'use get_player_career(player_id="{page.title}") '
                "for a player page"
            )
        elif kind in ("tournament", "disambiguation"):
            suggestion = (
                f'use search_players(query="{given}") to resolve the title first '
                f'(this is a {kind} page)'
            )
        else:
            suggestion = (
                f'use search_players(query="{given}") to resolve the page title first'
            )
        raise InvalidResource(given, kind, "team", suggestion)

    @staticmethod
    def _row_title(row: SquadEntry) -> str:
        """Page title for a roster row; explicit link wins when it is local."""
        if row.link and ":" not in row.link:
            return row.link
        return row.nickname

    def _stamp_canonical(self, stint: CareerStint) -> None:
        """Fill canonical_team from recorded redirect aliases, once."""
        if stint.canonical_team is None:
            canonical = self._store.canonical_title(stint.team.name)
            if name_key(canonical) != name_key(stint.team.name):
                stint.canonical_team = canonical

    async def _fetch_batch(self, titles: list[str], budget: "_Budget") -> dict:
        """Load many pages through the batched provider, honoring the budget.

        Budget is spent on fresh canonical pages only. When the remaining
        budget cannot cover the batch, nothing is fetched and the phase is
        aborted (resumed later); stages stay consistent that way.
        """
        wanted = [t for t in dict.fromkeys(titles) if t not in self._missing_pages]
        if not wanted:
            return {}
        batch = await self._client.get_wikitext_many(
            wanted, max_fresh=budget.limit - budget.used
        )
        # Record genuinely absent pages even when deferring the rest, so a
        # resumed run never re-queries them.
        self._missing_pages.update(batch.missing)
        if batch.deferred:
            raise _BudgetExhausted(
                f"page budget too small for {len(batch.deferred)} more pages"
            )
        budget.used += len(batch.fresh_pages)
        return batch.pages

    async def _fetch(self, title: str, budget: "_Budget | None", required: bool = True):
        """Fetch a page through cache+scheduler, honoring the page budget.

        ``budget=None`` skips budget accounting (single-page tools).
        Returns None when the page does not exist and ``required`` is False.
        Pages known-missing from an earlier run are skipped for free, so a
        resumed job converges instead of re-spending budget on 404s.
        """
        if title in self._missing_pages:
            if required:
                raise ProviderError(f"page not found: {title}", code="page_missing")
            return None
        if budget is not None and budget.exhausted and not self._fresh_cached(title):
            raise _BudgetExhausted(title)
        attempts = 0
        while True:
            try:
                page = await self._client.get_wikitext(title)
                if not page.from_cache and budget is not None:
                    budget.spend()  # only fresh upstream pages cost budget
                return page
            except RateLimited as exc:
                attempts += 1
                if attempts > 1:
                    if budget is None:
                        raise
                    raise _Paused(
                        f"rate limited twice; paused after Retry-After {exc.retry_after}s"
                    ) from exc
                await asyncio.sleep(exc.retry_after)
            except ProviderError as exc:
                if exc.code == "page_missing":
                    self._missing_pages.add(title)
                    if not required:
                        return None
                raise

    def _fresh_cached(self, title: str) -> bool:
        cached = self._store.get_page(title)
        if not cached:
            return False
        fetched = datetime.fromisoformat(cached["fetched_at"]).timestamp()
        return datetime.now(timezone.utc).timestamp() - fetched <= self._settings.cache_ttl_seconds

    def _remember_source(self, sources: dict, page) -> None:
        sources[page.title] = {
            "page": page.title, "page_id": page.page_id,
            "url": page_url(self._settings, page.title),
            "revision_id": page.revision_id,
            "fetched_at": page.fetched_at.isoformat(),
        }

    def _assemble(self, stages: dict) -> NetworkResult:
        player = stages.get("player")
        return NetworkResult(
            player=Player.model_validate(player) if player else Player(nickname="unknown"),
            current_teammates=[
                Teammate.model_validate(t) for t in stages.get("current_teammates", [])
            ],
            previous_teams=[
                PreviousTeamGroup.model_validate(g)
                for g in stages.get("previous_teams", [])
            ],
        )

    def _envelope(self, job: dict, served_from_cache: bool = False) -> Envelope:
        stages = job["stages"]
        warnings = list(stages.get("warnings", []))
        if served_from_cache:
            warnings.append("served from local cache")
        result = None
        if stages.get("result"):
            result = NetworkResult.model_validate(stages["result"])
        fetched_at = None
        if stages.get("player"):
            fetched_at = Player.model_validate(stages["player"]).sources[0].fetched_at if (
                Player.model_validate(stages["player"]).sources
            ) else None
        return Envelope(
            status=job["status"], job_id=job["job_id"], player_id=job["player_id"],
            result=result, progress=self._progress_line(job),
            fetched_at=fetched_at,
            sources=[Source.model_validate(s) for s in stages.get("sources", {}).values()],
            warnings=warnings,
            truncated=job["status"] == JobStatus.PARTIAL,
            error=(
                ErrorInfo.model_validate(job["error"]) if job.get("error") else None
            ),
        )

    def _progress_line(self, job: dict) -> str:
        stages = job["stages"]
        steps = ["target", "current team", "teammates", "previous teams"]
        done = sum(
            1 for key in ("player", "current_teammates", "current_teammates_done",
                          "history_done")
            if key in stages
        )
        return f"{min(done, len(steps))}/{len(steps)} stages done ({job['status']})"

    def _set_status(self, job_id, player_id, status, stages, budget_used) -> None:
        self._store.save_job(job_id, player_id, status, stages, budget_used)

    def _fail(self, job_id: str, message: str, code: str = "error") -> None:
        job = self._store.get_job(job_id)
        if not job:
            return
        self._store.save_job(
            job_id, job["player_id"], JobStatus.FAILED, job["stages"],
            job["budget_used"], error=ErrorInfo(code=code, message=message),
        )

    class _Budget:
        """Counts fresh page fetches; cached pages cost nothing."""

        def __init__(self, limit: int, used: int) -> None:
            self.limit = limit
            self.used = used

        @property
        def exhausted(self) -> bool:
            return self.used >= self.limit

        def spend(self) -> None:
            self.used += 1
