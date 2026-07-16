import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mpris_chroma import cover
from mpris_chroma.cover import resolve_cover


class ResolveCoverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_file_url_that_exists_is_used(self):
        img = self.tmp / "art.jpg"
        img.write_bytes(b"x")
        covers = self.tmp / "covers"
        covers.mkdir()
        self.assertEqual(resolve_cover(f"file://{img}", covers), img)

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
        # Redirect the cache into a temp dir so tests never touch ~/.cache.
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self._orig = cover.CACHE_DIR
        cover.CACHE_DIR = self.cache

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
