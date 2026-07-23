import hashlib
import ipaddress
import logging
import os
import socket
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, unquote

# Allowlisted HTTPS artwork provider *domains*: a host passes if it equals a
# domain or is a subdomain of one, so Spotify's several CDN subdomains all pass
# without enumeration. The allowlist is the primary SSRF control; the global-
# address check below is a DNS-rebinding backstop. Add a new remote provider via
# MPRIS_CHROMA_ART_DOMAINS (comma-separated) rather than editing code.
_DEFAULT_ART_DOMAINS = frozenset({"scdn.co", "spotifycdn.com"})

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

# Cache-growth budget (SEC-009). Each distinct URL would otherwise leave a
# permanent file; bound the cache by total bytes, entry count, and age, evicting
# least-recently-used first. Generous for a laptop's playback history and far
# below anything that fills a disk.
CACHE_MAX_BYTES = 128 * 1024 * 1024   # total on-disk budget (128 MiB)
CACHE_MAX_ENTRIES = 512               # total entry-count budget
CACHE_MAX_AGE = 30 * 24 * 60 * 60     # per-entry age budget (30 days, seconds)


class CoverError(Exception):
    """A cover could not be fetched for an expected, contained reason."""


class CoverTooLarge(CoverError):
    """Response exceeds MAX_COVER_BYTES (declared or actual)."""


class CoverTimeout(CoverError):
    """The total-transfer deadline elapsed before the body finished."""


class CoverRejected(CoverError):
    """A destination or local path was refused by policy (SSRF / confinement)."""


class CoverAborted(CoverError):
    """An in-flight download was aborted by should_stop (shutdown, SEC-001)."""


# --- Typed resolution outcome (4c, SEC-018) --------------------------------
#
# resolve_cover returns one of these instead of `Path | None`. The classification
# of transient-vs-policy is cover-domain knowledge exported as data, so the worker
# never interprets exceptions and the coordinator's retry logic has its input.
# This *evolves* SEC-015's containment contract (expected failures are values, not
# exceptions) — it does not invert it: MemoryError / programming bugs still
# propagate to the containment net in resolve_cover.


@dataclass(frozen=True, slots=True)
class Ready:
    """The cover resolved to a local file. content_id = (st_size, st_mtime_ns) so
    an in-place overwrite (new mtime) reads as a new cover (SEC-018 identity)."""

    path: Path
    content_id: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Retryable:
    """A transient failure (network/timeout/abort, or a cover write lagging the
    metadata line). The coordinator retries with capped backoff."""

    reason: str


@dataclass(frozen=True, slots=True)
class Rejected:
    """A deterministic policy/content refusal (SSRF, confinement, over-size,
    non-image). Not retried without a metadata change."""

    reason: str


Resolution = Ready | Retryable | Rejected


def _content_id(path: Path) -> tuple[int, int]:
    """Stat identity of a resolved cover: (size, mtime-ns). Uniform for local and
    remote — the remote cache object is validated pre-publish and atomically
    replaced (SEC-009), so its stat is a sound identity too."""
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


def _classify_error(exc: BaseException) -> Resolution:
    """Map a contained resolution error to Retryable/Rejected. Policy/content
    refusals that the same metadata will reproduce are Rejected; transient
    failures are Retryable."""
    if isinstance(exc, (CoverRejected, CoverTooLarge)):
        return Rejected(type(exc).__name__)
    return Retryable(type(exc).__name__)


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


def _art_domains() -> frozenset[str]:
    """Allowlisted provider domains: built-in defaults plus any comma-separated
    entries from MPRIS_CHROMA_ART_DOMAINS, so a new provider needs no code
    change."""
    extra = os.environ.get("MPRIS_CHROMA_ART_DOMAINS", "")
    added = {d.strip().lower() for d in extra.split(",") if d.strip()}
    return _DEFAULT_ART_DOMAINS | added


def _host_allowed(host: str, domains) -> bool:
    """True if host equals an allowlisted domain or is a subdomain of one. The
    required leading dot ('.' + domain) refuses lookalikes like 'evilscdn.co'."""
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


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
    if host is None or not _host_allowed(host, _art_domains()):
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


def _fetch(url: str, *, opener=_OPENER.open, now=time.monotonic,
           should_stop=None) -> bytes:
    """Fetch raw bytes for a URL, bounded by MAX_COVER_BYTES and DOWNLOAD_DEADLINE.

    The urlopen timeout bounds a single socket operation, not the whole
    transfer, so a server can drip bytes indefinitely while staying under it.
    Enforce a monotonic total deadline and a hard byte cap, streamed in chunks
    so peak memory is bounded by the cap rather than by the response size.

    should_stop, when given, is polled once per chunk; if it returns True the
    download is aborted with CoverAborted so a shutdown does not wait out the
    full deadline (SEC-001 §5.1). Raises CoverTooLarge / CoverTimeout /
    CoverAborted; opener/now/should_stop are injectable for tests.
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
            if should_stop is not None and should_stop():
                raise CoverAborted("download aborted by should_stop")
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


def _looks_like_image(data: bytes) -> bool:
    """True only if data begins with a JPEG, PNG, or WebP signature. This is the
    pre-publication content check (SEC-009): non-images never enter the cache.
    It is deliberately the same format set the decoder accepts (SEC-005), but a
    lightweight magic-byte sniff — the decoder still fully revalidates."""
    return (data.startswith(b"\xff\xd8\xff")               # JPEG
            or data.startswith(b"\x89PNG\r\n\x1a\n")        # PNG
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))  # WebP (RIFF container)


def _publish(dest: Path, data: bytes) -> None:
    """Atomically publish data to dest via a private, exclusive temp file in the
    same directory, then os.replace (SEC-009).

    os.replace renames onto the destination *name* atomically, so a reader never
    sees a partial file and a crash mid-write cannot leave a published entry.
    Because rename operates on the name and not a symlink's target, a planted
    symlink at dest is safely replaced in place — its target is never written
    through — rather than permanently poisoning that cache slot. The temp file
    is removed on every failure path."""
    fd, tmpname = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-", suffix=".img")
    tmp = Path(tmpname)  # mkstemp creates it 0600, owned by us, exclusively
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)  # no partial temp survives any failure
        raise


def _evict(cache_dir: Path, *, now=time.time) -> None:
    """Bound cache growth (SEC-009): remove entries past CACHE_MAX_AGE, then, if
    still over the byte or entry-count budget, evict least-recently-used first.
    Best-effort and race-tolerant — a file vanishing underneath us is ignored."""
    entries = []
    try:
        candidates = list(cache_dir.iterdir())
    except OSError:
        return
    for p in candidates:
        if p.name.startswith(".") or p.is_symlink():
            continue  # skip temp files and never follow/evict through symlinks
        try:
            st = p.stat()
        except OSError:
            continue
        if now() - st.st_mtime > CACHE_MAX_AGE:
            p.unlink(missing_ok=True)
        else:
            entries.append((st.st_mtime, st.st_size, p))
    entries.sort()  # oldest (least recently used) first
    total = sum(size for _, size, _ in entries)
    count = len(entries)
    for _, size, p in entries:
        if total <= CACHE_MAX_BYTES and count <= CACHE_MAX_ENTRIES:
            break
        p.unlink(missing_ok=True)
        total -= size
        count -= 1


def _resolve_local_cover(art_url: str, root: Path | None) -> Resolution:
    """Resolve a file:// artwork URL to a regular file confined beneath root.

    Requires an empty or 'localhost' authority and a configured player root; the
    symlink-resolved target must be a regular file at or beneath the resolved
    root. Authority smuggling (file://host/...) and symlink escape are refused
    (SEC-004). Classification (4c): confinement/authority/no-root are deterministic
    policy -> Rejected; a missing/not-yet-written file -> Retryable."""
    parts = urlparse(art_url)
    if parts.netloc not in ("", "localhost"):
        return Rejected("file authority not permitted")
    if root is None:
        return Rejected("no cover root for file://")
    try:
        real_root = root.resolve(strict=True)
    except OSError:
        return Retryable("cover root missing")
    try:
        target = Path(unquote(parts.path)).resolve(strict=True)
    except OSError:
        return Retryable("local cover file missing")  # write may lag the metadata
    if not target.is_file():
        return Retryable("not a regular file")
    if target != real_root and real_root not in target.parents:
        return Rejected("path outside cover root")
    return Ready(target, _content_id(target))


def resolve_cover(art_url: str, covers_dir: Path | None = None, *,
                  should_stop=None) -> Resolution:
    """Resolve the current album cover to a typed Resolution (4c, SEC-018).

    - file://  -> confined local file (Ready) or a typed refusal.
    - http(s):// -> a cached download (fetched once per URL, then reused).
    - otherwise, if covers_dir is given, the newest regular file in it.

    Evolves SEC-015's containment: expected failures become a typed Resolution
    (Ready/Retryable/Rejected) rather than None; unexpected exceptions (MemoryError,
    programming bugs) still propagate. should_stop is forwarded to the download so
    an in-flight fetch aborts at shutdown (contained as Retryable via CoverAborted).
    """
    if art_url.startswith("file://"):
        # file:// is authoritative: a confined hit or a typed refusal. It does not
        # fall through to the covers_dir scan, which would re-admit a symlink the
        # confinement just rejected; that scan remains for non-URL art_urls.
        return _resolve_local_cover(art_url, covers_dir)
    elif art_url.startswith(("http://", "https://")):
        dest = _cache_path(art_url)
        # Serve only a real regular file — never a planted symlink, which
        # is_file() would follow. A symlinked or partial entry is a miss and
        # gets atomically replaced below.
        if dest.is_file() and not dest.is_symlink():
            return Ready(dest, _content_id(dest))
        try:
            _check_destination(art_url)
            data = _fetch(art_url, should_stop=should_stop)
            if not data or not _looks_like_image(data):
                # Empty or non-image body never enters the cache; that URL's
                # content is not artwork, so it is a rejection, not transient.
                return Rejected("empty or non-image body")
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _publish(dest, data)
            _evict(dest.parent)
        except (OSError, CoverError) as exc:
            # Expected operational failures are contained and classified;
            # unexpected exceptions (MemoryError, programming bugs) propagate.
            # Log only the hostname so URL userinfo/query secrets never reach the
            # journal.
            host = urlparse(art_url).hostname or "?"
            _log_failure(host, f"cover fetch failed for {host}: {type(exc).__name__}")
            return _classify_error(exc)
        return Ready(dest, _content_id(dest))

    if covers_dir is not None:
        try:
            files = [f for f in covers_dir.iterdir() if f.is_file()]
        except (FileNotFoundError, NotADirectoryError):
            return Retryable("covers dir missing")  # may appear once the player writes
        if files:
            newest = max(files, key=lambda f: f.stat().st_mtime)
            return Ready(newest, _content_id(newest))
        return Retryable("no cover in dir yet")  # write may lag the metadata line
    return Rejected("no art source")
