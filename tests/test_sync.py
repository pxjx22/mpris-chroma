import unittest
import sys
from unittest.mock import patch

from mpris_chroma import sync
from mpris_chroma.apply import CtlError


class FollowCmdTest(unittest.TestCase):
    def test_default_playerctl(self):
        cmd = sync._follow_cmd()
        self.assertEqual(cmd[0], "playerctl")
        self.assertEqual(cmd[1], f"--player={sync.PLAYERS}")
        self.assertIn("-a", cmd)
        self.assertIn("--follow", cmd)
        self.assertIn("metadata", cmd)

    def test_custom_playerctl_path(self):
        cmd = sync._follow_cmd("/usr/bin/playerctl")
        self.assertEqual(cmd[0], "/usr/bin/playerctl")


class BoundedRevertTest(unittest.TestCase):
    @patch("mpris_chroma.sync.revert_wlchroma")
    def test_revert_success(self, mock_revert):
        sync._bounded_revert()
        mock_revert.assert_called_once()

    @patch("mpris_chroma.sync.revert_wlchroma")
    def test_revert_catches_ctl_error(self, mock_revert):
        mock_revert.side_effect = CtlError("test error")
        with self.assertLogs("mpris_chroma.sync", level="WARNING") as log:
            sync._bounded_revert()

        self.assertEqual(len(log.records), 1)
        self.assertIn("revert failed: test error", log.records[0].getMessage())
        mock_revert.assert_called_once()


class MainStartupTest(unittest.TestCase):
    @patch("mpris_chroma.sync._resolve_playerctl")
    @patch("sys.exit")
    def test_main_exits_on_missing_executable(self, mock_exit, mock_resolve):
        # We need to catch SystemExit to stop execution cleanly during sys.exit mock,
        # but mock_exit raises an exception to stop main from executing further.
        # Alternatively, we mock sys.exit to raise a custom exception.
        class TestExit(Exception):
            pass

        mock_exit.side_effect = TestExit
        mock_resolve.side_effect = sync.MissingExecutableError("not found")

        with self.assertLogs("mpris_chroma.sync", level="CRITICAL") as log:
            with self.assertRaises(TestExit):
                sync.main()

        self.assertEqual(len(log.records), 1)
        self.assertIn("not found", log.records[0].getMessage())
        mock_exit.assert_called_once_with(1)

if __name__ == "__main__":
    unittest.main()
