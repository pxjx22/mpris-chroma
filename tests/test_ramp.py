import unittest

from mpris_chroma import ramp


class ExpandTest(unittest.TestCase):
    def test_expands_to_twelve_cells(self):
        self.assertEqual(len(ramp.expand("#ff0000", "#00ff00", "#0000ff")), 12)

    def test_full_alpha_cells_are_the_endpoints(self):
        # palette.zig pairs (c1,c2), (c2,c3), (c3,c1); at alpha 1.0 the cell is
        # the foreground color exactly.
        cells = ramp.expand("#ff0000", "#00ff00", "#0000ff")
        self.assertEqual(cells[0], "#ff0000")
        self.assertEqual(cells[4], "#00ff00")
        self.assertEqual(cells[8], "#0000ff")

    def test_solid_palette_expands_to_one_color(self):
        self.assertEqual(set(ramp.expand("#c81e5a", "#c81e5a", "#c81e5a")),
                         {"#c81e5a"})

    def test_midpoint_cell_lies_between_its_endpoints(self):
        cells = ramp.expand("#000000", "#ffffff", "#000000")
        mid = int(cells[2][1:3], 16)   # (c1,c2) at alpha 0.50
        self.assertGreater(mid, 100)
        self.assertLess(mid, 155)


class LuminanceTest(unittest.TestCase):
    def test_black_and_white_anchor_the_range(self):
        self.assertAlmostEqual(ramp.mean_luminance("#000000", "#000000", "#000000"),
                               0.0, places=6)
        self.assertAlmostEqual(ramp.mean_luminance("#ffffff", "#ffffff", "#ffffff"),
                               1.0, places=6)

    def test_reference_palette_is_dark(self):
        # witch_hour, the configured preset this work is calibrated against.
        self.assertLess(ramp.mean_luminance("#120C14", "#4A2F5C", "#6D8F4F"), 0.12)

    def test_brighter_palette_scores_higher(self):
        dark = ramp.mean_luminance("#101010", "#181818", "#121212")
        bright = ramp.mean_luminance("#f2f2f2", "#e8e8e8", "#fafafa")
        self.assertGreater(bright, dark)


if __name__ == "__main__":
    unittest.main()
