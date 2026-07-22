import hashlib
import logging
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, unquote

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


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".img")


def _log_failure(key: str, message: str, *, now=time.monotonic) -> None:
    """Warn about a cover failure at most once per _LOG_INTERVAL per key, so a
    repeatedly-failing source cannot flood the journal."""
    t = now()
    if t - _last_logged.get(key, float("-inf")) >= _LOG_INTERVAL:
        _last_logged[key] = t
        _log.warning(message)


def _fetch(url: str, *, opener=urllib.request.urlopen, now=time.monotonic) -> bytes:
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
