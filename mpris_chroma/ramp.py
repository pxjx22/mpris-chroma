"""Model of wlchroma's palette expansion, for measurement only.

wlchroma turns the three colors we send into twelve cells before rendering
(`src/render/palette.zig:buildPalette`), blending consecutive pairs at four
alphas. Eight of the twelve are therefore midpoints, which is why apparent
screen brightness is the mean over this ramp and not over the three colors. The
daemon never calls this — it exists so the corpus test and the offline lab can
measure what the shader actually draws.
"""

import math

from .oklab import _to_linear   # one sRGB gamma decode, defined once

# (foreground, background) index pairs and the alphas, mirroring buildPalette.
_PAIRS = ((0, 1), (1, 2), (2, 0))
_ALPHAS = (1.00, 0.72, 0.50, 0.28)


def _channels(value: str) -> tuple[int, int, int]:
    r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
    return r, g, b


def expand(c1: str, c2: str, c3: str) -> list[str]:
    """The twelve cells wlchroma will render for this palette."""
    rgb = [_channels(c) for c in (c1, c2, c3)]
    cells = []
    for fg, bg in _PAIRS:
        for alpha in _ALPHAS:
            # Blended in 8-bit sRGB, matching palette.zig's blend() exactly —
            # deliberately not gamma-correct, because the point is to reproduce
            # what wlchroma does rather than what it ideally would do.
            # math.floor(x + 0.5) rounds half away from zero, matching Zig's @round
            # behavior. Python's round() uses banker's rounding and would diverge
            # by ±1/255 on any cell where the sum lands exactly on X.5.
            mixed = (math.floor(rgb[bg][k] * (1 - alpha) + rgb[fg][k] * alpha + 0.5)
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
