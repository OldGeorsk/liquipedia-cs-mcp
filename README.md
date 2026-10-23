# liquipedia-cs-mcp

Local MCP server that looks up Counter-Strike players on
[Liquipedia](https://liquipedia.net/counterstrike): profile, current
teammates, and the teammates of the three most recent previous teams.
MVP scope and rules live in `docs/PRD/`.

## Install & run

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # Windows; use .venv/bin/pip elsewhere
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- `GET /health` — plain liveness check.
- `POST /mcp` — MCP endpoint (Streamable HTTP, stateless).

## MCP tools

| Tool | Input | Output |
| --- | --- | --- |
| `search_players` | `query`, `limit` (1–5) | player candidates with `player_id` (page title) |
| `get_player_network` | `player_id` | cached result, or a `job_id` to poll |
| `get_lookup_result` | `job_id` | status, progress, partial/full result |

Every result carries `sources`, `fetched_at`/`served_at`, license
(CC BY-SA 3.0) and `warnings`. Missing facts come back as `null` with a
reason — nothing is inferred.

## Configuration (env vars, `LIQUIPEDIA_` prefix)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIQUIPEDIA_CONTACT` | repo owner's e-mail | required contact in the User-Agent |
| `LIQUIPEDIA_CACHE_TTL_SECONDS` | `86400` | page cache freshness |
| `LIQUIPEDIA_PAGE_BUDGET_PER_JOB` | `30` | fresh pages per lookup job |
| `LIQUIPEDIA_API_URL` | counterstrike wiki API | upstream endpoint |

## Compliance design

All upstream traffic passes one serialized scheduler: adjacent request
starts are spaced by a fresh random draw in [2.3, 3.0] s; `action=parse`
requests (unused in the MVP data path) would additionally respect
[33, 35] s. Timing state is persisted in SQLite, so restarts never reset
the clock. 429 answers honor `Retry-After`, retries re-queue through the
same gate, and page budgets make every job resumable (`partial` status).

## Development

```bash
.venv/Scripts/python -m pytest tests/          # offline suite (simulated clock)
.venv/Scripts/python tests/real_lookup_demo.py # live end-to-end check
```
