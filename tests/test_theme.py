import unittest
from unittest import mock

from mpris_chroma import sync
from mpris_chroma.sync import mode_from_color_scheme, _handle_scheme_change


class ColorSchemeMapTest(unittest.TestCase):
    def test_prefer_dark_maps_to_dark(self):
        # Portal values: 0 = no preference, 1 = prefer dark, 2 = prefer light.
        self.assertEqual(mode_from_color_scheme(1), "dark")

    def test_prefer_light_maps_to_light(self):
        self.assertEqual(mode_from_color_scheme(2), "light")

    def test_no_preference_defaults_to_dark(self):
        self.assertEqual(mode_from_color_scheme(0), "dark")

    def test_unknown_value_defaults_to_dark(self):
        # Future/undocumented portal values must not crash the daemon.
        self.assertEqual(mode_from_color_scheme(7), "dark")


class HandleSchemeChangeTest(unittest.TestCase):
    def test_flip_reapplies_current_cover_in_new_mode(self):
        # Theme flips to light while a cover is applied -> re-tone that cover.
        with mock.patch.object(sync, "_apply_all") as ap:
            mode = _handle_scheme_change(2, "/covers/a.jpg", "dark")
        self.assertEqual(mode, "light")
        ap.assert_called_once()
        (_cover, applied_mode), _ = ap.call_args
        self.assertEqual(str(_cover), "/covers/a.jpg")
        self.assertEqual(applied_mode, "light")

    def test_same_mode_is_noop(self):
        with mock.patch.object(sync, "_apply_all") as ap:
            mode = _handle_scheme_change(1, "/covers/a.jpg", "dark")
        self.assertEqual(mode, "dark")
        ap.assert_not_called()

    def test_flip_with_nothing_applied_updates_mode_only(self):
        # Reverted to wlchroma's config preset: that palette isn't ours to
        # re-tone. Track the mode for the next apply, but push nothing.
        with mock.patch.object(sync, "_apply_all") as ap:
            mode = _handle_scheme_change(2, None, "dark")
        self.assertEqual(mode, "light")
        ap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
