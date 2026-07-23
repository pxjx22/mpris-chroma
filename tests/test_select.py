import unittest

from mpris_chroma.select import select
from mpris_chroma.state import PlayerState


class SelectTest(unittest.TestCase):
    def test_single_playing_applies_its_cover(self):
        players = {"spotify": PlayerState("Playing", "coverS", 1)}
        self.assertEqual(select(players), ("apply", "coverS"))

    def test_two_playing_most_recent_seq_wins(self):
        players = {
            "jellyfin-tui": PlayerState("Playing", "coverJ", 1),
            "spotify": PlayerState("Playing", "coverS", 2),
        }
        self.assertEqual(select(players), ("apply", "coverS"))

    def test_active_paused_other_playing_switches(self):
        players = {
            "spotify": PlayerState("Paused", "coverS", 3),
            "jellyfin-tui": PlayerState("Playing", "coverJ", 2),
        }
        self.assertEqual(select(players), ("apply", "coverJ"))

    def test_all_paused_reverts(self):
        # Pausing means the music stopped mattering: fall back to the preset.
        players = {
            "spotify": PlayerState("Paused", "coverS", 2),
            "jellyfin-tui": PlayerState("Paused", "coverJ", 1),
        }
        self.assertEqual(select(players), ("revert", None))

    def test_all_stopped_reverts(self):
        players = {"spotify": PlayerState("Stopped", None, 4)}
        self.assertEqual(select(players), ("revert", None))

    def test_playing_without_cover_holds(self):
        # Playing but the cover failed to resolve (e.g. download error): wait.
        players = {"spotify": PlayerState("Playing", None, 5)}
        self.assertEqual(select(players), ("hold", None))

    def test_empty_reverts(self):
        self.assertEqual(select({}), ("revert", None))


class PlayerStateTest(unittest.TestCase):
    """PY-001: player state is a named, immutable record, not an opaque tuple."""

    def test_fields_are_named(self):
        s = PlayerState(status="Playing", cover_id="coverS", seq=7)
        self.assertEqual(s.status, "Playing")
        self.assertEqual(s.cover_id, "coverS")
        self.assertEqual(s.seq, 7)

    def test_is_immutable(self):
        s = PlayerState("Playing", "coverS", 1)
        with self.assertRaises(Exception):
            s.status = "Paused"  # frozen: states are replaced, not mutated


if __name__ == "__main__":
    unittest.main()
