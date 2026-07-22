import os
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from mpris_chroma import cover
from mpris_chroma.cover import resolve_cover

_GLOBAL_ADDR = "23.192.228.84"  # a public (globally routable) address


def _resolves_to(*addrs):
    return lambda host: list(addrs)


class ResolveCoverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_file_url_inside_root_is_used(self):
        covers = self.tmp / "covers"
        covers.mkdir()
        img = covers / "art.jpg"
        img.write_bytes(b"x")
        self.assertEqual(resolve_cover(f"file://{img}", covers), img.resolve())

    def test_remote_url_falls_back_to_newest_cover(self):
        covers = self.tmp / "covers"
        covers.mkdir()
        old = covers / "old.jpeg"
        old.write_bytes(b"x")
        new = covers / "new.jpeg"
        new.write_bytes(b"x")
        os.utime(old, (1, 1))
        os.utime(new, (time.time(), time.time()))
        self.assertEqual(
            resolve_cover("jellyfin:Items/abc/Images/Primary", covers), new
        )

    def test_empty_covers_dir_returns_none(self):
        covers = self.tmp / "covers"
        covers.mkdir()
        self.assertIsNone(resolve_cover("", covers))

    def test_missing_covers_dir_returns_none(self):
        self.assertIsNone(resolve_cover("", self.tmp / "nope"))


class HttpCoverTest(unittest.TestCase):
    def setUp(self):
        # Redirect the cache into a temp dir so tests never touch ~/.cache, and
        # mock DNS so destination validation stays offline and deterministic.
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self._orig = cover.CACHE_DIR
        cover.CACHE_DIR = self.cache
        self._dns = mock.patch.object(cover, "_resolve_host",
                                      return_value=[_GLOBAL_ADDR])
        self._dns.start()

    def tearDown(self):
        self._dns.stop()
        cover.CACHE_DIR = self._orig
        self._tmp.cleanup()

    def test_http_cache_miss_downloads_and_writes(self):
        url = "https://i.scdn.co/image/abc123"
        with mock.patch.object(cover, "_fetch", return_value=b"IMGDATA") as f:
            p = resolve_cover(url)
        self.assertIsNotNone(p)
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_bytes(), b"IMGDATA")
        f.assert_called_once_with(url)

    def test_http_cache_hit_does_not_refetch(self):
        url = "https://i.scdn.co/image/abc123"
        with mock.patch.object(cover, "_fetch", return_value=b"IMGDATA"):
            first = resolve_cover(url)
        with mock.patch.object(cover, "_fetch") as f:
            second = resolve_cover(url)
        self.assertEqual(first, second)
        f.assert_not_called()

    def test_http_fetch_failure_returns_none(self):
        url = "https://i.scdn.co/image/broken"
        with mock.patch.object(cover, "_fetch", side_effect=OSError("network")):
            self.assertIsNone(resolve_cover(url))

    def test_http_empty_body_returns_none(self):
        url = "https://i.scdn.co/image/empty"
        with mock.patch.object(cover, "_fetch", return_value=b""):
            self.assertIsNone(resolve_cover(url))

    def test_covers_dir_defaults_to_none(self):
        # No art, no covers_dir -> None, no crash.
        self.assertIsNone(resolve_cover(""))

    def test_http_write_bytes_failure_returns_none(self):
        url = "https://i.scdn.co/image/write-fails"
        with mock.patch.object(cover, "_fetch", return_value=b"IMG"), \
             mock.patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")):
            self.assertIsNone(resolve_cover(url))


class FakeResp:
    """Minimal stand-in for a urlopen() response/context manager."""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.read_called = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        self.read_called = True
        return self._chunks.pop(0) if self._chunks else b""


def _opener_for(resp):
    def _open(url, timeout=None):
        return resp
    return _open


class FetchBoundsTest(unittest.TestCase):
    """SEC-002: downloads are bounded by a byte cap and a total-transfer
    deadline, streamed so peak memory does not scale with the response."""

    def test_oversized_declared_length_rejected_before_reading_body(self):
        resp = FakeResp(
            [b"x" * 1024],
            headers={"Content-Length": str(cover.MAX_COVER_BYTES + 1)},
        )
        with self.assertRaises(cover.CoverTooLarge):
            cover._fetch("https://h/x", opener=_opener_for(resp))
        self.assertFalse(resp.read_called)  # rejected before any body read

    def test_oversized_chunked_body_stopped_at_cap(self):
        # No declared length; the cap must be enforced against actual bytes.
        resp = FakeResp([b"aaaa", b"bbbb", b"cccc"])  # 12 bytes
        with mock.patch.object(cover, "MAX_COVER_BYTES", 8):
            with self.assertRaises(cover.CoverTooLarge):
                cover._fetch("https://h/x", opener=_opener_for(resp))

    def test_total_deadline_aborts_slow_drip(self):
        resp = FakeResp([b"a"] * 1000)  # would never end on its own
        clock = iter([0.0, cover.DOWNLOAD_DEADLINE + 1.0])  # start, then past deadline
        with self.assertRaises(cover.CoverTimeout):
            cover._fetch("https://h/x", opener=_opener_for(resp),
                         now=lambda: next(clock))

    def test_normal_sized_image_succeeds(self):
        resp = FakeResp([b"IMG", b"DATA"])
        data = cover._fetch("https://h/x", opener=_opener_for(resp),
                            now=lambda: 0.0)
        self.assertEqual(data, b"IMGDATA")


class ResolveCoverErrorHandlingTest(unittest.TestCase):
    """SEC-015: expected network/disk/validation failures are contained and
    logged; unexpected programming errors propagate instead of masquerading as
    'no cover'; repeated failures are rate-limited so they cannot flood logs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = cover.CACHE_DIR
        cover.CACHE_DIR = Path(self._tmp.name)
        cover._last_logged.clear()
        self._dns = mock.patch.object(cover, "_resolve_host",
                                      return_value=[_GLOBAL_ADDR])
        self._dns.start()

    def tearDown(self):
        self._dns.stop()
        cover.CACHE_DIR = self._orig
        self._tmp.cleanup()

    def test_cover_error_is_contained_as_none(self):
        with mock.patch.object(cover, "_fetch", side_effect=cover.CoverTooLarge("big")):
            self.assertIsNone(resolve_cover("https://i.scdn.co/image/x"))

    def test_programming_error_propagates(self):
        # A TypeError is a bug, not an operational failure; it must reach the
        # supervisor rather than be silently converted to None.
        with mock.patch.object(cover, "_fetch", side_effect=TypeError("bug")):
            with self.assertRaises(TypeError):
                resolve_cover("https://i.scdn.co/image/x")

    def test_memory_error_propagates(self):
        with mock.patch.object(cover, "_fetch", side_effect=MemoryError()):
            with self.assertRaises(MemoryError):
                resolve_cover("https://i.scdn.co/image/x")

    def test_repeated_failures_are_rate_limited_in_logs(self):
        with mock.patch.object(cover, "_fetch", side_effect=OSError("net")), \
             mock.patch.object(cover._log, "warning") as warn:
            resolve_cover("https://i.scdn.co/image/x")
            resolve_cover("https://i.scdn.co/image/x")
        self.assertEqual(warn.call_count, 1)

    def test_failure_log_omits_url_credentials(self):
        with mock.patch.object(cover, "_fetch", side_effect=OSError("net")), \
             mock.patch.object(cover._log, "warning") as warn:
            resolve_cover("https://user:secret@i.scdn.co/image/x?token=abc")
        logged = " ".join(str(a) for a in warn.call_args.args)
        self.assertNotIn("secret", logged)
        self.assertNotIn("token", logged)


class DestinationPolicyTest(unittest.TestCase):
    """SEC-003: _check_destination is the pure allow/deny gate (given an
    injected resolver); only https to an allowlisted host resolving to global
    addresses is permitted, and every redirect hop is revalidated."""

    def _host(self):
        return next(iter(cover.ALLOWED_HOSTS))

    def test_accepts_allowlisted_https_url(self):
        cover._check_destination(f"https://{self._host()}/img/x",
                                 resolve=_resolves_to(_GLOBAL_ADDR))  # no raise

    def test_rejects_non_https_scheme(self):
        with self.assertRaises(cover.CoverRejected):
            cover._check_destination(f"http://{self._host()}/x",
                                     resolve=_resolves_to(_GLOBAL_ADDR))

    def test_rejects_url_userinfo(self):
        with self.assertRaises(cover.CoverRejected):
            cover._check_destination(f"https://user:pw@{self._host()}/x",
                                     resolve=_resolves_to(_GLOBAL_ADDR))

    def test_rejects_nonstandard_port(self):
        with self.assertRaises(cover.CoverRejected):
            cover._check_destination(f"https://{self._host()}:8080/x",
                                     resolve=_resolves_to(_GLOBAL_ADDR))

    def test_rejects_non_allowlisted_host(self):
        with self.assertRaises(cover.CoverRejected):
            cover._check_destination("https://evil.example/x",
                                     resolve=_resolves_to(_GLOBAL_ADDR))

    def test_rejects_non_global_addresses(self):
        for addr in ["127.0.0.1", "::1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
                     "169.254.1.1", "fe80::1", "224.0.0.1", "240.0.0.1",
                     "0.0.0.0", "::"]:
            with self.subTest(addr=addr):
                with self.assertRaises(cover.CoverRejected):
                    cover._check_destination(f"https://{self._host()}/x",
                                             resolve=_resolves_to(addr))

    def test_rejects_when_any_resolved_address_is_non_global(self):
        # A mix must reject: one private answer is enough (rebinding backstop).
        with self.assertRaises(cover.CoverRejected):
            cover._check_destination(f"https://{self._host()}/x",
                                     resolve=_resolves_to(_GLOBAL_ADDR, "127.0.0.1"))

    def test_redirect_to_loopback_is_rejected(self):
        handler = cover._ValidatingRedirectHandler()
        req = urllib.request.Request(f"https://{self._host()}/a")
        with self.assertRaises(cover.CoverRejected):
            handler.redirect_request(req, None, 302, "Found", {},
                                     "https://127.0.0.1/evil")


class LocalCoverConfinementTest(unittest.TestCase):
    """SEC-004: file:// is confined beneath a player's configured root, with
    explicit authority handling and symlink-escape refusal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "covers"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_accepts_cover_inside_root(self):
        img = self.root / "art.jpg"
        img.write_bytes(b"x")
        self.assertEqual(resolve_cover(f"file://{img}", self.root), img.resolve())

    def test_accepts_localhost_authority(self):
        img = self.root / "art.jpg"
        img.write_bytes(b"x")
        self.assertEqual(resolve_cover(f"file://localhost{img}", self.root),
                         img.resolve())

    def test_rejects_path_outside_root(self):
        outside = self.base / "secret.txt"
        outside.write_bytes(b"x")
        self.assertIsNone(resolve_cover(f"file://{outside}", self.root))

    def test_rejects_etc_passwd(self):
        self.assertIsNone(resolve_cover("file:///etc/passwd", self.root))

    def test_rejects_remote_authority(self):
        img = self.root / "art.jpg"
        img.write_bytes(b"x")
        self.assertIsNone(resolve_cover(f"file://remote-host{img}", self.root))

    def test_rejects_symlink_escaping_root(self):
        outside = self.base / "secret.txt"
        outside.write_bytes(b"x")
        link = self.root / "link.jpg"
        link.symlink_to(outside)
        self.assertIsNone(resolve_cover(f"file://{link}", self.root))

    def test_rejects_when_no_root_configured(self):
        img = self.root / "art.jpg"
        img.write_bytes(b"x")
        self.assertIsNone(resolve_cover(f"file://{img}", None))


if __name__ == "__main__":
    unittest.main()
