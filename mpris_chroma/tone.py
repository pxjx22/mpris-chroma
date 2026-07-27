"""Per-mode palette toning (spec §5).

The defect this replaces: a single HSV value band clamped all three slots into
one narrow window, so nothing could ever be dark, palettes had no depth, and the
band did not correspond to apparent brightness at all. Toning instead works in
Oklab and splits the problem along two independent axes — how bright this cover
is relative to other covers (compressed), and how its three slots relate to each
other (preserved).
"""

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
    # guards negative float epsilon at pure black, which would silently return a
    # complex when raised to a fractional exponent and fail confusingly at comparison.
    toned_anchor = lo + (hi - lo) * (max(anchor, 0.0) ** gamma)
    out = []
    for L, C, h in source_lch:
        moved = toned_anchor + SPREAD_GAIN * (L - anchor)
        out.append(Toned(L=min(hi, max(lo, moved)), h=h, c_src=C))
    return out


# Perceptual distinctness (spec §6). Toning alone does not fix collisions and
# slightly worsens them — compressing lightness pulls apart-in-L colors together
# — so the repair runs after toning, where the collision actually appears.
MIN_DE = 0.10                # pairs closer than this in Oklab read as duplicates
SEPARATION_STEP = 0.01       # per-pass lightness nudge
MAX_SEPARATION_SHIFT = 0.04  # total displacement any one slot may accumulate
MAX_SEPARATION_PASSES = 8    # loop safety net; the budget above binds first
_BUDGET_EPS = 1e-9           # float tolerance for "budget fully spent"; repeated
                             # subtraction settles a few ulps above exact 0.0


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
            # is diagnostic rather than a catch-all. Repeated float subtraction
            # rarely lands a spent budget on exact 0.0 (it settles a few ulps
            # above), so this is a tolerance comparison, not an exact one.
            reason = "budget" if all(b <= _BUDGET_EPS for b in budget) else "envelope"
            break
        if all(b <= _BUDGET_EPS for b in budget):
            # Detect exhaustion the moment it happens rather than waiting for a
            # following pass to confirm nothing moves: a pass that spends the
            # last of every slot's budget still counts as "moved" above, so
            # without this check a palette that exhausts budget on exactly the
            # final permitted pass would fall through with no break ever fired,
            # leaving `reason` at its "passes" default — mislabeling budget
            # exhaustion as a pass-cap hit.
            reason = "budget"
            break

    residual = _closest_pair(work)[2]
    resolved = residual >= MIN_DE
    if resolved:
        reason = "clear"
    # Re-pad: a cover with fewer than three real colors repeats its last one,
    # and that repeat must keep tracking the slot it mirrors.
    out = work + [work[-1]] * (len(slots) - n_distinct)
    return out, SeparationResult(resolved, reason, residual)
