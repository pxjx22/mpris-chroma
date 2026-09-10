import colorsys
import logging
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from . import oklab
from .tone import separate, tone

# Library-style logger: a NullHandler keeps it silent unless the application
# configures logging (sync.main does), so a cover that cannot be turned into a
# palette is diagnosable instead of silently becoming the default accent.
_log = logging.getLogger("mpris_chroma.colors")
_log.addHandler(logging.NullHandler())

VIBRANCY_WEIGHT = 0.5   # how much chroma (s*v) counts vs. pixel coverage;
                        # 0.0 restores pure most-pixels-wins ranking
VIBRANCY_MIN_POP = 0.01  # coverage below this gets no vibrancy boost, so a
                         # vivid noise speck can't jump the queue

# Minimum perceptual distance between two *source* picks. Selection has to
# reject three shades of one color before toning, or stage 4 is handed a
# collision it cannot solve. Measured on source colors, so it is
# mode-independent — mode must only re-tone the same three picks, never change
# which ones they are. Lower than tone.MIN_DE on purpose: separation is the
# designated repair for collisions that toning creates or worsens, so
# selection only has to reject picks that are already near-identical at the
# source — it does not need to anticipate what toning will do to them.
SELECT_MIN_DE = 0.08

# Album artwork is only ever a raster photo; SVG/PDF/HTML and other formats are
# unnecessary and expand the decoder attack surface. Only these three formats
# (identified by Pillow from the file's *signature*, never its extension) may be
# decoded (SEC-005).
_ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_SAMPLE = (100, 100)   # downsample target, mirrors the old `-resize 100x100`
_QUANTIZE_COLORS = 16  # palette size, mirrors the old `-colors 16`

# In-process decode containment (SEC-006). The ImageMagick subprocess is gone,
# so its `-limit` ceilings become Pillow-side bounds: reject a file larger than
# the byte budget before opening it, and an image whose declared dimensions
# exceed the pixel budget before decoding its body — that header-first check is
# the decompression-bomb guard. Both are far above real album art (Spotify
# 640x640, Jellyfin up to a few thousand px) and far below anything that could
# exhaust the daemon. Decoded memory is then fixed by the 100x100 downsample.
_MAX_DECODE_BYTES = 16 * 1024 * 1024  # 16 MiB; bounds local covers, which are
                                      # not size-capped by the download path
_MAX_PIXELS = 16_000_000              # ~16 MP declared-dimension ceiling

# Sentinel for PaletteMemo's empty slot (see PaletteMemo._key below). Plain
# `None` would be ambiguous: if `content_id` were ever `None`, `None != None`
# is `False`, so the empty slot would read as a hit and `self._value` would
# unpack `None` with a confusing TypeError. `_UNSET` can never equal a real
# content_id. Module-level but immutable, so the zero-mutable-state
# constraint on this module still holds.
_UNSET = object()


def _histogram(image_path: Path) -> list[tuple[int, tuple[float, float, float]]]:
    """Return [(count, (h,s,v)), ...] for a quantized version of the image.

    Decodes in-process with Pillow instead of shelling out to ImageMagick, and
    only for JPEG/PNG/WebP *content* — Pillow identifies the format from the
    file signature, so HTML mislabeled as `.jpg`, SVG, and PDF are refused
    before any pixels are decoded and no external delegate can be invoked.
    Unaccepted or undecodable input yields an empty histogram, which the caller
    maps to the safe default palette (SEC-005).
    """
    try:
        if image_path.stat().st_size > _MAX_DECODE_BYTES:
            return []
        with Image.open(image_path) as img:
            # `.format` and `.size` are set from the header during the lazy
            # open, before any pixel decoding — reject an unaccepted format or
            # an over-budget (decompression-bomb) size here, up front, so a
            # malicious cover's body is never decoded.
            if img.format not in _ACCEPTED_FORMATS:
                return []
            if img.width * img.height > _MAX_PIXELS:
                return []
            # Ask the JPEG decoder to scale down to ~the sample size during
            # decode; a pre-decode hint and a no-op for PNG/WebP, so neither
            # the gate order above nor the decode surface changes.
            img.draft("RGB", _SAMPLE)
            sample = img.convert("RGB").resize(_SAMPLE)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return []
    quantized = sample.quantize(colors=_QUANTIZE_COLORS)
    palette = quantized.getpalette()  # flat [r, g, b, r, g, b, ...]
    result = []
    # getcolors() returns (count, palette_index) pairs, or None if the palette
    # is empty — guard the None so degenerate quantizer output falls through to
    # the caller's default rather than crashing on unpacking. getcolors only
    # reports colors that occur, so counts are positive; palette entries are
    # bytes, so channels are already in 0..255.
    for count, index in (quantized.getcolors() or []):
        r, g, b = (palette[index * 3 + c] / 255 for c in range(3))
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

    An empty `hist` returns `([], 0)` rather than raising: `extract_colors`
    already guards this case before calling in, but the docstring above
    advertises other callers (a test, an offline lab), and padding
    `picked[-1]` below has nothing to repeat once `hist` is empty.
    """
    if not hist:
        return [], 0
    total = sum(count for count, _ in hist)
    # Most apparent first, vibrancy-aware: coverage plus a chroma bonus, so a
    # small vivid accent (a logo, a face) can beat a large drab background.
    ranked = sorted(hist, key=lambda e: _vibrancy_score(e[0], total, e[1]),
                    reverse=True)
    picked: list[tuple[float, float, float]] = []
    labs: list[tuple[float, float, float]] = []
    for _, hsv in ranked:
        if len(picked) == 3:
            break
        lch = oklab.to_lch(*oklab.srgb_to_oklab(*colorsys.hsv_to_rgb(*hsv)))
        # Each Lab is computed exactly once: the candidate's here, and each
        # pick's when it is appended — not rebuilt per comparison inside the
        # dedup loop (PERFORMANCE_AUDIT L-2).
        lab = oklab.from_lch(*lch)
        if all(oklab.delta_e(lab, picked_lab) >= SELECT_MIN_DE
               for picked_lab in labs):
            picked.append(lch)
            labs.append(lab)
    n_distinct = len(picked)
    # Repeat the last real color rather than fabricate a hue that isn't there.
    while len(picked) < 3:
        picked.append(picked[-1])
    return picked, n_distinct


def select_palette(image_path: Path
                   ) -> tuple[list[tuple[float, float, float]], int]:
    """Content-derived half of extraction: decode, quantize, rank, pick.

    Mode-free by construction — the picks depend only on the cover's pixels, so
    a caller may reuse this result across theme changes (see PaletteMemo).
    Returns ([], 0) for a cover that yields no extractable colors (rejected
    format, undecodable data, or a genuine no-color image); render_palette maps
    that to the default accent.
    """
    hist = _histogram(image_path)
    if not hist:
        # Log here rather than at the caller so a memoized failure is reported
        # once per cover instead of once per theme flip.
        _log.warning("no extractable colors from %s; using default palette",
                     image_path.name)
        return [], 0
    return _select(hist)


def render_palette(picks: list[tuple[float, float, float]], n_distinct: int,
                   mode: str, *, label: str = "?") -> tuple[str, str, str]:
    """Mode-derived half: tone into the mode's Oklab envelope, then separate.

    `label` names the cover in the diagnostic below only; it has no effect on
    the palette. It exists because this half no longer holds the path.
    """
    if not picks:
        return "#a48ec7", "#a48ec7", "#a48ec7"
    toned = tone(picks, mode)
    slots, result = separate(toned, mode, n_distinct)
    if not result.resolved and result.reason != "duplicates":
        # An unseparable palette is an accepted outcome, but never a silent one.
        _log.debug("palette for %s left a pair at dE %.3f (%s)",
                   label, result.residual_de, result.reason)
    c1, c2, c3 = (slot.to_hex() for slot in slots)
    return c1, c2, c3


def extract_colors(image_path: Path, mode: str = "dark") -> tuple[str, str, str]:
    """Extract the three most prominent, visibly distinct colors from an image.

    Faithful to the cover: colors are ranked by coverage plus a vibrancy bonus,
    then toned into the mode's Oklab lightness envelope — compressed relative to
    other covers, but keeping this cover's own spread — and finally pushed apart
    if two of them collide perceptually. No hues are ever invented; the three
    slots are real cover colors. If the cover has fewer than three distinct
    colors, the last one is repeated rather than fabricated.

    The uncached composition of select_palette and render_palette. Kept as the
    reference implementation: PaletteMemo must agree with it exactly.
    """
    picks, n_distinct = select_palette(image_path)
    return render_palette(picks, n_distinct, mode, label=image_path.name)


class PaletteMemo:
    """One-slot memo over the mode-free half of extraction.

    A theme flip re-runs the pipeline for an unchanged cover: the worker's dedup
    key is (content_id, mode), so a mode change misses it and reaches extract.
    But only `mode` changed, and select_palette does not depend on mode, so its
    result is reusable — turning a ~6.2 ms decode into a ~0.01 ms retone.

    One slot, because the access pattern a flip produces is exactly
    (cover X, dark) -> (cover X, light). No eviction policy, no size bound, no
    TTL — none of which can then be got wrong.

    Keyed on the content_id cover.py derives for SEC-018, so an in-place
    overwrite of a cover changes the key and misses. Not keyed on the path.

    Not thread-safe, and does not need to be: `extract` is called only from the
    single worker thread (worker.py `_serve`).

    `content_id` now has two consumers that must change together if it is ever
    strengthened: the worker's `(content_id, mode)` dedup (worker.py:191-193)
    and this memo's slot key. This memo's aliasing exposure is slightly wider
    than the worker's, though: the worker's key is reset by a committed revert
    or a failed ctl call, but neither resets this slot, so it stays keyed on
    the last extracted cover across both. Two covers that stat identical
    `(size, mtime_ns)` would alias here as they would there; the probability is
    negligible on ns-granularity filesystems.
    """

    def __init__(self, select=select_palette, render=render_palette):
        # select and render bind their defaults at `def` time, so
        # `mock.patch.object(colors, "select_palette")` after a PaletteMemo
        # has already been constructed will NOT affect it — tests must inject
        # a replacement through this constructor, not patch the module global.
        self._select = select
        self._render = render
        # _UNSET, not None: see the module-level sentinel comment above.
        self._key: tuple[int, int] | object = _UNSET
        self._value: tuple[list[tuple[float, float, float]], int] | None = None

    def __call__(self, image_path: Path, mode: str,
                 content_id: tuple[int, int]) -> tuple[str, str, str]:
        if content_id != self._key:
            # Value first, key second, and it matters. If _select raises, the
            # old key and old value stay consistent with each other. Setting
            # the key first would leave it pointing at a value that was never
            # computed, and every later flip on this cover would silently serve
            # the *previous* cover's palette.
            self._value = self._select(image_path)
            self._key = content_id
        picks, n_distinct = self._value
        return self._render(picks, n_distinct, mode, label=image_path.name)
