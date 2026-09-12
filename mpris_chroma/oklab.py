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

# The forward transform's cube roots are taken of linear-light values that are
# non-negative for real colors, but bisection in max_chroma probes outside the
# gamut where they can go slightly negative. math.cbrt is a real odd root for
# negative inputs, so it replaces the copysign form while staying within 1 ulp
# of it (max deviation 6.5e-16 over a 16-point probe set incl. negatives/zero,
# PERFORMANCE_AUDIT L-1) at roughly half the per-call cost.
def _cbrt(x: float) -> float:
    return math.cbrt(x)


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
    """True iff the color is representable in sRGB. The epsilon (1e-7) is
    empirical and prevents bisection from wandering at lightness extremes.
    Loosening toward 1e-4 regresses test_max_chroma_vanishes_at_the_extremes."""
    return all(-1e-7 <= c <= 1 + 1e-7 for c in _to_linear_rgb(L, a, b))


def oklab_to_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    r, g, b = _to_linear_rgb(L, a, b)
    return (min(1.0, max(0.0, _to_gamma(min(1.0, max(0.0, r))))),
            min(1.0, max(0.0, _to_gamma(min(1.0, max(0.0, g))))),
            min(1.0, max(0.0, _to_gamma(min(1.0, max(0.0, b))))))


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


def lch_to_hex(L: float, C: float, h: float) -> str:
    r, g, b = oklab_to_srgb(*from_lch(L, C, h))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
