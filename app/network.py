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
    Envelope, JobStatus, NetworkResult, Player, PreviousTeamGroup,
    RelationConfidence, Source, TeamStatus, Teammate,
)
from app.parsing import (
    SquadEntry, first_active_squad, overlap_verdict, parse_player, parse_squads,
    parse_team_history, recent_previous_teams,
)
from app.store import Store


class _BudgetExhausted(Exception):
    """Raised internally when a job hits its page budget."""


class _Paused(Exception):
    """Raised when upstream throttling refuses to let the job continue."""


class PlayerNetworkService:
    """Owns the single background worker that processes lookup jobs."""

    def __init__(self, settings: Settings, store: Store, client: LiquipediaClient) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._missing_pages: set[str] = set()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # Jobs still marked running after a restart are re-queued; their
        # stages survive in SQLite and cached pages make the resume cheap.
        self._worker = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def _run_worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id)
            except Exception as exc:  # defensive: a bug must not kill the worker
                self._fail(job_id, f"internal error: {exc}")
            finally:
                self._queue.task_done()

    # -- public API (used by MCP tools) --------------------------------------

    async def lookup(self, player_id: str) -> Envelope:
        """Return a cached result, or hand the player to the job queue."""
        player_id = player_id.strip()
        existing = self._store.latest_job_for_player(player_id)
        if existing and existing["status"] == JobStatus.COMPLETE:
            age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                existing["updated_at"]
            ).timestamp()
            if age <= self._settings.cache_ttl_seconds:
                return self._envelope(existing, served_from_cache=True)

        if existing and existing["status"] in (JobStatus.QUEUED, JobStatus.RUNNING):
            return Envelope(
                status=existing["status"], job_id=existing["job_id"], player_id=player_id,
                progress=self._progress_line(existing),
            )

        # partial / paused / stale-complete: create a resuming job (cache pays).
        stages = existing["stages"] if existing else {}
        stages.setdefault("warnings", [])
        if existing:
            stages["warnings"].append("resumed from a previous incomplete job")
        job_id = uuid.uuid4().hex[:12]
        self._store.save_job(job_id, player_id, JobStatus.QUEUED, stages, 0)
        self._queue.put_nowait(job_id)
        return Envelope(
            status=JobStatus.QUEUED, job_id=job_id, player_id=player_id,
            progress="job queued; poll get_lookup_result",
        )

    async def result(self, job_id: str) -> Envelope | None:
        job = self._store.get_job(job_id)
        return self._envelope(job) if job else None

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
                error="page budget exhausted; call get_player_network again to resume",
            )
            return
        except _Paused as exc:
            self._store.save_job(
                job_id, player_id, JobStatus.PAUSED, stages, budget.used, error=str(exc)
            )
            return
        except ProviderError as exc:
            self._fail(job_id, f"upstream error: {exc}")
            return

        result = NetworkResult(player=player, current_teammates=current,
                               previous_teams=previous)
        stages["result"] = result.model_dump(mode="json")
        stages["sources"] = sources
        stages["warnings"] = warnings
        status = JobStatus.PARTIAL if budget.exhausted else JobStatus.COMPLETE
        self._store.save_job(job_id, player_id, status, stages, budget.used)

    # -- stages ----------------------------------------------------------------

    async def _load_target(self, job_id, player_id, stages, budget, sources) -> Player:
        if "player" in stages:
            return Player.model_validate(stages["player"])
        page = await self._fetch(player_id, budget)
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

        teammates: list[Teammate] = []
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
        teammates = await self._profiles_for_rows(
            rows, team, player, budget, sources, warnings
        )
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
        for tenure in picks:
            page = await self._fetch(tenure.team.name, budget, required=False)
            if page is None:
                warnings.append(f"previous team page not found: {tenure.team.name}")
                done.append(PreviousTeamGroup(
                    tenure=tenure, note="team page missing; roster history unavailable"
                ))
                continue
            self._remember_source(sources, page)
            tenure.team.page_id = page.page_id
            target_window = (tenure.start, tenure.end)
            confirmed: list[Teammate] = []
            unconfirmed: list[Teammate] = []
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
                group = confirmed if verdict == "confirmed" else unconfirmed
                group.append(await self._teammate_from_row(
                    row, tenure.team, player, budget, sources, warnings,
                    relation=RelationConfidence.CONFIRMED if verdict == "confirmed"
                    else RelationConfidence.UNCONFIRMED,
                ))
            done.append(PreviousTeamGroup(
                tenure=tenure, teammates=confirmed, unconfirmed=unconfirmed,
            ))
        stages["previous_teams"] = [g.model_dump(mode="json") for g in done]
        stages["history_done"] = True
        return done

    async def _profiles_for_rows(
        self, rows, team, player, budget, sources, warnings,
    ) -> list[Teammate]:
        teammates: list[Teammate] = []
        for row in rows:
            if row.staff or not row.nickname or row.nickname == player.nickname:
                continue
            teammates.append(await self._teammate_from_row(
                row, team, player, budget, sources, warnings,
                relation=RelationConfidence.CONFIRMED,
            ))
        return teammates

    async def _teammate_from_row(
        self, row, team, player, budget, sources, warnings, relation,
    ) -> Teammate:
        """Prefer the teammate's own page; fall back to the roster row."""
        note = "substitute" if row.substitute else None
        profile: Player | None = None
        try:
            page = await self._fetch(row.nickname, budget, required=False)
        except _BudgetExhausted:
            raise
        if page is not None:
            self._remember_source(sources, page)
            profile = parse_player(
                page.wikitext, title=page.title, page_id=page.page_id,
                settings=self._settings, revision_id=page.revision_id,
                fetched_at=page.fetched_at,
            )
        if profile is None:
            fallback_missing = ["profile page unavailable; data from roster row only"]
            profile = Player(
                nickname=row.nickname, real_name=row.real_name,
                nationality=row.nationality, roles=list(row.roles) or [],
                team_status=TeamStatus.UNKNOWN, missing=fallback_missing,
            )
            if note is None:
                note = "profile not fetched (budget or missing page)"
        if row.roles:
            for role in row.roles:
                if role not in profile.roles:
                    profile.roles.append(role)
        if note == "substitute":
            profile.missing.append("marked as substitute in the roster row")
        return Teammate(player=profile, team=team, relation=relation, note=note)

    # -- helpers ----------------------------------------------------------------

    async def _fetch(self, title: str, budget: "_Budget", required: bool = True):
        """Fetch a page through cache+scheduler, honoring the page budget.

        Returns None when the page does not exist and ``required`` is False.
        Pages known-missing from an earlier run are skipped for free, so a
        resumed job converges instead of re-spending budget on 404s.
        """
        if title in self._missing_pages:
            if required:
                raise ProviderError(f"page not found: {title}", code="page_missing")
            return None
        if budget.exhausted and not self._fresh_cached(title):
            raise _BudgetExhausted(title)
        attempts = 0
        while True:
            try:
                page = await self._client.get_wikitext(title)
                if not page.from_cache:
                    budget.spend()  # only fresh upstream pages cost budget
                return page
            except RateLimited as exc:
                attempts += 1
                if attempts > 1:
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
            error=job.get("error"),
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

    def _fail(self, job_id: str, message: str) -> None:
        job = self._store.get_job(job_id)
        if not job:
            return
        self._store.save_job(
            job_id, job["player_id"], JobStatus.FAILED, job["stages"],
            job["budget_used"], error=message,
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
