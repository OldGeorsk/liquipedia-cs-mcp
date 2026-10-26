# liquipedia-cs-mcp
English | [简体中文](README.zh-CN.md)

- Local MCP server that looks up Counter-Strike teams and players on [Liquipedia](https://liquipedia.net/counterstrike):
- current team rosters (players and staff kept apart), player profiles, current teammates, and the teammates of the three most recent previous teams;
- full career histories (every stint with its game tag), and pairwise player relationships (verified shared stints, explicitly stated kinship);
- wrong resource types (a team page passed to a player tool, or the reverse) return `isError` with a machine-readable code and the right alternative instead of an empty success.

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -e .             # Windows; use .venv/bin/pip elsewhere
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Register `http://127.0.0.1:8000/mcp` (Streamable HTTP) in your MCP client:

- **ZCode** — `~/.zcode/cli/config.json` → `mcp.servers`
- **Claude Desktop / Cursor** — the client's `mcpServers` config
- **Codex CLI** — `~/.codex/config.toml` → `[mcp_servers.liquipedia-cs-mcp]` with the same `url`
- **ChatGPT desktop** — Settings → Connectors → Advanced → enable Developer mode → Add connector

```json
{ "liquipedia-cs-mcp": { "type": "http", "url": "http://127.0.0.1:8000/mcp" } }
```

> Too lazy to configure? Just hand this to your agent:
> "Install and register the local HTTP MCP server at http://127.0.0.1:8000/mcp in my MCP client, then verify the connection."

## MCP tools

| Tool | Input | Output |
| --- | --- | --- |
| `search_players` | `query`, `limit` (1–5) | player candidates with `player_id` |
| `get_team_roster` | `team_id`, `include_staff` | active players with nickname/real name/nationality/age/roles/join date and `confirmed`/`substitute`, plus staff listed separately |
| `get_player_network` | `player_id`, `wait_ms` (0–60000) | cached result, a terminal result after waiting, or a `job_id` to poll |
| `get_lookup_result` | `job_id` | status/progress + result with `sources`, timestamps, CC BY-SA 3.0 license, `warnings`; failed jobs carry `error.code` (`job_stalled`, ...) |
| `get_player_career` | `player_id` | full recorded team history: every stint with game tag (CS / CS:GO / CS2), dates at recorded precision, stint identity, rename-resolved teams |
| `get_player_relationship` | `player_a`, `player_b`, `include_non_overlapping` | overlapping shared stints (`confirmed` / `unconfirmed`), playing identities, explicitly stated kinship; non-overlapping stints are counted in `omitted` and hidden by default |

## Configuration (env vars, `LIQUIPEDIA_` prefix)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIQUIPEDIA_CONTACT` | *(set in `.env`)* | required contact in the User-Agent |
| `LIQUIPEDIA_CACHE_TTL_SECONDS` | `86400` | page cache freshness |
| `LIQUIPEDIA_PAGE_BUDGET_PER_JOB` | `30` | fresh pages per lookup job |
| `LIQUIPEDIA_JOB_STALL_SECONDS` | `60` | no-heartbeat threshold before an orphaned job becomes `failed` / `job_stalled` |
| `LIQUIPEDIA_API_URL` | counterstrike wiki API | upstream endpoint |

## Compliance design

- A single serialized scheduler paces every upstream request with randomized 2.3–3.0 s spacing (above Liquipedia's 2 s floor) and persists the clock in SQLite; 429s honor `Retry-After`, and page budgets keep every job resumable.
- Page fetches are batched (`titles=A|B|C`, ≤10 titles per request), so a full network lookup costs roughly 7 upstream requests instead of ~30.
