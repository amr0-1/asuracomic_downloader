# core/downloader.py
"""
Pure-async download engine and in-memory compilation pipeline.

Design
------
* **One shared :class:`aiohttp.ClientSession`** with a tuned
  :class:`aiohttp.TCPConnector` backs every request, so connections are
  pooled and reused instead of being re-established per image.
* **Two independent token buckets** throttle the API and the image CDN
  separately.  They are different origins with different limits, so a
  metadata-heavy run must not throttle image throughput.
* **Nothing touches the SSD on the way to a PDF.**  Page bytes are held in
  memory, handed to Pillow through :class:`io.BytesIO`, and the resulting
  PDF/CBZ is written once at the end.
* **CPU-bound compilation never touches the event loop.**  Pillow work runs
  inside :func:`asyncio.to_thread`, so a slow 40-page chapter does not stall
  in-flight network connections.
* **Skipped chapters never raise.**  A paywalled chapter arrives from the
  extractor with a status; the queue logs the reason and moves on.

Python 3.15 features
--------------------
* **PEP 810** - ``lazy import`` keeps Pillow out of the import path until a
  compilation is actually requested.
* **PEP 814** - configuration is immutable ``frozendict``; headers are
  composed with ``|``.
* **PEP 686** - UTF-8 is the default encoding, so ``open()`` needs no
  ``encoding=`` argument.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

lazy import aiohttp

from core.config import (
    BROWSER_PROFILES,
    CDN_BASE_HEADERS,
    CONCURRENCY,
    RETRY,
    TIMEOUTS,
    TOKEN_BUCKET_API,
    TOKEN_BUCKET_CDN,
    Endpoints,
    build_headers,
    resolve_endpoints,
)
from core.extractor import (
    ChapterExtraction,
    ExtractionStatus,
    PageImage,
)
from utils import converter as _converter

__all__ = [
    "TokenBucket",
    "DownloadStatus",
    "DownloadResult",
    "ChapterDownloader",
    "build_pdf_from_bytes",
    "build_cbz_from_bytes",
    "compile_chapter",
]


# ============================================================
# TOKEN BUCKET
# ============================================================
class TokenBucket:
    """
    A FIFO-fair asynchronous token bucket.

    The bucket holds ``capacity`` tokens and refills at ``rate`` tokens per
    second.  ``acquire()`` blocks (without blocking the event loop) until a
    token is available.

    Fairness matters here: with dozens of concurrent image downloads
    competing for the same bucket, a naive implementation lets a few
    unlucky tasks wait indefinitely while others hog the bucket.  This
    implementation hands out *slots* in arrival order, so no request is
    starved.

    The refill clock is advanced to the slot time when a token is reserved.
    Without that, a reserved-but-not-yet-consumed token would be credited a
    second time and the effective rate would drift above the configured
    limit - which is exactly how a scraper earns a Cloudflare 429.
    """

    __slots__ = ("rate", "capacity", "_credit", "_updated", "_next_slot", "_lock")

    def __init__(self, rate: float, capacity: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._credit = float(capacity)
        self._updated = time.monotonic()
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until *tokens* tokens are available, then consume them."""
        if tokens > self.capacity:
            raise ValueError(
                f"Cannot acquire {tokens} tokens from a bucket of capacity {self.capacity}."
            )
        async with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._updated)
            self._credit = min(self.capacity, self._credit + elapsed * self.rate)
            self._updated = now

            if self._credit >= tokens:
                self._credit -= tokens
                start = now
                if self._next_slot > now:
                    # Somebody is already queued ahead of us.
                    start = self._next_slot
                    self._next_slot = start + tokens / self.rate
            else:
                deficit = tokens - self._credit
                self._credit = 0.0
                start = max(now + deficit / self.rate, self._next_slot)
                self._next_slot = start + tokens / self.rate

            # The token is spent at ``start``; move the refill clock there
            # so it is not credited twice.
            self._updated = max(self._updated, start)

        delay = start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    @property
    def available(self) -> float:
        """Tokens currently available (approximate; ignores queued slots)."""
        elapsed = max(0.0, time.monotonic() - self._updated)
        return min(self.capacity, self._credit + elapsed * self.rate)


# ============================================================
# RESULT TYPES
# ============================================================
class DownloadStatus(StrEnum):
    """Outcome of downloading (and optionally compiling) one chapter."""

    OK = "ok"
    #: The chapter was skipped upstream (premium/locked/no pages).
    SKIPPED = "skipped"
    #: The chapter downloaded but every image failed.
    ERROR = "error"
    #: The chapter was cancelled by the caller.
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """The outcome of processing one chapter."""

    chapter_number: int
    status: DownloadStatus
    pages: int = 0
    output_path: str | None = None
    reason: str | None = None
    duration: float = 0.0
    bytes_downloaded: int = 0

    @property
    def ok(self) -> bool:
        return self.status is DownloadStatus.OK

    @property
    def is_skipped(self) -> bool:
        return self.status is DownloadStatus.SKIPPED


# ============================================================
# IN-MEMORY COMPILATION
# ============================================================
# The actual compilation lives in :mod:`utils.converter`; it is re-exported
# here so callers have a single obvious import site.
build_pdf_from_bytes = _converter.build_pdf_from_bytes
build_cbz_from_bytes = _converter.build_cbz_from_bytes


async def compile_chapter(
    pages: Sequence[tuple[bytes, str]],
    output_path: str,
    fmt: str,
) -> str:
    """
    Compile in-memory pages into *output_path* in the requested format.

    The CPU-bound work is dispatched to a worker thread so the event loop
    keeps servicing network connections while a chapter is compiled.
    """
    fmt = (fmt or "").lower()
    if fmt == "pdf":
        return await asyncio.to_thread(build_pdf_from_bytes, pages, output_path)
    if fmt == "cbz":
        return await asyncio.to_thread(build_cbz_from_bytes, pages, output_path)
    raise ValueError(f"Unsupported output format: {fmt!r}")


# ============================================================
# DOWNLOADER
# ============================================================
@dataclass(slots=True)
class _PagePayload:
    """One downloaded page, still in memory."""

    index: int
    data: bytes
    extension: str


class ChapterDownloader:
    """
    Downloads chapter pages and compiles them, fully asynchronously.

    The downloader owns a single :class:`aiohttp.ClientSession`; pass one in
    if you want to share the connection pool with the extractor.

    Example::

        async with ChapterDownloader() as dl:
            result = await dl.download_chapter(extraction, out_dir, fmt="pdf")
            if result.is_skipped:
                print(result.reason)
    """

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        endpoints: Endpoints | None = None,
        cdn_limiter: TokenBucket | None = None,
        api_limiter: TokenBucket | None = None,
        max_concurrency: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._endpoints = endpoints
        self._cdn_limiter = cdn_limiter or TokenBucket(
            TOKEN_BUCKET_CDN["rate"], TOKEN_BUCKET_CDN["capacity"]
        )
        self._api_limiter = api_limiter or TokenBucket(
            TOKEN_BUCKET_API["rate"], TOKEN_BUCKET_API["capacity"]
        )
        self._max_concurrency = max_concurrency or CONCURRENCY["download"]
        self._timeout = timeout if timeout is not None else TIMEOUTS["total"]
        self._semaphore: asyncio.Semaphore | None = None
        self._profile_index = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> ChapterDownloader:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Open the session (if we own it) and the concurrency gate."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        if self._session is None:
            connector = aiohttp.TCPConnector(
                limit=self._max_concurrency,
                limit_per_host=self._max_concurrency,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
                force_close=False,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers=self._next_headers(),
            )
            self._owns_session = True

    async def close(self) -> None:
        """Close the session if this downloader owns it."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def cdn_limiter(self) -> TokenBucket:
        """The token bucket governing CDN traffic."""
        return self._cdn_limiter

    @property
    def api_limiter(self) -> TokenBucket:
        """The token bucket governing API traffic."""
        return self._api_limiter

    async def ensure_endpoints(self) -> Endpoints:
        """Resolve the working hosts on demand."""
        if self._endpoints is None:
            self._endpoints = await resolve_endpoints()
        return self._endpoints

    # ------------------------------------------------------------------
    # Headers
    # ------------------------------------------------------------------
    def _next_headers(self) -> frozendict:
        """
        Build the header set for the next CDN request.

        The fingerprint rotates on every call so a burst of downloads does
        not arrive with one identical ``User-Agent``/``Sec-CH-UA`` pair.
        """
        profile = BROWSER_PROFILES[self._profile_index % len(BROWSER_PROFILES)]
        self._profile_index += 1
        headers = build_headers(profile=profile, base=CDN_BASE_HEADERS)
        return headers

    # ------------------------------------------------------------------
    # Single image
    # ------------------------------------------------------------------
    async def _fetch_page(
        self, page: PageImage, index: int
    ) -> tuple[_PagePayload | None, str | None]:
        """
        Download one page into memory.

        Returns:
            ``(payload, error)``.  Exactly one of the two is ``None``; on
            failure *error* carries a short human-readable reason.
        """
        endpoints = await self.ensure_endpoints()
        attempts = RETRY["max_attempts"]
        status_forcelist = RETRY["status_forcelist"]

        last_error: str | None = None
        for attempt in range(1, attempts + 1):
            # Throttle just below the rate limit.
            await self._cdn_limiter.acquire()
            headers = self._next_headers() | {"Referer": f"{endpoints.web_base}/"}
            try:
                async with self._session.get(
                    page.url, headers=headers, allow_redirects=True
                ) as response:
                    if response.status in status_forcelist:
                        retry_after = response.headers.get("Retry-After")
                        await response.read()
                        last_error = f"HTTP {response.status}"
                        if attempt < attempts:
                            await asyncio.sleep(self._backoff(attempt, retry_after))
                            continue
                        return None, last_error
                    if response.status >= 400:
                        await response.read()
                        return None, f"HTTP {response.status}"
                    data = await response.read()
                    if not data:
                        return None, "Empty response body."
                    return (
                        _PagePayload(index=index, data=data, extension=page.extension),
                        None,
                    )
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts:
                    await asyncio.sleep(self._backoff(attempt, None))
                    continue
                return None, last_error
        return None, last_error or "Unknown error."

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        """
        Compute the delay before the next attempt.

        A ``Retry-After`` header always wins: the origin knows better than
        we do how long Cloudflare wants us to wait.
        """
        if retry_after:
            try:
                return min(float(retry_after), RETRY["max_delay"])
            except ValueError:
                pass
        return min(RETRY["base_delay"] * (2 ** (attempt - 1)), RETRY["max_delay"])

    # ------------------------------------------------------------------
    # Chapter
    # ------------------------------------------------------------------
    async def download_chapter(
        self,
        extraction: ChapterExtraction,
        output_dir: str,
        *,
        fmt: str = "none",
        save_images: bool = False,
        stop_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> DownloadResult:
        """
        Download one chapter and optionally compile it.

        A chapter that was skipped upstream (premium/locked) is **not** an
        error: the reason is logged and the result comes back with
        :attr:`DownloadStatus.SKIPPED` so the caller can carry on with the
        rest of the batch.

        Args:
            extraction: the chapter metadata and page URLs.
            output_dir: directory to write the output into.
            fmt: ``"pdf"``, ``"cbz"`` or ``"none"`` to only keep the images.
            save_images: also write the raw page files to disk.
            stop_check: callable returning True to abort early.
            progress_callback: called with ``(completed, total)``.

        Returns:
            A :class:`DownloadResult`.
        """
        started = time.monotonic()
        chapter = extraction.chapter

        # --- Skip-with-reason: never raise, just report. ---
        if extraction.is_skipped:
            reason = extraction.reason or "Chapter was skipped upstream."
            return DownloadResult(
                chapter_number=chapter.number,
                status=DownloadStatus.SKIPPED,
                reason=reason,
                duration=time.monotonic() - started,
            )

        if extraction.status is not ExtractionStatus.OK or not extraction.pages:
            return DownloadResult(
                chapter_number=chapter.number,
                status=DownloadStatus.ERROR,
                reason=extraction.reason or "No pages available for this chapter.",
                duration=time.monotonic() - started,
            )

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)

        total = len(extraction.pages)
        completed = 0
        payloads: list[_PagePayload | None] = [None] * total
        errors: list[str] = []

        async def pull(index: int, page: PageImage) -> None:
            nonlocal completed
            if stop_check and stop_check():
                return
            async with self._semaphore:
                if stop_check and stop_check():
                    return
                payload, error = await self._fetch_page(page, index)
            if payload is None:
                if error:
                    errors.append(f"page {index + 1}: {error}")
            else:
                payloads[index] = payload
            completed += 1
            if progress_callback:
                progress_callback(completed, total)

        await asyncio.gather(
            *(pull(index, page) for index, page in enumerate(extraction.pages))
        )

        if stop_check and stop_check():
            return DownloadResult(
                chapter_number=chapter.number,
                status=DownloadStatus.CANCELLED,
                pages=completed,
                reason="Cancelled by the caller.",
                duration=time.monotonic() - started,
            )

        # Keep only the pages that actually arrived, in reading order.
        downloaded = [payload for payload in payloads if payload is not None]
        if not downloaded:
            detail = "; ".join(errors[:3]) if errors else "no details available"
            return DownloadResult(
                chapter_number=chapter.number,
                status=DownloadStatus.ERROR,
                reason=f"Every page failed to download ({detail}).",
                duration=time.monotonic() - started,
            )

        total_bytes = sum(len(payload.data) for payload in downloaded)
        pairs = [(payload.data, payload.extension) for payload in downloaded]

        os.makedirs(output_dir, exist_ok=True)
        output_path: str | None = None

        fmt = (fmt or "none").lower()
        if fmt in {"pdf", "cbz"}:
            output_path = os.path.join(output_dir, f"{chapter.slug}.{fmt}")
            try:
                await compile_chapter(pairs, output_path, fmt)
            except Exception as exc:
                return DownloadResult(
                    chapter_number=chapter.number,
                    status=DownloadStatus.ERROR,
                    pages=len(downloaded),
                    reason=f"Compilation to {fmt.upper()} failed: {exc}",
                    duration=time.monotonic() - started,
                    bytes_downloaded=total_bytes,
                )

        if save_images or fmt == "none":
            images_dir = os.path.join(output_dir, "images")
            os.makedirs(images_dir, exist_ok=True)
            for payload in downloaded:
                target = os.path.join(
                    images_dir, f"{payload.index + 1:03d}{payload.extension}"
                )
                with open(target, "wb") as handle:
                    handle.write(payload.data)

        notes: list[str] = []
        if len(downloaded) != total:
            notes.append(f"{len(downloaded)}/{total} pages downloaded.")
        if errors:
            notes.append("; ".join(errors[:3]))

        return DownloadResult(
            chapter_number=chapter.number,
            status=DownloadStatus.OK,
            pages=len(downloaded),
            output_path=output_path,
            reason=" ".join(notes) if notes else None,
            duration=time.monotonic() - started,
            bytes_downloaded=total_bytes,
        )

    async def download_chapters(
        self,
        extractions: Iterable[ChapterExtraction],
        output_dir: str,
        *,
        fmt: str = "none",
        save_images: bool = False,
        stop_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        chapter_callback: Callable[[DownloadResult], None] | None = None,
    ) -> list[DownloadResult]:
        """
        Download several chapters, one after another.

        Chapters are processed sequentially because the compilation step is
        CPU- and memory-heavy; only the *page* downloads within a chapter
        run concurrently.  A skipped chapter is reported and the batch
        continues.
        """
        results: list[DownloadResult] = []
        # Materialise once: a generator would be exhausted by a len() call.
        items = list(extractions)
        total = len(items)
        for index, extraction in enumerate(items, start=1):
            if stop_check and stop_check():
                break
            result = await self.download_chapter(
                extraction,
                output_dir,
                fmt=fmt,
                save_images=save_images,
                stop_check=stop_check,
            )
            results.append(result)
            if chapter_callback:
                chapter_callback(result)
            if progress_callback:
                progress_callback(index, total)
        return results
