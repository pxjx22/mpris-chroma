import colorsys
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from mpris_chroma import colors
from mpris_chroma.colors import (
    extract_colors, VIBRANCY_WEIGHT, VIBRANCY_MIN_POP, _vibrancy_score,
)
from mpris_chroma import oklab, ramp
from mpris_chroma.tone import ENVELOPES, NEUTRAL_C, MIN_DE, separate, tone


def _rgb(hexc: str) -> tuple[int, int, int]:
    return tuple(int(hexc[i:i + 2], 16) for i in (1, 3, 5))


def _solid(path: Path, hexcolor: str):
    Image.new("RGB", (64, 64), _rgb(hexcolor)).save(path)


def _halves(path: Path, left: str, right: str):
    img = Image.new("RGB", (64, 64))
    img.paste(Image.new("RGB", (32, 64), _rgb(left)), (0, 0))
    img.paste(Image.new("RGB", (32, 64), _rgb(right)), (32, 0))
    img.save(path)


def _thirds(path: Path, a: str, b: str, c: str):
    img = Image.new("RGB", (64, 64))
    img.paste(Image.new("RGB", (22, 64), _rgb(a)), (0, 0))
    img.paste(Image.new("RGB", (22, 64), _rgb(b)), (22, 0))
    img.paste(Image.new("RGB", (20, 64), _rgb(c)), (44, 0))
    img.save(path)


def _hsv(hexc: str):
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hsv(r, g, b)


class VibrancyScoreTest(unittest.TestCase):
    # score = coverage + VIBRANCY_WEIGHT * chroma (chroma = s*v), so a
    # small-but-vivid accent can outrank a large-but-drab background.

    def test_vivid_accent_outranks_large_drab_region(self):
        # The accent doesn't need to dethrone the cover's base color — it
        # needs to beat the weakest slot. 3% vivid vs. a drab 40% region.
        drab = _vibrancy_score(400, 1000, (0.60, 0.35, 0.20))   # 40%, muddy
        accent = _vibrancy_score(30, 1000, (0.07, 1.0, 1.0))    # 3%, vivid
        self.assertGreater(accent, drab)

    def test_speck_gets_no_vibrancy_boost(self):
        # Below the population floor, vividness cannot jump the queue —
        # a lone noise pixel must not become a palette slot.
        speck = _vibrancy_score(2, 1000, (0.07, 1.0, 1.0))
        self.assertAlmostEqual(speck, 2 / 1000)

    def test_grayscale_scores_by_coverage_only(self):
        # Zero saturation -> zero chroma -> pure population ranking, so
        # grayscale covers keep their existing behavior exactly.
        big = _vibrancy_score(600, 1000, (0.0, 0.0, 0.5))
        small = _vibrancy_score(300, 1000, (0.0, 0.0, 0.9))
        self.assertAlmostEqual(big, 0.6)
        self.assertGreater(big, small)


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
        # Was: saturation >= S_MIN and value >= V_MIN. The intent survives —
        # a dark but genuinely red cover must come out visible and still red —
        # but it is now stated in the space the pipeline actually works in.
        img = self.tmp / "dark.png"
        _solid(img, "#3a0d0d")
        c1, _, _ = extract_colors(img)
        L, C, _ = oklab.hex_to_lch(c1)
        lo, hi = ENVELOPES["dark"]
        self.assertGreaterEqual(L, lo - 1e-9)
        self.assertLessEqual(L, hi + 1e-9)
        self.assertGreater(C, NEUTRAL_C)

    def test_grayscale_cover_stays_neutral(self):
        # The regression: a grayscale cover must NOT be tinted into fake colors.
        img = self.tmp / "gray.png"
        _thirds(img, "#202020", "#808080", "#d0d0d0")
        for c in extract_colors(img):
            self.assertLess(oklab.hex_to_lch(c)[1], NEUTRAL_C)

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
        # Light mode remaps only lightness: hues come from the cover either way,
        # and every slot lands in the light envelope.
        img = self.tmp / "lm.png"
        _thirds(img, "#e01010", "#10e010", "#1010e0")
        dark = extract_colors(img, mode="dark")
        light = extract_colors(img, mode="light")
        for cd, cl in zip(dark, light):
            self.assertAlmostEqual(oklab.hex_to_lch(cd)[2],
                                   oklab.hex_to_lch(cl)[2], places=1)
        lo, _ = ENVELOPES["light"]
        for cl in light:
            self.assertGreaterEqual(oklab.hex_to_lch(cl)[0], lo - 1e-9)

    def test_small_vivid_accent_makes_the_palette(self):
        # The dominance failure mode: three drab regions own the pixel count,
        # a small vivid logo owns the identity. The accent must land a slot.
        img = self.tmp / "accent.png"
        _thirds(img, "#202a33", "#332028", "#2a3320")  # drab blue/plum/olive
        base = Image.open(img).convert("RGB")
        base.paste(Image.new("RGB", (10, 10), _rgb("#ff6a00")), (27, 27))
        base.save(img)
        accent_hue = _hsv("#ff6a00")[0]
        hues = [_hsv(c)[0] for c in extract_colors(img)]
        self.assertTrue(any(abs(h - accent_hue) < 0.04 for h in hues),
                        f"orange accent missing from {hues}")

    def test_solid_cover_repeats_not_invents(self):
        # A truly solid cover has one color; slots repeat it rather than fabricate.
        img = self.tmp / "solid.png"
        _solid(img, "#c81e5a")
        c1, c2, c3 = extract_colors(img)
        self.assertEqual(c1, c2)
        self.assertEqual(c2, c3)


# Widest Oklab hue shift attributable purely to 8-bit output quantization,
# measured across this fixture in both modes (worst observed 0.0073 rad). This
# is 0.16% of the hue circle — about three times tighter than the tolerance the
# original HSV form of this test used, and it measures the right quantity.
HUE_QUANTIZATION_TOLERANCE = 0.01


class ModeIndependenceTest(unittest.TestCase):
    """Selection must run once, mode-free; only the lightness envelope moves.
    Re-selecting per mode can swap in a different cover color and change a hue
    on a theme flip."""

    def test_selection_takes_no_mode_at_all(self):
        # The invariant held structurally: if _select cannot see the mode, it
        # cannot vary by it. Stronger than any behavioral sample.
        import inspect
        self.assertNotIn("mode", inspect.signature(colors._select).parameters)

    def test_both_modes_tone_the_same_picked_hues(self):
        # Asserted in Oklab hue, which the pipeline holds genuinely fixed. The
        # original compared HSV hue, but a fixed Oklab hue converts back to
        # different HSV hues at different lightnesses — measured drift 0.0286
        # against its own 0.005 tolerance. The old metric reported a hue change
        # that never happened; this one measures what actually must not move.
        hist = [
            (100, (0.70, 0.60, 0.30)),  # dominant purple, dark
            (50, (0.70, 0.60, 0.62)),   # same hue, brighter
            (10, (0.10, 0.80, 0.50)),   # orange that sneaks in if light re-selects
        ]
        picks, _ = colors._select(hist)
        with mock.patch.object(colors, "_histogram", return_value=hist):
            dark = extract_colors(Path("unused"), mode="dark")
            light = extract_colors(Path("unused"), mode="light")
        for src, cd, cl in zip(picks, dark, light):
            for out in (cd, cl):
                with self.subTest(color=out):
                    delta = abs(oklab.hex_to_lch(out)[2] - src[2])
                    self.assertLess(delta, HUE_QUANTIZATION_TOLERANCE)


class PipelineInvariantTest(unittest.TestCase):
    """Properties of the whole extract path that no single stage owns."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_distinct_cover_yields_separated_slots(self):
        img = self.tmp / "d.png"
        _thirds(img, "#d1a973", "#b89265", "#9d7156")   # three tans: a collision
        labs = [oklab.from_lch(*oklab.hex_to_lch(c)) for c in extract_colors(img)]
        for i in range(3):
            for j in range(i + 1, 3):
                with self.subTest(pair=(i, j)):
                    self.assertGreaterEqual(oklab.delta_e(labs[i], labs[j]),
                                            MIN_DE - 1e-9)

    def test_bright_cover_no_longer_washes_out(self):
        # The defect this work exists to fix, asserted end to end.
        img = self.tmp / "bright.png"
        _thirds(img, "#f2f2f2", "#e8e8e8", "#fafafa")
        self.assertLess(ramp.mean_luminance(*extract_colors(img, mode="dark")), 0.25)

    def test_every_slot_is_in_gamut(self):
        # Assert on the pipeline's own pre-quantization slots, not on re-decoded
        # hex: lch_to_hex clamps into sRGB before quantizing, so a hex round-trip
        # is in-gamut by construction and would pass even with chroma pushed far
        # past the ceiling. The second clause pins that the hex we emit really
        # does represent the slot we computed.
        img = self.tmp / "g.png"
        _thirds(img, "#ffee00", "#00e5ff", "#1010e0")   # low-ceiling hues
        for mode in ("dark", "light"):
            picked, n = colors._select(colors._histogram(img))
            slots, _ = separate(tone(picked, mode), mode, n)
            for s in slots:
                with self.subTest(mode=mode, slot=s.to_hex()):
                    self.assertTrue(oklab.in_gamut(*s.to_lab()))
                    self.assertLess(
                        oklab.delta_e(s.to_lab(),
                                      oklab.from_lch(*oklab.hex_to_lch(s.to_hex()))),
                        0.005)


DEFAULT_ACCENT = "#a48ec7"  # the pathological/rejected fallback triple


class FormatAllowlistTest(unittest.TestCase):
    """SEC-005: only JPEG/PNG/WebP *content* (by signature, not the file
    extension) may be decoded. Anything else — HTML mislabeled as .jpg, SVG,
    PDF, or a truncated header — must return the safe default triple without
    ever reaching a decoder, rather than crashing or rendering."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _assert_decoded(self, path):
        result = extract_colors(path)
        # A real image decodes to its own colors, never the purple fallback.
        self.assertNotEqual(result, (DEFAULT_ACCENT,) * 3)
        for c in result:
            self.assertRegex(c, r"^#[0-9a-f]{6}$")

    def test_accepts_png(self):
        p = self.tmp / "cover.png"
        Image.new("RGB", (64, 64), (224, 16, 80)).save(p, "PNG")
        self._assert_decoded(p)

    def test_accepts_jpeg(self):
        p = self.tmp / "cover.jpg"
        Image.new("RGB", (64, 64), (224, 16, 80)).save(p, "JPEG")
        self._assert_decoded(p)

    def test_accepts_webp(self):
        p = self.tmp / "cover.webp"
        Image.new("RGB", (64, 64), (224, 16, 80)).save(p, "WEBP")
        self._assert_decoded(p)

    def test_rejects_html_renamed_as_jpg(self):
        p = self.tmp / "evil.jpg"
        p.write_bytes(b"<!DOCTYPE html>\n<html><body>not an image</body></html>")
        self.assertEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)

    def test_rejects_svg(self):
        # ImageMagick would rasterize this via a delegate; the allowlist must not.
        p = self.tmp / "vector.svg"
        p.write_bytes(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
            b'<rect width="64" height="64" fill="#e01050"/></svg>')
        self.assertEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)

    def test_rejects_pdf(self):
        p = self.tmp / "doc.pdf"
        p.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF")
        self.assertEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)

    def test_rejects_truncated_png(self):
        # Valid PNG signature, then garbage: identified as PNG but undecodable.
        p = self.tmp / "broken.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        self.assertEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)


class DecodeBoundsTest(unittest.TestCase):
    """SEC-006: in-process containment for the Pillow decode path — an
    oversized-dimension image (a decompression bomb) and an oversized file are
    refused before their pixels are decoded, so a malicious cover cannot exhaust
    memory. Normal and multi-frame covers still extract."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rejects_oversized_dimensions(self):
        # Valid PNG, but its header declares more pixels than the decode budget.
        # The guard must reject it from the header, before decoding the body.
        p = self.tmp / "bomb.png"
        Image.new("RGB", (4100, 4100), (224, 16, 80)).save(p, "PNG")  # ~16.8 MP
        self.assertGreater(4100 * 4100, colors._MAX_PIXELS)
        self.assertEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)

    def test_rejects_oversized_file_bytes(self):
        # A file larger than the byte budget is refused before Image.open, so a
        # huge local cover cannot be streamed into the decoder.
        p = self.tmp / "huge.png"
        Image.new("RGB", (64, 64), (224, 16, 80)).save(p, "PNG")
        with mock.patch.object(colors, "_MAX_DECODE_BYTES", 8):
            self.assertEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)

    def test_within_budget_still_extracts(self):
        # A normal-sized cover stays under both budgets and extracts a palette.
        p = self.tmp / "ok.png"
        Image.new("RGB", (640, 640), (224, 16, 80)).save(p, "PNG")
        self.assertNotEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)

    def test_animated_webp_decodes_first_frame(self):
        # Multi-frame content is bounded to its first frame; it must not crash
        # or iterate every frame.
        p = self.tmp / "anim.webp"
        frame1 = Image.new("RGB", (64, 64), (224, 16, 80))
        frame2 = Image.new("RGB", (64, 64), (16, 80, 224))
        frame1.save(p, "WEBP", save_all=True, append_images=[frame2], duration=100)
        self.assertNotEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)


class ColorDataBoundsTest(unittest.TestCase):
    """SEC-019: the quantized color data is explicitly bounded and diagnosable.
    A cover that yields no extractable colors must fall back to the default
    *observably* (not silently), degenerate quantizer output must not crash
    extraction, and CMYK / alpha-bearing covers must still produce valid sRGB
    palettes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _valid_hex(self, colors_tuple):
        for c in colors_tuple:
            self.assertRegex(c, r"^#[0-9a-f]{6}$")

    def test_unextractable_cover_logs_default_fallback(self):
        # A cover with no extractable colors returns the default, but must say
        # so on the log rather than masquerading as a valid empty image.
        p = self.tmp / "evil.jpg"
        p.write_bytes(b"<!DOCTYPE html><html>not an image</html>")
        with self.assertLogs("mpris_chroma.colors", level="WARNING"):
            result = extract_colors(p)
        self.assertEqual(result, (DEFAULT_ACCENT,) * 3)

    def test_degenerate_quantizer_output_does_not_crash(self):
        # If the quantizer ever returns no colors (a documented Pillow API
        # result when a palette is empty), extraction must fall back to the
        # default, not raise while unpacking None.
        p = self.tmp / "ok.png"
        Image.new("RGB", (64, 64), (224, 16, 80)).save(p, "PNG")

        class _Empty:
            def getpalette(self):
                return []

            def getcolors(self):
                return None

        with mock.patch("PIL.Image.Image.quantize", return_value=_Empty()):
            self.assertEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)

    def test_cmyk_jpeg_yields_valid_srgb_palette(self):
        p = self.tmp / "cmyk.jpg"
        Image.new("CMYK", (64, 64), (0, 255, 255, 0)).save(p, "JPEG")  # red-ish
        result = extract_colors(p)
        self.assertNotEqual(result, (DEFAULT_ACCENT,) * 3)
        self._valid_hex(result)

    def test_rgba_png_yields_valid_palette(self):
        p = self.tmp / "rgba.png"
        Image.new("RGBA", (64, 64), (224, 16, 80, 255)).save(p, "PNG")
        result = extract_colors(p)
        self.assertNotEqual(result, (DEFAULT_ACCENT,) * 3)
        self._valid_hex(result)

    def test_dash_prefixed_path_is_a_file_not_an_option(self):
        # SEC-016: pre-migration a leading-dash path could be read as an
        # ImageMagick option; Pillow opens it as a plain file path, so the
        # option-injection risk is structurally gone.
        p = self.tmp / "-dash.png"
        Image.new("RGB", (64, 64), (224, 16, 80)).save(p, "PNG")
        self.assertNotEqual(extract_colors(p), (DEFAULT_ACCENT,) * 3)


_CORPUS_DIRS = [Path.home() / ".local/share/jellyfin-tui/covers",
                Path.home() / ".cache/mpris-chroma/covers"]


def _corpus() -> list[Path]:
    return [p for d in _CORPUS_DIRS if d.is_dir()
            for p in sorted(d.glob("*")) if p.is_file()]


@unittest.skipUnless(len(_corpus()) >= 20,
                     "real cover corpus not present (opt-in)")
class CorpusTest(unittest.TestCase):
    """Aggregate behavior over real covers. Unit tests pin properties of a
    single palette; only the corpus catches a constant that quietly wrecks the
    distribution — the exact failure that motivated this work."""

    @classmethod
    def setUpClass(cls):
        cls.covers = _corpus()
        cls.dark = [extract_colors(p, mode="dark") for p in cls.covers]
        cls.light = [extract_colors(p, mode="light") for p in cls.covers]

    def test_dark_median_luminance_is_near_the_reference(self):
        reference = ramp.mean_luminance("#120C14", "#4A2F5C", "#6D8F4F")
        lums = sorted(ramp.mean_luminance(*c) for c in self.dark)
        median = lums[len(lums) // 2]
        self.assertLess(median, reference * 2.0)
        self.assertGreater(median, reference * 0.3)

    def test_dark_mode_still_distinguishes_bright_from_dark_covers(self):
        # Compression, not normalization: if p90 collapses onto p10 the palette
        # has stopped responding to the cover at all.
        lums = sorted(ramp.mean_luminance(*c) for c in self.dark)
        self.assertGreater(lums[9 * len(lums) // 10], lums[len(lums) // 10] * 2)

    def test_light_is_brighter_than_dark_for_every_cover(self):
        for cover, d, l in zip(self.covers, self.dark, self.light):
            with self.subTest(cover=cover.name):
                self.assertGreater(ramp.mean_luminance(*l),
                                   ramp.mean_luminance(*d))

    def test_light_mode_does_not_cluster_at_the_top(self):
        # The defect the old BANDS["light"] = 0.70-0.97 had: every cover crushed
        # into one bright band, indistinguishable from each other.
        lums = sorted(ramp.mean_luminance(*c) for c in self.light)
        spread = lums[9 * len(lums) // 10] - lums[len(lums) // 10]
        self.assertGreater(spread, 0.10)

    def test_hues_survive_the_mode_flip(self):
        for cover, d, l in zip(self.covers, self.dark, self.light):
            for cd, cl in zip(d, l):
                with self.subTest(cover=cover.name):
                    hd = oklab.hex_to_lch(cd)[2]
                    hl = oklab.hex_to_lch(cl)[2]
                    if oklab.hex_to_lch(cd)[1] < NEUTRAL_C:
                        continue   # a neutral has no meaningful hue angle
                    delta = abs((hd - hl + math.pi) % (2 * math.pi) - math.pi)
                    self.assertLess(delta, 0.05)


if __name__ == "__main__":
    unittest.main()
