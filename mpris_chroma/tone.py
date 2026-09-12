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
MAX_SEPARATION_PASSES = 8    # iteration cap. Usually the budget binds first (a
                             # slot taking full steps spends 0.04 in 4 passes),
                             # but clamping yields partial steps with no lower
                             # bound, so this is a real terminal condition too.
_BUDGET_EPS = 1e-9           # float tolerance for "budget fully spent"; repeated
                             # subtraction settles a few ulps above exact 0.0


@dataclass(frozen=True, slots=True)
class SeparationResult:
    """Why separation stopped. A palette that cannot be separated within budget
    is an accepted outcome — for a genuinely monochrome cover it is the correct
    one — but it must be observable rather than silent (SEC-019 precedent)."""

    resolved: bool
    reason: str          # clear | duplicates | budget | blocked | passes
    residual_de: float   # closest remaining pair; MIN_DE or more when resolved


def _closest_pair(slots: list[Toned]) -> tuple[int, int, float]:
    labs = [s.to_lab() for s in slots]
    n = len(labs)
    delta_e = oklab.delta_e
    if n == 3:
        d01 = delta_e(labs[0], labs[1])
        d02 = delta_e(labs[0], labs[2])
        d12 = delta_e(labs[1], labs[2])
        if d01 <= d02 and d01 <= d12:
            return (0, 1, d01)
        if d02 <= d12:
            return (0, 2, d02)
        return (1, 2, d12)

    best_i, best_j, best_d = 0, 1, float("inf")
    for i in range(n):
        lab_i = labs[i]
        for j in range(i + 1, n):
            d = delta_e(lab_i, labs[j])
            if d < best_d:
                best_i, best_j, best_d = i, j, d
    return best_i, best_j, best_d


def separate(slots: list[Toned], mode: str,
             n_distinct: int) -> tuple[list[Toned], SeparationResult]:
    """Push colliding slots apart in lightness only, within bounds (spec §6).

    Selection is never revisited — these are the same three cover colors, and
    hue is untouched. Only the *real* picks participate: when a cover yielded
    fewer than three distinct colors the extras are repeats, and separating them
    would invent contrast the cover does not have.
    """
    lo, hi = ENVELOPES[mode]
    if n_distinct > len(slots):
        raise ValueError(
            f"n_distinct ({n_distinct}) exceeds the number of slots ({len(slots)})"
        )
    if n_distinct < 2:
        return list(slots), SeparationResult(False, "duplicates", 0.0)

    work = list(slots[:n_distinct])
    # Rank order is fixed once, here, and held for the rest of the routine. Every
    # move is clamped against neighbours in this order, which is what makes
    # monotonicity hold by construction: pushing a pair apart preserves that
    # pair's order, but unclamped it could shove a slot past a *third* one.
    # `rank` is derived from `order` and `order` never changes after this
    # point, so it is built once here rather than rebuilt every pass.
    order = sorted(range(n_distinct), key=lambda i: (work[i].L, i))
    rank = {slot: pos for pos, slot in enumerate(order)}
    budget = [MAX_SEPARATION_SHIFT] * n_distinct
    reason = "passes"

    for _ in range(MAX_SEPARATION_PASSES):
        i, j, dist = _closest_pair(work)
        if dist >= MIN_DE:
            reason = "clear"
            break
        # Move the higher-ranked slot of the pair up and the lower one down.
        up, down = (i, j) if rank[i] > rank[j] else (j, i)
        moved = False
        for idx, direction in ((up, +1.0), (down, -1.0)):
            step = min(SEPARATION_STEP, budget[idx])
            if step <= 0.0:
                continue
            target = work[idx].L + direction * step
            # Envelope first, then neighbours: clamping into [lo, hi] here
            # means the neighbour clamp below only ever narrows further
            # toward work[idx].L, never reopens room the envelope had just
            # closed off. Applying it last (as this used to) let the
            # neighbour clamp hand back a target the envelope had rejected,
            # which could invert rank order and blow the budget in one move
            # when the input already sat outside the envelope.
            target = min(hi, max(lo, target))
            # For input already outside the envelope, clamping alone is not
            # enough: if work[idx].L is already past the bound on the side
            # this move is heading further into, the clamp snaps `target`
            # back across work[idx].L in the *opposite* direction from the
            # one requested — e.g. a slot already above `hi` asked to move
            # further up gets clamped down to `hi`, which is a downward
            # move, not an upward one. That is not this slot's separation
            # move; refuse it rather than apply it, or it can shove the
            # slot straight through a neighbour it was never cleared
            # against and invert rank order.
            if (target - work[idx].L) * direction <= 0.0:
                continue
            pos = rank[idx]
            if direction > 0 and pos + 1 < n_distinct:
                target = min(target, work[order[pos + 1]].L)
            if direction < 0 and pos - 1 >= 0:
                target = max(target, work[order[pos - 1]].L)
            delta = target - work[idx].L
            if delta * direction <= 0.0:
                continue
            # Final cap at the slot's remaining budget. For in-envelope input
            # this is a no-op (the clamps above already keep |delta| <= step
            # <= budget[idx]), but for out-of-envelope input the envelope
            # clamp above can jump `target` further than `step` allowed (e.g.
            # snapping straight to `lo` from well below it) — the budget must
            # still win that fight, or the displacement invariant breaks on
            # exactly the input this routine is supposed to tame.
            if abs(delta) > budget[idx]:
                delta = budget[idx] if delta > 0 else -budget[idx]
            target = work[idx].L + delta
            delta = abs(delta)
            budget[idx] -= delta
            work[idx] = Toned(L=target, h=work[idx].h, c_src=work[idx].c_src)
            moved = True
        # Check exhaustion before blockage so both stay reachable: a pass that
        # spends the last of every budget still counts as "moved", so without
        # this check first, budget exhaustion could hide behind next pass's
        # "not moved" and get mislabeled — or never fire at all if the pass
        # cap is hit on the same pass. Repeated float subtraction rarely lands
        # a spent budget on exact 0.0 (it settles a few ulps above), so this
        # is a tolerance comparison, not an exact one.
        if all(b <= _BUDGET_EPS for b in budget):
            reason = "budget"          # every slot spent its full allowance
            break
        if not moved:
            reason = "blocked"         # budget remains; envelope or neighbour pins it
            break

    residual = _closest_pair(work)[2]
    resolved = residual >= MIN_DE
    if resolved:
        reason = "clear"
    # Re-pad: a cover with fewer than three real colors repeats its last one,
    # and that repeat must keep tracking the slot it mirrors.
    out = work + [work[-1]] * (len(slots) - n_distinct)
    return out, SeparationResult(resolved, reason, residual)
