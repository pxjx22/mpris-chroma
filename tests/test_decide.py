import unittest
from pathlib import Path
from unittest import mock

from mpris_chroma import apply, sync
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


class ApplyStateReliabilityTest(unittest.TestCase):
    """SEC-007: `applied` advances only on a confirmed wlchroma change. A failed
    apply leaves it unchanged so the next event retries the same cover; a failed
    revert leaves it set so a later event retries the revert. `_apply_all` /
    `_revert_all` contain a CtlError as a False result rather than propagating."""

    def _line(self, name, status, art):
        return f"{name}\t{status}\t{art}\n"

    def test_failed_apply_does_not_advance_applied(self):
        players, seq, applied = {}, 0, None
        with mock.patch.object(sync, "resolve_cover", return_value="/covers/a.jpg"), \
             mock.patch.object(sync, "_apply_all", return_value=False) as ap:
            seq, applied = _handle_line(self._line("spotify", "Playing", "http://x"),
                                        players, seq, applied)
        ap.assert_called_once()
        self.assertIsNone(applied)  # not advanced on failure

    def test_same_cover_retried_after_failed_apply(self):
        players, seq, applied = {}, 0, None
        with mock.patch.object(sync, "resolve_cover", return_value="/covers/a.jpg"), \
             mock.patch.object(sync, "_apply_all", return_value=False) as ap:
            seq, applied = _handle_line(self._line("spotify", "Playing", "http://x"),
                                        players, seq, applied)
            seq, applied = _handle_line(self._line("spotify", "Playing", "http://x"),
                                        players, seq, applied)
        self.assertEqual(ap.call_count, 2)  # retried, not deduped as "applied"

    def test_applied_advances_after_successful_apply(self):
        players, seq, applied = {}, 0, None
        with mock.patch.object(sync, "resolve_cover", return_value="/covers/a.jpg"), \
             mock.patch.object(sync, "_apply_all", return_value=True):
            seq, applied = _handle_line(self._line("spotify", "Playing", "http://x"),
                                        players, seq, applied)
        self.assertEqual(applied, "/covers/a.jpg")

    def test_failed_revert_keeps_applied_for_retry(self):
        players, seq, applied = {}, 0, "/covers/a.jpg"
        players["spotify"] = ("Playing", "/covers/a.jpg", 1)
        with mock.patch.object(sync, "resolve_cover", return_value=None), \
             mock.patch.object(sync, "_revert_all", return_value=False) as rv:
            seq, applied = _handle_line(self._line("spotify", "Stopped", ""),
                                        players, seq, applied)
        rv.assert_called_once()
        self.assertEqual(applied, "/covers/a.jpg")  # kept so revert can retry

    def test_apply_all_contains_ctl_error_as_false(self):
        with mock.patch.object(sync, "extract_colors",
                               return_value=("#aa0000", "#00bb00", "#0000cc")), \
             mock.patch.object(sync, "apply_wlchroma",
                               side_effect=apply.CtlError("ctl down")):
            self.assertFalse(sync._apply_all(Path("/covers/a.jpg"), "dark"))

    def test_revert_all_contains_ctl_error_as_false(self):
        with mock.patch.object(sync, "revert_wlchroma",
                               side_effect=apply.CtlError("ctl down")):
            self.assertFalse(sync._revert_all())


if __name__ == "__main__":
    unittest.main()
