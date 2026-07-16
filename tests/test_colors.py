import colorsys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpris_chroma import colors
from mpris_chroma.colors import (
    clamp_hsv, hex_of, extract_colors, S_MIN, V_MIN, V_MAX, NEUTRAL_S, MAGICK,
    BANDS,
)


def _solid(path: Path, hexcolor: str):
    subprocess.run([MAGICK, "-size", "64x64", f"xc:{hexcolor}", str(path)], check=True)


def _halves(path: Path, left: str, right: str):
    subprocess.run(
        [MAGICK, "-size", "32x64", f"xc:{left}", "-size", "32x64", f"xc:{right}",
         "+append", str(path)], check=True)


def _thirds(path: Path, a: str, b: str, c: str):
    subprocess.run(
        [MAGICK, "-size", "22x64", f"xc:{a}", "-size", "22x64", f"xc:{b}",
         "-size", "20x64", f"xc:{c}", "+append", str(path)], check=True)


def _hsv(hexc: str):
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hsv(r, g, b)


class ClampTest(unittest.TestCase):
    def test_clamp_enriches_colored_pixel(self):
        # A pixel that already has a hue gets its saturation lifted.
        _, s, _ = clamp_hsv(0.5, 0.20, 0.6)
        self.assertGreaterEqual(s, S_MIN)

    def test_clamp_leaves_neutral_untinted(self):
        # A near-neutral (grayscale) pixel keeps its low saturation — no fake color.
        _, s, _ = clamp_hsv(0.0, 0.03, 0.6)
        self.assertLess(s, S_MIN)
        self.assertLessEqual(s, NEUTRAL_S)

    def test_clamp_bounds_value(self):
        _, _, vlo = clamp_hsv(0.5, 0.8, 0.05)
        _, _, vhi = clamp_hsv(0.5, 0.8, 0.99)
        self.assertGreaterEqual(vlo, V_MIN)
        self.assertLessEqual(vhi, V_MAX)

    def test_hex_of_roundtrips_format(self):
        self.assertEqual(hex_of(0.0, 1.0, 1.0), "#ff0000")


class ModeBandTest(unittest.TestCase):
    def test_dark_band_matches_legacy_constants(self):
        # "dark" is the historical behavior; keep it bit-identical.
        self.assertEqual(BANDS["dark"], (V_MIN, V_MAX))

    def test_light_band_sits_above_dark(self):
        lo_d, hi_d = BANDS["dark"]
        lo_l, hi_l = BANDS["light"]
        self.assertGreater(lo_l, lo_d)
        self.assertGreater(hi_l, hi_d)

    def test_clamp_light_mode_lifts_value_higher(self):
        _, _, v_dark = clamp_hsv(0.5, 0.8, 0.05, mode="dark")
        _, _, v_light = clamp_hsv(0.5, 0.8, 0.05, mode="light")
        self.assertGreaterEqual(v_light, BANDS["light"][0])
        self.assertGreater(v_light, v_dark)

    def test_clamp_light_mode_caps_at_light_ceiling(self):
        _, _, v = clamp_hsv(0.5, 0.8, 0.99, mode="light")
        self.assertLessEqual(v, BANDS["light"][1])

    def test_clamp_default_mode_is_dark(self):
        # Callers that never pass a mode keep today's behavior exactly.
        self.assertEqual(clamp_hsv(0.5, 0.8, 0.05),
                         clamp_hsv(0.5, 0.8, 0.05, mode="dark"))

    def test_mode_never_changes_hue(self):
        h_dark, _, _ = clamp_hsv(0.33, 0.8, 0.5, mode="dark")
        h_light, _, _ = clamp_hsv(0.33, 0.8, 0.5, mode="light")
        self.assertEqual(h_dark, 0.33)
        self.assertEqual(h_light, 0.33)


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extract_returns_three_valid_hex(self):
        img = self.tmp / "v.png"
        _solid(img, "#e01050")
        c1, c2, c3 = extract_colors(img)
        for c in (c1, c2, c3):
            self.assertEqual(len(c), 7)
            self.assertEqual(c[0], "#")
            int(c[1:], 16)  # parses as hex

    def test_dark_colored_cover_is_lifted_to_readable(self):
        img = self.tmp / "dark.png"
        _solid(img, "#3a0d0d")  # dark but genuinely red (has a hue)
        c1, _, _ = extract_colors(img)
        _, s, v = _hsv(c1)
        self.assertGreaterEqual(s, S_MIN)   # colored -> saturation enriched
        self.assertGreaterEqual(v, V_MIN)   # dark    -> value lifted

    def test_grayscale_cover_stays_neutral(self):
        # The regression: a grayscale cover must NOT be tinted into fake colors.
        img = self.tmp / "gray.png"
        _thirds(img, "#202020", "#808080", "#d0d0d0")
        for c in extract_colors(img):
            self.assertLess(_hsv(c)[1], S_MIN)  # every slot stays near-neutral

    def test_two_tone_cover_yields_distinct_hues(self):
        img = self.tmp / "two.png"
        _halves(img, "#e01010", "#1010e0")  # red | blue
        c1, c2, _ = extract_colors(img)
        self.assertGreater(abs(_hsv(c1)[0] - _hsv(c2)[0]), 0.05)

    def test_three_color_cover_yields_three_distinct_slots(self):
        img = self.tmp / "three.png"
        _thirds(img, "#e01010", "#10e010", "#1010e0")  # red | green | blue
        c1, c2, c3 = extract_colors(img)
        self.assertNotEqual(c1, c2)
        self.assertNotEqual(c2, c3)
        self.assertNotEqual(c1, c3)

    def test_light_mode_same_hues_brighter_values(self):
        # Light mode remaps only the value band: hue comes from the cover
        # either way, but every slot lands in the light band.
        img = self.tmp / "lm.png"
        _thirds(img, "#e01010", "#10e010", "#1010e0")
        dark = extract_colors(img, mode="dark")
        light = extract_colors(img, mode="light")
        for cd, cl in zip(dark, light):
            self.assertAlmostEqual(_hsv(cd)[0], _hsv(cl)[0], places=1)
        for cl in light:
            self.assertGreaterEqual(_hsv(cl)[2], BANDS["light"][0] - 0.01)

    def test_mode_switch_never_changes_which_colors_are_picked(self):
        # Regression: light's narrower band shrinks RGB distances, so a color
        # that passed COLOR_MIN_DIST in dark can collide in light — re-running
        # selection then swaps in a different cover color, changing a hue on a
        # theme flip. Selection must happen once; only the band moves.
        hist = [
            (100, (0.70, 0.60, 0.30)),  # dominant purple, dark
            (50, (0.70, 0.60, 0.62)),   # same hue, brighter: distinct only in dark band
            (10, (0.10, 0.80, 0.50)),   # orange that sneaks in if light re-selects
        ]
        with mock.patch.object(colors, "_histogram", return_value=hist):
            dark = extract_colors(Path("unused"), mode="dark")
            light = extract_colors(Path("unused"), mode="light")
        for cd, cl in zip(dark, light):
            self.assertAlmostEqual(_hsv(cd)[0], _hsv(cl)[0], places=2)

    def test_solid_cover_repeats_not_invents(self):
        # A truly solid cover has one color; slots repeat it rather than fabricate.
        img = self.tmp / "solid.png"
        _solid(img, "#c81e5a")
        c1, c2, c3 = extract_colors(img)
        self.assertEqual(c1, c2)
        self.assertEqual(c2, c3)


if __name__ == "__main__":
    unittest.main()
