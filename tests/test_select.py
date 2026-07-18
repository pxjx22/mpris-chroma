import unittest

from mpris_chroma.select import select


class SelectTest(unittest.TestCase):
    def test_single_playing_applies_its_cover(self):
        players = {"spotify": ("Playing", "coverS", 1)}
        self.assertEqual(select(players), ("apply", "coverS"))

    def test_two_playing_most_recent_seq_wins(self):
        players = {
            "jellyfin-tui": ("Playing", "coverJ", 1),
            "spotify": ("Playing", "coverS", 2),
        }
        self.assertEqual(select(players), ("apply", "coverS"))

    def test_active_paused_other_playing_switches(self):
        players = {
            "spotify": ("Paused", "coverS", 3),
            "jellyfin-tui": ("Playing", "coverJ", 2),
        }
        self.assertEqual(select(players), ("apply", "coverJ"))

    def test_all_paused_reverts(self):
        # Pausing means the music stopped mattering: fall back to the preset.
        players = {
            "spotify": ("Paused", "coverS", 2),
            "jellyfin-tui": ("Paused", "coverJ", 1),
        }
        self.assertEqual(select(players), ("revert", None))

    def test_all_stopped_reverts(self):
        players = {"spotify": ("Stopped", None, 4)}
        self.assertEqual(select(players), ("revert", None))

    def test_playing_without_cover_holds(self):
        # Playing but the cover failed to resolve (e.g. download error): wait.
        players = {"spotify": ("Playing", None, 5)}
        self.assertEqual(select(players), ("hold", None))

    def test_empty_reverts(self):
        self.assertEqual(select({}), ("revert", None))
