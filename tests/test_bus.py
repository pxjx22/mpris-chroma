import unittest

from mpris_chroma.coordinator import player_name_from_bus


class BusNameTest(unittest.TestCase):
    def test_strips_mpris_prefix_to_player_name(self):
        # The players dict is keyed by playerctl's {{playerName}}, which equals
        # the bus name minus the MPRIS prefix.
        self.assertEqual(
            player_name_from_bus("org.mpris.MediaPlayer2.spotify"), "spotify")

    def test_keeps_instance_suffix(self):
        # An instance-suffixed bus name maps to the same suffixed playerName
        # playerctl reports, so eviction still matches the dict key.
        self.assertEqual(
            player_name_from_bus("org.mpris.MediaPlayer2.jellyfin-tui.instance7"),
            "jellyfin-tui.instance7")

    def test_non_mpris_name_is_none(self):
        # Unrelated bus-name churn (e.g. org.freedesktop.*) is not a player.
        self.assertIsNone(player_name_from_bus("org.freedesktop.Notifications"))
        self.assertIsNone(player_name_from_bus(":1.42"))


if __name__ == "__main__":
    unittest.main()
