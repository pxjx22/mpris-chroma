import tempfile
import unittest
from pathlib import Path

from mpris_chroma.apply import apply_wlchroma, revert_wlchroma, FADE_MS

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


if __name__ == "__main__":
    unittest.main()
