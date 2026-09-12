import math
import unittest

from mpris_chroma import oklab


def _hex_to_lch(value: str) -> tuple[float, float, float]:
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return oklab.to_lch(*oklab.srgb_to_oklab(r, g, b))


class RoundTripTest(unittest.TestCase):
    def test_srgb_oklab_round_trip(self):
        # Every channel combination must survive the forward+inverse transform;
        # the pipeline converts back and forth per slot, so drift compounds.
        for rgb in [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.2, 0.6, 0.9),
                    (0.85, 0.06, 0.31), (0.5, 0.5, 0.5)]:
            with self.subTest(rgb=rgb):
                back = oklab.oklab_to_srgb(*oklab.srgb_to_oklab(*rgb))
                for got, want in zip(back, rgb):
                    self.assertAlmostEqual(got, want, places=6)

    def test_lch_round_trip(self):
        lab = oklab.srgb_to_oklab(0.2, 0.6, 0.9)
        back = oklab.from_lch(*oklab.to_lch(*lab))
        for got, want in zip(back, lab):
            self.assertAlmostEqual(got, want, places=9)

    def test_hex_round_trip(self):
        # Quantization to 8 bits is the only permitted loss.
        for hexc in ("#000000", "#ffffff", "#e01050", "#0284c4"):
            with self.subTest(hexc=hexc):
                self.assertEqual(oklab.lch_to_hex(*_hex_to_lch(hexc)), hexc)


class LightnessTest(unittest.TestCase):
    def test_black_and_white_anchor_the_scale(self):
        self.assertAlmostEqual(oklab.srgb_to_oklab(0.0, 0.0, 0.0)[0], 0.0, places=6)
        self.assertAlmostEqual(oklab.srgb_to_oklab(1.0, 1.0, 1.0)[0], 1.0, places=6)

    def test_lightness_is_perceptual_not_hsv_value(self):
        # The premise of the whole design: #d9d9d9 and #0284c4 share HSV value
        # ~0.8 but are nowhere near each other in apparent lightness.
        grey = _hex_to_lch("#d9d9d9")[0]
        blue = _hex_to_lch("#0284c4")[0]
        self.assertGreater(grey - blue, 0.2)

    def test_neutral_has_zero_chroma(self):
        for hexc in ("#000000", "#808080", "#ffffff"):
            with self.subTest(hexc=hexc):
                self.assertLess(_hex_to_lch(hexc)[1], 1e-6)


class GamutTest(unittest.TestCase):
    def test_max_chroma_is_in_gamut_and_boundary_is_tight(self):
        # Just inside must be representable; just outside must not be. This is
        # what makes the spec's "reduce chroma to the boundary" policy exact.
        for L in (0.15, 0.35, 0.55, 0.75):
            for h_deg in (29, 60, 110, 142, 195, 264, 328):
                with self.subTest(L=L, h=h_deg):
                    h = math.radians(h_deg)
                    c = oklab.max_chroma(L, h)
                    self.assertTrue(oklab.in_gamut(*oklab.from_lch(L, c * 0.99, h)))
                    self.assertFalse(oklab.in_gamut(*oklab.from_lch(L, c + 0.01, h)))

    def test_max_chroma_is_hue_dependent(self):
        # Blue reaches far more chroma than yellow at the same lightness; this
        # is why chroma targets must be a fraction of the ceiling, not absolute.
        L = 0.25
        yellow = oklab.max_chroma(L, math.radians(110))
        blue = oklab.max_chroma(L, math.radians(264))
        self.assertGreater(blue, yellow * 2)

    def test_max_chroma_vanishes_at_the_extremes(self):
        for L in (0.0, 1.0):
            for h_deg in (29, 142, 264):
                with self.subTest(L=L, h=h_deg):
                    self.assertLess(oklab.max_chroma(L, math.radians(h_deg)), 0.01)


class DeltaETest(unittest.TestCase):
    def test_identical_colors_have_zero_distance(self):
        lab = oklab.srgb_to_oklab(0.2, 0.6, 0.9)
        self.assertAlmostEqual(oklab.delta_e(lab, lab), 0.0, places=9)

    def test_distance_is_symmetric_and_positive(self):
        a = oklab.srgb_to_oklab(0.2, 0.6, 0.9)
        b = oklab.srgb_to_oklab(0.9, 0.2, 0.1)
        self.assertAlmostEqual(oklab.delta_e(a, b), oklab.delta_e(b, a), places=9)
        self.assertGreater(oklab.delta_e(a, b), 0.0)

    def test_near_duplicates_score_below_the_separation_threshold(self):
        # Two tans that the old RGB check passed must read as a collision.
        self.assertLess(
            oklab.delta_e(oklab.srgb_to_oklab(*[c / 255 for c in (209, 169, 115)]),
                          oklab.srgb_to_oklab(*[c / 255 for c in (184, 146, 101)])),
            0.10)


if __name__ == "__main__":
    unittest.main()
