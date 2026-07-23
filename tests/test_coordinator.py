import unittest
from pathlib import Path

from mpris_chroma.coordinator import Coordinator, mode_from_color_scheme
from mpris_chroma.worker import (COMMITTED, FAILED_RETRYABLE, CoverTarget,
                                 Desired, Result)

_JF_DIR = Path("/covers/jf")


def _covers_dir_for(name):
    return _JF_DIR if name == "jellyfin-tui" else None


def _line(name, status, art=""):
    return f"{name}\t{status}\t{art}\n"


class _Harness:
    def __init__(self, mode="dark"):
        self.submitted = []
        self.coord = Coordinator(
            submit=self.submitted.append,
            covers_dir_for=_covers_dir_for,
            mode=mode,
        )

    @property
    def last(self):
        return self.submitted[-1]


class ColorSchemeMapTest(unittest.TestCase):
    # Portal values: 0 = no preference, 1 = prefer dark, 2 = prefer light.
    def test_prefer_dark_maps_to_dark(self):
        self.assertEqual(mode_from_color_scheme(1), "dark")

    def test_prefer_light_maps_to_light(self):
        self.assertEqual(mode_from_color_scheme(2), "light")

    def test_no_preference_defaults_to_dark(self):
        self.assertEqual(mode_from_color_scheme(0), "dark")

    def test_unknown_value_defaults_to_dark(self):
        self.assertEqual(mode_from_color_scheme(7), "dark")


class OnLineTest(unittest.TestCase):
    def test_playing_with_art_submits_an_apply(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        self.assertEqual(len(h.submitted), 1)
        gen, desired = h.last
        self.assertEqual(gen, 1)
        self.assertEqual(desired, Desired(CoverTarget("https://x/a", None), "dark"))

    def test_malformed_line_is_ignored(self):
        h = _Harness()
        h.coord.on_line("garbage\n")
        self.assertEqual(h.submitted, [])

    def test_repeated_identical_line_is_deduped(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        self.assertEqual(len(h.submitted), 1)  # value-dedup: one job

    def test_newer_cover_supersedes_and_bumps_gen(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.on_line(_line("spotify", "Playing", "https://x/b"))
        self.assertEqual([g for g, _ in h.submitted], [1, 2])

    def test_stop_submits_a_revert(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.on_line(_line("spotify", "Stopped"))
        _gen, desired = h.last
        self.assertEqual(desired, Desired(None, "dark"))

    def test_playing_without_art_source_holds_no_submit(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", ""))
        self.assertEqual(h.submitted, [])

    def test_dir_scan_player_always_resubmits_reusing_gen(self):
        # jellyfin-tui empty art -> dir-scan identity is unstable, so a repeat
        # line resubmits (content may have changed) but reuses the gen (design
        # §3): no value change to bump on, so it cannot preempt a running job.
        h = _Harness()
        h.coord.on_line(_line("jellyfin-tui", "Playing", ""))
        h.coord.on_line(_line("jellyfin-tui", "Playing", ""))
        self.assertEqual(len(h.submitted), 2)
        self.assertEqual([g for g, _ in h.submitted], [1, 1])  # gen reused


class OnVanishTest(unittest.TestCase):
    def test_last_player_vanishing_submits_revert_and_evicts(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.on_vanish("org.mpris.MediaPlayer2.spotify")
        self.assertNotIn("spotify", h.coord.players)
        self.assertEqual(h.last[1], Desired(None, "dark"))

    def test_non_player_bus_name_is_ignored(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        before = len(h.submitted)
        h.coord.on_vanish("org.freedesktop.Notifications")
        self.assertEqual(len(h.submitted), before)
        self.assertIn("spotify", h.coord.players)

    def test_unknown_player_vanishing_is_noop(self):
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        before = len(h.submitted)
        h.coord.on_vanish("org.mpris.MediaPlayer2.firefox")
        self.assertEqual(len(h.submitted), before)


class OnSchemeTest(unittest.TestCase):
    def test_flip_retones_current_target_in_new_mode(self):
        h = _Harness(mode="dark")
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.on_scheme(2)  # 2 = prefer light
        _gen, desired = h.last
        self.assertEqual(desired, Desired(CoverTarget("https://x/a", None), "light"))

    def test_same_mode_is_noop(self):
        h = _Harness(mode="dark")
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        before = len(h.submitted)
        h.coord.on_scheme(1)  # 1 = prefer dark == current
        self.assertEqual(len(h.submitted), before)

    def test_flip_with_nothing_applied_updates_mode_only(self):
        h = _Harness(mode="dark")
        h.coord.on_scheme(2)
        self.assertEqual(h.submitted, [])
        self.assertEqual(h.coord.mode, "light")

    def test_flip_after_revert_does_not_retone(self):
        # last desired is a revert (target None): the preset isn't ours to retone.
        h = _Harness(mode="dark")
        h.coord.on_line(_line("spotify", "Stopped"))
        before = len(h.submitted)
        h.coord.on_scheme(2)
        self.assertEqual(len(h.submitted), before)


class AdoptTest(unittest.TestCase):
    def _apply(self, h):
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        return h.last[0]  # gen

    def test_committed_updates_applied_bookkeeping(self):
        h = _Harness()
        gen = self._apply(h)
        h.coord.adopt(Result(gen, COMMITTED, "/cache/a"))
        self.assertEqual(h.coord.applied, "/cache/a")

    def test_failed_resets_dedup_so_next_identical_line_retries(self):
        # Interim (4c unit 2): failed still resets the dedup key so the next
        # identical line resubmits (4b behavior). Unit 3 replaces this with a
        # backoff timer (identical line dedups; the timer retries).
        h = _Harness()
        gen = self._apply(h)
        h.coord.adopt(Result(gen, FAILED_RETRYABLE, None))
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        self.assertEqual(len(h.submitted), 2)  # retried, not deduped

    def test_stale_gen_result_is_dropped(self):
        h = _Harness()
        self._apply(h)                       # gen 1
        h.coord.on_line(_line("spotify", "Playing", "https://x/b"))  # gen 2
        h.coord.adopt(Result(1, COMMITTED, "/cache/a"))  # stale
        self.assertIsNone(h.coord.applied)   # not adopted

    def test_slow_job_does_not_block_a_later_theme_flip(self):
        # A submitted apply whose result has NOT been adopted must not stop a
        # theme flip from being handled and submitted immediately.
        h = _Harness(mode="dark")
        self._apply(h)  # gen 1 in flight, not adopted
        h.coord.on_scheme(2)
        self.assertEqual(h.last[1].mode, "light")


if __name__ == "__main__":
    unittest.main()
