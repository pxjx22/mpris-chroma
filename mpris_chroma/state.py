"""Typed state model for the sync daemon (PY-001).

A named, immutable record for per-player state, plus the small type aliases the
selection and palette-mode logic speak in. This is the foundation the async
event pipeline builds on: generation tokens, a resolved-cover state machine, and
retry bookkeeping will land here as later findings need them, but only when they
are actually used — today it holds exactly the three fields in use.
"""

from dataclasses import dataclass
from typing import Literal

# What select() decides the desktop should do.
Action = Literal["apply", "hold", "revert"]

# Palette brightness band. Hue/saturation always come from the cover; the mode
# only remaps how bright the palette lands (see colors.BANDS).
Mode = Literal["dark", "light"]


@dataclass(frozen=True, slots=True)
class PlayerState:
    """One player's current state, as tracked in the `players` map.

    Frozen because a state is replaced wholesale on each event, never mutated in
    place — which is exactly how the line handler reassigns `players[name]`.
    """

    status: str            # MPRIS status: "Playing" / "Paused" / "Stopped"
    cover_id: str | None   # resolved cover path/id, or None if unresolved
    seq: int               # monotonic recency counter (higher = more recent)
