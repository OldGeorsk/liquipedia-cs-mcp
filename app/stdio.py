"""Stdio entry point: an MCP client spawns this process and talks over pipes.

Same tool server as `app.main` (uvicorn/HTTP) but with zero client-side
setup: no port, no manual start. The working directory, cache database and
.env all anchor to this repository, so clients may launch us from anywhere.
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


async def run() -> None:
    # Relative paths (db, .env) must resolve against the repo, not whatever
    # directory the spawning client happens to sit in.
    os.chdir(_REPO_ROOT)
    settings = Settings()
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
