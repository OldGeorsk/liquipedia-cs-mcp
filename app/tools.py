"""MCP tools: PRD section 5 lookups plus the career/relationship tools."""

import json

from mcp.server.mcpserver import MCPServer

from app.config import APP_NAME, APP_VERSION, LICENSE_NAME, LICENSE_URL, Settings
from app.liquipedia import LiquipediaClient, page_url
from app.models import Envelope, utcnow
from app.network import PlayerNetworkService

_INSTRUCTIONS = (
    "Liquipedia Counter-Strike player lookup. "
    "First call search_players to resolve a nickname to a player_id (page title). "
    "get_player_network then get_lookup_result run the async teammate lookup; "
    "get_player_career returns a player's full recorded team history; "
    "get_player_relationship compares two players' histories for shared stints "
    "and explicitly stated kinship. "
    f"All text data is {LICENSE_NAME} via Liquipedia."
)


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_server(
    service: PlayerNetworkService, client: LiquipediaClient, settings: Settings
) -> MCPServer:
    server = MCPServer(name=APP_NAME, version=APP_VERSION, instructions=_INSTRUCTIONS)

    @server.tool()
    async def search_players(query: str, limit: int = 5) -> str:
        """Search Counter-Strike players by nickname or name.

        Returns up to `limit` (1-5) candidates. When several match, let the
        user pick one `player_id` before calling get_player_network.
        """
        if not query or not query.strip():
            return _json({"error": "query must be a non-empty string"})
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5:
            return _json({"error": "limit must be an integer between 1 and 5"})

        hits = await client.search(query.strip(), limit)
        return _json({
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
    async def get_player_network(player_id: str) -> str:
        """Look up a player's profile, current teammates and recent-team teammates.

        `player_id` is the exact page title returned by search_players.
        A cached finished lookup returns immediately; otherwise a job starts
        and this returns its job_id for get_lookup_result polling.
        """
        if not player_id or not player_id.strip():
            return _json({"error": "player_id must be a non-empty string"})
        envelope = await service.lookup(player_id)
        return envelope.model_dump_json(indent=2)

    @server.tool()
    async def get_lookup_result(job_id: str) -> str:
        """Fetch status, progress and any results so far for a lookup job."""
        envelope = await service.result(job_id.strip())
        if envelope is None:
            return _json({"error": f"unknown job_id: {job_id}"})
        return envelope.model_dump_json(indent=2)

    @server.tool()
    async def get_player_career(player_id: str) -> str:
        """Return a player's complete recorded team history.

        Covers every stint on the page: early teams, academy stints and
        repeated spells, each with game tag (CS / CS:GO / CS2), dates at
        their recorded precision, stint identity (inactive, stand-in, ...)
        and source. Cached results return immediately.
        """
        if not player_id or not player_id.strip():
            return _json({"error": "player_id must be a non-empty string"})
        envelope = await service.career(player_id)
        return envelope.model_dump_json(indent=2)

    @server.tool()
    async def get_player_relationship(player_a: str, player_b: str) -> str:
        """Judge the relationship between two players from their pages.

        Returns every team both have recorded stints at, with the verified
        overlap window ('confirmed' / 'unconfirmed' / 'none'), the identity
        each had at the time, and any family relation explicitly stated on
        either page (never inferred). Sequential-only stints never come back
        as confirmed teammates.
        """
        if not player_a.strip() or not player_b.strip():
            return _json({"error": "player_a and player_b must be non-empty strings"})
        envelope = await service.relationship(player_a, player_b)
        return envelope.model_dump_json(indent=2)

    return server
