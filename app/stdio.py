"""Stdio entry point: an MCP client spawns this process and talks over pipes.

Works in two layouts with zero configuration:
- a repository checkout: ``python -m app.stdio`` anchors to the repo root,
  so ``data/`` and ``.env`` stay local to the clone;
- an installed package (pip / uvx): the console script ``liquipedia-cs-mcp``
  keeps its database under ``~/.liquipedia-cs-mcp/`` instead — an installed
  copy must never write into site-packages.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

from app.config import Settings
from app.liquipedia import LiquipediaClient
from app.network import PlayerNetworkService
from app.scheduler import RequestScheduler
from app.store import Store
from app.tools import build_server

_REPO_ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = Path.home() / ".liquipedia-cs-mcp"
_REPO_MARKER = "pyproject.toml"  # only a checkout has this next to app/


def anchor_runtime() -> Path:
    """Point relative paths at the right place for the current layout.

    Returns the directory backing the SQLite cache. In a checkout we chdir
    to the repo so ``data/`` and ``.env`` resolve there; an installed copy
    instead defaults ``LIQUIPEDIA_DB_PATH`` to a per-user directory.
    """
    if (_REPO_ROOT / _REPO_MARKER).exists():
        os.chdir(_REPO_ROOT)
        return _REPO_ROOT / "data"
    USER_DATA_DIR.mkdir(exist_ok=True)
    os.environ.setdefault("LIQUIPEDIA_DB_PATH", str(USER_DATA_DIR / "liquipedia.db"))
    return USER_DATA_DIR


async def run() -> None:
    anchor_runtime()
    settings = Settings()
    default_contact = Settings.model_fields["contact"].default
    if settings.contact == default_contact:
        logging.getLogger(__name__).warning(
            "LIQUIPEDIA_CONTACT is not set; the User-Agent carries a "
            "placeholder. Set it via the env field of the MCP client config."
        )
    store = Store(settings.db_path)
    scheduler = RequestScheduler(store, settings)
    http = httpx.AsyncClient(
        timeout=settings.request_timeout, follow_redirects=True,
        headers={"Accept-Encoding": "gzip"},
    )
    client = LiquipediaClient(settings, store, scheduler, http)
    service = PlayerNetworkService(settings, store, client)
    server = build_server(service, client, settings)

    await service.start()
    try:
        await server.run_stdio_async()
    finally:
        await service.stop()
        await http.aclose()


def main() -> None:
    # stdout carries the JSON-RPC stream; diagnostics must stay on stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
