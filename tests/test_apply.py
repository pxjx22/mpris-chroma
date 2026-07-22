import tempfile
import unittest
from pathlib import Path

from mpris_chroma.apply import (
    apply_wlchroma,
    revert_wlchroma,
    _config_palette,
    FADE_MS,
)

CONFIG = """\
version = 2
[effect]
name = "colormix"
[effect.settings]
palette = ["#120C14", "#4A2F5C", "#6D8F4F"]
"""


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))

        class R:
            returncode = 0

        return R()


class ApplyTest(unittest.TestCase):
    def test_apply_wlchroma_calls_set_colors_with_fade(self):
        rec = Rec()
        apply_wlchroma("#aa0000", "#00bb00", "#0000bb", ctl="CTL", run=rec)
        self.assertEqual(
            rec.calls,
            [["CTL", "set-colors", "#aa0000", "#00bb00", "#0000bb", str(FADE_MS)]],
        )

    def test_apply_wlchroma_zero_fade_omits_arg(self):
        # fade_ms=0 means instant; the trailing arg must not be sent.
        rec = Rec()
        apply_wlchroma("#aa0000", "#00bb00", "#0000bb", fade_ms=0, ctl="CTL", run=rec)
        self.assertEqual(
            rec.calls, [["CTL", "set-colors", "#aa0000", "#00bb00", "#0000bb"]]
        )

    def test_revert_fades_to_config_palette(self):
        # Revert reads the live config preset and glides back with the same
        # fade as apply, so a closed player settles smoothly, not with a snap.
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text(CONFIG)
            rec = Rec()
            revert_wlchroma(ctl="CTL", run=rec, config_path=cfg)
        self.assertEqual(
            rec.calls,
            [["CTL", "set-colors", "#120C14", "#4A2F5C", "#6D8F4F", str(FADE_MS)]],
        )

    def test_revert_falls_back_to_witch_hour_when_config_unreadable(self):
        # A missing/malformed config must not crash the daemon mid-revert; it
        # falls back to the named default palette.
        rec = Rec()
        revert_wlchroma(ctl="CTL", run=rec, config_path=Path("/no/such/config.toml"))
        self.assertEqual(rec.calls, [["CTL", "set-palette", "witch_hour"]])


def _palette_body(palette_toml: str) -> str:
    return f"version = 2\n[effect]\nname = \"colormix\"\n[effect.settings]\npalette = {palette_toml}\n"


class ConfigPaletteValidationTest(unittest.TestCase):
    """SEC-008: _config_palette must return three well-formed '#rrggbb' hex
    strings or None. Anything else (wrong type/length/charset, or a value
    bearing whitespace or a newline that could inject an extra wlchroma-ctl
    IPC line) yields None so the caller reverts to a safe default."""

    def _palette_from(self, raw: bytes) -> object:
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_bytes(raw)
            return _config_palette(cfg)

    def _from_toml(self, palette_toml: str) -> object:
        return self._palette_from(_palette_body(palette_toml).encode())

    def test_valid_lowercase_hex_accepted(self):
        self.assertEqual(
            self._from_toml('["#aa0000", "#00bb00", "#0000cc"]'),
            ("#aa0000", "#00bb00", "#0000cc"),
        )

    def test_valid_uppercase_hex_accepted_and_case_preserved(self):
        self.assertEqual(
            self._from_toml('["#120C14", "#4A2F5C", "#6D8F4F"]'),
            ("#120C14", "#4A2F5C", "#6D8F4F"),
        )

    def test_integer_elements_rejected(self):
        self.assertIsNone(self._from_toml("[1, 2, 3]"))

    def test_float_elements_rejected(self):
        self.assertIsNone(self._from_toml("[1.0, 2.0, 3.0]"))

    def test_table_element_rejected(self):
        self.assertIsNone(self._from_toml('["#aa0000", "#00bb00", {x = 1}]'))

    def test_nested_array_element_rejected(self):
        self.assertIsNone(self._from_toml('["#aa0000", "#00bb00", ["#0000cc"]]'))

    def test_short_hex_rejected(self):
        self.assertIsNone(self._from_toml('["#aa000", "#00bb00", "#0000cc"]'))

    def test_long_hex_rejected(self):
        self.assertIsNone(self._from_toml('["#aa00000", "#00bb00", "#0000cc"]'))

    def test_hashless_hex_rejected(self):
        self.assertIsNone(self._from_toml('["aa0000", "#00bb00", "#0000cc"]'))

    def test_non_hex_charset_rejected(self):
        self.assertIsNone(self._from_toml('["#gg0000", "#00bb00", "#0000cc"]'))

    def test_whitespace_bearing_hex_rejected(self):
        self.assertIsNone(self._from_toml('["#aa0000 ", "#00bb00", "#0000cc"]'))

    def test_multiline_hex_rejected(self):
        # A trailing newline must not slip past validation: wlchroma-ctl joins
        # argv into one whitespace-delimited IPC line, so an embedded newline
        # could inject a second protocol line.
        self.assertIsNone(self._from_toml('["#aa0000\\n", "#00bb00", "#0000cc"]'))

    def test_wrong_length_list_rejected(self):
        self.assertIsNone(self._from_toml('["#aa0000", "#00bb00"]'))

    def test_invalid_utf8_returns_none_without_crashing(self):
        # tomllib decodes as UTF-8; a lone 0xFF byte raises UnicodeDecodeError,
        # which must be contained rather than crash the daemon mid-revert.
        self.assertIsNone(self._palette_from(b"palette = \xff\xff\n"))


class RevertMalformedPaletteTest(unittest.TestCase):
    def test_revert_falls_back_when_palette_element_invalid(self):
        # An invalid element (int) must not reach wlchroma-ctl; revert uses the
        # named default palette instead, without crashing.
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text(_palette_body("[1, 2, 3]"))
            rec = Rec()
            revert_wlchroma(ctl="CTL", run=rec, config_path=cfg)
        self.assertEqual(rec.calls, [["CTL", "set-palette", "witch_hour"]])


if __name__ == "__main__":
    unittest.main()
