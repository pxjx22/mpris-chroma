# Palette Toning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the uniform HSV value clamp that makes dark-mode palettes wash out with per-mode Oklab toning — lightness compressed across covers, preserved within a cover — plus gamut-aware chroma and a bounded perceptual-distinctness repair.

**Architecture:** Four stages in a pipeline; mode enters only at stage 3. `histogram → select (mode-independent) → tone(mode) → separate(mode)`. Stages 3 and 4 are pure functions over typed records with no image I/O, so they unit-test directly. The color-space math lives in its own dependency-free module, and wlchroma's 12-cell ramp expansion is modelled separately so the corpus test and the lab measure the same thing the shader renders.

**Tech Stack:** Python 3.11+, stdlib only for the new math (`math`, `dataclasses`, `colorsys`), Pillow for decoding (already a dependency), `unittest` for tests. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-27-palette-quality-design.md` — read §5 and §6 before Task 2.

## Global Constraints

- **No new dependencies.** stdlib + Pillow only. The Oklab transforms are ~40 lines of arithmetic; do not add `colour-science`, `numpy`, or similar.
- **Python 3.11+**; the repo uses `dataclass(frozen=True, slots=True)` for typed records (see `mpris_chroma/state.py`) — follow it.
- **Tests are thread-free `unittest`**, run by `python -m unittest discover -s tests`. No pytest, no threads in tests.
- **Hue is never modified** by toning, gamut mapping, or separation, in any mode.
- **Selection stays mode-independent.** Stages 1–2 never receive `mode`.
- **These test classes must not be edited and must still pass:** `VibrancyScoreTest`, `FormatAllowlistTest`, `DecodeBoundsTest`, `ColorDataBoundsTest`, and these `ExtractTest` methods: `test_extract_returns_three_valid_hex`, `test_two_tone_cover_yields_distinct_hues`, `test_three_color_cover_yields_three_distinct_slots`, `test_small_vivid_accent_makes_the_palette`, `test_solid_cover_repeats_not_invents`.
- **`test_mode_switch_never_changes_which_colors_are_picked` is restated, not frozen** (spec §8). Its HSV-hue comparison cannot survive Oklab toning — measured drift 0.0286 against a 0.005 tolerance, while Oklab hue drift is exactly 0. Task 5 replaces it with a stricter Oklab-hue assertion. Do not attempt to make the HSV form pass.
- **The undecodable-cover fallback is unchanged:** `("#a48ec7", "#a48ec7", "#a48ec7")`, logged at WARNING (SEC-005/SEC-019).
- **Library-logger pattern:** every new module that logs uses `logging.getLogger("mpris_chroma.<name>")` plus `addHandler(logging.NullHandler())`, matching `colors.py:10`.
- **Comment style:** the repo explains *why*, not *what*. Match the density of `colors.py`.
- **Baseline:** 224 tests green (221 default + 3 opt-in skipped). Never commit with the suite red.

---

## File Structure

| File | Responsibility |
|---|---|
| `mpris_chroma/oklab.py` | **Create.** Color space only: sRGB↔Oklab, LCh polar form, sRGB gamut chroma ceiling, ΔE. No project knowledge. |
| `mpris_chroma/tone.py` | **Create.** Stages 3–4: the `Toned` record, `tone()`, `separate()`, and every tuning constant from spec §5. Pure; no image I/O. |
| `mpris_chroma/ramp.py` | **Create.** Models wlchroma's `buildPalette` 12-cell expansion and its mean luminance, so the corpus test and the lab measure what the shader renders. |
| `mpris_chroma/colors.py` | **Modify.** Keeps histogram + selection + orchestration. Loses `clamp_hsv`, `BANDS`, `S_MIN`, `V_MIN`, `V_MAX`, `NEUTRAL_S`, `COLOR_MIN_DIST`. |
| `tools/palette_lab.py` | **Create.** Offline A/B walker over the real cover corpus. |
| `tests/test_oklab.py` | **Create.** Color-space properties. |
| `tests/test_tone.py` | **Create.** Toning + separation properties. |
| `tests/test_ramp.py` | **Create.** Ramp expansion matches `palette.zig`. |
| `tests/test_colors.py` | **Modify.** Replace `ClampTest`/`ModeBandTest`; restate 3 `ExtractTest` methods; add the opt-in corpus test. |
| `README.md` | **Modify.** Tuning table, ranking description, new Theme switching section. |

---

## Task 1: Oklab color space

**Files:**
- Create: `mpris_chroma/oklab.py`
- Test: `tests/test_oklab.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `srgb_to_oklab(r,g,b) -> (L,a,b)`, `oklab_to_srgb(L,a,b) -> (r,g,b)` (linear-clamped sRGB 0–1), `to_lch(L,a,b) -> (L,C,h)`, `from_lch(L,C,h) -> (L,a,b)`, `max_chroma(L,h) -> float`, `delta_e(lab1,lab2) -> float`, `hex_to_lch(s) -> (L,C,h)`, `lch_to_hex(L,C,h) -> str`, `in_gamut(L,a,b) -> bool`. All floats; hue in radians.

- [ ] **Step 1: Write the failing test**

Create `tests/test_oklab.py`:

```python
import math
import unittest

from mpris_chroma import oklab


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
                self.assertEqual(oklab.lch_to_hex(*oklab.hex_to_lch(hexc)), hexc)


class LightnessTest(unittest.TestCase):
    def test_black_and_white_anchor_the_scale(self):
        self.assertAlmostEqual(oklab.srgb_to_oklab(0.0, 0.0, 0.0)[0], 0.0, places=6)
        self.assertAlmostEqual(oklab.srgb_to_oklab(1.0, 1.0, 1.0)[0], 1.0, places=6)

    def test_lightness_is_perceptual_not_hsv_value(self):
        # The premise of the whole design: #d9d9d9 and #0284c4 share HSV value
        # ~0.8 but are nowhere near each other in apparent lightness.
        grey = oklab.hex_to_lch("#d9d9d9")[0]
        blue = oklab.hex_to_lch("#0284c4")[0]
        self.assertGreater(grey - blue, 0.2)

    def test_neutral_has_zero_chroma(self):
        for hexc in ("#000000", "#808080", "#ffffff"):
            with self.subTest(hexc=hexc):
                self.assertLess(oklab.hex_to_lch(hexc)[1], 1e-6)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_oklab -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mpris_chroma.oklab'`

- [ ] **Step 3: Write minimal implementation**

Create `mpris_chroma/oklab.py`:

```python
"""Oklab / OkLCh color space.

Palette work needs three things HSV cannot give: a lightness axis that matches
apparent brightness (so a "band" means what it says), a hue angle that can be
held genuinely fixed while lightness moves, and a chroma axis whose sRGB ceiling
can be computed — without which "reduce chroma to the gamut boundary" is not
expressible. Transforms are Bjorn Ottosson's; no dependencies beyond `math`.

Hue is in radians throughout. sRGB channels are 0-1, not 0-255.
"""

import functools
import math

# The forward transform's cube roots are taken of non-negative linear-light
# values for real colors, but bisection in max_chroma probes outside the gamut
# where they can go slightly negative; copysign keeps that a real number instead
# of a domain error.
def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1 / 3), x)


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def srgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    r, g, b = _to_linear(r), _to_linear(g), _to_linear(b)
    l = _cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = _cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = _cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _to_linear_rgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    """Inverse transform stopping at linear light, so gamut tests can see
    out-of-range values before gamma encoding clamps them away."""
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def in_gamut(L: float, a: float, b: float) -> bool:
    """True iff the color is representable in sRGB. The epsilon absorbs the
    float error that would otherwise make an exactly-on-boundary color flicker
    in and out of gamut between calls."""
    return all(-1e-4 <= c <= 1 + 1e-4 for c in _to_linear_rgb(L, a, b))


def oklab_to_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    return tuple(min(1.0, max(0.0, _to_gamma(min(1.0, max(0.0, c)))))
                 for c in _to_linear_rgb(L, a, b))


def to_lch(L: float, a: float, b: float) -> tuple[float, float, float]:
    return L, math.hypot(a, b), math.atan2(b, a)


def from_lch(L: float, C: float, h: float) -> tuple[float, float, float]:
    return L, C * math.cos(h), C * math.sin(h)


# sRGB's most chromatic color sits near C=0.32 in Oklab, so 0.5 is a safe upper
# bracket; 24 bisection steps resolve to ~3e-8, far finer than 8-bit output.
_C_BRACKET = 0.5
_BISECTIONS = 24


# Memoized because `Toned.C` derives chroma from the *current* lightness on every
# access — the design that stops a stored chroma going stale when separation
# moves L — which makes this the hottest function in the pipeline. Within one
# palette the same (L, h) recurs constantly, so the hit rate is high. The cache
# is keyed on floats and is unbounded in principle, hence maxsize.
@functools.lru_cache(maxsize=4096)
def max_chroma(L: float, h: float) -> float:
    """Greatest chroma that is still in sRGB at this lightness and hue.

    The ceiling varies about threefold across hue at fixed lightness — blue
    reaches ~0.176 at L=0.25 where yellow manages ~0.058 — which is why chroma
    targets are expressed as a fraction of this value rather than absolutely.
    """
    lo, hi = 0.0, _C_BRACKET
    for _ in range(_BISECTIONS):
        mid = (lo + hi) / 2
        if in_gamut(*from_lch(L, mid, h)):
            lo = mid
        else:
            hi = mid
    return lo


def delta_e(lab1: tuple[float, float, float],
            lab2: tuple[float, float, float]) -> float:
    """Euclidean distance in Oklab. Oklab is designed so this approximates
    perceived difference, which plain RGB distance does not."""
    return math.dist(lab1, lab2)


def hex_to_lch(value: str) -> tuple[float, float, float]:
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return to_lch(*srgb_to_oklab(r, g, b))


def lch_to_hex(L: float, C: float, h: float) -> str:
    r, g, b = oklab_to_srgb(*from_lch(L, C, h))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_oklab -v`
Expected: PASS, 12 tests.

Then confirm nothing else broke: `python -m unittest discover -s tests`
Expected: `Ran 236 tests`, `OK (skipped=3)`

- [ ] **Step 5: Commit**

```bash
git add mpris_chroma/oklab.py tests/test_oklab.py
git commit -m "feat: add Oklab color space with sRGB gamut ceiling"
```

---

## Task 2: Toning — stage 3

**Files:**
- Create: `mpris_chroma/tone.py`
- Test: `tests/test_tone.py`

**Interfaces:**
- Consumes: everything from `mpris_chroma.oklab` (Task 1).
- Produces: `Toned` frozen dataclass with fields `L: float`, `h: float`, `c_src: float` and property `C: float`; `ENVELOPES: dict[str, tuple[float, float]]`; `GAMMA: dict[str, float]`; constants `SPREAD_GAIN`, `CHROMA_FRAC`, `NEUTRAL_C`; `chroma_for(L, h, c_src) -> float`; `tone(source_lch: list[tuple[float,float,float]], mode: str) -> list[Toned]`.

**Read first:** spec §5. `Toned.C` is a derived property on purpose — separation moves `L`, and deriving chroma from the current `L` means chroma can never go stale or fall out of gamut behind your back.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tone.py`:

```python
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

    def test_bright_cover_is_compressed_downward_in_dark_mode(self):
        # The washing defect: a near-white cover must not stay near-white.
        src = [_lch("#f2f2f2"), _lch("#e8e8e8"), _lch("#fafafa")]
        for slot in tone.tone(src, "dark"):
            self.assertLess(slot.L, 0.62)

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_tone -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mpris_chroma.tone'`

- [ ] **Step 3: Write minimal implementation**

Create `mpris_chroma/tone.py` (separation is added in Task 3 — this task stops at `tone`):

```python
"""Per-mode palette toning (spec §5).

The defect this replaces: a single HSV value band clamped all three slots into
one narrow window, so nothing could ever be dark, palettes had no depth, and the
band did not correspond to apparent brightness at all. Toning instead works in
Oklab and splits the problem along two independent axes — how bright this cover
is relative to other covers (compressed), and how its three slots relate to each
other (preserved).
"""

import math
from dataclasses import dataclass

from . import oklab

# Oklab lightness envelope per mode. Dark is fitted against the witch_hour
# reference palette and validated across the cover corpus; light is a
# provisional seed reasoned from symmetry, with no equivalent anchor to fit
# against, and is expected to move after visual review (spec §5).
ENVELOPES: dict[str, tuple[float, float]] = {
    "dark": (0.15, 0.55),
    "light": (0.55, 0.92),
}
# Compression exponent. Below 1 the top of the range compresses harder than the
# bottom, which is what pulls bright covers down without flattening dark ones;
# light inverts it so the bottom compresses instead.
GAMMA: dict[str, float] = {
    "dark": 0.85,
    "light": 1.18,
}
SPREAD_GAIN = 1.0     # k: how much of the cover's own lightness spread survives
CHROMA_FRAC = 0.85    # target chroma as a fraction of the in-gamut ceiling
NEUTRAL_C = 0.02      # below this a slot is genuinely grey and is left alone


def chroma_for(L: float, h: float, c_src: float) -> float:
    """Chroma for a slot at lightness L, given the source color's chroma.

    Expressed against the ceiling rather than as an absolute number because the
    ceiling varies about threefold across hue — an absolute target is
    simultaneously unreachable for cyan and unambitious for blue. A source that
    is already near-neutral is exempt, so a grayscale cover is never tinted.
    """
    ceiling = oklab.max_chroma(L, h)
    if c_src < NEUTRAL_C:
        return min(c_src, ceiling)
    return min(max(c_src, CHROMA_FRAC * ceiling), ceiling)


@dataclass(frozen=True, slots=True)
class Toned:
    """One toned palette slot.

    Chroma is derived rather than stored: separation moves `L`, and the in-gamut
    ceiling moves with it, so a stored chroma would silently fall out of gamut.
    `c_src` is the *source* color's chroma and is mode-independent.
    """

    L: float
    h: float
    c_src: float

    @property
    def C(self) -> float:
        return chroma_for(self.L, self.h, self.c_src)

    def to_hex(self) -> str:
        return oklab.lch_to_hex(self.L, self.C, self.h)

    def to_lab(self) -> tuple[float, float, float]:
        return oklab.from_lch(self.L, self.C, self.h)


def tone(source_lch: list[tuple[float, float, float]], mode: str) -> list[Toned]:
    """Map source OkLCh slots into the mode's envelope (spec §5).

    Cross-cover: the palette's mean lightness is the anchor, and it is pushed
    through a compressive curve, so a bright cover lands darker while still
    landing above a dark one. Within-cover: each slot keeps its own offset from
    that anchor, so a contrasty cover stays contrasty and a flat one stays flat.
    """
    lo, hi = ENVELOPES[mode]
    gamma = GAMMA[mode]
    anchor = sum(L for L, _, _ in source_lch) / len(source_lch)
    # anchor is a mean of Oklab lightnesses, so it is already in 0..1; the max()
    # only guards a negative float epsilon at pure black before fractional
    # exponentiation, which would otherwise be a domain error.
    toned_anchor = lo + (hi - lo) * (max(anchor, 0.0) ** gamma)
    out = []
    for L, C, h in source_lch:
        moved = toned_anchor + SPREAD_GAIN * (L - anchor)
        out.append(Toned(L=min(hi, max(lo, moved)), h=h, c_src=C))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_tone -v`
Expected: PASS, 15 tests.

Run: `python -m unittest discover -s tests`
Expected: `Ran 251 tests`, `OK (skipped=3)`

- [ ] **Step 5: Commit**

```bash
git add mpris_chroma/tone.py tests/test_tone.py
git commit -m "feat: add per-mode Oklab toning with ceiling-relative chroma"
```

---

## Task 3: Perceptual separation — stage 4

**Files:**
- Modify: `mpris_chroma/tone.py` (append; do not alter Task 2's code)
- Test: `tests/test_tone.py` (append)

**Interfaces:**
- Consumes: `Toned`, `ENVELOPES`, `chroma_for` from Task 2; `delta_e` from Task 1.
- Produces: `MIN_DE`, `SEPARATION_STEP`, `MAX_SEPARATION_SHIFT`, `MAX_SEPARATION_PASSES`; `SeparationResult` frozen dataclass with `resolved: bool`, `reason: str`, `residual_de: float`; `separate(slots: list[Toned], mode: str, n_distinct: int) -> tuple[list[Toned], SeparationResult]`.

**Read first:** spec §6, in particular the three-way clamp in step 2 and the duplicate carve-out. `reason` is one of `"clear"`, `"duplicates"`, `"envelope"`, `"budget"`, `"passes"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tone.py` (above the `if __name__` block):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_tone.SeparationTest -v`
Expected: FAIL — `AttributeError: module 'mpris_chroma.tone' has no attribute 'separate'`

- [ ] **Step 3: Write minimal implementation**

Append to `mpris_chroma/tone.py`:

```python
# Perceptual distinctness (spec §6). Toning alone does not fix collisions and
# slightly worsens them — compressing lightness pulls apart-in-L colors together
# — so the repair runs after toning, where the collision actually appears.
MIN_DE = 0.10                # pairs closer than this in Oklab read as duplicates
SEPARATION_STEP = 0.01       # per-pass lightness nudge
MAX_SEPARATION_SHIFT = 0.04  # total displacement any one slot may accumulate
MAX_SEPARATION_PASSES = 8    # loop safety net; the budget above binds first


@dataclass(frozen=True, slots=True)
class SeparationResult:
    """Why separation stopped. A palette that cannot be separated within budget
    is an accepted outcome — for a genuinely monochrome cover it is the correct
    one — but it must be observable rather than silent (SEC-019 precedent)."""

    resolved: bool
    reason: str          # clear | duplicates | envelope | budget | passes
    residual_de: float   # closest remaining pair; MIN_DE or more when resolved


def _closest_pair(slots: list[Toned]) -> tuple[int, int, float]:
    labs = [s.to_lab() for s in slots]
    best = (0, 1, float("inf"))
    for i in range(len(slots)):
        for j in range(i + 1, len(slots)):
            d = oklab.delta_e(labs[i], labs[j])
            if d < best[2]:
                best = (i, j, d)
    return best


def separate(slots: list[Toned], mode: str,
             n_distinct: int) -> tuple[list[Toned], SeparationResult]:
    """Push colliding slots apart in lightness only, within bounds (spec §6).

    Selection is never revisited — these are the same three cover colors, and
    hue is untouched. Only the *real* picks participate: when a cover yielded
    fewer than three distinct colors the extras are repeats, and separating them
    would invent contrast the cover does not have.
    """
    lo, hi = ENVELOPES[mode]
    if n_distinct < 2:
        return list(slots), SeparationResult(False, "duplicates", 0.0)

    work = list(slots[:n_distinct])
    # Rank order is fixed once, here, and held for the rest of the routine. Every
    # move is clamped against neighbours in this order, which is what makes
    # monotonicity hold by construction: pushing a pair apart preserves that
    # pair's order, but unclamped it could shove a slot past a *third* one.
    order = sorted(range(n_distinct), key=lambda i: (work[i].L, i))
    budget = [MAX_SEPARATION_SHIFT] * n_distinct
    reason = "passes"

    for _ in range(MAX_SEPARATION_PASSES):
        i, j, dist = _closest_pair(work)
        if dist >= MIN_DE:
            reason = "clear"
            break
        # Move the higher-ranked slot of the pair up and the lower one down.
        rank = {slot: pos for pos, slot in enumerate(order)}
        up, down = (i, j) if rank[i] > rank[j] else (j, i)
        moved = False
        for idx, direction in ((up, +1.0), (down, -1.0)):
            step = min(SEPARATION_STEP, budget[idx])
            if step <= 0.0:
                continue
            target = work[idx].L + direction * step
            # Three bounds: the envelope, the slot's remaining budget (already
            # applied via `step`), and its immediate neighbours' current
            # positions, so ranks can never swap.
            pos = rank[idx]
            if direction > 0 and pos + 1 < n_distinct:
                target = min(target, work[order[pos + 1]].L)
            if direction < 0 and pos - 1 >= 0:
                target = max(target, work[order[pos - 1]].L)
            target = min(hi, max(lo, target))
            delta = abs(target - work[idx].L)
            if delta <= 0.0:
                continue
            budget[idx] -= delta
            work[idx] = Toned(L=target, h=work[idx].h, c_src=work[idx].c_src)
            moved = True
        if not moved:
            # Nothing could move: either every slot is pinned at an envelope
            # bound or every budget is spent. Distinguish the two so the reason
            # is diagnostic rather than a catch-all.
            reason = "budget" if all(b <= 0.0 for b in budget) else "envelope"
            break

    residual = _closest_pair(work)[2]
    resolved = residual >= MIN_DE
    if resolved:
        reason = "clear"
    # Re-pad: a cover with fewer than three real colors repeats its last one,
    # and that repeat must keep tracking the slot it mirrors.
    out = work + [work[-1]] * (len(slots) - n_distinct)
    return out, SeparationResult(resolved, reason, residual)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_tone -v`
Expected: PASS, 27 tests (15 from Task 2, 12 new).

Run: `python -m unittest discover -s tests`
Expected: `Ran 263 tests`, `OK (skipped=3)`

- [ ] **Step 5: Commit**

```bash
git add mpris_chroma/tone.py tests/test_tone.py
git commit -m "feat: add bounded perceptual separation for colliding slots"
```

---

## Task 4: wlchroma ramp model

**Files:**
- Create: `mpris_chroma/ramp.py`
- Test: `tests/test_ramp.py`

**Interfaces:**
- Consumes: nothing (operates on hex strings).
- Produces: `expand(c1, c2, c3) -> list[str]` (12 hex cells), `mean_luminance(c1, c2, c3) -> float`.

**Why this exists:** wlchroma expands the three colors into 12 cells before rendering (`~/wlchroma/src/render/palette.zig:buildPalette`), so perceived screen brightness is the mean over that ramp, not over the three colors. The corpus test and the lab must both measure the thing the shader actually draws, and neither should carry its own copy of the blend math.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ramp.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_ramp -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mpris_chroma.ramp'`

- [ ] **Step 3: Write minimal implementation**

Create `mpris_chroma/ramp.py`:

```python
"""Model of wlchroma's palette expansion, for measurement only.

wlchroma turns the three colors we send into twelve cells before rendering
(`src/render/palette.zig:buildPalette`), blending consecutive pairs at four
alphas. Eight of the twelve are therefore midpoints, which is why apparent
screen brightness is the mean over this ramp and not over the three colors. The
daemon never calls this — it exists so the corpus test and the offline lab can
measure what the shader actually draws.
"""

from .oklab import _to_linear   # one sRGB gamma decode, defined once

# (foreground, background) index pairs and the alphas, mirroring buildPalette.
_PAIRS = ((0, 1), (1, 2), (2, 0))
_ALPHAS = (1.00, 0.72, 0.50, 0.28)


def _channels(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def expand(c1: str, c2: str, c3: str) -> list[str]:
    """The twelve cells wlchroma will render for this palette."""
    rgb = [_channels(c) for c in (c1, c2, c3)]
    cells = []
    for fg, bg in _PAIRS:
        for alpha in _ALPHAS:
            # Blended in 8-bit sRGB, matching palette.zig's blend() exactly —
            # deliberately not gamma-correct, because the point is to reproduce
            # what wlchroma does rather than what it ideally would do.
            mixed = (round(rgb[bg][k] * (1 - alpha) + rgb[fg][k] * alpha)
                     for k in range(3))
            cells.append("#%02x%02x%02x" % tuple(mixed))
    return cells


def mean_luminance(c1: str, c2: str, c3: str) -> float:
    """Mean relative luminance across the twelve rendered cells (0-1)."""
    total = 0.0
    for cell in expand(c1, c2, c3):
        r, g, b = (_to_linear(v / 255) for v in _channels(cell))
        total += 0.2126 * r + 0.7152 * g + 0.0722 * b
    return total / 12
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_ramp -v`
Expected: PASS, 7 tests.

Run: `python -m unittest discover -s tests`
Expected: `Ran 270 tests`, `OK (skipped=3)`

- [ ] **Step 5: Commit**

```bash
git add mpris_chroma/ramp.py tests/test_ramp.py
git commit -m "feat: model wlchroma's 12-cell ramp for measurement"
```

---

## Task 5: Wire the pipeline into colors.py

**Files:**
- Modify: `mpris_chroma/colors.py` (replace lines 13–31 constants, `clamp_hsv`, and the body of `extract_colors`)
- Modify: `tests/test_colors.py` (delete `ClampTest` and `ModeBandTest`; restate 3 `ExtractTest` methods)

**Interfaces:**
- Consumes: `tone.tone`, `tone.separate`, `tone.Toned` (Tasks 2–3); `oklab.hex_to_lch`, `oklab.srgb_to_oklab`, `oklab.to_lch`, `oklab.delta_e` (Task 1).
- Produces: `extract_colors(image_path, mode="dark") -> tuple[str, str, str]` — signature unchanged; `SELECT_MIN_DE`.

**Note — one constant the spec did not name.** Selection used `COLOR_MIN_DIST` (RGB distance on value-clamped colors) to avoid picking three near-identical histogram entries. The spec removes it and assigns distinctness to stage 4, but selection still needs *some* dedup or it will pick three shades of one color and hand stage 4 an unsolvable problem. This plan introduces `SELECT_MIN_DE = 0.08` — the same perceptual metric, applied to *source* colors so it stays mode-independent. Slightly below `MIN_DE` because toning compresses distances afterwards. Back-fill this into spec §4 when the task lands.

- [ ] **Step 1: Write the failing test**

In `tests/test_colors.py`: delete the `ClampTest` and `ModeBandTest` classes entirely, and delete `clamp_hsv, hex_of, S_MIN, V_MIN, V_MAX, NEUTRAL_S, BANDS` from the import block, leaving:

```python
from mpris_chroma import colors
from mpris_chroma.colors import (
    extract_colors, VIBRANCY_WEIGHT, VIBRANCY_MIN_POP, _vibrancy_score,
)
from mpris_chroma import oklab, ramp
from mpris_chroma.tone import ENVELOPES, NEUTRAL_C, MIN_DE
```

Replace the three `ExtractTest` methods that assert in HSV terms:

```python
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
```

Replace `test_mode_switch_never_changes_which_colors_are_picked` — the assertion
metric changes, the invariant it guards gets *stricter*:

```python
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
```

Delete the old `test_mode_switch_never_changes_which_colors_are_picked` from
`ExtractTest`; this class replaces it. Keep `mock` and `Path` imported — both
are already used elsewhere in the file.

Add a new class for the properties that only exist end-to-end:

```python
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
        img = self.tmp / "g.png"
        _thirds(img, "#ffee00", "#00e5ff", "#1010e0")   # low-ceiling hues
        for mode in ("dark", "light"):
            for c in extract_colors(img, mode=mode):
                with self.subTest(mode=mode, color=c):
                    self.assertTrue(oklab.in_gamut(*oklab.from_lch(*oklab.hex_to_lch(c))))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_colors -v`
Expected: FAIL — `ImportError: cannot import name 'oklab'`-adjacent failures, and `AttributeError` on the new invariants, because `extract_colors` still returns HSV-clamped colors.

- [ ] **Step 3: Write minimal implementation**

In `mpris_chroma/colors.py`: delete `S_MIN`, `V_MIN`, `V_MAX`, `NEUTRAL_S`, `COLOR_MIN_DIST`, `BANDS`, `clamp_hsv`, `hex_of`, and `_rgb_dist`. Keep `VIBRANCY_WEIGHT`, `VIBRANCY_MIN_POP`, `_vibrancy_score`, `_histogram` and every SEC guard untouched. Add to the imports:

```python
from . import oklab
from .tone import separate, tone
```

Add the selection constant next to the vibrancy ones:

```python
# Minimum perceptual distance between two *source* picks. Selection has to
# reject three shades of one color before toning, or stage 4 is handed a
# collision it cannot solve. Measured on source colors, so it is
# mode-independent — mode must only re-tone the same three picks, never change
# which ones they are. Below tone.MIN_DE because toning compresses distances.
SELECT_MIN_DE = 0.08
```

Add the selection stage as its own function. It is separate because three
callers need exactly it — `extract_colors`, the mode-independence test, and the
offline lab — and because taking no `mode` parameter is what makes
mode-independence structural rather than a property to be re-checked:

```python
def _select(hist: list[tuple[int, tuple[float, float, float]]]
            ) -> tuple[list[tuple[float, float, float]], int]:
    """Rank the histogram and pick up to three distinct source colors.

    Returns the picks as OkLCh plus how many of them are *real* — fewer than
    three means the cover had fewer distinct colors and the list was padded by
    repeating the last, which separation must know so it never gives a solid
    cover contrast it does not have.

    Deliberately takes no `mode`: mode may only re-tone the same three picks,
    never change which ones they are, and the cheapest way to guarantee that is
    to make it impossible to express.
    """
    total = sum(count for count, _ in hist)
    # Most apparent first, vibrancy-aware: coverage plus a chroma bonus, so a
    # small vivid accent (a logo, a face) can beat a large drab background.
    ranked = sorted(hist, key=lambda e: _vibrancy_score(e[0], total, e[1]),
                    reverse=True)
    picked: list[tuple[float, float, float]] = []
    for _, hsv in ranked:
        if len(picked) == 3:
            break
        lch = oklab.to_lch(*oklab.srgb_to_oklab(*colorsys.hsv_to_rgb(*hsv)))
        if all(oklab.delta_e(oklab.from_lch(*lch), oklab.from_lch(*p))
               >= SELECT_MIN_DE for p in picked):
            picked.append(lch)
    n_distinct = len(picked)
    # Repeat the last real color rather than fabricate a hue that isn't there.
    while len(picked) < 3:
        picked.append(picked[-1])
    return picked, n_distinct
```

Replace `extract_colors` with:

```python
def extract_colors(image_path: Path, mode: str = "dark") -> tuple[str, str, str]:
    """Extract the three most prominent, visibly distinct colors from an image.

    Faithful to the cover: colors are ranked by coverage plus a vibrancy bonus,
    then toned into the mode's Oklab lightness envelope — compressed relative to
    other covers, but keeping this cover's own spread — and finally pushed apart
    if two of them collide perceptually. No hues are ever invented; the three
    slots are real cover colors. If the cover has fewer than three distinct
    colors, the last one is repeated rather than fabricated.
    """
    hist = _histogram(image_path)
    if not hist:
        # Rejected format, undecodable data, or a genuine no-color image: fall
        # back to the default accent, but log it so a persistently unreadable
        # cover is diagnosable rather than a silent, wrong-looking palette.
        _log.warning("no extractable colors from %s; using default palette",
                     image_path.name)
        return "#a48ec7", "#a48ec7", "#a48ec7"

    picked, n_distinct = _select(hist)
    toned = tone(picked, mode)
    slots, result = separate(toned, mode, n_distinct)
    if not result.resolved and result.reason != "duplicates":
        # An unseparable palette is an accepted outcome, but never a silent one.
        _log.debug("palette for %s left a pair at dE %.3f (%s)",
                   image_path.name, result.residual_de, result.reason)
    return tuple(slot.to_hex() for slot in slots)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_colors -v`
Expected: PASS. Confirm the frozen tests are among them, especially
`test_mode_switch_never_changes_which_colors_are_picked` and
`test_solid_cover_repeats_not_invents`.

Run: `python -m unittest discover -s tests`
Expected: `Ran 264 tests` (10 removed with ClampTest/ModeBandTest, 1 moved out of ExtractTest, 5 added), `OK (skipped=3)`

Sanity-check the real corpus before committing:

```bash
python - <<'PY'
import statistics
from pathlib import Path
from mpris_chroma import ramp
from mpris_chroma.colors import extract_colors
dirs = [Path.home()/'.local/share/jellyfin-tui/covers',
        Path.home()/'.cache/mpris-chroma/covers']
lums = sorted(ramp.mean_luminance(*extract_colors(p, mode='dark'))
              for d in dirs for p in sorted(d.glob('*')) if p.is_file())
print("n=%d  p10=%.3f median=%.3f p90=%.3f  (witch_hour=%.3f)" % (
    len(lums), lums[len(lums)//10], statistics.median(lums),
    lums[9*len(lums)//10], ramp.mean_luminance('#120C14','#4A2F5C','#6D8F4F')))
PY
```

Expected: median near 0.07 and well under the current 0.215. If the median is above 0.12, stop and report rather than committing — the toning constants are not behaving as the spec measured.

- [ ] **Step 5: Commit**

```bash
git add mpris_chroma/colors.py tests/test_colors.py
git commit -m "feat: tone extracted palettes in Oklab per theme mode"
```

---

## Task 6: Opt-in corpus test

**Files:**
- Modify: `tests/test_colors.py` (append one class)

**Interfaces:**
- Consumes: `extract_colors` (Task 5), `ramp.mean_luminance` (Task 4).
- Produces: nothing consumed elsewhere.

**Why opt-in:** the corpus lives in the user's home directory, so a fresh clone or CI has no covers. This follows the 3 existing opt-in tests — skipped, not failed, when the data is absent.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_colors.py`:

```python
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
```

Add `import math` to the top of the file if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_colors.CorpusTest -v`
Expected: on this machine the corpus is present, so these run. Any failure here is a real signal about the constants — report it rather than loosening the assertion.

- [ ] **Step 3: Confirm it passes on the real corpus**

Run: `python -m unittest tests.test_colors.CorpusTest -v`
Expected: PASS, 5 tests.

If `test_light_mode_does_not_cluster_at_the_top` fails, that is the provisional light seed being wrong — expected per spec §5. Record the actual spread and report it; do not widen the assertion to make it pass.

- [ ] **Step 4: Confirm the default run is unaffected elsewhere**

Run: `python -m unittest discover -s tests`
Expected: `Ran 269 tests`, `OK (skipped=3)`

- [ ] **Step 5: Commit**

```bash
git add tests/test_colors.py
git commit -m "test: add opt-in corpus checks for palette distribution"
```

---

## Task 7: The offline A/B lab

**Files:**
- Create: `tools/palette_lab.py`

**Interfaces:**
- Consumes: `extract_colors` (Task 5), `ramp.mean_luminance` (Task 4), `tone`/`separate` and the constants (Tasks 2–3), `apply.apply_wlchroma` and `apply.revert_wlchroma` (existing).
- Produces: nothing imported elsewhere — a standalone script, like `tools/fake_mpris.py`.

**Behavior (spec §9):** walks the corpus one cover at a time, shows it in `imv`, pushes a candidate palette live through the real `wlchroma-ctl`, and records verdicts. Candidate parameter sets are a table at the top of the file. Restores the configured palette on exit.

- [ ] **Step 1: Write the script**

Create `tools/palette_lab.py`:

```python
#!/usr/bin/env python3
"""Offline A/B walker for palette candidates.

Visual acceptance cannot be asserted in a test, so this drives the real
wlchroma-ctl over a corpus of real covers and records which candidate the user
prefers per cover. No daemon, no D-Bus, no threads — cover in, palette out.

    tools/palette_lab.py --a current --b oklab-v1
    tools/palette_lab.py --list
    tools/palette_lab.py --replay        # re-score recorded verdicts
"""

import argparse
import hashlib
import json
import subprocess
import sys
import termios
import tty
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpris_chroma import oklab, ramp                       # noqa: E402
from mpris_chroma.apply import apply_wlchroma, revert_wlchroma, CtlError  # noqa: E402
from mpris_chroma.colors import _histogram, _select, extract_colors  # noqa: E402
from mpris_chroma import tone as tone_mod                  # noqa: E402

CORPUS_DIRS = [Path.home() / ".local/share/jellyfin-tui/covers",
               Path.home() / ".cache/mpris-chroma/covers"]
VERDICTS = Path(__file__).resolve().parent / "verdicts.json"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A named parameter set. Adding a variant is one line in CANDIDATES."""

    name: str
    lo: float
    hi: float
    gamma: float
    k: float
    cfrac: float
    min_de: float
    current: bool = False    # True = today's shipped behavior, for A/B baseline
    yellow_assist: float = 0.0
    """Spec §11's deferred knob, off by default. Above zero, lifts lightness for
    hues whose in-gamut chroma ceiling is low — yellows and cyans, which read
    deader than blues at the same lightness. It trades away some of decision
    D1's fidelity, so it is judged on real yellow covers here rather than
    decided in the abstract, and it lives only in the lab until it earns its way
    into production."""


CANDIDATES = {
    c.name: c for c in [
        Candidate("current", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, current=True),
        Candidate("oklab-v1", 0.15, 0.55, 0.85, 1.0, 0.85, 0.10),
        Candidate("oklab-darker", 0.10, 0.50, 0.75, 1.0, 0.85, 0.10),
        Candidate("oklab-vivid", 0.15, 0.55, 0.85, 1.0, 0.95, 0.10),
        Candidate("oklab-yellowlift", 0.15, 0.55, 0.85, 1.0, 0.85, 0.10,
                  yellow_assist=0.06),
    ]
}

# Chroma ceiling of the most saturated hue in sRGB (blue) at mid lightness, used
# as the yardstick for "this hue is chroma-starved" by yellow_assist.
_REFERENCE_CEILING = 0.30


def _assist(slots, amount: float, hi: float):
    """Lift lightness for hues that cannot hold much chroma anyway."""
    if amount <= 0.0:
        return slots
    lifted = []
    for s in slots:
        starved = 1.0 - min(1.0, oklab.max_chroma(s.L, s.h) / _REFERENCE_CEILING)
        lifted.append(tone_mod.Toned(L=min(hi, s.L + amount * starved),
                                     h=s.h, c_src=s.c_src))
    return lifted


def render(path: Path, cand: Candidate, mode: str):
    """Palette for this cover under this candidate, plus its separation result."""
    if cand.current:
        return extract_colors(path, mode=mode), None
    hist = _histogram(path)
    if not hist:
        return ("#a48ec7",) * 3, None
    picked, n = _select(hist)
    # Patch the module constants for this render only, then restore: the lab
    # exists to compare parameter sets, and the pipeline reads them as globals.
    saved = (tone_mod.ENVELOPES[mode], tone_mod.GAMMA[mode], tone_mod.SPREAD_GAIN,
             tone_mod.CHROMA_FRAC, tone_mod.MIN_DE)
    tone_mod.ENVELOPES[mode] = (cand.lo, cand.hi)
    tone_mod.GAMMA[mode] = cand.gamma
    tone_mod.SPREAD_GAIN = cand.k
    tone_mod.CHROMA_FRAC = cand.cfrac
    tone_mod.MIN_DE = cand.min_de
    try:
        toned = _assist(tone_mod.tone(picked, mode), cand.yellow_assist, cand.hi)
        slots, result = tone_mod.separate(toned, mode, n)
        return tuple(s.to_hex() for s in slots), result
    finally:
        (tone_mod.ENVELOPES[mode], tone_mod.GAMMA[mode], tone_mod.SPREAD_GAIN,
         tone_mod.CHROMA_FRAC, tone_mod.MIN_DE) = saved


def corpus(holdout: int) -> list[Path]:
    """Corpus minus a deterministic holdout slice, so the constants can be
    checked cold against covers no verdict ever touched."""
    files = [p for d in CORPUS_DIRS if d.is_dir()
             for p in sorted(d.glob("*")) if p.is_file()]
    if holdout <= 0:
        return files
    ranked = sorted(files, key=lambda p: hashlib.sha256(p.name.encode()).hexdigest())
    return sorted(set(files) - set(ranked[:holdout]))


def _read_key() -> str:
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":                      # arrow keys arrive as an escape seq
            ch += sys.stdin.read(2)
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _describe(colors_tuple, result) -> str:
    lch = [oklab.hex_to_lch(c) for c in colors_tuple]
    line = "%s   L %s   lum %.3f" % (
        " ".join(colors_tuple),
        " ".join(".%02d" % round(l[0] * 100) for l in lch),
        ramp.mean_luminance(*colors_tuple))
    if result is not None and not result.resolved and result.reason != "duplicates":
        line += "   collision dE %.3f (%s)" % (result.residual_de, result.reason)
    return line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--a", default="current", choices=sorted(CANDIDATES))
    ap.add_argument("--b", default="oklab-v1", choices=sorted(CANDIDATES))
    ap.add_argument("--mode", default="dark", choices=("dark", "light"))
    ap.add_argument("--holdout", type=int, default=25)
    ap.add_argument("--list", action="store_true", help="show candidates and exit")
    ap.add_argument("--replay", action="store_true",
                    help="score recorded verdicts without touching the display")
    args = ap.parse_args()

    if args.list:
        for c in CANDIDATES.values():
            print("%-14s lo=%.2f hi=%.2f g=%.2f k=%.2f cfrac=%.2f min_de=%.2f%s"
                  % (c.name, c.lo, c.hi, c.gamma, c.k, c.cfrac, c.min_de,
                     "   (shipped behavior)" if c.current else ""))
        return 0

    verdicts = json.loads(VERDICTS.read_text()) if VERDICTS.exists() else {}
    if args.replay:
        tally = {}
        for key, v in verdicts.items():
            tally[v] = tally.get(v, 0) + 1
        print("recorded verdicts: %s (%d covers)"
              % (tally, len(verdicts)))
        return 0

    a, b = CANDIDATES[args.a], CANDIDATES[args.b]
    files = corpus(args.holdout)
    if not files:
        print("no covers found in %s" % ", ".join(str(d) for d in CORPUS_DIRS))
        return 1

    i, showing_b, viewer = 0, True, None
    try:
        while 0 <= i < len(files):
            path = files[i]
            pa, ra = render(path, a, args.mode)
            pb, rb = render(path, b, args.mode)
            if viewer is not None:
                viewer.terminate()
            viewer = subprocess.Popen(["imv", str(path)],
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            live = pb if showing_b else pa
            try:
                apply_wlchroma(*live)
            except CtlError as e:
                print("\nwlchroma-ctl failed: %s" % e)
                return 1
            key = "%s|%s|%s" % (path.name, a.name, b.name)
            counts = {}
            for v in verdicts.values():
                counts[v] = counts.get(v, 0) + 1
            print("\033[2J\033[H", end="")
            print("[ %d/%d ] %-28s verdicts: A %d  B %d  = %d"
                  % (i + 1, len(files), path.name,
                     counts.get("a", 0), counts.get("b", 0), counts.get("=", 0)))
            print(" %s A  %-12s %s" % (" " if showing_b else "▶", a.name,
                                       _describe(pa, ra)))
            print(" %s B  %-12s %s" % ("▶" if showing_b else " ", b.name,
                                       _describe(pb, rb)))
            if key in verdicts:
                print("   recorded: %s" % verdicts[key])
            print("   [space] toggle   [a/b/=] verdict   "
                  "[←/→] cover   [q] quit")

            ch = _read_key()
            if ch == " ":
                showing_b = not showing_b
            elif ch in ("a", "b", "="):
                verdicts[key] = ch
                VERDICTS.write_text(json.dumps(verdicts, indent=1, sort_keys=True))
                i += 1
            elif ch == "\x1b[C":
                i += 1
            elif ch == "\x1b[D":
                i = max(0, i - 1)
            elif ch in ("q", "\x03"):
                break
    finally:
        if viewer is not None:
            viewer.terminate()
        # Never leave the desktop stuck on a candidate.
        try:
            revert_wlchroma()
        except CtlError:
            pass
    print("\nverdicts saved to %s" % VERDICTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify it lists candidates without touching the display**

Run: `python tools/palette_lab.py --list`
Expected: five lines, `current` marked `(shipped behavior)`.

- [ ] **Step 3: Verify rendering works headlessly**

Run:

```bash
python - <<'PY'
from pathlib import Path
import sys; sys.path.insert(0, "tools")
import palette_lab as lab
files = lab.corpus(25)
print("corpus %d covers (25 held out)" % len(files))
for p in files[:3]:
    for name in ("current", "oklab-v1"):
        pal, res = lab.render(p, lab.CANDIDATES[name], "dark")
        print("%-10s %-12s %s" % (p.name[:10], name, lab._describe(pal, res)))
PY
```

Expected: three covers, each showing `current` noticeably brighter (`lum`) than `oklab-v1`.

- [ ] **Step 4: Make it executable and confirm the suite is untouched**

```bash
chmod +x tools/palette_lab.py
python -m unittest discover -s tests
```

Expected: `Ran 269 tests`, `OK (skipped=3)` — the lab is not imported by the suite.

- [ ] **Step 5: Commit**

```bash
git add tools/palette_lab.py
git commit -m "tools: add offline A/B palette lab over the real cover corpus"
```

---

## Task 8: Documentation

**Files:**
- Modify: `README.md` (tuning table at lines 99–107; the wlchroma and light/dark bullets at lines 39–50; new section after Operate)

**Interfaces:**
- Consumes: the constants from Tasks 2–3.
- Produces: nothing.

- [ ] **Step 1: Replace the tuning table**

Replace the table under `## Tuning` (and its lead-in sentence) with:

```markdown
Color feel is controlled by constants in `mpris_chroma/tone.py` (toning and
separation) and `mpris_chroma/colors.py` (ranking and selection). Lightness and
chroma are in [Oklab](https://bottosson.github.io/posts/oklab/), so a lightness
target means apparent brightness rather than HSV's `value`.

| Constant | Meaning | Dark | Light |
|----------|---------|------|-------|
| `ENVELOPES` | Oklab lightness envelope per mode | `0.15`–`0.55` | `0.55`–`0.92` |
| `GAMMA` | compression exponent across covers (`<1` pulls bright covers down) | `0.85` | `1.18` |
| `SPREAD_GAIN` | how much of a cover's own lightness spread survives (`1.0` = all) | `1.0` | `1.0` |
| `CHROMA_FRAC` | target chroma as a fraction of the in-gamut ceiling | `0.85` | `0.85` |
| `NEUTRAL_C` | chroma at or below which a slot stays grey (never tinted) | `0.02` | `0.02` |
| `MIN_DE` | minimum perceptual distance between two slots | `0.10` | `0.10` |
| `MAX_SEPARATION_SHIFT` | most one slot may be moved to resolve a collision | `0.04` | `0.04` |
| `SELECT_MIN_DE` | minimum distance between two *source* picks (`colors.py`) | `0.08` | `0.08` |
| `VIBRANCY_WEIGHT` | chroma bonus vs. pixel coverage in ranking (`0.0` = most-pixels-wins) | `0.5` | `0.5` |
| `VIBRANCY_MIN_POP` | coverage below this gets no vibrancy boost (noise guard) | `0.01` | `0.01` |

`tools/palette_lab.py` walks a corpus of real covers and A/Bs candidate
parameter sets live through `wlchroma-ctl`, which is how these were chosen.
Run `tools/palette_lab.py --list` to see the built-in sets.
```

- [ ] **Step 2: Correct the two behavior bullets**

Replace the `**wlchroma:**` bullet's second half and the `**Light/dark aware:**` bullet with:

```markdown
- **wlchroma:** all three palette slots are set to the three most apparent,
  visibly-distinct colors in the cover. Ranking is vibrancy-weighted (coverage
  plus a chroma bonus), so a small vivid accent — a logo, a face — can take a
  slot from a large drab background instead of the palette being all backdrop.
  Colors are never invented: hues come from the cover, and a grayscale cover
  stays grey rather than being tinted. Colors cross-fade over `FADE_MS`
  (see `mpris_chroma/apply.py`) instead of snapping.
- **Light/dark aware:** hue always comes from the cover; the theme decides where
  the palette sits in *lightness*. Each cover is toned into a per-mode Oklab
  envelope — compressed relative to other covers, so a bright cover cannot wash
  out the desktop, but keeping that cover's own contrast, so a flat cover stays
  flat and a contrasty one stays contrasty. Chroma is then set as a fraction of
  what sRGB can actually show at that lightness and hue, which is what keeps
  dark palettes saturated instead of muddy. The daemon reads `color-scheme` from
  the freedesktop settings portal and re-tones the current palette live when you
  flip themes (same hues, different lightness). Set `MPRIS_CHROMA_MODE=light` or
  `dark` to force a mode (skips the portal); unset follows the system, defaulting
  to dark when no portal answers or no preference is set.
```

- [ ] **Step 3: Add the Theme switching section**

Insert after the `## Operate` section:

```markdown
## Theme switching

The daemon subscribes to `SettingChanged` on `org.freedesktop.portal.Settings`,
so anything that implements the portal's Settings interface drives it live — no
restart, no configuration on this side. Check what your desktop currently
reports with:

```bash
gdbus call --session --dest org.freedesktop.portal.Desktop \
  --object-path /org/freedesktop/portal/desktop \
  --method org.freedesktop.portal.Settings.ReadOne \
  org.freedesktop.appearance color-scheme
```

`uint32 1` is prefer-dark, `2` is prefer-light, `0` is no preference (treated as
dark). Out of the box on most setups this is served by
`xdg-desktop-portal-gtk`, which proxies `org.gnome.desktop.interface
color-scheme`.

To drive it from [darkman](https://gitlab.com/WhyNotHugo/darkman), either
register darkman as the Settings backend:

```ini
# ~/.config/xdg-desktop-portal/portals.conf
[preferred]
org.freedesktop.impl.portal.Settings=darkman
```

or, to leave the portal backend alone, have darkman set the gsettings key that
the gtk portal already republishes — a script in `~/.local/share/dark-mode.d/`
and `~/.local/share/light-mode.d/` running:

```bash
gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'   # or 'prefer-light'
```

Either way the daemon re-tones the current cover in place: same hues, different
lightness envelope.
```

- [ ] **Step 4: Verify the docs match the code**

```bash
python - <<'PY'
from mpris_chroma import tone
from mpris_chroma.colors import SELECT_MIN_DE
print("ENVELOPES", tone.ENVELOPES)
print("GAMMA", tone.GAMMA)
print("SPREAD_GAIN", tone.SPREAD_GAIN, "CHROMA_FRAC", tone.CHROMA_FRAC)
print("NEUTRAL_C", tone.NEUTRAL_C, "MIN_DE", tone.MIN_DE)
print("MAX_SEPARATION_SHIFT", tone.MAX_SEPARATION_SHIFT)
print("SELECT_MIN_DE", SELECT_MIN_DE)
PY
```

Expected: every value matches the README table. Fix the table, not the code, on any mismatch.

Run: `python -m unittest discover -s tests`
Expected: `Ran 269 tests`, `OK (skipped=3)`

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: describe Oklab toning, separation, and theme switching"
```

---

## After the plan: tuning

The constants that ship are the spec's starting values, validated against the
corpus in aggregate but not yet against the user's eye. The remaining loop is
theirs to drive:

1. `tools/palette_lab.py --a current --b oklab-v1` — walk the corpus, record verdicts.
2. Adjust constants in `tone.py`, or add a `Candidate` row, and re-walk.
3. `tools/palette_lab.py --replay` — check which recorded verdicts a change would flip.
4. Re-run `python -m unittest tests.test_colors.CorpusTest` and the 25-cover
   holdout cold before settling.

The light envelope is expected to move; the dark constants are expected to hold.
