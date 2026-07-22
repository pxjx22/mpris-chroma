import hashlib
import ipaddress
import logging
import socket
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, unquote

# HTTPS artwork providers permitted for remote covers. The allowlist is the
# primary SSRF control; address validation below is a DNS-rebinding backstop.
ALLOWED_HOSTS = frozenset({"i.scdn.co", "mosaic.scdn.co"})

# Library-style logger: a NullHandler keeps it silent unless the application
# configures logging (sync.main does), and lets tests assert on it directly.
_log = logging.getLogger("mpris_chroma.cover")
_log.addHandler(logging.NullHandler())
_LOG_INTERVAL = 60.0  # seconds; at most one warning per key per interval
_last_logged: dict[str, float] = {}

CACHE_DIR = Path.home() / ".cache/mpris-chroma/covers"
DOWNLOAD_TIMEOUT = 5       # per-socket-operation timeout (seconds)
DOWNLOAD_DEADLINE = 20.0   # total-transfer deadline (seconds), independent of the socket timeout
MAX_COVER_BYTES = 10 * 1024 * 1024  # hard cap on compressed artwork (10 MiB)
_CHUNK = 64 * 1024


class CoverError(Exception):
    """A cover could not be fetched for an expected, contained reason."""


class CoverTooLarge(CoverError):
    """Response exceeds MAX_COVER_BYTES (declared or actual)."""


class CoverTimeout(CoverError):
    """The total-transfer deadline elapsed before the body finished."""


class CoverRejected(CoverError):
    """A destination or local path was refused by policy (SSRF / confinement)."""


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".img")


def _log_failure(key: str, message: str, *, now=time.monotonic) -> None:
    """Warn about a cover failure at most once per _LOG_INTERVAL per key, so a
    repeatedly-failing source cannot flood the journal."""
    t = now()
    if t - _last_logged.get(key, float("-inf")) >= _LOG_INTERVAL:
        _last_logged[key] = t
        _log.warning(message)


def _resolve_host(host: str) -> list[str]:
    """Resolve all A/AAAA addresses for host (network; injected in tests)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _is_global_address(addr: str) -> bool:
    """True only for a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False
    return ip.is_global


def _check_destination(url: str, *, resolve=None) -> None:
    """Raise CoverRejected unless url is https to an allowlisted host on a
    default port, without userinfo, resolving only to global addresses.

    The allowlist is the primary control; the address check is a DNS-rebinding
    backstop. Pure given the injected resolver, so it is unit-testable without
    a network."""
    if resolve is None:
        resolve = _resolve_host
    parts = urlparse(url)
    if parts.scheme != "https":
        raise CoverRejected(f"non-https scheme {parts.scheme!r}")
    if parts.username or parts.password:
        raise CoverRejected("url userinfo not permitted")
    if parts.port not in (None, 443):
        raise CoverRejected(f"port {parts.port} not permitted")
    host = parts.hostname
    if host is None or host not in ALLOWED_HOSTS:
        raise CoverRejected(f"host {host!r} not allowlisted")
    addrs = resolve(host)
    if not addrs:
        raise CoverRejected(f"no addresses for {host}")
    for addr in addrs:
        if not _is_global_address(addr):
            raise CoverRejected(f"non-global address {addr} for {host}")


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Revalidate every redirect hop against the destination policy so a public
    URL cannot bounce to an internal target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_destination(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Default opener: urllib caps redirects at 10 and each hop is revalidated.
_OPENER = urllib.request.build_opener(_ValidatingRedirectHandler())


def _fetch(url: str, *, opener=_OPENER.open, now=time.monotonic) -> bytes:
    """Fetch raw bytes for a URL, bounded by MAX_COVER_BYTES and DOWNLOAD_DEADLINE.

    The urlopen timeout bounds a single socket operation, not the whole
    transfer, so a server can drip bytes indefinitely while staying under it.
    Enforce a monotonic total deadline and a hard byte cap, streamed in chunks
    so peak memory is bounded by the cap rather than by the response size.
    Raises CoverTooLarge / CoverTimeout; opener/now are injectable for tests.
    """
    deadline = now() + DOWNLOAD_DEADLINE
    with opener(url, timeout=DOWNLOAD_TIMEOUT) as resp:
        declared = resp.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > MAX_COVER_BYTES:
                    raise CoverTooLarge(f"declared {declared} bytes exceeds cap")
            except ValueError:
                pass  # malformed header; the actual-byte check below still applies
        chunks: list[bytes] = []
        total = 0
        while True:
            if now() > deadline:
                raise CoverTimeout("total transfer deadline exceeded")
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_COVER_BYTES:
                raise CoverTooLarge(f"body exceeded {MAX_COVER_BYTES} byte cap")
            chunks.append(chunk)
    return b"".join(chunks)


def resolve_cover(art_url: str, covers_dir: Path | None = None) -> Path | None:
    """Resolve the current album cover to a local image path.

    - file://  -> the local path if it exists.
    - http(s):// -> a cached download (fetched once per URL, then reused).
    - otherwise, if covers_dir is given, the newest regular file in it.
    Returns None on any failure; never raises.
    """
    if art_url.startswith("file://"):
        p = Path(unquote(urlparse(art_url).path))
        if p.is_file():
            return p
    elif art_url.startswith(("http://", "https://")):
        dest = _cache_path(art_url)
        if dest.is_file():
            return dest
        try:
            _check_destination(art_url)
            data = _fetch(art_url)
            if not data:
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except (OSError, CoverError) as exc:
            # Expected operational failures (network via URLError/HTTPError/
            # socket timeout, filesystem, or a bounded-download rejection) are
            # contained. Unexpected exceptions (MemoryError, programming bugs)
            # deliberately propagate to the supervisor. Log only the hostname so
            # URL userinfo/query secrets never reach the journal.
            host = urlparse(art_url).hostname or "?"
            _log_failure(host, f"cover fetch failed for {host}: {type(exc).__name__}")
            return None
        return dest

    if covers_dir is not None:
        try:
            files = [f for f in covers_dir.iterdir() if f.is_file()]
        except (FileNotFoundError, NotADirectoryError):
            return None
        if files:
            return max(files, key=lambda f: f.stat().st_mtime)
    return None
