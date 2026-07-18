import unittest
from unittest import mock

from mpris_chroma import sync
from mpris_chroma.sync import _handle_vanish


class HandleVanishTest(unittest.TestCase):
    def test_last_player_vanishing_reverts(self):
        # Spotify was playing, then its process closed -> bus name lost.
        # No more players -> revert to the config preset.
        players = {"spotify": ("Playing", "/covers/a.jpg", 1)}
        applied = "/covers/a.jpg"
        with mock.patch.object(sync, "_apply_all") as ap, \
             mock.patch.object(sync, "_revert_all") as rv:
            applied = _handle_vanish("org.mpris.MediaPlayer2.spotify",
                                     players, applied)
            rv.assert_called_once()
            ap.assert_not_called()
            self.assertIsNone(applied)
            self.assertNotIn("spotify", players)

    def test_non_player_bus_name_is_ignored(self):
        players = {"spotify": ("Playing", "/covers/a.jpg", 1)}
        applied = "/covers/a.jpg"
        with mock.patch.object(sync, "_apply_all") as ap, \
             mock.patch.object(sync, "_revert_all") as rv:
            applied = _handle_vanish("org.freedesktop.Notifications",
                                     players, applied)
            rv.assert_not_called()
            ap.assert_not_called()
            self.assertEqual(applied, "/covers/a.jpg")
            self.assertIn("spotify", players)

    def test_unknown_player_vanishing_is_noop(self):
        # A player we never tracked (not in the dict) vanishing changes nothing.
        players = {"spotify": ("Playing", "/covers/a.jpg", 1)}
        applied = "/covers/a.jpg"
        with mock.patch.object(sync, "_apply_all"), \
             mock.patch.object(sync, "_revert_all") as rv:
            applied = _handle_vanish("org.mpris.MediaPlayer2.firefox",
                                     players, applied)
            rv.assert_not_called()
            self.assertEqual(applied, "/covers/a.jpg")

    def test_vanish_reverts_when_only_paused_player_remains(self):
        # jellyfin-tui closes and spotify is merely paused -> revert.
        players = {
            "spotify": ("Paused", None, 1),
            "jellyfin-tui": ("Playing", "/covers/j.jpg", 2),
        }
        applied = "/covers/j.jpg"
        with mock.patch.object(sync, "_apply_all"), \
             mock.patch.object(sync, "_revert_all") as rv:
            applied = _handle_vanish("org.mpris.MediaPlayer2.jellyfin-tui",
                                     players, applied)
            rv.assert_called_once()
            self.assertIsNone(applied)


if __name__ == "__main__":
    unittest.main()
