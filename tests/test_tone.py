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
        _, hi = ENVELOPES["dark"]
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


class SeparationTest(unittest.TestCase):
    def _collide(self, mode="dark"):
        # Three tans that the old RGB distance check passed: a real collision
        # from the corpus, not a synthetic one.
        src = [_lch("#d1a973"), _lch("#b89265"), _lch("#9d7156")]
        return tone.tone(src, mode)

    def test_collision_is_separated(self):
        slots, result = tone.separate(self._collide(), "dark", 3)
        labs = [s.to_lab() for s in slots]
        for i in range(3):
            for j in range(i + 1, 3):
                with self.subTest(pair=(i, j)):
                    self.assertGreaterEqual(oklab.delta_e(labs[i], labs[j]),
                                            tone.MIN_DE - 1e-9)
        self.assertTrue(result.resolved)
        self.assertEqual(result.reason, "clear")

    def test_already_distinct_palette_is_untouched(self):
        before = tone.tone([_lch("#e01050"), _lch("#10e050"), _lch("#5010e0")], "dark")
        after, result = tone.separate(before, "dark", 3)
        for a, b in zip(before, after):
            self.assertEqual(a.L, b.L)
        self.assertEqual(result.reason, "clear")

    def test_hue_is_never_modified(self):
        before = self._collide()
        after, _ = tone.separate(before, "dark", 3)
        for a, b in zip(before, after):
            self.assertAlmostEqual(a.h, b.h, places=12)

    def test_no_slot_moves_beyond_the_displacement_budget(self):
        # The bound on separation's departure from source fidelity (spec §6).
        before = self._collide()
        after, _ = tone.separate(before, "dark", 3)
        for a, b in zip(before, after):
            self.assertLessEqual(abs(a.L - b.L), tone.MAX_SEPARATION_SHIFT + 1e-9)

    def test_no_slot_crosses_a_neighbour(self):
        # Without the neighbour clamp a slot can be pushed past a third slot and
        # invert the ordering that mode-independence relies on.
        before = self._collide()
        order = sorted(range(3), key=lambda i: before[i].L)
        after, _ = tone.separate(before, "dark", 3)
        ranked = [after[i].L for i in order]
        self.assertEqual(ranked, sorted(ranked))

    def test_output_stays_inside_the_envelope(self):
        lo, hi = ENVELOPES["dark"]
        after, _ = tone.separate(self._collide(), "dark", 3)
        for slot in after:
            self.assertGreaterEqual(slot.L, lo - 1e-9)
            self.assertLessEqual(slot.L, hi + 1e-9)

    def test_duplicate_slots_are_never_separated(self):
        # A solid cover repeats its one real color; separating it would make the
        # cover appear to have contrast it does not have.
        src = [_lch("#c81e5a")] * 3
        toned = tone.tone(src, "dark")
        after, result = tone.separate(toned, "dark", 1)
        self.assertEqual(after[0].L, after[1].L)
        self.assertEqual(after[1].L, after[2].L)
        self.assertEqual(result.reason, "duplicates")
        self.assertFalse(result.resolved)

    def test_two_distinct_slots_separate_and_the_pad_follows(self):
        src = [_lch("#d1a973"), _lch("#b89265")]
        toned = tone.tone(src, "dark") + [tone.tone(src, "dark")[-1]]
        after, _ = tone.separate(toned, "dark", 2)
        self.assertEqual(after[1].L, after[2].L)   # pad still mirrors slot 2

    def test_monochrome_collision_reports_rather_than_forcing(self):
        # Two colors that are the same hue and nearly the same lightness cannot
        # be pushed apart within budget; that is the correct outcome, but it has
        # to be observable rather than silent.
        src = [_lch("#4a4a4a"), _lch("#4b4b4b"), _lch("#4c4c4c")]
        _, result = tone.separate(tone.tone(src, "dark"), "dark", 3)
        self.assertFalse(result.resolved)
        self.assertIn(result.reason, ("budget", "envelope", "passes"))
        self.assertGreater(result.residual_de, 0.0)
        self.assertLess(result.residual_de, tone.MIN_DE)

    def test_budget_exhaustion_is_reported_distinctly(self):
        # Each terminal condition must be reachable and correctly named, or the
        # reason string is decoration rather than diagnosis. These two sit mid
        # envelope with room to move, so "envelope" is ruled out and only the
        # displacement budget can stop them.
        lo, hi = ENVELOPES["dark"]
        mid = (lo + hi) / 2
        h = math.radians(29)
        slots = [Toned(L=mid, h=h, c_src=0.05),
                 Toned(L=mid + 0.001, h=h, c_src=0.05),
                 Toned(L=mid + 0.002, h=h, c_src=0.05)]
        after, result = tone.separate(slots, "dark", 3)
        self.assertEqual(result.reason, "budget")
        self.assertFalse(result.resolved)
        for before, moved in zip(slots, after):
            self.assertLess(abs(before.L - moved.L), lo)   # nowhere near a bound

    def test_budget_binds_before_the_pass_cap(self):
        # The two constants are not redundant: the displacement budget is the
        # operative limit and the pass cap is only a loop safety net.
        self.assertLess(tone.MAX_SEPARATION_SHIFT / tone.SEPARATION_STEP,
                        tone.MAX_SEPARATION_PASSES)

    def test_separation_terminates_in_light_mode_too(self):
        after, result = tone.separate(self._collide("light"), "light", 3)
        lo, hi = ENVELOPES["light"]
        for slot in after:
            self.assertGreaterEqual(slot.L, lo - 1e-9)
            self.assertLessEqual(slot.L, hi + 1e-9)
        self.assertIn(result.reason, ("clear", "budget", "envelope", "passes"))


if __name__ == "__main__":
    unittest.main()
