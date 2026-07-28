import unittest
from pathlib import Path

from mpris_chroma.colors import PaletteMemo


def _memo(select=None, render=None):
    """A memo wired with inert fakes. Both halves are injected so these tests
    exercise the slot, not the extraction pipeline (Task 1 covers that)."""
    return PaletteMemo(
        select=select or (lambda p: ([(0.5, 0.1, 1.0)], 1)),
        render=render or (lambda picks, n, mode, label="?": (mode, mode, mode)),
    )


class PaletteMemoHitTest(unittest.TestCase):
    def test_same_content_id_selects_once_across_two_modes(self):
        # The whole point: a theme flip misses the worker's (content_id, mode)
        # dedup but must not re-run selection.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        cid = (10, 100)
        self.assertEqual(memo(Path("/c/a.jpg"), "dark", cid), ("dark",) * 3)
        self.assertEqual(memo(Path("/c/a.jpg"), "light", cid), ("light",) * 3)
        self.assertEqual(len(calls), 1)

    def test_mode_is_applied_after_the_hit_not_stored(self):
        # If mode leaked into the slot, the second call would return "dark".
        memo = _memo()
        cid = (10, 100)
        memo(Path("/c/a.jpg"), "dark", cid)
        self.assertEqual(memo(Path("/c/a.jpg"), "light", cid), ("light",) * 3)


class PaletteMemoMissTest(unittest.TestCase):
    def test_new_content_id_reselects(self):
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        memo(Path("/c/a.jpg"), "dark", (10, 100))
        memo(Path("/c/b.jpg"), "dark", (20, 200))
        self.assertEqual(len(calls), 2)

    def test_same_path_with_new_content_misses(self):
        # SEC-018 identity: an in-place overwrite changes (size, mtime_ns), so
        # the same pathname must NOT serve a stale palette.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        memo(Path("/c/a.jpg"), "dark", (10, 100))
        memo(Path("/c/a.jpg"), "dark", (10, 999))
        self.assertEqual(len(calls), 2)

    def test_identity_is_the_key_not_the_path(self):
        # Documented consequence, not an accident: the key is content identity,
        # exactly as worker.py's dedup treats it. Two paths that stat identical
        # are the same cover to this memo. Inherited from SEC-018, not new.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([(0.5, 0.1, 1.0)], 1))
        memo(Path("/c/a.jpg"), "dark", (10, 100))
        memo(Path("/c/b.jpg"), "dark", (10, 100))
        self.assertEqual(len(calls), 1)
