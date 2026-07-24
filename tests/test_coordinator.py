import unittest
from pathlib import Path

from mpris_chroma.coordinator import (RETRY_BASE_MS, RETRY_CAP_MS,
                                      RETRY_MAX_ATTEMPTS, Coordinator,
                                      backoff_delay_ms, mode_from_color_scheme)
from mpris_chroma.worker import (COMMITTED, FAILED_RETRYABLE, REJECTED,
                                 CoverTarget, Desired, Result)

_JF_DIR = Path("/covers/jf")


def _covers_dir_for(name):
    return _JF_DIR if name == "jellyfin-tui" else None


def _line(name, status, art=""):
    return f"{name}\t{status}\t{art}\n"


class _Harness:
    def __init__(self, mode="dark"):
        self.submitted = []
        self.scheduled = []   # (delay_ms, fn) per armed retry timer
        self.cancelled = []   # handles passed to cancel
        self._next_handle = 0
        self.coord = Coordinator(
            submit=self.submitted.append,
            covers_dir_for=_covers_dir_for,
            mode=mode,
            schedule=self._schedule,
            cancel=self.cancelled.append,
            jitter=lambda: 1.0,   # deterministic in tests
        )

    def _schedule(self, delay_ms, fn):
        self._next_handle += 1
        self.scheduled.append((delay_ms, fn))
        return self._next_handle

    def fire_last_timer(self):
        """Invoke the most recently armed retry callback (as GLib would)."""
        self.scheduled[-1][1]()

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

    # The 4b "failed resets dedup so the next identical line retries" contract is
    # redesigned in 4c (design §4.1): recovery no longer depends on unrelated
    # MPRIS events — the backoff timer owns resubmission and identical lines
    # dedup. See RetryTimerTest below for the replacement contract.

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


class BackoffDelayTest(unittest.TestCase):
    """Pure delay math: capped exponential with an injected jitter multiplier."""

    def test_first_attempt_is_base_delay(self):
        self.assertEqual(backoff_delay_ms(1, lambda: 1.0), RETRY_BASE_MS)

    def test_delay_doubles_per_attempt(self):
        self.assertEqual(backoff_delay_ms(2, lambda: 1.0), 2 * RETRY_BASE_MS)
        self.assertEqual(backoff_delay_ms(3, lambda: 1.0), 4 * RETRY_BASE_MS)

    def test_delay_is_capped(self):
        self.assertEqual(backoff_delay_ms(30, lambda: 1.0), RETRY_CAP_MS)

    def test_jitter_multiplies(self):
        self.assertEqual(backoff_delay_ms(1, lambda: 1.5),
                         int(1.5 * RETRY_BASE_MS))


class RetryTimerTest(unittest.TestCase):
    """Design §4/§4.1: a failed_retryable result arms a coordinator-owned
    backoff timer that owns resubmission; identical lines dedup (the redefined
    SEC-007 parity); both staleness guards make a stale retry structurally
    unable to overwrite a newer track."""

    def _fail_apply(self, h, art="https://x/a"):
        h.coord.on_line(_line("spotify", "Playing", art))
        gen = h.last[0]
        h.coord.adopt(Result(gen, FAILED_RETRYABLE, None))
        return gen

    def test_retryable_failure_arms_timer_and_identical_line_dedups(self):
        # The §4.1 redefinition: the timer owns the retry; a repeat of the same
        # line no longer resubmits.
        h = _Harness()
        self._fail_apply(h)
        self.assertEqual(len(h.scheduled), 1)
        self.assertEqual(h.scheduled[0][0], RETRY_BASE_MS)
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        self.assertEqual(len(h.submitted), 1)  # deduped; timer will retry

    def test_timer_fire_force_resubmits_with_fresh_gen(self):
        h = _Harness()
        gen = self._fail_apply(h)
        h.fire_last_timer()
        self.assertEqual(len(h.submitted), 2)  # force-resubmit despite dedup key
        self.assertEqual(h.last[0], gen + 1)   # fresh gen: result is adoptable
        self.assertEqual(h.last[1], h.submitted[0][1])  # same desire

    def test_backoff_doubles_across_consecutive_failures(self):
        h = _Harness()
        gen = self._fail_apply(h)                       # attempt 1: base
        h.fire_last_timer()
        h.coord.adopt(Result(gen + 1, FAILED_RETRYABLE, None))  # attempt 2
        self.assertEqual([d for d, _ in h.scheduled],
                         [RETRY_BASE_MS, 2 * RETRY_BASE_MS])

    def test_attempt_cap_exhausts_no_further_timer(self):
        h = _Harness()
        gen = self._fail_apply(h)                       # attempt 1 armed
        for i in range(1, RETRY_MAX_ATTEMPTS + 1):      # fail every retry too
            h.fire_last_timer()
            h.coord.adopt(Result(gen + i, FAILED_RETRYABLE, None))
        # attempts 2..MAX were armed; the failure past the cap armed nothing.
        self.assertEqual(len(h.scheduled), RETRY_MAX_ATTEMPTS)

    def test_gen_bump_cancels_armed_timer(self):
        # Guard 1: any new desired value supersedes an outstanding retry.
        h = _Harness()
        self._fail_apply(h, art="https://x/a")
        armed_handle = h._next_handle
        h.coord.on_line(_line("spotify", "Playing", "https://x/b"))
        self.assertEqual(h.cancelled, [armed_handle])

    def test_late_fire_after_desire_changed_is_dropped(self):
        # Guard 2 (fire-time): even if the callback still runs after the desire
        # moved on, it must not submit the stale target.
        h = _Harness()
        self._fail_apply(h, art="https://x/a")
        stale_fire = h.scheduled[0][1]
        h.coord.on_line(_line("spotify", "Playing", "https://x/b"))
        before = len(h.submitted)
        stale_fire()  # simulates the race where cancel lost to the dispatch
        self.assertEqual(len(h.submitted), before)  # old track cannot overwrite

    def test_rejected_arms_no_timer_and_identical_line_dedups(self):
        # A policy rejection is terminal without a metadata change: no timer,
        # and repeats of the same line do not spin.
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.adopt(Result(h.last[0], REJECTED, None))
        self.assertEqual(h.scheduled, [])
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        self.assertEqual(len(h.submitted), 1)

    def test_new_desire_restarts_backoff_from_base(self):
        h = _Harness()
        gen = self._fail_apply(h, art="https://x/a")
        h.fire_last_timer()
        h.coord.adopt(Result(gen + 1, FAILED_RETRYABLE, None))  # A at 2x now
        h.coord.on_line(_line("spotify", "Playing", "https://x/b"))
        h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        self.assertEqual(h.scheduled[-1][0], RETRY_BASE_MS)  # B starts fresh

    def test_shutdown_cancels_armed_timer_and_late_fire_is_noop(self):
        h = _Harness()
        self._fail_apply(h)
        armed_handle = h._next_handle
        fire = h.scheduled[0][1]
        h.coord.begin_shutdown()
        self.assertIn(armed_handle, h.cancelled)
        before = len(h.submitted)
        fire()  # late dispatch after shutdown
        self.assertEqual(len(h.submitted), before)


def _commit(h, cover_id="/cache/x"):
    """Adopt a committed result for the most recent submission."""
    h.coord.adopt(Result(h.last[0], COMMITTED, cover_id))


class RankingTest(unittest.TestCase):
    """Design §5 (revised): state-based eligibility, RETRYING stays selected,
    adopt re-runs selection, retone anchors on the applied cover. These are the
    three review defects plus the restored cover-aware ranking, as behavior."""

    def test_rejected_newest_falls_back_to_older_ready_without_new_event(self):
        # Defect 3: the fallback happens inside adopt, not on the next event.
        h = _Harness()
        h.coord.on_line(_line("jellyfin-tui", "Playing", "https://x/j"))
        _commit(h, "/cache/j")                        # older player READY+applied
        h.coord.on_line(_line("spotify", "Playing", "https://x/s"))  # newer wins
        h.coord.adopt(Result(h.last[0], REJECTED, None))
        gen, desired = h.last
        self.assertEqual(desired.target, CoverTarget("https://x/j", _JF_DIR))

    def test_own_event_of_applied_player_does_not_flap_to_other_ready(self):
        # Defect 1: an event from the applied newest player must dedup, not hand
        # the palette to the other ready player.
        h = _Harness()
        h.coord.on_line(_line("jellyfin-tui", "Playing", "https://x/j"))
        _commit(h, "/cache/j")
        h.coord.on_line(_line("spotify", "Playing", "https://x/s"))
        _commit(h, "/cache/s")                        # spotify newest, applied
        before = len(h.submitted)
        h.coord.on_line(_line("spotify", "Playing", "https://x/s"))  # repeat
        self.assertEqual(len(h.submitted), before)    # deduped, no flap

    def test_retrying_newest_stays_selected_and_timer_survives(self):
        # Defect 2: the RETRYING winner is not skipped, so its own retry timer
        # is not gen-bump-cancelled by a fallback submission.
        h = _Harness()
        h.coord.on_line(_line("jellyfin-tui", "Playing", "https://x/j"))
        _commit(h, "/cache/j")
        h.coord.on_line(_line("spotify", "Playing", "https://x/s"))
        before = len(h.submitted)
        h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        self.assertEqual(len(h.submitted), before)    # no fallback submission
        self.assertEqual(h.cancelled, [])             # timer survives
        self.assertEqual(len(h.scheduled), 1)

    def test_exhausted_newest_falls_back_to_older_ready(self):
        h = _Harness()
        h.coord.on_line(_line("jellyfin-tui", "Playing", "https://x/j"))
        _commit(h, "/cache/j")
        h.coord.on_line(_line("spotify", "Playing", "https://x/s"))
        gen = h.last[0]
        h.coord.adopt(Result(gen, FAILED_RETRYABLE, None))     # attempt 1
        for i in range(1, RETRY_MAX_ATTEMPTS + 1):
            h.fire_last_timer()
            h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        # The exhaustion transition itself falls back to the older ready cover.
        self.assertEqual(h.last[1].target, CoverTarget("https://x/j", _JF_DIR))

    def test_transient_failure_retries_and_eventually_applies(self):
        # Ledger required test #1, end to end at the coordinator level.
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        h.fire_last_timer()
        h.coord.adopt(Result(h.last[0], COMMITTED, "/cache/a"))
        self.assertEqual(h.coord.applied, "/cache/a")

    def test_retone_anchors_on_applied_cover_not_retrying_winner(self):
        # §5: a theme flip re-tones what is SHOWING even while a newer player is
        # mid-retry — the exact 4b gap this closes.
        h = _Harness(mode="dark")
        h.coord.on_line(_line("jellyfin-tui", "Playing", "https://x/j"))
        _commit(h, "/cache/j")                        # jellyfin applied
        h.coord.on_line(_line("spotify", "Playing", "https://x/s"))
        h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))  # spotify retrying
        h.coord.on_scheme(2)                          # flip to light
        gen, desired = h.last
        self.assertEqual(desired,
                         Desired(CoverTarget("https://x/j", _JF_DIR), "light"))

    def test_retone_commit_restarts_the_superseded_retry_chain(self):
        # The retone's gen bump cancels the retrying winner's timer and demotes
        # it to PENDING; the retone commit then re-selects it fresh in new mode.
        h = _Harness(mode="dark")
        h.coord.on_line(_line("jellyfin-tui", "Playing", "https://x/j"))
        _commit(h, "/cache/j")
        h.coord.on_line(_line("spotify", "Playing", "https://x/s"))
        h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        h.coord.on_scheme(2)                          # retone jellyfin; timer dies
        self.assertEqual(len(h.cancelled), 1)         # spotify's timer cancelled
        _commit(h, "/cache/j")                        # retone lands
        gen, desired = h.last
        self.assertEqual(desired,
                         Desired(CoverTarget("https://x/s", None), "light"))

    def test_exhausted_dir_player_recovers_on_its_next_line(self):
        # A dir-scan player's art identity never changes, so exhaustion resets on
        # the player's next MPRIS event (the reviewed recovery boundary) — it
        # must not strand forever.
        h = _Harness()
        h.coord.on_line(_line("jellyfin-tui", "Playing", ""))
        h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        for i in range(1, RETRY_MAX_ATTEMPTS + 1):
            h.fire_last_timer()
            h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        before = len(h.submitted)                     # exhausted; no timer left
        h.coord.on_line(_line("jellyfin-tui", "Playing", ""))  # next track
        self.assertEqual(len(h.submitted), before + 1)  # re-attempted

    def test_exhausted_url_player_recovers_on_its_next_line(self):
        # F1 (review): a url player's identity — and hence its desired value —
        # is unchanged on its next line, so the EXHAUSTED->PENDING reset must
        # also clear the dedup key or value-dedup absorbs the resubmit and the
        # player strands (recovered network, same track, stale palette).
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))
        h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        for i in range(1, RETRY_MAX_ATTEMPTS + 1):
            h.fire_last_timer()
            h.coord.adopt(Result(h.last[0], FAILED_RETRYABLE, None))
        before = len(h.submitted)                     # exhausted; no timer left
        h.coord.on_line(_line("spotify", "Playing", "https://x/a"))  # same track
        self.assertEqual(len(h.submitted), before + 1)   # re-attempted
        self.assertGreater(h.last[0], h.submitted[-2][0])  # fresh gen, not the
        #                                     dir-style same-gen resubmit path

    def test_rejected_player_recovers_on_identity_change(self):
        # Policy rejection is terminal for THAT metadata; new artwork unlocks it.
        h = _Harness()
        h.coord.on_line(_line("spotify", "Playing", "https://x/bad"))
        h.coord.adopt(Result(h.last[0], REJECTED, None))
        h.coord.on_line(_line("spotify", "Playing", "https://x/good"))
        self.assertEqual(h.last[1].target, CoverTarget("https://x/good", None))


if __name__ == "__main__":
    unittest.main()
