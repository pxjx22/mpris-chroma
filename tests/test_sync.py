import unittest

from mpris_chroma.sync import _follow_cmd


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


if __name__ == "__main__":
    unittest.main()
