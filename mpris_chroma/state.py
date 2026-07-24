"""Typed state model for the sync daemon (PY-001).

A named, immutable record for per-player state, plus the small type aliases the
selection and palette-mode logic speak in. Since 4b (SEC-001), the daemon tracks
each player's *unresolved* art_url and resolves off-thread; the generation token
that orders that async work is coordinator-scoped (a vanish of one player can
invalidate a job scheduled for another), so it lives on the Coordinator, not
here. The 4c (SEC-018) cover-state machine and retry bookkeeping are likewise
coordinator-scoped (coordinator.CoverState and the winner-only retry scalars) —
this module stays the home of the immutable per-player event record only.
"""

from dataclasses import dataclass
from typing import Literal

# Palette brightness band. Hue/saturation always come from the cover; the mode
# only remaps how bright the palette lands (see colors.BANDS).
Mode = Literal["dark", "light"]


@dataclass(frozen=True, slots=True)
class PlayerState:
    """One player's current state, as tracked in the `players` map.

    Frozen because a state is replaced wholesale on each event, never mutated in
    place — which is exactly how the line handler reassigns `players[name]`.
    """

    status: str   # MPRIS status: "Playing" / "Paused" / "Stopped"
    art_url: str  # the player's mpris:artUrl (unresolved); "" if none reported
    seq: int      # monotonic recency counter (higher = more recent)
