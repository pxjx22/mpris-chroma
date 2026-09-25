"""Dump golden fixtures from the Python pipeline for the Rust port's parity tests.

The Rust crate in rust/ ports mpris_chroma module by module; its unit tests
port the Python tests, but those check properties, not exact values. This
script records what the Python code actually outputs over a seeded corpus, so
`cargo test` can prove the port matches it — hex output exactly, floats to a
tight tolerance (libm cbrt/pow/hypot may differ by an ulp between runtimes).

Only pure modules are exercised (oklab, tone, ramp), so Pillow is not needed.
`render` cases replicate colors.render_palette (tone -> separate -> to_hex)
without importing colors.py, which pulls in Pillow at import time.

Run from the repo root and commit the output:

    python tools/dump_golden.py > rust/tests/fixtures/color_golden.json
"""

import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpris_chroma import oklab, ramp, tone  # noqa: E402

SEED = 20260925
MODES = ("dark", "light")


def _rand_hex(rng):
    return "#%06x" % rng.randrange(0x1000000)


def _near_hex(rng, base, spread):
    """A color within `spread` 8-bit steps of `base` per channel, to produce
    the near-collisions that exercise separate()'s repair loop."""
    r, g, b = (int(base[i:i + 2], 16) for i in (1, 3, 5))
    jitter = lambda c: min(255, max(0, c + rng.randint(-spread, spread)))
    return "#%02x%02x%02x" % (jitter(r), jitter(g), jitter(b))


def _hexes(rng):
    fixed = ["#000000", "#ffffff", "#808080", "#010101", "#fefefe",
             "#ff0000", "#00ff00", "#0000ff", "#ffff00", "#00ffff",
             "#ff00ff", "#e01050", "#0284c4", "#d1a973", "#120c14"]
    return fixed + [_rand_hex(rng) for _ in range(185)]


def _max_chroma_cases():
    cases = []
    for i in range(21):
        L = i / 20
        for k in range(12):
            h = -math.pi + k * (2 * math.pi / 12)
            cases.append({"L": L, "h": h, "max_chroma": oklab.max_chroma(L, h)})
    return cases


def _render_cases(rng):
    """(picks, n_distinct) pairs shaped like colors._select's output: up to
    three source LCh picks, padded by repeating the last real one."""
    cases = []
    for i in range(240):
        kind = i % 4
        if kind == 0:      # unrelated colors: usually already clear
            hexes = [_rand_hex(rng) for _ in range(3)]
        elif kind == 1:    # one hue family: forces separation
            base = _rand_hex(rng)
            hexes = [base] + [_near_hex(rng, base, 24) for _ in range(2)]
        elif kind == 2:    # near-monochrome: budget/blocked/passes territory
            base = _rand_hex(rng)
            hexes = [base] + [_near_hex(rng, base, 4) for _ in range(2)]
        else:              # fewer than three real picks
            n = rng.choice((1, 2))
            base = _rand_hex(rng)
            hexes = [base] + [_near_hex(rng, base, 30) for _ in range(n - 1)]
        n_distinct = len(hexes)
        picks = [oklab.hex_to_lch(h) for h in hexes]
        picks += [picks[-1]] * (3 - n_distinct)
        for mode in MODES:
            toned = tone.tone(picks, mode)
            slots, result = tone.separate(toned, mode, n_distinct)
            cases.append({
                "source_hex": hexes,
                "picks": picks,
                "n_distinct": n_distinct,
                "mode": mode,
                "toned_L": [s.L for s in toned],
                "separated_L": [s.L for s in slots],
                "hex": [s.to_hex() for s in slots],
                "resolved": result.resolved,
                "reason": result.reason,
                "residual_de": result.residual_de,
            })
    return cases


def _separate_cases(rng):
    """Raw Toned input straight into separate(), including out-of-envelope
    lightness, which tone() never produces but separate() must still tame."""
    cases = []
    for _ in range(160):
        mode = rng.choice(MODES)
        lo, hi = tone.ENVELOPES[mode]
        center = rng.uniform(lo - 0.08, hi + 0.08)
        slots = [tone.Toned(L=center + rng.uniform(-0.03, 0.03),
                            h=rng.uniform(-math.pi, math.pi),
                            c_src=rng.choice((0.005, 0.03, 0.08, 0.2)))
                 for _ in range(3)]
        n_distinct = rng.choice((2, 3, 3))
        out, result = tone.separate(slots, mode, n_distinct)
        cases.append({
            "mode": mode,
            "n_distinct": n_distinct,
            "input": [[s.L, s.h, s.c_src] for s in slots],
            "separated_L": [s.L for s in out],
            "hex": [s.to_hex() for s in out],
            "resolved": result.resolved,
            "reason": result.reason,
            "residual_de": result.residual_de,
        })
    return cases


def _ramp_cases(rng):
    cases = []
    for _ in range(60):
        palette = [_rand_hex(rng) for _ in range(3)]
        cases.append({"palette": palette,
                      "cells": ramp.expand(*palette),
                      "mean_luminance": ramp.mean_luminance(*palette)})
    # Exact .5 blend ties: where half-away and ties-to-even disagree.
    for palette in (["#010100", "#000001", "#000000"],
                    ["#030303", "#000000", "#050505"]):
        cases.append({"palette": palette,
                      "cells": ramp.expand(*palette),
                      "mean_luminance": ramp.mean_luminance(*palette)})
    return cases


def main():
    rng = random.Random(SEED)
    hexes = _hexes(rng)
    golden = {
        "generator": "tools/dump_golden.py",
        "seed": SEED,
        "hex": [{"hex": h, "lch": oklab.hex_to_lch(h),
                 "round_trip": oklab.lch_to_hex(*oklab.hex_to_lch(h))}
                for h in hexes],
        "max_chroma": _max_chroma_cases(),
        "render": _render_cases(rng),
        "separate": _separate_cases(rng),
        "ramp": _ramp_cases(rng),
    }
    json.dump(golden, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
