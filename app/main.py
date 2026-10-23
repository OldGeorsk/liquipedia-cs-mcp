"""FastAPI entry point: a /health endpoint plus the MCP server at /mcp."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.config import Settings
from app.liquipedia import LiquipediaClient
from app.network import PlayerNetworkService
from app.scheduler import RequestScheduler
from app.store import Store
from app.tools import build_server


def create_app(settings: Settings | None = None, client: LiquipediaClient | None = None) -> FastAPI:
    """Build the app; ``client`` lets tests inject an offline wiki double."""
    settings = settings or Settings()
    store = Store(settings.db_path)
    scheduler = RequestScheduler(store, settings)

    owns_http = client is None
    http: httpx.AsyncClient | None = None
    if owns_http:
        http = httpx.AsyncClient(
            timeout=settings.request_timeout, follow_redirects=True,
            headers={"Accept-Encoding": "gzip"},
        )
        client = LiquipediaClient(settings, store, scheduler, http)
    service = PlayerNetworkService(settings, store, client)

    mcp = build_server(service, client, settings)
    mcp_app = mcp.streamable_http_app(stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            await service.stop()
            if owns_http and http is not None:
                await http.aclose()

    app = FastAPI(title="liquipedia-cs-mcp", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # Mounted last so /health stays a plain FastAPI route, not an MCP route.
    app.mount("/", mcp_app)
    return app


app = create_app()
