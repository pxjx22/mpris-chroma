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
    """decide() replaces select(): it picks a desired end-state from unresolved
    player state (status + art_url), since resolution now happens off-thread.
    The cover-aware ranking select() had ('older Playing-with-cover outranks a
    newer Playing-without') is intentionally dropped here and deferred to 4c —
    see SECURITY_AUDIT.md SEC-018."""

    def d(self, players, mode="dark"):
        return decide(players, mode, _covers_dir_for)

    def test_single_playing_with_art_applies_that_cover(self):
        players = {"spotify": PlayerState("Playing", "https://x/a", 1)}
        self.assertEqual(self.d(players),
                         Desired(CoverTarget("https://x/a", None), "dark"))

    def test_two_playing_most_recent_seq_wins(self):
        players = {
            "spotify": PlayerState("Playing", "https://x/s", 2),
            "jellyfin-tui": PlayerState("Playing", "https://x/j", 1),
        }
        self.assertEqual(self.d(players),
                         Desired(CoverTarget("https://x/s", None), "dark"))

    def test_paused_and_playing_switches_to_playing(self):
        players = {
            "spotify": PlayerState("Paused", "https://x/s", 3),
            "jellyfin-tui": PlayerState("Playing", "https://x/j", 2),
        }
        self.assertEqual(self.d(players),
                         Desired(CoverTarget("https://x/j", _JF_DIR), "dark"))

    def test_all_paused_reverts(self):
        players = {
            "spotify": PlayerState("Paused", "https://x/s", 2),
            "jellyfin-tui": PlayerState("Paused", "https://x/j", 1),
        }
        self.assertEqual(self.d(players), Desired(None, "dark"))

    def test_all_stopped_reverts(self):
        players = {"spotify": PlayerState("Stopped", "", 4)}
        self.assertEqual(self.d(players), Desired(None, "dark"))

    def test_empty_reverts(self):
        self.assertEqual(self.d({}), Desired(None, "dark"))

    def test_playing_without_art_source_holds(self):
        # spotify has no covers_dir and no art_url -> no source -> hold (None).
        players = {"spotify": PlayerState("Playing", "", 5)}
        self.assertIsNone(self.d(players))

    def test_playing_jellyfin_empty_art_still_resolves_via_dir(self):
        # jellyfin-tui empty art but a covers_dir IS configured -> dir-scan
        # source, so it is an apply target, not a hold (design §4).
        players = {"jellyfin-tui": PlayerState("Playing", "", 5)}
        self.assertEqual(self.d(players),
                         Desired(CoverTarget("", _JF_DIR), "dark"))

    def test_mode_is_carried_into_the_desired(self):
        players = {"spotify": PlayerState("Playing", "https://x/a", 1)}
        self.assertEqual(self.d(players, mode="light"),
                         Desired(CoverTarget("https://x/a", None), "light"))


if __name__ == "__main__":
    unittest.main()
