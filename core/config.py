# core/config.py
"""
Immutable configuration, browser-fingerprint rotation and boot-time domain
resolution for the Asura Scans downloader.

Python 3.15 features in play here
---------------------------------
* **PEP 810 - explicit lazy imports.** ``lazy import aiohttp`` keeps the HTTP
  stack out of the CLI's import path until something actually needs the
  network.  The Typer CLI therefore boots without paying for aiohttp.
* **PEP 814 - the builtin ``frozendict``.** Every shared mapping below is
  immutable and hashable, so the same object can be handed to an unbounded
  number of asyncio tasks without any risk of cross-task mutation.  Dynamic
  headers are layered on with the ``|`` operator, which keeps the result a
  ``frozendict`` too.
* **PEP 686 - UTF-8 is the default encoding.** The endpoint cache is written
  with a bare ``open()`` call and no ``encoding=`` argument.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# PEP 810 - explicit lazy imports.  isort does not understand the ``lazy``
# soft keyword, so these are kept in their own block.
# isort: off
lazy import asyncio
lazy import aiohttp
# isort: on


def _supports_brotli() -> bool:
    """
    Whether the optional ``brotli`` codec is importable.

    ``aiohttp`` only advertises ``br`` when it can actually decode the
    response, otherwise it raises ``ClientResponseError`` on the body.  The
    Asura CDN happily sends brotli, so we probe once and only announce
    ``br`` when the codec is present.
    """
    try:
        import brotli  # noqa: F401
    except ImportError:
        return False
    return True


#: ``Accept-Encoding`` value negotiated against the locally installed codecs.
ACCEPT_ENCODING: Final[str] = (
    "gzip, deflate, br" if _supports_brotli() else "gzip, deflate"
)

__all__ = [
    "APP_NAME",
    "BASE_HEADERS",
    "CDN_BASE_HEADERS",
    "BROWSER_PROFILES",
    "CONCURRENCY",
    "TIMEOUTS",
    "RETRY",
    "TOKEN_BUCKET",
    "ENDPOINT_CACHE_TTL",
    "Endpoints",
    "DomainResolver",
    "build_headers",
    "resolve_endpoints",
    "resolve_endpoints_sync",
    "get_resolver",
]


# ============================================================
# IDENTITY
# ============================================================
APP_NAME: Final[str] = "asura-scans-downloader"

#: Where the resolved-endpoint cache lives.  ``~`` is expanded by pathlib.
CACHE_DIR: Final[Path] = Path.home() / ".cache" / APP_NAME
ENDPOINT_CACHE_FILE: Final[Path] = CACHE_DIR / "endpoints.json"

#: How long a resolved domain stays fresh before we re-probe the network.
ENDPOINT_CACHE_TTL: Final[float] = 6 * 3600.0  # 6 hours


# ============================================================
# HEADERS (PEP 814 - frozendict)
# ============================================================
#: Rotating browser fingerprints.  ``User-Agent`` and ``Sec-CH-UA`` are kept
#: together in one profile so a request never mixes a Chrome 140 UA with a
#: Chrome 131 client hint - that mismatch is exactly what Cloudflare's
#: bot score looks for.
BROWSER_PROFILES: Final[tuple[frozendict, ...]] = (
    frozendict(
        {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "sec_ch_ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        }
    ),
    frozendict(
        {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
            "sec_ch_ua": '"Chromium";v="141", "Not=A?Brand";v="24", "Google Chrome";v="141"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        }
    ),
    frozendict(
        {
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "sec_ch_ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"macOS"',
        }
    ),
    frozendict(
        {
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
            ),
            "sec_ch_ua": '"Chromium";v="139", "Not=A?Brand";v="24", "Google Chrome";v="139"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Linux"',
        }
    ),
    frozendict(
        {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0"
            ),
            "sec_ch_ua": '"Chromium";v="138", "Not=A?Brand";v="24", "Microsoft Edge";v="138"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        }
    ),
)


#: Headers that are identical for every request to the API.  Deliberately
#: free of ``Origin``/``Referer`` because those depend on the resolved
#: domain and are merged in later via the ``|`` operator.
BASE_HEADERS: Final[frozendict] = frozendict(
    {
        # NOTE: a User-Agent is mandatory.  Cloudflare challenges (HTTP 403
        # "Just a moment...") any request that still carries aiohttp's
        # default ``Python/3.15 aiohttp/x.y`` agent.  ``build_headers``
        # overwrites this with the rotated profile.
        "User-Agent": BROWSER_PROFILES[0]["user_agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": ACCEPT_ENCODING,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=0, i",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
    }
)

#: Headers used for image pulls from the CDN.  The CDN is a different
#: origin from the API, so it gets its own fingerprint-friendly set.
CDN_BASE_HEADERS: Final[frozendict] = frozendict(
    {
        "User-Agent": BROWSER_PROFILES[0]["user_agent"],
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": ACCEPT_ENCODING,
        "Priority": "u=1, i",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-site",
        "Connection": "keep-alive",
    }
)


# ============================================================
# CONCURRENCY / TIMEOUTS / RETRIES (frozendict)
# ============================================================
CONCURRENCY: Final[frozendict] = frozendict(
    {
        # Concurrent requests against the JSON API (chapter lists, search).
        "api": 8,
        # Concurrent image downloads from the CDN.
        "download": 12,
        # Concurrent chapter-metadata lookups during a batch run.
        "scrape": 8,
        # TCP connections kept alive in the aiohttp pool.
        "pool": 24,
        # Chapters compiled to PDF/CBZ at the same time.
        "compile": 2,
    }
)

TIMEOUTS: Final[frozendict] = frozendict(
    {
        "connect": 10.0,
        "total": 45.0,
        "socket_read": 30.0,
    }
)

RETRY: Final[frozendict] = frozendict(
    {
        "max_attempts": 4,
        "base_delay": 0.6,
        "max_delay": 8.0,
        "status_forcelist": (429, 500, 502, 503, 504),
        # Seconds to wait when the origin explicitly asks us to slow down.
        "respect_retry_after": True,
    }
)

#: Token bucket settings.  ``rate`` is the sustained request rate in req/s,
#: ``capacity`` is the burst size (the bucket depth).
#:
#: The API and the image CDN are separate origins with separate limits, so
#: they get **separate buckets** - a metadata-heavy run must not throttle
#: image throughput, and vice versa.
TOKEN_BUCKET_API: Final[frozendict] = frozendict(
    {
        "rate": 8.0,
        "capacity": 12.0,
    }
)

TOKEN_BUCKET_CDN: Final[frozendict] = frozendict(
    {
        "rate": 20.0,
        "capacity": 30.0,
    }
)

#: Backwards-compatible alias for the CDN bucket.
TOKEN_BUCKET: Final[frozendict] = TOKEN_BUCKET_CDN


# ============================================================
# DYNAMIC DOMAIN RESOLUTION
# ============================================================
#: Front-end candidates, most-recently-known-good first.  Asura rotates
#: these TLDs fairly often; the resolver probes them in parallel and keeps
#: whichever one actually serves the site.
WEB_DOMAIN_CANDIDATES: Final[tuple[str, ...]] = (
    "asurascans.com",
    "asuracomic.net",
    "asuracomic.com",
    "asurscans.com",
    "asura-scans.com",
    "asurascans.net",
)

#: Fallback API hosts, used when the front-end page does not advertise one
#: (for example when the site is serving a static/error page).
API_DOMAIN_CANDIDATES: Final[tuple[str, ...]] = (
    "api.asurascans.com",
    "api.asuracomic.net",
    "api.asuracomic.com",
)

#: Pulls ``https://api.<something>`` out of the rendered front-end page.
#: The Astro build preconnects to the API host, which makes this a reliable
#: signal even when the TLD itself has been swapped out.
_API_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"https://api\.[a-z0-9.-]+(?::\d+)?", re.IGNORECASE
)

#: A page that merely mentions "asura" is not proof of life - we also want
#: to see the API preconnect or a chapter/image CDN reference.
_LIVENESS_MARKERS: Final[tuple[str, ...]] = (
    "asurascans",
    "asura-images",
    "api.asura",
)

#: How many bytes of the homepage to read while probing for the API hint.
_PROBE_READ_LIMIT: Final[int] = 128 * 1024


@dataclass(frozen=True, slots=True)
class Endpoints:
    """The set of hosts the scraper should talk to."""

    api_base: str
    web_base: str
    cdn_base: str
    resolved_at: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_base": self.api_base,
            "web_base": self.web_base,
            "cdn_base": self.cdn_base,
            "resolved_at": self.resolved_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Endpoints:
        return cls(
            api_base=raw["api_base"],
            web_base=raw["web_base"],
            cdn_base=raw["cdn_base"],
            resolved_at=float(raw.get("resolved_at", 0.0)),
            source=raw.get("source", "cache"),
        )

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.resolved_at) > ENDPOINT_CACHE_TTL


def _origin_of(url: str) -> str:
    """Return ``scheme://host`` for *url* (no trailing slash)."""
    match = re.match(r"^(https?://[^/]+)", url, re.IGNORECASE)
    return match.group(1) if match else url.rstrip("/")


class DomainResolver:
    """
    Boot-time resolver for the currently-working Asura domain.

    Asura Scans rotates its TLD fairly aggressively, so nothing in the code
    base may hard-code a host name.  The resolver:

    1. Probes the front-end candidates concurrently and follows redirects to
       learn the canonical web origin.
    2. Extracts the API host the front-end advertises (the Astro build
       preconnects to it), falling back to a static candidate list.
    3. Verifies the API host answers ``GET /health`` with a healthy payload.
    4. Caches the result on disk (PEP 686 - UTF-8 by default) for
       ``ENDPOINT_CACHE_TTL`` seconds.
    """

    def __init__(
        self,
        *,
        web_candidates: tuple[str, ...] = WEB_DOMAIN_CANDIDATES,
        api_candidates: tuple[str, ...] = API_DOMAIN_CANDIDATES,
        cache_file: Path | str = ENDPOINT_CACHE_FILE,
        ttl: float = ENDPOINT_CACHE_TTL,
        timeout: float = 8.0,
    ) -> None:
        self._web_candidates = web_candidates
        self._api_candidates = api_candidates
        self._cache_file = Path(cache_file)
        self._ttl = ttl
        self._timeout = timeout
        self._cached: Endpoints | None = None

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------
    def load_cache(self) -> Endpoints | None:
        """Return the cached endpoints if present and still fresh."""
        if self._cached is not None and not self._cached.is_stale:
            return self._cached
        try:
            with open(self._cache_file) as fh:  # PEP 686: UTF-8 by default
                cached = Endpoints.from_dict(json.load(fh))
        except (OSError, ValueError, KeyError):
            return None
        if (time.time() - cached.resolved_at) > self._ttl:
            return None
        self._cached = cached
        return cached

    def save_cache(self, endpoints: Endpoints) -> None:
        """Persist *endpoints* to the on-disk cache."""
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_file, "w") as fh:  # PEP 686: UTF-8 by default
                json.dump(endpoints.to_dict(), fh, indent=2)
        except OSError:
            pass
        self._cached = endpoints

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------
    async def _probe_web(self, session: aiohttp.ClientSession, domain: str) -> tuple[str, str] | None:
        """Return ``(origin, discovered_api_host)`` if *domain* serves Asura."""
        url = f"https://{domain}/"
        try:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return None
                # The preconnect hint lives in <head>, so a bounded read is
                # plenty - the full homepage is ~780 kB and we do not want
                # to pull that down for every candidate.
                raw = await resp.content.read(_PROBE_READ_LIMIT)
                final = str(resp.url)
        except Exception:
            return None

        body = raw.decode("utf-8", "replace")
        lowered = body.lower()
        if not any(marker in lowered for marker in _LIVENESS_MARKERS):
            return None

        api_match = _API_HOST_PATTERN.search(body)
        discovered = api_match.group(0).rstrip("/") if api_match else ""
        return _origin_of(final), discovered

    async def _probe_api(self, session: aiohttp.ClientSession, api_base: str) -> bool:
        """Check whether *api_base* answers ``/health``."""
        try:
            async with session.get(f"{api_base}/health") as resp:
                if resp.status != 200:
                    return False
                payload = await resp.json(content_type=None)
        except Exception:
            return False
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).lower()
            return status in {"healthy", "ok", "up"}
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def resolve(self, *, force: bool = False) -> Endpoints:
        """Resolve the working API/web/CDN hosts."""
        if not force:
            cached = self.load_cache()
            if cached is not None:
                return cached

        timeout = aiohttp.ClientTimeout(total=self._timeout, connect=self._timeout)
        connector = aiohttp.TCPConnector(limit=len(self._web_candidates))
        # A User-Agent is mandatory, otherwise Cloudflare answers with a
        # 403 "Just a moment..." interstitial instead of the JSON.
        headers = BASE_HEADERS | {
            "User-Agent": BROWSER_PROFILES[0]["user_agent"],
            "Sec-CH-UA": BROWSER_PROFILES[0]["sec_ch_ua"],
            "Sec-CH-UA-Mobile": BROWSER_PROFILES[0]["sec_ch_ua_mobile"],
            "Sec-CH-UA-Platform": BROWSER_PROFILES[0]["sec_ch_ua_platform"],
        }

        discovered_api_hosts: list[str] = []
        web_origin = ""

        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, headers=headers
        ) as session:
            # Probe the web front-end and the static API candidates
            # concurrently - several Asura TLDs are dead and would otherwise
            # serialise into one timeout each.
            web_results, api_results = await asyncio.gather(
                asyncio.gather(
                    *(self._probe_web(session, d) for d in self._web_candidates),
                    return_exceptions=True,
                ),
                asyncio.gather(
                    *(self._probe_api(session, self._normalise_api(c))
                      for c in self._api_candidates),
                    return_exceptions=True,
                ),
            )

            for result in web_results:
                if isinstance(result, tuple) and result[1]:
                    discovered_api_hosts.append(result[1])
                if not web_origin and isinstance(result, tuple) and result[0]:
                    web_origin = result[0]

            # Prefer the host the site itself advertises, then fall back to
            # the static candidate list.
            api_hosts: list[str] = []
            api_hosts.extend(self._normalise_api(h) for h in discovered_api_hosts)
            api_hosts.extend(self._normalise_api(c) for c in self._api_candidates)

            healthy_static = {
                self._normalise_api(c)
                for c, ok in zip(self._api_candidates, api_results, strict=False)
                if ok is True
            }

            api_base = ""
            for candidate in api_hosts:
                if candidate in healthy_static:
                    api_base = candidate
                    break

            # Nothing from the static list worked - try the advertised hosts
            # one by one, they may point at a brand-new TLD.
            if not api_base:
                for candidate in api_hosts:
                    if candidate in healthy_static:
                        continue
                    if await self._probe_api(session, candidate):
                        api_base = candidate
                        break

        if not api_base:
            raise RuntimeError(
                "Could not resolve a working Asura API host. "
                f"Tried: {', '.join(api_hosts) or '(none)'}"
            )

        if not web_origin:
            web_origin = f"https://{self._web_candidates[0]}"

        cdn_base = self._derive_cdn_base(api_base)
        endpoints = Endpoints(
            api_base=api_base,
            web_base=web_origin,
            cdn_base=cdn_base,
            resolved_at=time.time(),
            source="probe" if discovered_api_hosts else "fallback",
        )
        self.save_cache(endpoints)
        return endpoints

    @staticmethod
    def _normalise_api(candidate: str) -> str:
        """Normalise an API host into a full ``https://host`` origin."""
        base = candidate.rstrip("/")
        if not base.startswith("http"):
            base = f"https://{base}"
        return base

    @staticmethod
    def _derive_cdn_base(api_base: str) -> str:
        """The image CDN mirrors the API host with a ``cdn.`` prefix."""
        host = _origin_of(api_base).split("://", 1)[-1]
        if host.startswith("api."):
            host = host[4:]
        return f"https://cdn.{host}"

    def invalidate(self) -> None:
        """Drop the in-memory + on-disk cache (used after a domain failure)."""
        self._cached = None
        try:
            self._cache_file.unlink(missing_ok=True)
        except OSError:
            pass


# ============================================================
# HEADER BUILDING
# ============================================================
def build_headers(
    *,
    profile: frozendict | None = None,
    referer: str | None = None,
    origin: str | None = None,
    accept: str | None = None,
    base: frozendict = BASE_HEADERS,
    extra: dict[str, str] | None = None,
) -> frozendict:
    """
    Compose the final header set for a request.

    Every layer is merged with ``|`` so the result stays an immutable
    ``frozendict`` and can be shared safely between concurrent tasks.
    """
    headers = base
    if profile is not None:
        headers = headers | {
            "User-Agent": profile["user_agent"],
            "Sec-CH-UA": profile["sec_ch_ua"],
            "Sec-CH-UA-Mobile": profile["sec_ch_ua_mobile"],
            "Sec-CH-UA-Platform": profile["sec_ch_ua_platform"],
        }
    if referer:
        headers = headers | {"Referer": referer}
    if origin:
        headers = headers | {"Origin": origin}
    if accept:
        headers = headers | {"Accept": accept}
    if extra:
        headers = headers | extra
    return headers


# ============================================================
# MODULE-LEVEL SINGLETON
# ============================================================
_resolver: DomainResolver | None = None


def get_resolver() -> DomainResolver:
    """Return the process-wide :class:`DomainResolver`."""
    global _resolver
    if _resolver is None:
        _resolver = DomainResolver()
    return _resolver


async def resolve_endpoints(*, force: bool = False) -> Endpoints:
    """Resolve the working hosts, using the on-disk cache when possible."""
    return await get_resolver().resolve(force=force)


def resolve_endpoints_sync(*, force: bool = False) -> Endpoints:
    """Blocking wrapper around :func:`resolve_endpoints`."""
    return asyncio.run(resolve_endpoints(force=force))


# ============================================================
# DEPRECATED ALIASES - kept so the old modules keep importing during the
# staged refactor.  They will be removed once scraper/downloader are
# rewritten.
# ============================================================
MAX_WORKERS: Final[int] = CONCURRENCY["download"]
MAX_DOWNLOAD_WORKERS: Final[int] = CONCURRENCY["download"]
MAX_SCRAPE_WORKERS: Final[int] = CONCURRENCY["scrape"]
CHUNK_SIZE: Final[int] = 65536
HTTP_TIMEOUT: Final[int] = int(TIMEOUTS["total"])
CONNECTION_POOL_SIZE: Final[int] = CONCURRENCY["pool"]
MAX_RETRIES: Final[int] = RETRY["max_attempts"]
RETRY_BACKOFF: Final[float] = RETRY["base_delay"]
USE_ASYNC_DOWNLOAD: Final[bool] = True
