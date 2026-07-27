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
