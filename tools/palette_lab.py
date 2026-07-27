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

# The fallback triple the pipeline returns for a cover it correctly refuses to
# decode (SEC-005) — every slot identical, so it can never arise from a real
# extraction (selection always rejects near-duplicate picks at SELECT_MIN_DE).
_FALLBACK = ("#a48ec7",) * 3


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
    """Palette for this cover under this candidate, plus its separation result.

    The histogram is checked once, up front, for both branches: 7 covers in
    this corpus are corrupted downloads (HTML error pages saved with an image
    extension) that the pipeline correctly refuses by signature and maps to
    the fallback triple. That triple is indistinguishable from a real
    lavender-ish extraction unless the caller knows *why* it was returned, so
    this reports "corrupt" as the second element instead of a SeparationResult
    — the walker can then label it instead of presenting a stock swatch as if
    it were this cover's actual palette.
    """
    hist = _histogram(path)
    if not hist:
        return _FALLBACK, "corrupt"
    if cand.current:
        return extract_colors(path, mode=mode), None
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
    if result == "corrupt":
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
        # Never leave an orphaned viewer or the desktop stuck on a candidate.
        _stop_viewer(viewer)
        try:
            revert_wlchroma()
        except CtlError:
            pass
    print("\nverdicts saved to %s" % VERDICTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
