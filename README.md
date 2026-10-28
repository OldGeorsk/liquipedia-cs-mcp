# liquipedia-cs-mcp
English | [简体中文](README.zh-CN.md)

- Local MCP server that looks up Counter-Strike teams and players on [Liquipedia](https://liquipedia.net/counterstrike):
- current team rosters (players and staff kept apart), player profiles, current teammates, and the teammates of the three most recent previous teams;
- full career histories (every stint with its game tag), and pairwise player relationships (verified shared stints, explicitly stated kinship);
- team search that filters out player/tournament pages, and one-call relationship matrices across up to five rosters (or an explicit player list) with zero per-pair upstream requests;
- a team's full tournament record (`/Results` history) with per-tier summaries, plus a generic `search_wiki` / `read_wiki_page` fallback that reaches any wiki page when no dedicated tool fits;
- wrong resource types (a team page passed to a player tool, or the reverse) return `isError` with a machine-readable code and the right alternative instead of an empty success.

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -e .             # Windows; use .venv/bin/pip elsewhere
```

Register as a **stdio** server — the client spawns and manages the process itself: no port, no manual start. Replace `D:/src/liquipedia-mcp` with your clone path:

```json
{
  "liquipedia-cs-mcp": {
    "type": "stdio",
    "command": "D:/src/liquipedia-mcp/.venv/Scripts/python.exe",
    "args": ["-m", "app.stdio"]
  }
}
```

On macOS/Linux the command is `<clone>/.venv/bin/python`.

- **ZCode** — `~/.zcode/cli/config.json` → `mcp.servers`
- **Claude Desktop / Cursor** — the client's `mcpServers` config
- **Codex CLI** — `~/.codex/config.toml` → `[mcp_servers.liquipedia-cs-mcp]` with the same `command`/`args`

Advanced (one shared instance for many clients, and the only mode ChatGPT desktop connectors can use): run
`.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` and register `{"type": "http", "url": "http://127.0.0.1:8000/mcp"}` instead. Rate limiting coordinates through one process, so avoid heavy concurrent use of stdio and HTTP instances at the same time.

> Too lazy to configure? Just hand this to your agent:
> "Register the stdio MCP server at D:/src/liquipedia-mcp (command: .venv/Scripts/python.exe, args: -m app.stdio) in my MCP client, then verify the connection."

## MCP tools

| Tool | Input | Output |
| --- | --- | --- |
| `search_players` | `query`, `limit` (1–5) | player candidates with `player_id` |
| `search_teams` | `query`, `limit` (1–10) | team candidates with `team_id`; player/tournament/disambiguation pages are filtered out, exact title matches lead |
| `get_team_roster` | `team_id`, `include_staff` | active players with nickname/real name/nationality/age/roles/join date and `confirmed`/`substitute`, plus staff listed separately |
| `get_player_network` | `player_id`, `wait_ms` (0–60000) | cached result, a terminal result after waiting, or a `job_id` to poll |
| `get_lookup_result` | `job_id` | status/progress + result with `sources`, timestamps, CC BY-SA 3.0 license, `warnings`; failed jobs carry `error.code` (`job_stalled`, ...) |
| `get_player_career` | `player_id` | full recorded team history: every stint with game tag (CS / CS:GO / CS2), dates at recorded precision, stint identity, rename-resolved teams |
| `get_player_relationship` | `player_a`, `player_b`, `include_non_overlapping` | overlapping shared stints (`confirmed` / `unconfirmed`), playing identities, explicitly stated kinship; non-overlapping stints are counted in `omitted` and hidden by default |
| `get_relationship_matrix` | `teams` (2–5) or `players` (2–20), `include_staff`, `include_non_overlapping`, `max_pairs` (1–500) | one call compares every cross-team pair: `connections[]`, `same_team_no_overlap[]`, `stats`, `skipped[]`; invalid teams fail the whole call, a missing player page yields `partial` |
| `get_team_achievements` | `team_id`, `tier` (optional, e.g. `S-Tier` or `S`) | full tournament record from the team's `/Results` subpage: every row with date/placement/tier/tournament/score/opponent/prize, plus per-tier summaries (events/wins/runner-up/top4/prize) |
| `search_wiki` | `query`, `limit` (1–20) | unfiltered full-text search over any page type — the fallback when dedicated tools miss |
| `read_wiki_page` | `title`, `format` (`wikitext`/`html`), `section` | raw content of any page including subpages, capped at 40k chars; `html` is required for LPDB-generated tables (results, statistics) |

## Configuration (env vars, `LIQUIPEDIA_` prefix)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIQUIPEDIA_CONTACT` | *(set in `.env`)* | required contact in the User-Agent |
| `LIQUIPEDIA_CACHE_TTL_SECONDS` | `86400` | page cache freshness |
| `LIQUIPEDIA_PAGE_BUDGET_PER_JOB` | `30` | fresh pages per lookup job |
| `LIQUIPEDIA_JOB_STALL_SECONDS` | `60` | no-heartbeat threshold before an orphaned job becomes `failed` / `job_stalled` |
| `LIQUIPEDIA_API_URL` | counterstrike wiki API | upstream endpoint |

## Compliance design

- A single serialized scheduler paces every upstream request with randomized 2.3–3.0 s spacing (above Liquipedia's 2 s floor; rendered-page parse requests get a wider 33–35 s lane) and persists the clock in SQLite; 429s honor `Retry-After`, and page budgets keep every job resumable.
- Page fetches are batched (`titles=A|B|C`, ≤10 titles per request), so a full network lookup costs roughly 7 upstream requests instead of ~30.
