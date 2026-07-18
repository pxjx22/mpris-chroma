import colorsys
import re
import shutil
import subprocess
from pathlib import Path

S_MIN = 0.45          # saturation floor for pixels that already have a hue
V_MIN = 0.45          # value floor: lift near-black enough to be visible
V_MAX = 0.85          # value ceiling: keep bright colors from blowing out
NEUTRAL_S = 0.12      # at/below this saturation a pixel is treated as neutral
                      # (grayscale) and its saturation is NOT enriched
COLOR_MIN_DIST = 0.12  # min RGB distance between the three chosen slots

VIBRANCY_WEIGHT = 0.5   # how much chroma (s*v) counts vs. pixel coverage;
                        # 0.0 restores pure most-pixels-wins ranking
VIBRANCY_MIN_POP = 0.01  # coverage below this gets no vibrancy boost, so a
                         # vivid noise speck can't jump the queue

# Value band per theme mode. Hue and saturation always come from the cover;
# light-vs-dark only remaps how bright the palette lands. "dark" is the
# historical band; "light" lifts it so colors read on a light desktop theme.
BANDS = {
    "dark": (V_MIN, V_MAX),
    "light": (0.70, 0.97),
}
MAGICK = shutil.which("magick") or "magick"  # ImageMagick 7 binary, from PATH

# ImageMagick histogram lines look like:
#   1234: ( 237, 28, 36) #ED1C24 srgb(237,28,36)
_HIST_RE = re.compile(r"^\s*(\d+):\s*\(\s*(\d+),\s*(\d+),\s*(\d+)")


def clamp_hsv(h: float, s: float, v: float,
              mode: str = "dark") -> tuple[float, float, float]:
    """Lift value into the mode's visible band; enrich saturation only for
    pixels that already have a hue, so genuine neutrals (grayscale) are left
    untinted. Hue is never touched — mode only moves the brightness band."""
    v_min, v_max = BANDS[mode]
    v = min(max(v, v_min), v_max)
    if s > NEUTRAL_S:
        s = max(s, S_MIN)
    return h, s, v


def hex_of(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _histogram(image_path: Path) -> list[tuple[int, tuple[float, float, float]]]:
    """Return [(count, (h,s,v)), ...] for a quantized version of the image."""
    out = subprocess.run(
        [MAGICK, str(image_path), "-resize", "100x100", "-colors", "16",
         "-depth", "8", "-format", "%c", "histogram:info:-"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    result = []
    for line in out.splitlines():
        m = _HIST_RE.match(line)
        if not m:
            continue
        count = int(m.group(1))
        r, g, b = (int(m.group(i)) / 255 for i in (2, 3, 4))
        result.append((count, colorsys.rgb_to_hsv(r, g, b)))
    return result


def _vibrancy_score(count: int, total: int,
                    hsv: tuple[float, float, float]) -> float:
    """Rank a histogram entry by coverage plus a vibrancy bonus.

    Pure pixel-count ranking finds backgrounds, not identity: a mostly-black
    cover with a brilliant logo never picks the logo. Adding chroma (s*v,
    vivid-and-bright) lets a small vivid accent outrank a large drab region,
    while grayscale entries (chroma 0) keep pure coverage ranking.
    """
    frac = count / total
    if frac < VIBRANCY_MIN_POP:
        return frac
    _, s, v = hsv
    return frac + VIBRANCY_WEIGHT * s * v


def _rgb_dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Euclidean distance between two HSV colors in RGB space (0-1 per channel)."""
    ra, ga, ba = colorsys.hsv_to_rgb(*a)
    rb, gb, bb = colorsys.hsv_to_rgb(*b)
    return ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5


def extract_colors(image_path: Path, mode: str = "dark") -> tuple[str, str, str]:
    """Extract the three most prominent, visibly distinct colors from an image.

    Faithful to the cover: colors are ranked by how much of the image they cover
    (the most *apparent* colors), then clamped only for visibility — value is
    lifted into a readable band and saturation is enriched, but saturation is
    left alone for near-neutral pixels so a grayscale cover stays grayscale
    rather than being tinted. No hues are ever invented; the three slots are real
    cover colors, kept apart by at least COLOR_MIN_DIST. If the cover has fewer
    than three distinct colors, the last one is repeated.
    """
    hist = _histogram(image_path)
    if not hist:
        # Pathological image: return the default accent three times.
        return "#a48ec7", "#a48ec7", "#a48ec7"

    # Most apparent first, vibrancy-aware: coverage plus a chroma bonus, so
    # a small vivid accent (a logo, a face) can beat a large drab background.
    total = sum(count for count, _ in hist)
    ranked = sorted(hist, key=lambda e: _vibrancy_score(e[0], total, e[1]),
                    reverse=True)

    # Select in the historical dark band regardless of mode: light's narrower
    # band shrinks RGB distances, and re-selecting there can swap in a
    # different cover color — changing a hue on a theme flip. Mode must only
    # move the brightness of the SAME three picks.
    picked: list[tuple[float, float, float]] = []
    for _, hsv in ranked:
        if len(picked) == 3:
            break
        lifted = clamp_hsv(*hsv)
        if all(_rgb_dist(lifted, clamp_hsv(*p)) >= COLOR_MIN_DIST for p in picked):
            picked.append(hsv)

    # Fewer than three distinct colors in the cover: repeat the last real one
    # rather than fabricate a hue that isn't there.
    while len(picked) < 3:
        picked.append(picked[-1])

    c1, c2, c3 = (hex_of(*clamp_hsv(*p, mode=mode)) for p in picked)
    return c1, c2, c3
