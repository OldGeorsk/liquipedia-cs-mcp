"""SQLite persistence: page cache, jobs and scheduler timing state.

Three tables, no ORM. All access is serialized behind a single lock because
the server is single-process; calls are tiny local-file operations.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    title TEXT PRIMARY KEY,          -- canonical page title
    page_id INTEGER,
    wikitext TEXT NOT NULL,
    revision_id INTEGER,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    player_id TEXT NOT NULL,
    status TEXT NOT NULL,
    stages TEXT NOT NULL,            -- JSON: partial results and remaining scope
    budget_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,                      -- JSON ErrorInfo, or a legacy plain string
    started_at TEXT,
    heartbeat_at TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY,            -- e.g. "last_request_at", "last_parse_at"
    value REAL NOT NULL              -- wall-clock epoch seconds
);
CREATE TABLE IF NOT EXISTS redirects (
    src TEXT PRIMARY KEY,            -- requested title (or normalized form)
    dst TEXT NOT NULL                -- canonical page title
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_json(error: Any) -> str | None:
    """Serialize an ErrorInfo / dict / plain string for the jobs table."""
    if error is None:
        return None
    if isinstance(error, str):
        return error
    if hasattr(error, "model_dump"):
        error = error.model_dump(exclude_none=True)
    return json.dumps(error, ensure_ascii=False)


def _error_dict(raw: str | None) -> dict[str, Any] | None:
    """Read both new JSON errors and legacy plain-string errors."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"code": "error", "message": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"code": "error", "message": str(parsed)}


class Store:
    """Thin synchronous wrapper over sqlite3; safe for one process."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """Add job-timing columns to databases created before they existed."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        for name in ("started_at", "heartbeat_at"):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")

    # -- scheduler timing -------------------------------------------------

    def get_time(self, key: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM scheduler_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_time(self, key: str, value: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scheduler_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # -- redirect aliases -----------------------------------------------------

    def get_redirect(self, src: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT dst FROM redirects WHERE src = ?", (src,)
            ).fetchone()
        return row[0] if row else None

    def canonical_title(self, title: str) -> str:
        """Follow saved redirect aliases to the canonical title."""
        seen: set[str] = set()
        while title not in seen:
            seen.add(title)
            nxt = self.get_redirect(title)
            if not nxt or nxt == title:
                return title
            title = nxt
        return title

    def save_redirect(self, src: str, dst: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO redirects (src, dst) VALUES (?, ?) "
                "ON CONFLICT(src) DO UPDATE SET dst = excluded.dst",
                (src, dst),
            )
            self._conn.commit()

    # -- page cache --------------------------------------------------------

    def get_page(self, title: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT title, page_id, wikitext, revision_id, fetched_at "
                "FROM pages WHERE title = ?",
                (title,),
            ).fetchone()
        if not row:
            return None
        return {
            "title": row[0],
            "page_id": row[1],
            "wikitext": row[2],
            "revision_id": row[3],
            "fetched_at": row[4],
        }

    def save_page(
        self, title: str, page_id: int | None, wikitext: str,
        revision_id: int | None, fetched_at: datetime,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO pages (title, page_id, wikitext, revision_id, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(title) DO UPDATE SET "
                "page_id = excluded.page_id, wikitext = excluded.wikitext, "
                "revision_id = excluded.revision_id, fetched_at = excluded.fetched_at",
                (title, page_id, wikitext, revision_id, fetched_at.isoformat()),
            )
            self._conn.commit()

    # -- jobs ---------------------------------------------------------------

    def save_job(
        self, job_id: str, player_id: str, status: str,
        stages: dict[str, Any], budget_used: int, error: Any = None,
    ) -> None:
        now = _now_iso()
        # started_at is stamped once, on the first non-queued state.
        started = None if status == "queued" else now
        error_text = _error_json(error)
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (job_id, player_id, status, stages, budget_used, "
                "created_at, updated_at, error, started_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, "
                "stages = excluded.stages, budget_used = excluded.budget_used, "
                "updated_at = excluded.updated_at, error = excluded.error, "
                "started_at = COALESCE(jobs.started_at, excluded.started_at), "
                "heartbeat_at = excluded.heartbeat_at",
                (job_id, player_id, status, json.dumps(stages, ensure_ascii=False),
                 budget_used, now, now, error_text, started, now),
            )
            self._conn.commit()

    def touch_job(self, job_id: str) -> None:
        """Refresh a running job's heartbeat without rewriting its stages."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE job_id = ?",
                (_now_iso(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, player_id, status, stages, budget_used, "
                "created_at, updated_at, error, started_at, heartbeat_at "
                "FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "job_id": row[0], "player_id": row[1], "status": row[2],
            "stages": json.loads(row[3]), "budget_used": row[4],
            "created_at": row[5], "updated_at": row[6], "error": _error_dict(row[7]),
            "started_at": row[8], "heartbeat_at": row[9],
        }

    def unfinished_jobs(self) -> list[dict[str, Any]]:
        """Jobs left queued/running, e.g. by a process restart."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM jobs WHERE status IN ('queued', 'running') "
                "ORDER BY updated_at"
            ).fetchall()
        return [job for row in rows if (job := self.get_job(row[0]))]

    def latest_job_for_player(self, player_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id FROM jobs WHERE player_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (player_id,),
            ).fetchone()
        return self.get_job(row[0]) if row else None
