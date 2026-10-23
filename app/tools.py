"""The three MCP tools defined by PRD section 5."""

import json

from mcp.server.mcpserver import MCPServer

from app.config import APP_NAME, APP_VERSION, LICENSE_NAME, LICENSE_URL, Settings
from app.liquipedia import LiquipediaClient, page_url
from app.models import Envelope, utcnow
from app.network import PlayerNetworkService

_INSTRUCTIONS = (
    "Liquipedia Counter-Strike player lookup. "
    "First call search_players to resolve a nickname to a player_id (page title), "
    "then get_player_network to start or resume a lookup, "
    "then get_lookup_result with the returned job_id until status is complete. "
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

    return server
