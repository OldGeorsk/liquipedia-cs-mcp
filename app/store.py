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
    error TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY,            -- e.g. "last_request_at", "last_parse_at"
    value REAL NOT NULL              -- wall-clock epoch seconds
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Thin synchronous wrapper over sqlite3; safe for one process."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

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
        stages: dict[str, Any], budget_used: int, error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (job_id, player_id, status, stages, budget_used, "
                "created_at, updated_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, "
                "stages = excluded.stages, budget_used = excluded.budget_used, "
                "updated_at = excluded.updated_at, error = excluded.error",
                (job_id, player_id, status, json.dumps(stages, ensure_ascii=False),
                 budget_used, _now_iso(), _now_iso(), error),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, player_id, status, stages, budget_used, "
                "created_at, updated_at, error FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "job_id": row[0], "player_id": row[1], "status": row[2],
            "stages": json.loads(row[3]), "budget_used": row[4],
            "created_at": row[5], "updated_at": row[6], "error": row[7],
        }

    def latest_job_for_player(self, player_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id FROM jobs WHERE player_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (player_id,),
            ).fetchone()
        return self.get_job(row[0]) if row else None
