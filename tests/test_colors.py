import colorsys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from mpris_chroma import colors
from mpris_chroma.colors import (
    clamp_hsv, hex_of, extract_colors, S_MIN, V_MIN, V_MAX, NEUTRAL_S,
    BANDS, VIBRANCY_WEIGHT, VIBRANCY_MIN_POP, _vibrancy_score,
)


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


if __name__ == "__main__":
    unittest.main()
