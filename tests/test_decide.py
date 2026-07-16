import unittest
from unittest import mock

from mpris_chroma import sync
from mpris_chroma.sync import _follow_cmd, _handle_line


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


class HandleLineTest(unittest.TestCase):
    def _line(self, name, status, art):
        return f"{name}\t{status}\t{art}\n"

    def test_playing_applies_and_dedupes(self):
        players, seq, applied = {}, 0, None
        with mock.patch.object(sync, "resolve_cover", return_value="/covers/a.jpg"), \
             mock.patch.object(sync, "_apply_all") as ap, \
             mock.patch.object(sync, "_revert_all") as rv:
            seq, applied = _handle_line(self._line("spotify", "Playing", "http://x"),
                                        players, seq, applied)
            self.assertEqual(applied, "/covers/a.jpg")
            ap.assert_called_once()
            # same cover again -> no re-apply
            seq, applied = _handle_line(self._line("spotify", "Playing", "http://x"),
                                        players, seq, applied)
            self.assertEqual(ap.call_count, 1)
            rv.assert_not_called()

    def test_stop_reverts(self):
        players, seq, applied = {}, 0, "/covers/a.jpg"
        players["spotify"] = ("Playing", "/covers/a.jpg", 1)
        with mock.patch.object(sync, "resolve_cover", return_value=None), \
             mock.patch.object(sync, "_apply_all"), \
             mock.patch.object(sync, "_revert_all") as rv:
            seq, applied = _handle_line(self._line("spotify", "Stopped", ""),
                                        players, seq, applied)
            rv.assert_called_once()
            self.assertIsNone(applied)

    def test_ignores_malformed_line(self):
        players, seq, applied = {}, 0, None
        with mock.patch.object(sync, "_apply_all") as ap, \
             mock.patch.object(sync, "_revert_all") as rv:
            seq, applied = _handle_line("garbage\n", players, seq, applied)
            ap.assert_not_called()
            rv.assert_not_called()
            self.assertIsNone(applied)


if __name__ == "__main__":
    unittest.main()
