# liquipedia-cs-mcp

- Local MCP server that looks up Counter-Strike players on [Liquipedia](https://liquipedia.net/counterstrike): 
- profile, current teammates, and the teammates of the three most recent previous teams.

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # Windows; use .venv/bin/pip elsewhere
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Register `http://127.0.0.1:8000/mcp` (Streamable HTTP) in your MCP client:

- **ZCode** — `~/.zcode/cli/config.json` → `mcp.servers`
- **Claude Desktop / Cursor** — the client's `mcpServers` config

```json
{ "liquipedia-cs-mcp": { "type": "http", "url": "http://127.0.0.1:8000/mcp" } }
```

> Too lazy to configure? Just hand this to your agent:
> "Install and register the local HTTP MCP server at http://127.0.0.1:8000/mcp in my MCP client, then verify the connection."

## MCP tools

| Tool | Input | Output |
| --- | --- | --- |
| `search_players` | `query`, `limit` (1–5) | player candidates with `player_id` |
| `get_player_network` | `player_id` | cached result, or a `job_id` to poll |
| `get_lookup_result` | `job_id` | status/progress + result with `sources`, timestamps, CC BY-SA 3.0 license, `warnings`; unknowns are `null` with a reason |

## Configuration (env vars, `LIQUIPEDIA_` prefix)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIQUIPEDIA_CONTACT` | repo owner's e-mail | required contact in the User-Agent |
| `LIQUIPEDIA_CACHE_TTL_SECONDS` | `86400` | page cache freshness |
| `LIQUIPEDIA_PAGE_BUDGET_PER_JOB` | `30` | fresh pages per lookup job |
| `LIQUIPEDIA_API_URL` | counterstrike wiki API | upstream endpoint |

## Compliance design

A single serialized scheduler paces every upstream request with randomized
2.3–3.0 s spacing (above Liquipedia's 2 s floor) and persists the clock in
SQLite; 429s honor `Retry-After`, and page budgets keep every job resumable.
