"""MCP tools: PRD section 5 lookups plus the career/roster/relationship tools."""

import json

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from app.config import APP_NAME, APP_VERSION, LICENSE_NAME, LICENSE_URL, Settings
from app.liquipedia import LiquipediaClient, page_url
from app.models import Envelope, ErrorInfo, JobStatus, utcnow
from app.network import InvalidResource, PlayerNetworkService

_INSTRUCTIONS = (
    "Liquipedia Counter-Strike lookup. "
    "First call search_players to resolve a nickname to a player_id (page title), "
    "or search_teams to resolve a team name to a team page title. "
    "get_team_roster returns a team's current players and staff from the team page. "
    "get_player_network then get_lookup_result run the async teammate lookup; "
    "get_player_career returns a player's full recorded team history; "
    "get_player_relationship compares two players' histories for shared stints "
    "and explicitly stated kinship; get_relationship_matrix does the same for "
    "every cross-team pair in up to 5 rosters, or for an explicit player list, "
    "in one call. "
    "Player tools reject team/tournament pages with isError and a concrete "
    "alternative; never treat an error as an empty result. "
    f"All text data is {LICENSE_NAME} via Liquipedia."
)


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _text(payload) -> CallToolResult:
    body = payload if isinstance(payload, str) else _json(payload)
    return CallToolResult(content=[TextContent(type="text", text=body)])


def _tool_error(error: ErrorInfo) -> CallToolResult:
    """Input errors are isError results carrying a machine-readable code."""
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=_json({"error": error.model_dump(exclude_none=True)}),
        )],
        is_error=True,
    )


def _input_error(envelope: Envelope) -> CallToolResult | None:
    if (
        envelope.status == JobStatus.FAILED
        and envelope.error is not None
        and envelope.error.code in {
            "invalid_resource_type", "page_missing", "same_player",
        }
    ):
        return _tool_error(envelope.error)
    return None


def _invalid_argument(message: str, given: str | None = None) -> CallToolResult:
    return _tool_error(ErrorInfo(code="invalid_argument", message=message, given=given))


def build_server(
    service: PlayerNetworkService, client: LiquipediaClient, settings: Settings
) -> MCPServer:
    server = MCPServer(name=APP_NAME, version=APP_VERSION, instructions=_INSTRUCTIONS)

    @server.tool()
    async def search_players(query: str, limit: int = 5) -> CallToolResult:
        """Search Counter-Strike players by nickname or name.

        Returns up to `limit` (1-5) candidates. When several match, let the
        user pick one `player_id` before calling get_player_network.
        """
        if not query or not query.strip():
            return _invalid_argument("query must be a non-empty string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5:
            return _invalid_argument("limit must be an integer between 1 and 5")

        hits = await client.search(query.strip(), limit)
        return _text({
            "query": query.strip(),
            "candidates": [
                {
                    "player_id": hit.title,
                    "nickname": hit.title,
                    "page_id": hit.page_id,
                    "url": page_url(settings, hit.title),
                    "snippet": hit.snippet,
                }
                for hit in hits
            ],
            "license": LICENSE_NAME,
            "license_url": LICENSE_URL,
            "served_at": utcnow().isoformat(),
        })

    @server.tool()
    async def search_teams(query: str, limit: int = 5) -> CallToolResult:
        """Search Counter-Strike team pages by name.

        Returns up to `limit` (1-10) team candidates; player, tournament and
        disambiguation pages are filtered out. An exact title match leads the
        list. An empty `candidates` list is a valid answer, not an error.
        """
        if not query or not query.strip():
            return _invalid_argument("query must be a non-empty string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            return _invalid_argument("limit must be an integer between 1 and 10")

        envelope = await service.search_teams(query.strip(), limit)
        if envelope.status == JobStatus.FAILED and envelope.error is not None:
            return _tool_error(envelope.error)
        return _text({
            "query": envelope.result.query,
            "candidates": [
                candidate.model_dump(mode="json")
                for candidate in envelope.result.candidates
            ],
            "warnings": envelope.warnings,
            "license": LICENSE_NAME,
            "license_url": LICENSE_URL,
            "sources": [
                source.model_dump(mode="json") for source in envelope.sources
            ],
            "fetched_at": (
                envelope.fetched_at.isoformat() if envelope.fetched_at else None
            ),
            "served_at": utcnow().isoformat(),
        })

    @server.tool()
    async def get_team_roster(team_id: str, include_staff: bool = True) -> CallToolResult:
        """Return a team's current active roster and staff.

        `team_id` is the team page title (for example `Team Falcons`).
        Players carry nickname, real name, nationality, age, roles, join date
        and a `confirmed` / `substitute` relation. Staff are listed separately
        and never mixed into the roster. Pass a player page and the tool
        returns isError with the right alternative.
        """
        if not team_id or not team_id.strip():
            return _invalid_argument("team_id must be a non-empty string")
        try:
            envelope = await service.roster(team_id.strip(), include_staff)
        except InvalidResource as exc:
            return _tool_error(exc.as_error())
        if (error := _input_error(envelope)) is not None:
            return error
        return _text(envelope.model_dump_json(indent=2))

    @server.tool()
    async def get_player_network(
        player_id: str, wait_ms: int = 15000,
    ) -> CallToolResult:
        """Look up a player's profile, current teammates and recent-team teammates.

        `player_id` is the exact page title returned by search_players.
        A cached finished lookup returns immediately. Otherwise the tool waits
        up to `wait_ms` (0-60000) for the background job, then returns either
        the terminal result or a `job_id` for get_lookup_result polling.
        """
        if not player_id or not player_id.strip():
            return _invalid_argument("player_id must be a non-empty string")
        if (
            not isinstance(wait_ms, int) or isinstance(wait_ms, bool)
            or not 0 <= wait_ms <= 60000
        ):
            return _invalid_argument("wait_ms must be an integer between 0 and 60000")
        try:
            envelope = await service.lookup(player_id.strip())
        except InvalidResource as exc:
            return _tool_error(exc.as_error())
        if (
            envelope.job_id
            and envelope.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            and wait_ms > 0
        ):
            envelope = await service.wait(envelope.job_id, wait_ms) or envelope
        if (error := _input_error(envelope)) is not None:
            return error
        return _text(envelope.model_dump_json(indent=2))

    @server.tool()
    async def get_lookup_result(job_id: str) -> CallToolResult:
        """Fetch status, progress and any results so far for a lookup job.

        Jobs end in `complete`, `partial` (resumable), `paused` (throttled)
        or `failed`; failed jobs carry `error.code`, including `job_stalled`.
        """
        envelope = await service.result(job_id.strip())
        if envelope is None:
            return _tool_error(ErrorInfo(
                code="unknown_job", message=f"unknown job_id: {job_id}",
                given=job_id,
            ))
        return _text(envelope.model_dump_json(indent=2))

    @server.tool()
    async def get_player_career(player_id: str) -> CallToolResult:
        """Return a player's complete recorded team history.

        Covers every stint on the page: early teams, academy stints and
        repeated spells, each with game tag (CS / CS:GO / CS2), dates at
        their recorded precision, stint identity (inactive, stand-in, ...)
        and source. Cached results return immediately.
        """
        if not player_id or not player_id.strip():
            return _invalid_argument("player_id must be a non-empty string")
        try:
            envelope = await service.career(player_id.strip())
        except InvalidResource as exc:
            return _tool_error(exc.as_error())
        if (error := _input_error(envelope)) is not None:
            return error
        return _text(envelope.model_dump_json(indent=2))

    @server.tool()
    async def get_player_relationship(
        player_a: str, player_b: str, include_non_overlapping: bool = False,
    ) -> CallToolResult:
        """Judge the relationship between two players from their pages.

        Returns every overlapping shared stint with the verified window
        ('confirmed' / 'unconfirmed'), the identity each had at the time, and
        any family relation explicitly stated on either page (never inferred).
        Non-overlapping stints are hidden by default and counted in `omitted`;
        set `include_non_overlapping` to list them. Sequential-only stints
        never come back as confirmed teammates.
        """
        if not player_a.strip() or not player_b.strip():
            return _invalid_argument("player_a and player_b must be non-empty strings")
        try:
            envelope = await service.relationship(
                player_a.strip(), player_b.strip(), include_non_overlapping
            )
        except InvalidResource as exc:
            return _tool_error(exc.as_error())
        if (error := _input_error(envelope)) is not None:
            return error
        return _text(envelope.model_dump_json(indent=2))

    @server.tool()
    async def get_relationship_matrix(
        teams: list[str] | None = None,
        players: list[str] | None = None,
        include_staff: bool = False,
        include_non_overlapping: bool = False,
        max_pairs: int = 300,
    ) -> CallToolResult:
        """Compare player relationships across several teams in one call.

        Pass `teams` (2-5 team page titles) to compare every cross-team pair
        of the current rosters, or `players` (2-20 player page titles) to
        compare all pairs of an explicit list - never both. Results separate
        confirmed overlaps, unconfirmed overlaps, kinship and same-team
        different-period records; a missing player page yields `partial` with
        `skipped`, while an invalid team or too many pairs is an error.
        """
        team_list = [t.strip() for t in (teams or []) if t and t.strip()]
        player_list = [p.strip() for p in (players or []) if p and p.strip()]
        if bool(team_list) == bool(player_list):
            return _invalid_argument("provide exactly one of teams or players")
        if team_list and not 2 <= len(team_list) <= 5:
            return _invalid_argument("teams must contain 2 to 5 team page titles")
        if player_list and not 2 <= len(player_list) <= 20:
            return _invalid_argument("players must contain 2 to 20 player page titles")
        if (
            not isinstance(max_pairs, int) or isinstance(max_pairs, bool)
            or not 1 <= max_pairs <= 500
        ):
            return _invalid_argument("max_pairs must be an integer between 1 and 500")

        try:
            envelope = await service.matrix(
                team_list or None, player_list or None,
                include_staff, include_non_overlapping, max_pairs,
            )
        except InvalidResource as exc:
            return _tool_error(exc.as_error())
        if envelope.status == JobStatus.FAILED and envelope.error is not None:
            return _tool_error(envelope.error)
        return _text(envelope.model_dump_json(indent=2))

    return server
