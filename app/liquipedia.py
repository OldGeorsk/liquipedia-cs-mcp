"""MediaWiki provider: the only module that talks to Liquipedia.

Every request goes through the scheduler, responses come from cache when
fresh, and identical in-flight requests are coalesced into one upstream call.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import Settings
from app.scheduler import RequestScheduler
from app.store import Store


class ProviderError(Exception):
    """Upstream refused or returned a MediaWiki error object."""

    def __init__(self, message: str, code: str = "provider_error") -> None:
        super().__init__(message)
        self.code = code


class RateLimited(Exception):
    """HTTP 429; carries the server-provided Retry-After seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass
class SearchHit:
    title: str
    page_id: int | None
    snippet: str  # plain-text fragment with search highlights stripped


@dataclass
class PageContent:
    title: str            # canonical title after redirect resolution
    page_id: int | None
    wikitext: str
    revision_id: int | None
    fetched_at: datetime
    from_cache: bool


_TAG = re.compile(r"<[^>]+>")


def page_url(settings: Settings, title: str) -> str:
    return f"{settings.api_url.rsplit('/api.php', 1)[0]}/{title.replace(' ', '_')}"


class LiquipediaClient:
    """Search and raw-wikitext fetching for one wiki, fully rate-limited."""

    def __init__(
        self, settings: Settings, store: Store, scheduler: RequestScheduler,
        http: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._store = store
        self._scheduler = scheduler
        self._http = http
        self._inflight: dict[str, asyncio.Task[PageContent]] = {}

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        params = {
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": str(limit), "sroffset": "0", "format": "json",
        }
        data = await self._request(params)
        hits: list[SearchHit] = []
        for item in data.get("query", {}).get("search", []):
            snippet = _TAG.sub("", item.get("snippet", "")).strip()
            hits.append(SearchHit(item.get("title", ""), item.get("pageid"), snippet))
        return hits

    async def get_wikitext(self, title: str) -> PageContent:
        """Return cached wikitext when fresh; otherwise fetch one revision."""
        cached = self._store.get_page(title)
        if cached and self._is_fresh(cached):
            return PageContent(
                title=cached["title"], page_id=cached["page_id"],
                wikitext=cached["wikitext"], revision_id=cached["revision_id"],
                fetched_at=datetime.fromisoformat(cached["fetched_at"]),
                from_cache=True,
            )
        # Coalesce: concurrent callers for the same title share one request.
        existing = self._inflight.get(title)
        if existing is not None:
            return await asyncio.shield(existing)
        task = asyncio.create_task(self._fetch_wikitext(title))
        self._inflight[title] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._inflight.pop(title, None)

    async def _fetch_wikitext(self, title: str) -> PageContent:
        params = {
            "action": "query", "prop": "revisions", "rvprop": "ids|content",
            "rvslots": "main", "titles": title, "redirects": "1", "format": "json",
        }
        data = await self._request(params)
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            revisions = page.get("revisions") or []
            if "missing" in page or not revisions:
                raise ProviderError(f"page not found: {title}", code="page_missing")
            slot = revisions[0].get("slots", {}).get("main", {})
            canonical = page.get("title", title)
            fetched_at = datetime.now(timezone.utc)
            content = slot.get("*", "")
            self._store.save_page(
                canonical, page.get("pageid"), content,
                revisions[0].get("revid"), fetched_at,
            )
            return PageContent(
                title=canonical, page_id=page.get("pageid"), wikitext=content,
                revision_id=revisions[0].get("revid"), fetched_at=fetched_at,
                from_cache=False,
            )
        raise ProviderError(f"page not found: {title}", code="page_missing")

    def _is_fresh(self, cached: dict) -> bool:
        fetched = datetime.fromisoformat(cached["fetched_at"]).timestamp()
        now = datetime.now(timezone.utc).timestamp()
        return now - fetched <= self._settings.cache_ttl_seconds

    async def _request(self, params: dict[str, str]) -> dict:
        await self._scheduler.acquire()  # every upstream call starts here
        response = await self._http.get(
            self._settings.api_url, params=params,
            headers={"User-Agent": self._settings.user_agent},
        )
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "35"))
            raise RateLimited(retry_after)
        if response.status_code == 403:
            raise ProviderError("upstream returned 403; stopping politely", code="forbidden")
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise ProviderError(
                data["error"].get("info", "unknown MediaWiki error"),
                code=data["error"].get("code", "mediawiki_error"),
            )
        return data
