# core/extractor.py
"""
Data-layer extractor for Asura Scans.

This module replaces the old Playwright/DOM scraper entirely.  Asura's
front-end is an Astro application that talks to a first-party JSON API, so
everything we need - series metadata, the chapter list and the page image
URLs - is available as JSON.  No browser, no scrolling, no DOM walking.

Endpoint map (all relative to the resolved API base)::

    GET  /api/search?q=&limit=&offset=      -> {"data": [...], "meta": {...}}
    GET  /api/series/{slug}                  -> {"series": {...}, ...}
    GET  /api/series/{slug}/chapters?limit=&offset=
                                           -> {"data": [{number, slug, ...}]}
    GET  /api/series/{slug}/chapters/{chapter_slug}
                                           -> {"data": {"chapter": {"pages": [...]},
                                                        "is_locked": bool,
                                                        "unlock_time": str|null}}

Python 3.15 features used
--------------------------
* **PEP 798 - unpacking in comprehensions.**  ``[*chunk for chunk in chunks]``
  flattens the paginated API responses into a single list, and
  ``{**mapping for mapping in maps}`` merges the several dictionary shapes
  the API returns into one normalised mapping.
* **PEP 814 - builtin ``frozendict``.**  All configuration comes from
  :mod:`core.config` as immutable mappings; the extractor merges per-request
  headers with ``|`` and never mutates shared state.
* **PEP 810 - explicit lazy imports.**  ``aiohttp`` is imported lazily so
  that merely importing this module stays cheap.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import unquote, urlparse

lazy import aiohttp

from core.config import (
    BROWSER_PROFILES,
    CONCURRENCY,
    RETRY,
    TIMEOUTS,
    Endpoints,
    build_headers,
    resolve_endpoints,
)

__all__ = [
    "ExtractionStatus",
    "PageImage",
    "ChapterRef",
    "SeriesInfo",
    "SearchResult",
    "ChapterExtraction",
    "AsuraExtractor",
    "ExtractionError",
    "SeriesNotFoundError",
    "ChapterNotFoundError",
    "parse_series_url",
    "parse_chapter_url",
]


# ============================================================
# ERRORS
# ============================================================
class ExtractionError(Exception):
    """Base class for every error raised by the extractor."""


class SeriesNotFoundError(ExtractionError):
    """The requested series slug does not exist upstream."""


class ChapterNotFoundError(ExtractionError):
    """The requested chapter does not exist upstream."""


# ============================================================
# RESULT TYPES
# ============================================================
class ExtractionStatus(StrEnum):
    """
    Outcome of a chapter extraction.

    ``SKIPPED_*`` members are *not* errors - they describe content the
    server intentionally withholds.  The download queue treats them as
    "log a warning and move on" so that one paywalled chapter never sinks
    an entire batch.
    """

    OK = "ok"
    #: Premium / early-access chapter.  Page images are not available.
    SKIPPED_PREMIUM = "skipped_premium"
    #: The chapter exists but the server returned no page images for it.
    SKIPPED_NO_PAGES = "skipped_no_pages"
    #: The chapter could not be found upstream.
    NOT_FOUND = "not_found"
    #: A transport or server error that survived the retry policy.
    ERROR = "error"

    @property
    def is_skipped(self) -> bool:
        """Whether this outcome is a graceful skip rather than a failure."""
        return self in _SKIP_STATUSES


_SKIP_STATUSES: Final[frozenset[ExtractionStatus]] = frozenset(
    {ExtractionStatus.SKIPPED_PREMIUM, ExtractionStatus.SKIPPED_NO_PAGES}
)


@dataclass(frozen=True, slots=True)
class PageImage:
    """A single page of a chapter, hosted on the image CDN."""

    url: str
    width: int = 0
    height: int = 0

    @property
    def extension(self) -> str:
        """File extension of the image, derived from the CDN URL."""
        path = urlparse(self.url).path
        _, _, ext = path.rpartition(".")
        return f".{ext}" if ext and len(ext) <= 5 else ".jpg"


@dataclass(frozen=True, slots=True)
class ChapterRef:
    """Lightweight chapter metadata as returned by the chapter-list endpoint."""

    number: int
    slug: str
    page_count: int = 0
    is_premium: bool = False
    series_slug: str = ""
    published_at: str | None = None
    early_access_until: str | None = None

    @property
    def label(self) -> str:
        """Human readable chapter label, e.g. ``Chapter 25``."""
        return f"Chapter {self.number}"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ChapterRef:
        return cls(
            number=int(payload.get("number") or 0),
            slug=str(payload.get("slug") or ""),
            page_count=int(payload.get("page_count") or 0),
            is_premium=bool(payload.get("is_premium")),
            series_slug=str(payload.get("series_slug") or ""),
            published_at=payload.get("published_at"),
            early_access_until=payload.get("early_access_until"),
        )


@dataclass(frozen=True, slots=True)
class SeriesInfo:
    """Metadata for one series."""

    id: int
    slug: str
    title: str
    status: str = ""
    type: str = ""
    chapter_count: int = 0
    rating: float = 0.0
    cover_url: str | None = None
    public_url: str | None = None
    genres: tuple[str, ...] = ()

    @property
    def is_novel(self) -> bool:
        """Whether this entry is a text novel rather than a comic."""
        return self.type.lower() == "novel"


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One hit from ``/api/search``."""

    id: int
    slug: str
    title: str
    latest_chapter: str = ""
    chapter_count: int = 0
    cover_url: str | None = None
    public_url: str | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class ChapterExtraction:
    """
    The result of extracting a single chapter.

    ``status`` tells the caller what happened.  For
    :attr:`ExtractionStatus.OK` the ``pages`` tuple holds the image URLs in
    reading order.  For a skipped chapter ``pages`` is empty and ``reason``
    explains why - the download queue logs it and continues.
    """

    chapter: ChapterRef
    status: ExtractionStatus
    pages: tuple[PageImage, ...] = ()
    reason: str | None = None
    unlock_time: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is ExtractionStatus.OK

    @property
    def is_skipped(self) -> bool:
        """Whether the chapter was skipped rather than extracted."""
        return self.status.is_skipped

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def image_urls(self) -> tuple[str, ...]:
        """Just the CDN URLs, in page order."""
        return tuple(page.url for page in self.pages)


# ============================================================
# URL PARSING
# ============================================================
#: The front-end appends a constant build hash to the slug in public URLs
#: (``/comics/solo-swordmaster-f212c582``).  The API wants the bare slug.
_HASH_SUFFIX: Final[re.Pattern[str]] = re.compile(r"-[0-9a-f]{8}$")


def _strip_hash_suffix(value: str) -> str:
    """Remove a trailing ``-<8 hex>`` build hash from *value*."""
    return _HASH_SUFFIX.sub("", value)


def parse_series_url(url_or_slug: str) -> str:
    """
    Turn a user-supplied series URL (or bare slug) into an API slug.

    Handles every shape the site has used::

        /comics/solo-swordmaster-f212c582
        /comics/solo-swordmaster
        /comics/solo-swordmaster/chapter/25   -> series part only
        solo-swordmaster                        -> unchanged

    Raises:
        ValueError: if no series slug can be derived.
    """
    raw = (url_or_slug or "").strip()
    if not raw:
        raise ValueError("No series URL or slug supplied.")

    if "://" not in raw:
        # Bare slug (possibly with the build hash).
        slug = _strip_hash_suffix(raw.strip("/"))
        if not slug:
            raise ValueError(f"Could not derive a series slug from {url_or_slug!r}.")
        return slug

    path = unquote(urlparse(raw).path)
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts):
        if part in {"comics", "series"} and index + 1 < len(parts):
            slug = _strip_hash_suffix(parts[index + 1])
            if slug:
                return slug
        if part == "novels" and index + 1 < len(parts):
            slug = _strip_hash_suffix(parts[index + 1])
            if slug:
                return slug

    raise ValueError(
        f"Could not derive a series slug from {url_or_slug!r}. "
        "Expected a URL like https://asurascans.com/comics/<slug>."
    )


def parse_chapter_url(url: str) -> tuple[str, int | None]:
    """
    Parse a chapter URL into ``(series_slug, chapter_number)``.

    ``chapter_number`` is ``None`` when the URL does not point at a
    specific chapter.
    """
    slug = parse_series_url(url)
    path = unquote(urlparse(url).path)
    parts = [part for part in path.split("/") if part]
    number: int | None = None
    for index, part in enumerate(parts):
        if part == "chapter" and index + 1 < len(parts):
            candidate = parts[index + 1]
            # Both "/chapter/25" and "/chapter/chapter-25" are in the wild.
            if candidate.startswith("chapter-"):
                candidate = candidate[len("chapter-") :]
            try:
                number = int(candidate)
            except ValueError:
                number = None
            break
    return slug, number


# ============================================================
# RATE LIMITER PROTOCOL
# ============================================================
@runtime_checkable
class RateLimiter(Protocol):
    """
    Structural type for a rate limiter.

    The download queue's token bucket satisfies this protocol and can be
    handed to the extractor so API traffic and image traffic share one
    budget.
    """

    async def acquire(self) -> None:
        """Block until the caller is allowed to issue a request."""
        ...


# ============================================================
# EXTRACTOR
# ============================================================
class AsuraExtractor:
    """
    Async client for the Asura Scans JSON API.

    The extractor owns a single :class:`aiohttp.ClientSession` so that all
    requests share one connection pool.  Use it as an async context manager
    to have the session managed for you::

        async with AsuraExtractor() as ex:
            series = await ex.get_series("solo-swordmaster")

    or pass an existing ``session`` in when the download queue wants to
    share a pool.
    """

    #: Page size used for the paginated chapter-list endpoint.
    #:
    #: NOTE: the upstream API mis-handles ``limit=100`` - when it is combined
    #: with an explicit ``offset`` the window shifts by one and the newest
    #: chapter is dropped from the result.  99 is the largest value that
    #: behaves correctly, so we stay well clear of it.
    MAX_PAGE_SIZE: Final[int] = 99

    #: Page size used for search.  The endpoint silently falls back to 20
    #: for any ``limit`` above 50, so we cap it explicitly.
    MAX_SEARCH_PAGE_SIZE: Final[int] = 50

    #: The API refuses ``limit`` values of 500 or more.
    HARD_LIMIT_CEILING: Final[int] = 400

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        endpoints: Endpoints | None = None,
        limiter: RateLimiter | None = None,
        max_concurrency: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._endpoints = endpoints
        self._limiter = limiter
        self._max_concurrency = max_concurrency or CONCURRENCY["api"]
        self._timeout = timeout if timeout is not None else TIMEOUTS["total"]
        self._semaphore: asyncio.Semaphore | None = None
        self._profile_index = 0
        self._owns_semaphore = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> AsuraExtractor:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Resolve endpoints and open the HTTP session if needed."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
            self._owns_semaphore = True
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers=self._next_headers(),
            )
            self._owns_session = True

    async def close(self) -> None:
        """Close the session if this extractor owns it."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def endpoints(self) -> Endpoints:
        """The resolved API/web/CDN hosts (resolving them if necessary)."""
        if self._endpoints is None:
            raise ExtractionError(
                "Endpoints have not been resolved yet - call `await start()` "
                "or use the extractor as an async context manager."
            )
        return self._endpoints

    async def ensure_endpoints(self) -> Endpoints:
        """Resolve the working hosts on demand and remember them."""
        if self._endpoints is None:
            self._endpoints = await resolve_endpoints()
        return self._endpoints

    # ------------------------------------------------------------------
    # Header rotation
    # ------------------------------------------------------------------
    def _next_headers(self, referer: str | None = None) -> frozendict:
        """
        Build the headers for the next request.

        The browser fingerprint rotates on every call so a burst of
        requests does not arrive with one identical ``User-Agent`` /
        ``Sec-CH-UA`` pair - Cloudflare scores repeated identical
        fingerprints much more aggressively than varied ones.
        """
        profile = BROWSER_PROFILES[self._profile_index % len(BROWSER_PROFILES)]
        self._profile_index += 1
        headers = build_headers(profile=profile)
        if referer:
            headers = headers | {"Referer": referer}
        return headers

    # ------------------------------------------------------------------
    # Low-level request
    # ------------------------------------------------------------------
    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> Any:
        """
        Perform a GET against the API and return the decoded JSON body.

        Retries transient failures (429/5xx) with exponential backoff and
        honours a ``Retry-After`` header when the origin sends one.
        """
        endpoints = await self.ensure_endpoints()
        url = f"{endpoints.api_base}{path}"
        headers = self._next_headers(referer=referer)

        attempts = RETRY["max_attempts"]
        status_forcelist = RETRY["status_forcelist"]

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self._limiter is not None:
                await self._limiter.acquire()
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(self._max_concurrency)

            try:
                async with self._semaphore:
                    async with self._session.get(url, params=params, headers=headers) as resp:
                        if resp.status == 404:
                            raise _NotFoundSignal(await resp.text())
                        if resp.status in status_forcelist:
                            retry_after = resp.headers.get("Retry-After")
                            body = await resp.text()
                            last_error = ExtractionError(
                                f"HTTP {resp.status} from {url}: {body[:180]}"
                            )
                            if attempt < attempts:
                                await asyncio.sleep(self._backoff(attempt, retry_after))
                                continue
                            raise last_error
                        if resp.status >= 400:
                            body = await resp.text()
                            raise ExtractionError(
                                f"HTTP {resp.status} from {url}: {body[:180]}"
                            )
                        return await resp.json(content_type=None)
            except _NotFoundSignal:
                raise
            except ExtractionError:
                raise
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(self._backoff(attempt, None))
                    continue
                raise ExtractionError(
                    f"Request to {url} failed after {attempts} attempts: {exc}"
                ) from exc

        raise ExtractionError(f"Request to {url} failed: {last_error}")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        """
        Compute the delay before the next attempt.

        A ``Retry-After`` header always wins - the origin knows better
        than we do how long Cloudflare wants us to wait.
        """
        if retry_after:
            try:
                return min(float(retry_after), RETRY["max_delay"])
            except ValueError:
                pass
        delay = RETRY["base_delay"] * (2 ** (attempt - 1))
        return min(delay, RETRY["max_delay"])

    # ------------------------------------------------------------------
    # Slug resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _slug_candidates(slug: str) -> list[str]:
        """
        Return the slug forms to try, most-likely first.

        The public URLs carry a build hash (``solo-swordmaster-f212c582``)
        that the API accepts inconsistently - some series resolve with it,
        some only without it.  Trying both makes the extractor immune to
        whichever form the site happens to prefer.
        """
        bare = _strip_hash_suffix(slug)
        if bare and bare != slug:
            return [bare, slug]
        return [slug]

    async def resolve_slug(self, slug: str) -> str:
        """
        Return the slug form the API actually accepts for *slug*.

        Raises:
            SeriesNotFoundError: if neither form resolves.
        """
        for candidate in self._slug_candidates(slug):
            try:
                payload = await self._request(f"/api/series/{candidate}")
            except _NotFoundSignal:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("series"), dict):
                return str(payload["series"].get("slug") or candidate)
        raise SeriesNotFoundError(f"Series {slug!r} not found.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        pages: int = 1,
        per_page: int = 20,
    ) -> list[SearchResult]:
        """
        Search for series by name.

        Args:
            query: the search term.
            pages: how many pages of results to fetch.
            per_page: results per page (capped at
                :attr:`MAX_SEARCH_PAGE_SIZE`).

        Returns:
            The combined, de-duplicated result list.
        """
        limit = max(1, min(per_page, self.MAX_SEARCH_PAGE_SIZE))

        async def fetch_page(offset: int) -> list[dict[str, Any]]:
            payload = await self._request(
                "/api/search",
                params={"q": query, "limit": limit, "offset": offset},
            )
            # The API returns ``"data": null`` (not an empty list) when the
            # offset is past the end of the result set.
            return payload.get("data") or []

        chunks = await asyncio.gather(
            *(fetch_page(offset * limit) for offset in range(max(1, pages)))
        )
        # PEP 798 - flatten the per-page payloads into one list.
        raw_results = [*chunk for chunk in chunks]

        seen: set[str] = set()
        results: list[SearchResult] = []
        for item in raw_results:
            slug = str(item.get("slug") or "")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            latest = ""
            chapters = item.get("latest_chapters") or []
            if chapters:
                latest = f"Chapter {chapters[0].get('number')}"
            results.append(
                SearchResult(
                    id=int(item.get("id") or 0),
                    slug=slug,
                    title=str(item.get("title") or slug),
                    latest_chapter=latest,
                    chapter_count=int(item.get("chapter_count") or 0),
                    cover_url=item.get("cover"),
                    public_url=item.get("public_url"),
                    source_url=item.get("source_url"),
                )
            )
        return results

    async def get_series(self, slug: str) -> SeriesInfo:
        """
        Fetch the metadata for a single series.

        The API is picky about the slug: some series resolve only with the
        bare slug while others only resolve with the build-hash suffix
        still attached.  We try the bare form first and fall back to the
        suffixed form.

        Raises:
            SeriesNotFoundError: if the series does not exist.
        """
        payload: Any = None
        for candidate in self._slug_candidates(slug):
            try:
                payload = await self._request(f"/api/series/{candidate}")
            except _NotFoundSignal:
                continue
            series = payload.get("series") if isinstance(payload, dict) else None
            if isinstance(series, dict):
                break
            # A 200 with a different payload shape means this candidate
            # was not the right one - try the next form.
            payload = None

        if payload is None:
            raise SeriesNotFoundError(f"Series {slug!r} not found.")

        series = payload["series"]
        # Unpacking inside the comprehension maps each genre mapping's
        # items, keeping only the "name" entry.
        genre_names = tuple(
            value
            for genre in (series.get("genres") or [])
            if isinstance(genre, dict)
            for key, value in genre.items()
            if key == "name" and value
        )

        return SeriesInfo(
            id=int(series.get("id") or 0),
            slug=str(series.get("slug") or slug),
            title=str(series.get("title") or slug),
            status=str(series.get("status") or ""),
            type=str(series.get("type") or ""),
            chapter_count=int(series.get("chapter_count") or 0),
            rating=float(series.get("rating") or 0.0),
            cover_url=series.get("cover"),
            public_url=series.get("public_url"),
            genres=genre_names,
        )

    async def _repair_missing_chapters(
        self,
        series_slug: str,
        chapters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Make sure the chapter list is complete.

        The upstream pagination is not perfectly reliable, so we compare the
        number of chapters we received against the series' advertised
        ``chapter_count``.  If something is missing we re-request the tail
        of the list without an ``offset`` parameter, which sidesteps the
        off-by-one bug entirely.
        """
        try:
            series = await self.get_series(series_slug)
        except (SeriesNotFoundError, ExtractionError):
            return chapters

        expected = series.chapter_count
        if not expected or len(chapters) >= expected:
            return chapters

        # Re-fetch using offset-free windows and merge what we get.
        merged: dict[int, dict[str, Any]] = {
            int(item.get("number") or 0): item for item in chapters
        }
        limit = self.MAX_PAGE_SIZE
        start = 0
        while len(merged) < expected and start < 10_000:
            params: dict[str, Any] = {"limit": limit}
            if start:
                params["offset"] = start
            payload = await self._request(
                f"/api/series/{series_slug}/chapters", params=params
            )
            page = payload.get("data") or []
            if not page:
                break
            for item in page:
                merged[int(item.get("number") or 0)] = item
            start += limit

        return list(merged.values())

    async def list_chapters(self, series_slug: str) -> list[ChapterRef]:
        """
        Fetch the full chapter list for a series.

        The endpoint is paginated with ``limit``/``offset``; we keep
        paging until a short page comes back.

        Workaround for an upstream bug: ``limit=100`` combined with an
        explicit ``offset`` shifts the window by one and silently drops the
        newest chapter.  We therefore use a page size that is not 100 and
        verify the result against the series' own ``chapter_count``,
        repairing the list if a chapter is still missing.
        """
        limit = self.MAX_PAGE_SIZE
        chunks: list[list[dict[str, Any]]] = []
        offset = 0

        while True:
            payload = await self._request(
                f"/api/series/{series_slug}/chapters",
                params={"limit": limit, "offset": offset},
            )
            page = payload.get("data") or []
            chunks.append(page)
            if len(page) < limit:
                break
            offset += limit
            if offset > 10_000:  # paranoia - never loop forever
                break

        # PEP 798 - flatten the paginated payloads into a single list.
        raw_chapters = [*chunk for chunk in chunks]

        raw_chapters = await self._repair_missing_chapters(
            series_slug, raw_chapters
        )

        # Tuple unpacking inside the comprehension maps the raw dicts onto
        # the dataclass in one pass.
        return [
            ChapterRef(
                number=number,
                slug=chapter_slug,
                page_count=page_count,
                is_premium=is_premium,
                series_slug=series_slug,
                published_at=published_at,
                early_access_until=early_access_until,
            )
            for (
                number,
                chapter_slug,
                page_count,
                is_premium,
                published_at,
                early_access_until,
            ) in (
                (
                    int(item.get("number") or 0),
                    str(item.get("slug") or ""),
                    int(item.get("page_count") or 0),
                    bool(item.get("is_premium")),
                    item.get("published_at"),
                    item.get("early_access_until"),
                )
                for item in raw_chapters
            )
        ]

    async def get_chapter(
        self,
        series_slug: str,
        chapter_slug: str | int,
        *,
        fallback: ChapterRef | None = None,
    ) -> ChapterExtraction:
        """
        Fetch the page image URLs for a single chapter.

        A paywalled or early-access chapter is **not** an error: the
        result carries :attr:`ExtractionStatus.SKIPPED_PREMIUM`, an empty
        ``pages`` tuple and a human-readable ``reason``.  The download
        queue logs the warning and carries on with the next chapter.

        Args:
            series_slug: the series the chapter belongs to.
            chapter_slug: the chapter slug (``chapter-25``) or its number.
            fallback: metadata used to populate the result when the API
                omits it.

        Returns:
            A :class:`ChapterExtraction`.

        Raises:
            ChapterNotFoundError: if the chapter does not exist.
        """
        try:
            payload = await self._request(
                f"/api/series/{series_slug}/chapters/{chapter_slug}"
            )
        except _NotFoundSignal as exc:
            raise ChapterNotFoundError(
                f"Chapter {chapter_slug!r} of {series_slug!r} not found."
            ) from exc

        data = payload.get("data") or {}
        chapter_payload = data.get("chapter") or {}

        number = int(chapter_payload.get("number") or 0)
        resolved_slug = str(chapter_payload.get("slug") or str(chapter_slug))
        is_premium = bool(chapter_payload.get("is_premium"))
        page_count = int(chapter_payload.get("page_count") or 0)
        published_at = chapter_payload.get("published_at")
        early_access_until = chapter_payload.get("early_access_until")

        if fallback is not None:
            number = number or fallback.number
            resolved_slug = resolved_slug or fallback.slug
            page_count = page_count or fallback.page_count
            published_at = published_at or fallback.published_at
            early_access_until = early_access_until or fallback.early_access_until

        reference = ChapterRef(
            number=number,
            slug=resolved_slug,
            page_count=page_count,
            is_premium=is_premium,
            series_slug=series_slug,
            published_at=published_at,
            early_access_until=early_access_until,
        )

        is_locked = bool(data.get("is_locked"))
        unlock_time = data.get("unlock_time")

        # PEP 798 + tuple unpacking: map the raw page dictionaries onto
        # PageImage in a single pass.
        pages = tuple(
            PageImage(url=url, width=width, height=height)
            for url, width, height in (
                (
                    str(page.get("url") or ""),
                    int(page.get("width") or 0),
                    int(page.get("height") or 0),
                )
                for page in (chapter_payload.get("pages") or [])
            )
        )

        if is_locked:
            reason = "Premium/early-access chapter is locked."
            if unlock_time:
                reason = f"Premium/early-access chapter is locked until {unlock_time}."
            return ChapterExtraction(
                chapter=reference,
                status=ExtractionStatus.SKIPPED_PREMIUM,
                pages=(),
                reason=reason,
                unlock_time=unlock_time,
            )

        if not pages:
            return ChapterExtraction(
                chapter=reference,
                status=ExtractionStatus.SKIPPED_NO_PAGES,
                pages=(),
                reason="The API returned no page images for this chapter.",
                unlock_time=unlock_time,
            )

        return ChapterExtraction(
            chapter=reference,
            status=ExtractionStatus.OK,
            pages=pages,
            reason=None,
            unlock_time=unlock_time,
        )

    async def extract_chapters(
        self,
        series_slug: str,
        chapters: list[ChapterRef],
        *,
        concurrency: int | None = None,
    ) -> list[ChapterExtraction]:
        """
        Extract several chapters concurrently.

        Locked chapters come back as skipped results, never as raised
        exceptions, so one paywalled chapter cannot abort the batch.
        """
        limit = concurrency or CONCURRENCY["scrape"]
        gate = asyncio.Semaphore(limit)

        async def pull(reference: ChapterRef) -> ChapterExtraction:
            async with gate:
                try:
                    return await self.get_chapter(
                        series_slug, reference.slug, fallback=reference
                    )
                except ChapterNotFoundError:
                    return ChapterExtraction(
                        chapter=reference,
                        status=ExtractionStatus.NOT_FOUND,
                        reason="Chapter not found upstream.",
                    )
                except ExtractionError as exc:
                    return ChapterExtraction(
                        chapter=reference,
                        status=ExtractionStatus.ERROR,
                        reason=str(exc),
                    )

        return list(await asyncio.gather(*(pull(ref) for ref in chapters)))


class _NotFoundSignal(Exception):
    """Internal marker for an HTTP 404, translated per-endpoint."""


# ============================================================
# SYNC CONVENIENCE
# ============================================================
def run_sync(coro: Any) -> Any:
    """Run *coro* to completion on a fresh event loop."""
    return asyncio.run(coro)
