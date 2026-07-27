import math
import unittest

from mpris_chroma import oklab, tone
from mpris_chroma.tone import ENVELOPES, GAMMA, NEUTRAL_C, Toned


def _lch(hexc):
    return oklab.hex_to_lch(hexc)


class ChromaRuleTest(unittest.TestCase):
    def test_colored_slot_is_lifted_toward_its_ceiling(self):
        h = math.radians(29)
        c = tone.chroma_for(0.30, h, 0.02)
        self.assertGreater(c, 0.02)
        self.assertLessEqual(c, oklab.max_chroma(0.30, h))

    def test_chroma_never_exceeds_the_ceiling(self):
        # Policy (a): hue and lightness are preserved, chroma yields.
        for L in (0.15, 0.35, 0.55):
            for h_deg in (60, 110, 195, 264):
                with self.subTest(L=L, h=h_deg):
                    h = math.radians(h_deg)
                    self.assertLessEqual(tone.chroma_for(L, h, 0.30),
                                         oklab.max_chroma(L, h) + 1e-9)

    def test_neutral_slot_is_not_enriched(self):
        # Below NEUTRAL_C a slot is genuinely grey; lifting it would invent a hue.
        self.assertLess(tone.chroma_for(0.30, 0.0, 0.001), NEUTRAL_C)


class ToneTest(unittest.TestCase):
    def test_output_lands_inside_the_mode_envelope(self):
        for mode in ("dark", "light"):
            lo, hi = ENVELOPES[mode]
            for src in ([_lch("#000000")], [_lch("#ffffff")],
                        [_lch("#e01050"), _lch("#10e050"), _lch("#5010e0")]):
                with self.subTest(mode=mode, n=len(src)):
                    for slot in tone.tone(src, mode):
                        self.assertGreaterEqual(slot.L, lo - 1e-9)
                        self.assertLessEqual(slot.L, hi + 1e-9)

    def test_output_is_in_gamut(self):
        src = [_lch("#ffee00"), _lch("#00e5ff"), _lch("#1010e0")]
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                for slot in tone.tone(src, mode):
                    self.assertTrue(oklab.in_gamut(*oklab.from_lch(slot.L, slot.C, slot.h)))

    def test_hue_is_never_modified(self):
        src = [_lch("#e01050"), _lch("#10e050"), _lch("#5010e0")]
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                for got, want in zip(tone.tone(src, mode), src):
                    self.assertAlmostEqual(got.h, want[2], places=12)

    def test_lightness_order_is_preserved(self):
        # Monotonicity is what lets selection stay mode-independent: the curve
        # re-tones, it never reorders.
        src = [_lch("#202020"), _lch("#808080"), _lch("#e8e8e8")]
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                out = [s.L for s in tone.tone(src, mode)]
                self.assertLess(out[0], out[1])
                self.assertLess(out[1], out[2])

    def test_bright_cover_is_compressed_not_merely_clamped(self):
        # The washing defect: a near-white cover must be pulled down by the
        # compression curve, not just flattened against the envelope ceiling.
        # A clamp-only implementation would pin all three slots at `hi` and
        # destroy the spread; compression seats the anchor below the ceiling
        # and keeps the slots apart.
        lo, hi = ENVELOPES["dark"]
        src = [_lch("#f2f2f2"), _lch("#e8e8e8"), _lch("#fafafa")]
        out = tone.tone(src, "dark")
        pinned = sum(1 for s in out if s.L >= hi - 1e-9)
        self.assertLess(pinned, 3, "all slots pinned at the ceiling: clamped, not compressed")
        self.assertGreater(max(s.L for s in out) - min(s.L for s in out), 0.02)
        self.assertLess(sum(s.L for s in out) / 3, hi - 0.005)

    def test_dark_cover_stays_darker_than_a_bright_one(self):
        # Compression, not normalization: cross-cover ordering must survive.
        dark = tone.tone([_lch("#101010"), _lch("#181818"), _lch("#121212")], "dark")
        bright = tone.tone([_lch("#f2f2f2"), _lch("#e8e8e8"), _lch("#fafafa")], "dark")
        self.assertLess(sum(s.L for s in dark) / 3, sum(s.L for s in bright) / 3)

    def test_within_cover_spread_is_largely_preserved(self):
        src = [_lch("#101010"), _lch("#808080"), _lch("#f0f0f0")]
        out = tone.tone(src, "dark")
        src_spread = max(s[0] for s in src) - min(s[0] for s in src)
        out_spread = max(s.L for s in out) - min(s.L for s in out)
        self.assertGreater(out_spread, 0.5 * src_spread)

    def test_flat_cover_stays_flat(self):
        # Decision D1: fidelity to the source. A flat cover is not given
        # contrast it never had — that is separation's job, and only on collision.
        src = [_lch("#4a4a4a"), _lch("#4c4c4c"), _lch("#4b4b4b")]
        out = tone.tone(src, "dark")
        self.assertLess(max(s.L for s in out) - min(s.L for s in out), 0.05)

    def test_grayscale_stays_grayscale(self):
        src = [_lch("#202020"), _lch("#808080"), _lch("#d0d0d0")]
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                for slot in tone.tone(src, mode):
                    self.assertLess(slot.C, NEUTRAL_C)

    def test_light_mode_is_brighter_than_dark_for_the_same_source(self):
        src = [_lch("#e01050"), _lch("#10e050"), _lch("#5010e0")]
        dark = tone.tone(src, "dark")
        light = tone.tone(src, "light")
        for d, l in zip(dark, light):
            self.assertGreater(l.L, d.L)

    def test_gamma_light_is_the_structural_inverse_of_dark(self):
        self.assertAlmostEqual(GAMMA["light"], 1 / GAMMA["dark"], places=2)


class TonedRecordTest(unittest.TestCase):
    def test_chroma_follows_lightness(self):
        # The property exists so separation can move L without stranding a
        # chroma value that is no longer reachable at the new lightness.
        h = math.radians(110)   # yellow: a hue with a low, fast-moving ceiling
        low = Toned(L=0.15, h=h, c_src=0.30)
        high = Toned(L=0.45, h=h, c_src=0.30)
        self.assertLess(low.C, high.C)
        self.assertLessEqual(low.C, oklab.max_chroma(0.15, h) + 1e-9)


if __name__ == "__main__":
    unittest.main()
