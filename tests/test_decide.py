import unittest
from pathlib import Path

from mpris_chroma.select import decide
from mpris_chroma.state import PlayerState
from mpris_chroma.sync import _follow_cmd
from mpris_chroma.worker import CoverTarget, Desired

_JF_DIR = Path("/covers/jf")


def _covers_dir_for(name):
    """Only jellyfin-tui has a local cover directory (as in production)."""
    return _JF_DIR if name == "jellyfin-tui" else None


class FollowCmdTest(unittest.TestCase):
    def test_watches_both_players_with_names(self):
        cmd = _follow_cmd()
        joined = " ".join(cmd)
        self.assertIn("metadata", cmd)          # required subcommand
        self.assertIn("--follow", cmd)
        self.assertIn("-a", cmd)                # all whitelisted players
        self.assertIn("jellyfin-tui,spotify", joined)
        self.assertIn("{{playerName}}", joined)

    def test_metadata_precedes_format(self):
        cmd = _follow_cmd()
        self.assertLess(cmd.index("metadata"), cmd.index("--format"))


class DecideTest(unittest.TestCase):
    """decide() picks the desired end-state from unresolved player state plus a
    per-player eligibility predicate (fed by the 4c cover-state machine), and
    attributes the winning player so adopt() can transition its state. Returns
    (Desired, winner_name) for an apply, (Desired(None), None) for a revert, and
    None for hold. The 4c eligibility fallback restores select()'s cover-aware
    ranking: an ineligible (rejected/exhausted) or source-less newest player
    falls back to the newest older eligible one."""

    def d(self, players, mode="dark", ineligible=()):
        return decide(players, mode, _covers_dir_for,
                      lambda name: name not in ineligible)

    def test_single_playing_with_art_applies_that_cover(self):
        players = {"spotify": PlayerState("Playing", "https://x/a", 1)}
        self.assertEqual(self.d(players),
                         (Desired(CoverTarget("https://x/a", None), "dark"),
                          "spotify"))

    def test_two_playing_most_recent_seq_wins(self):
        players = {
            "spotify": PlayerState("Playing", "https://x/s", 2),
            "jellyfin-tui": PlayerState("Playing", "https://x/j", 1),
        }
        self.assertEqual(self.d(players),
                         (Desired(CoverTarget("https://x/s", None), "dark"),
                          "spotify"))

    def test_paused_and_playing_switches_to_playing(self):
        players = {
            "spotify": PlayerState("Paused", "https://x/s", 3),
            "jellyfin-tui": PlayerState("Playing", "https://x/j", 2),
        }
        self.assertEqual(self.d(players),
                         (Desired(CoverTarget("https://x/j", _JF_DIR), "dark"),
                          "jellyfin-tui"))

    def test_all_paused_reverts(self):
        players = {
            "spotify": PlayerState("Paused", "https://x/s", 2),
            "jellyfin-tui": PlayerState("Paused", "https://x/j", 1),
        }
        self.assertEqual(self.d(players), (Desired(None, "dark"), None))

    def test_all_stopped_reverts(self):
        players = {"spotify": PlayerState("Stopped", "", 4)}
        self.assertEqual(self.d(players), (Desired(None, "dark"), None))

    def test_empty_reverts(self):
        self.assertEqual(self.d({}), (Desired(None, "dark"), None))

    def test_playing_without_art_source_holds(self):
        # spotify has no covers_dir and no art_url -> no source -> hold (None).
        players = {"spotify": PlayerState("Playing", "", 5)}
        self.assertIsNone(self.d(players))

    def test_playing_jellyfin_empty_art_still_resolves_via_dir(self):
        # jellyfin-tui empty art but a covers_dir IS configured -> dir-scan
        # source, so it is an apply target, not a hold (design §4).
        players = {"jellyfin-tui": PlayerState("Playing", "", 5)}
        self.assertEqual(self.d(players),
                         (Desired(CoverTarget("", _JF_DIR), "dark"),
                          "jellyfin-tui"))

    def test_mode_is_carried_into_the_desired(self):
        players = {"spotify": PlayerState("Playing", "https://x/a", 1)}
        self.assertEqual(self.d(players, mode="light"),
                         (Desired(CoverTarget("https://x/a", None), "light"),
                          "spotify"))

    def test_newest_ineligible_falls_back_to_older_eligible(self):
        # The restored ranking (SEC-018): a rejected/exhausted newest player is
        # skipped in favor of the newest older eligible one.
        players = {
            "spotify": PlayerState("Playing", "https://x/s", 2),
            "jellyfin-tui": PlayerState("Playing", "https://x/j", 1),
        }
        self.assertEqual(self.d(players, ineligible={"spotify"}),
                         (Desired(CoverTarget("https://x/j", _JF_DIR), "dark"),
                          "jellyfin-tui"))

    def test_all_playing_ineligible_holds(self):
        # Playing players exist but none can produce a cover -> hold, not revert.
        players = {"spotify": PlayerState("Playing", "https://x/s", 1)}
        self.assertIsNone(self.d(players, ineligible={"spotify"}))

    def test_newest_without_art_source_falls_back_to_older_with_source(self):
        # Original select() semantic restored: a newest Playing player that can
        # never resolve (no art source) does not block an older one that can.
        players = {
            "spotify": PlayerState("Playing", "", 3),        # no source
            "jellyfin-tui": PlayerState("Playing", "https://x/j", 2),
        }
        self.assertEqual(self.d(players),
                         (Desired(CoverTarget("https://x/j", _JF_DIR), "dark"),
                          "jellyfin-tui"))


if __name__ == "__main__":
    unittest.main()
