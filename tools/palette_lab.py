#!/usr/bin/env python3
"""Offline A/B walker for palette candidates.

Visual acceptance cannot be asserted in a test, so this drives the real
wlchroma-ctl over a corpus of real covers and records which candidate the user
prefers per cover. No daemon, no D-Bus, no threads — cover in, palette out.

    tools/palette_lab.py --a legacy --b oklab-v1
    tools/palette_lab.py --list
    tools/palette_lab.py --replay        # re-score recorded verdicts
"""

import argparse
import colorsys
import enum
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import termios
import tty
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpris_chroma import oklab, ramp                       # noqa: E402
from mpris_chroma.apply import apply_wlchroma, revert_wlchroma, CtlError  # noqa: E402
from mpris_chroma.colors import _histogram, _select, _vibrancy_score  # noqa: E402
from mpris_chroma import tone as tone_mod                  # noqa: E402

CORPUS_DIRS = [Path.home() / ".local/share/jellyfin-tui/covers",
               Path.home() / ".cache/mpris-chroma/covers"]
VERDICTS = Path(__file__).resolve().parent / "verdicts.json"

# The fallback triple the pipeline returns for a cover it correctly refuses to
# decode (SEC-005) — every slot identical, so it can never arise from a real
# extraction (selection always rejects near-duplicate picks at SELECT_MIN_DE).
_FALLBACK: tuple[str, str, str] = ("#a48ec7", "#a48ec7", "#a48ec7")


class RenderOutcome(enum.Enum):
    """Sentinel for render()'s second return slot when a cover can't be read.

    A bare "corrupt" string literal typo-mismatches silently (`== "corupt"`
    is just False, not an error); an enum member fails loudly instead.
    """
    CORRUPT = "corrupt"


# --- Historical reproduction, lab-only -------------------------------------
# Pre-rework HSV pipeline, reproduced verbatim from mpris_chroma/colors.py at
# git commit 4ac796d (before the Oklab tone/separate rework). Kept ONLY so the
# lab has a real "before" to put next to "after" — it is not, and must never
# become, part of mpris_chroma/. These constants and functions were removed
# from the shipped pipeline on purpose; do not re-add them there.
_LEGACY_S_MIN = 0.45            # saturation floor for pixels that already have a hue
_LEGACY_V_MIN, _LEGACY_V_MAX = 0.45, 0.85
_LEGACY_NEUTRAL_S = 0.12        # at/below this, a pixel is neutral: not enriched
_LEGACY_COLOR_MIN_DIST = 0.12   # min RGB distance between the three chosen slots
_LEGACY_BANDS = {"dark": (_LEGACY_V_MIN, _LEGACY_V_MAX), "light": (0.70, 0.97)}


def _legacy_clamp_hsv(h: float, s: float, v: float,
                       mode: str = "dark") -> tuple[float, float, float]:
    v_min, v_max = _LEGACY_BANDS[mode]
    v = min(max(v, v_min), v_max)
    if s > _LEGACY_NEUTRAL_S:
        s = max(s, _LEGACY_S_MIN)
    return h, s, v


def _legacy_hex_of(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _legacy_rgb_dist(a: tuple[float, float, float],
                      b: tuple[float, float, float]) -> float:
    """Euclidean distance between two HSV colors in RGB space (0-1/channel)."""
    ra, ga, ba = colorsys.hsv_to_rgb(*a)
    rb, gb, bb = colorsys.hsv_to_rgb(*b)
    return ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5


def _legacy_extract(hist: list[tuple[int, tuple[float, float, float]]],
                     mode: str) -> tuple[str, str, str]:
    """Pre-rework selection and render, byte-for-byte from commit 4ac796d.

    `hist` is assumed non-empty — the caller (`render`) applies the shared
    undecodable-cover check before branching here, same as the new path, so
    a corrupted cover is labeled "corrupt" under both candidates rather than
    silently handed a fabricated palette by either one.

    Selection always ranked and compared in the "dark" band regardless of
    `mode` (narrower bands shrink RGB distances enough to swap which colors
    get picked, which would mean a theme flip changes hue — not just
    brightness). Only the final render below uses `mode`.
    """
    total = sum(count for count, _ in hist)
    ranked = sorted(hist, key=lambda e: _vibrancy_score(e[0], total, e[1]),
                    reverse=True)
    picked: list[tuple[float, float, float]] = []
    for _, hsv in ranked:
        if len(picked) == 3:
            break
        lifted = _legacy_clamp_hsv(*hsv)   # default mode="dark", by design above
        if all(_legacy_rgb_dist(lifted, _legacy_clamp_hsv(*p)) >= _LEGACY_COLOR_MIN_DIST
               for p in picked):
            picked.append(hsv)
    while len(picked) < 3:
        picked.append(picked[-1])
    # Destructure rather than `tuple(generator)`: a generator satisfies
    # `list`/`tuple` structurally but not the declared 3-tuple arity, the same
    # defect already fixed in oklab.oklab_to_srgb, ramp._channels, and
    # colors.extract_colors.
    c1, c2, c3 = (_legacy_hex_of(*_legacy_clamp_hsv(*p, mode=mode)) for p in picked)
    return c1, c2, c3
# --- end historical reproduction -------------------------------------------


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
    legacy: bool = False      # True = route to _legacy_extract (the pre-rework
                              # HSV path, above); False = the tone/separate path
    yellow_assist: float = 0.0
    """Spec §11's deferred knob, off by default. Above zero, lifts lightness for
    hues whose in-gamut chroma ceiling is low — yellows and cyans, which read
    deader than blues at the same lightness. It trades away some of decision
    D1's fidelity, so it is judged on real yellow covers here rather than
    decided in the abstract, and it lives only in the lab until it earns its way
    into production."""


CANDIDATES = {
    c.name: c for c in [
        Candidate("legacy", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, legacy=True),
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


def render(path: Path, cand: Candidate, mode: str
           ) -> tuple[tuple[str, str, str], "tone_mod.SeparationResult | None | RenderOutcome"]:
    """Palette for this cover under this candidate, plus its separation result.

    The histogram is checked once, up front, for both branches: 7 covers in
    this corpus are corrupted downloads (HTML error pages saved with an image
    extension) that the pipeline correctly refuses by signature and maps to
    the fallback triple. That triple is indistinguishable from a real
    lavender-ish extraction unless the caller knows *why* it was returned, so
    this reports RenderOutcome.CORRUPT as the second element instead of a
    SeparationResult — the walker can then label it instead of presenting a
    stock swatch as if it were this cover's actual palette.
    """
    hist = _histogram(path)
    if not hist:
        return _FALLBACK, RenderOutcome.CORRUPT
    if cand.legacy:
        return _legacy_extract(hist, mode), None
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
        c1, c2, c3 = (s.to_hex() for s in slots)
        return (c1, c2, c3), result
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
            # A lone ESC is indistinguishable from the first byte of an arrow
            # sequence until the rest either shows up or doesn't. Raw mode
            # disables ISIG, so an unconditional read(2) here would block
            # forever on a bare ESC with no Ctrl-C left to escape it — the
            # user would be locked out with only two more keypresses as an
            # exit. An arrow sequence's remaining bytes arrive together,
            # well inside this window; a bare ESC never sends more, so a
            # timeout here means "just ESC."
            if select.select([sys.stdin], [], [], 0.05)[0]:
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
    if result is RenderOutcome.CORRUPT:
        line += "   [unreadable cover -> fallback palette, not a real extraction]"
    elif result is not None and not result.resolved and result.reason != "duplicates":
        line += "   collision dE %.3f (%s)" % (result.residual_de, result.reason)
    return line


def _stop_viewer(viewer) -> None:
    """Make sure the cover-preview process is actually gone, not just asked to
    leave. `terminate()` only delivers SIGTERM; if `imv` is slow to react (or
    wedged) and this process exits right after, the child is reparented and
    orphaned rather than cleaned up — exactly what this tool must never do.
    `kill()` is the backstop for a process that ignores SIGTERM.
    """
    if viewer is None:
        return
    viewer.terminate()
    try:
        viewer.wait(timeout=1)
    except subprocess.TimeoutExpired:
        viewer.kill()
        viewer.wait()


def _load_verdicts() -> dict:
    """Load recorded verdicts, tolerating a missing or corrupt file.

    These judgements can only come from a human sitting through the walker
    again; a truncated write or a stray byte must never make the whole
    session start over silently, so this warns on stderr and starts empty
    rather than propagating JSONDecodeError out of main().
    """
    if not VERDICTS.exists():
        return {}
    try:
        return json.loads(VERDICTS.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print("warning: %s is unreadable (%s); starting from no verdicts"
              % (VERDICTS, e), file=sys.stderr)
        return {}


def _save_verdicts(verdicts: dict) -> None:
    """Write verdicts atomically: temp file in the same directory, then
    os.replace() into position. os.replace is atomic on POSIX, so a process
    killed mid-write leaves either the old file or the new one, never a
    half-written truncation of the user's accumulated judgements — the
    entire point of the tool, and not something a re-run can regenerate.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(VERDICTS.parent),
                                     prefix=".verdicts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(verdicts, indent=1, sort_keys=True))
        os.replace(tmp_name, VERDICTS)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main() -> int:
    # SIGTERM (killed from another shell) and SIGHUP (the terminal window
    # closing) otherwise tear the interpreter down mid-syscall if delivery
    # lands while blocked in _read_key()'s raw-mode read — landing both of
    # this tool's worst outcomes at once: the desktop stuck on a candidate
    # palette AND the terminal left in raw mode, the latter with no
    # signal-driven way to undo it. Raising SystemExit instead unwinds
    # normally, so the existing `finally` (viewer teardown + revert) and
    # _read_key's own termios restore both still run.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(1))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--a", default="legacy", choices=sorted(CANDIDATES))
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
                     "   (pre-rework HSV baseline, commit 4ac796d)"
                     if c.legacy else ""))
        return 0

    verdicts = _load_verdicts()
    if args.replay:
        # Keys now encode (cover, a, b, mode) (IMPORTANT 4), so one flat tally
        # across every pairing and mode is no longer meaningful — a "12 A / 9
        # B" total could be legacy-vs-oklab-v1 in dark mixed with an unrelated
        # oklab-vivid-vs-oklab-darker in light. Group by (a, b, mode) instead.
        groups: dict[tuple[str, str, str], dict[str, int]] = {}
        for key, v in verdicts.items():
            parts = key.split("|")
            if len(parts) == 4:
                _, a_name, b_name, mode = parts
            elif len(parts) == 3:
                # Verdict recorded before IMPORTANT 4's fix — mode unknown.
                _, a_name, b_name = parts
                mode = "?"
            else:
                continue
            tally = groups.setdefault((a_name, b_name, mode), {})
            tally[v] = tally.get(v, 0) + 1
        if not groups:
            print("no verdicts recorded yet")
            return 0
        for (a_name, b_name, mode), tally in sorted(groups.items()):
            print("%-14s vs %-16s [%-5s] %s (%d covers)"
                  % (a_name, b_name, mode, tally, sum(tally.values())))
        return 0

    a, b = CANDIDATES[args.a], CANDIDATES[args.b]
    raw = corpus(0)
    if not raw:
        print("no covers found in %s" % ", ".join(str(d) for d in CORPUS_DIRS))
        return 1
    files = corpus(args.holdout)
    if not files:
        # Distinct from "no corpus at all" (MINOR 9): the covers exist, the
        # holdout slice just ate all of them.
        print("holdout (%d) consumed the entire corpus (%d covers) — "
              "pass a smaller --holdout" % (args.holdout, len(raw)))
        return 1

    i, showing_b, viewer, direction = 0, True, None, 1
    skipped_corrupt = 0
    try:
        while 0 <= i < len(files):
            path = files[i]
            pa, ra = render(path, a, args.mode)
            pb, rb = render(path, b, args.mode)
            if ra is RenderOutcome.CORRUPT and rb is RenderOutcome.CORRUPT:
                # MINOR 7: both candidates fall back to the identical stock
                # triple for an undecodable cover — never push that to the
                # desktop or solicit a verdict that would mean nothing.
                # Advance in whatever direction the walker was already
                # travelling so this can't get stuck bouncing at one index;
                # running off either end exits the while loop normally.
                skipped_corrupt += 1
                i += direction
                continue
            _stop_viewer(viewer)
            viewer = subprocess.Popen(["imv", str(path)],
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            live = pb if showing_b else pa
            try:
                apply_wlchroma(*live)
            except CtlError as e:
                print("\nwlchroma-ctl failed: %s" % e)
                return 1
            # Mode is part of the key (IMPORTANT 4): the same cover/pairing
            # judged under --mode light must not silently overwrite the
            # --mode dark verdict for it.
            key = "%s|%s|%s|%s" % (path.name, a.name, b.name, args.mode)
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
                _save_verdicts(verdicts)
                i += 1
                direction = 1
            elif ch == "\x1b[C":
                i += 1
                direction = 1
            elif ch == "\x1b[D":
                i = max(0, i - 1)
                direction = -1
            elif ch in ("q", "\x03"):
                break
    finally:
        # Never leave an orphaned viewer or the desktop stuck on a candidate.
        # Nested, not sequential (CRITICAL 1): a second Ctrl-C landing inside
        # _stop_viewer's wait() — bounded, or unbounded after kill() — raises
        # KeyboardInterrupt right there. If revert_wlchroma() ran after it in
        # the same block, that interrupt would skip the revert and strand the
        # desktop on a candidate palette. Nesting means a failure in either
        # step cannot suppress the other.
        try:
            _stop_viewer(viewer)
        finally:
            try:
                revert_wlchroma()
            except CtlError:
                pass
    print("\nverdicts saved to %s" % VERDICTS)
    if skipped_corrupt:
        print("(%d corrupt cover(s) skipped — no verdict recorded)"
              % skipped_corrupt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
