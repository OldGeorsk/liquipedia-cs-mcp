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


@dataclass
class BatchResult:
    """Outcome of a batched page fetch."""

    pages: dict[str, PageContent]  # by requested title
    missing: set[str]              # requested titles with no page upstream
    deferred: set[str]             # skipped: would exceed max_fresh
    fresh_pages: set[str]          # canonical titles actually loaded now


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

    async def search(
        self, query: str, limit: int, namespace: int | None = 0,
    ) -> list[SearchHit]:
        """Full-text search; default namespace 0 keeps players/tournaments down."""
        params = {
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": str(limit), "sroffset": "0", "format": "json",
        }
        if namespace is not None:
            params["srnamespace"] = str(namespace)
        data = await self._request(params)
        hits: list[SearchHit] = []
        for item in data.get("query", {}).get("search", []):
            snippet = _TAG.sub("", item.get("snippet", "")).strip()
            hits.append(SearchHit(item.get("title", ""), item.get("pageid"), snippet))
        return hits

    async def get_wikitext(self, title: str) -> PageContent:
        """Return cached wikitext when fresh; otherwise fetch one revision."""
        cached = self._cached_page(title)
        if cached:
            return cached
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

    async def get_wikitext_many(
        self, titles: list[str], max_fresh: int | None = None,
    ) -> BatchResult:
        """Fetch several pages in as few requests as possible.

        MediaWiki accepts ``titles=A|B|C``; each chunk of ``batch_page_limit``
        uncached titles becomes exactly one upstream request, so N pages cost
        N/batch_page_limit requests instead of N. Cached titles are served
        free; ``max_fresh`` (the caller's remaining page budget) defers the
        titles that would overspend it instead of fetching partially.
        """
        pages: dict[str, PageContent] = {}
        wanted: list[str] = []
        for title in dict.fromkeys(titles):  # dedupe, keep order
            cached = self._cached_page(title)
            if cached is not None:
                pages[title] = cached
            else:
                wanted.append(title)
        fresh_budget = len(wanted) if max_fresh is None else max(0, max_fresh)
        deferred = set(wanted[fresh_budget:])
        result = BatchResult(pages=pages, missing=set(), deferred=deferred,
                             fresh_pages=set())
        for start in range(0, fresh_budget, self._settings.batch_page_limit):
            chunk = wanted[start:start + self._settings.batch_page_limit]
            chunk_pages, chunk_missing, chunk_fresh = await self._fetch_chunk(chunk)
            result.pages.update(chunk_pages)
            result.missing.update(chunk_missing)
            result.fresh_pages.update(chunk_fresh)
        return result

    async def _fetch_wikitext(self, title: str) -> PageContent:
        params = {
            "action": "query", "prop": "revisions", "rvprop": "ids|content",
            "rvslots": "main", "titles": title, "redirects": "1", "format": "json",
        }
        data = await self._request(params)
        query = data.get("query", {})
        # Remember redirect/normalization chains so future lookups hit cache.
        previous = title
        for redirect in query.get("redirects", []) + query.get("normalized", []):
            self._store.save_redirect(previous, redirect.get("to", previous))
            previous = redirect.get("to", previous)
        pages = query.get("pages", {})
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

    async def _fetch_chunk(self, titles: list[str]) -> tuple[dict[str, PageContent], set[str], set[str]]:
        """One upstream request for several titles.

        Returns (pages by requested title, missing titles, fresh canonical titles).
        """
        params = {
            "action": "query", "prop": "revisions", "rvprop": "ids|content",
            "rvslots": "main", "titles": "|".join(titles), "redirects": "1",
            "format": "json",
        }
        data = await self._request(params)
        query = data.get("query", {})
        # Map every hop of the redirect/normalization chains for this response.
        hop: dict[str, str] = {}
        for redirect in query.get("redirects", []) + query.get("normalized", []):
            src, dst = redirect.get("from", ""), redirect.get("to", "")
            if src and dst:
                hop[src] = dst
                self._store.save_redirect(src, dst)
        fetched_at = datetime.now(timezone.utc)
        by_canonical: dict[str, PageContent] = {}
        for page in query.get("pages", {}).values():
            revisions = page.get("revisions") or []
            if "missing" in page or not revisions:
                continue
            slot = revisions[0].get("slots", {}).get("main", {})
            content = slot.get("*", "")
            canonical = page.get("title", "")
            self._store.save_page(
                canonical, page.get("pageid"), content,
                revisions[0].get("revid"), fetched_at,
            )
            by_canonical[canonical] = PageContent(
                title=canonical, page_id=page.get("pageid"), wikitext=content,
                revision_id=revisions[0].get("revid"), fetched_at=fetched_at,
                from_cache=False,
            )
        pages: dict[str, PageContent] = {}
        missing: set[str] = set()
        for title in titles:
            target = title
            seen: set[str] = set()
            while target in hop and target not in seen:
                seen.add(target)
                target = hop[target]
            if target in by_canonical:
                pages[title] = by_canonical[target]
            else:
                missing.add(title)
        fresh = {page.title for page in by_canonical.values()}
        return pages, missing, fresh

    def _cached_page(self, title: str) -> PageContent | None:
        """Fresh cached page for a title, following redirect aliases."""
        row = self._store.get_page(self._canonical(title))
        if not row or not self._is_fresh(row):
            return None
        return PageContent(
            title=row["title"], page_id=row["page_id"],
            wikitext=row["wikitext"], revision_id=row["revision_id"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            from_cache=True,
        )

    def _canonical(self, title: str) -> str:
        """Follow saved redirect aliases to the canonical title."""
        return self._store.canonical_title(title)

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
