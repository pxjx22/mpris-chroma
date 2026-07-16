import hashlib
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, unquote

CACHE_DIR = Path.home() / ".cache/mpris-chroma/covers"
DOWNLOAD_TIMEOUT = 5  # seconds


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".img")


def _fetch(url: str) -> bytes:
    """Fetch raw bytes for a URL. Patched out in tests; may raise on failure."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
        return resp.read()


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
        except Exception:
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
