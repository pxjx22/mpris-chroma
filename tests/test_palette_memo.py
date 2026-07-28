import unittest
from pathlib import Path

from mpris_chroma.colors import PaletteMemo, render_palette


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


DEFAULT_ACCENT = "#a48ec7"


def _counting_select(calls, boom):
    """A select fake that records every call and can be armed to raise.

    Shared by both exception tests: they differ in what they assert after the
    failure, not in how the failure is produced.
    """
    def select(p):
        calls.append(p)
        if boom["on"]:
            raise MemoryError("decode blew up")
        return ([(0.5, 0.1, 1.0)], len(calls))
    return select


class PaletteMemoFailureTest(unittest.TestCase):
    def test_an_unextractable_cover_is_cached_as_the_default(self):
        # Caching the failure is deliberate: it turns the "no extractable
        # colors" warning from once-per-flip into once-per-cover. Safe because
        # a rewritten file changes content_id and misses.
        calls = []
        memo = _memo(select=lambda p: calls.append(p) or ([], 0),
                     render=render_palette)
        cid = (10, 100)
        self.assertEqual(memo(Path("/c/bad.jpg"), "dark", cid),
                         (DEFAULT_ACCENT,) * 3)
        self.assertEqual(memo(Path("/c/bad.jpg"), "light", cid),
                         (DEFAULT_ACCENT,) * 3)
        self.assertEqual(len(calls), 1)


class PaletteMemoExceptionSafetyTest(unittest.TestCase):
    def test_a_raising_select_does_not_poison_the_slot(self):
        # The failure mode this memo most needs guarding: if the key were
        # assigned before the value, a select that raises would leave the slot
        # claiming the NEW key while holding the OLD picks, and the retry below
        # would be served the previous cover's palette without re-selecting.
        calls, boom = [], {"on": False}
        memo = _memo(select=_counting_select(calls, boom),
                     render=lambda picks, n, mode, label="?": (str(n),) * 3)
        memo(Path("/a.jpg"), "dark", (10, 100))           # slot holds A
        boom["on"] = True
        with self.assertRaises(MemoryError):
            memo(Path("/b.jpg"), "dark", (20, 200))       # raises mid-update
        boom["on"] = False
        memo(Path("/b.jpg"), "dark", (20, 200))           # must RE-select B

        # Three calls: A, the failed B, the successful B. With key-before-value
        # ordering this is 2 -- the last call hits a slot that wrongly claims
        # (20, 200) and returns A's palette.
        self.assertEqual(len(calls), 3)

    def test_the_slot_still_serves_the_old_cover_after_a_failure(self):
        calls, boom = [], {"on": False}
        memo = _memo(select=_counting_select(calls, boom),
                     render=lambda picks, n, mode, label="?": (str(n),) * 3)
        first = memo(Path("/a.jpg"), "dark", (10, 100))
        boom["on"] = True
        with self.assertRaises(MemoryError):
            memo(Path("/b.jpg"), "dark", (20, 200))
        # A's key was never overwritten, so this is still a hit: 2 calls, not 3.
        self.assertEqual(memo(Path("/a.jpg"), "light", (10, 100)), first)
        self.assertEqual(len(calls), 2)
