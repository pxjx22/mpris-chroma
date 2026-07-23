from collections.abc import Callable
from pathlib import Path

from .state import Mode, PlayerState
from .worker import CoverTarget, Desired


def decide(players: dict[str, PlayerState], mode: Mode,
           covers_dir_for: Callable[[str], Path | None]) -> Desired | None:
    """Pick the desired end-state from the per-player states (4b, SEC-001).

    Resolution now happens off-thread, so this decides from *unresolved* state —
    status plus art_url — and returns a Desired for the worker to converge to:

    - No player Playing -> revert (Desired with target=None).
    - Newest Playing player (by seq) with an *art source* -> apply its cover.
    - Newest Playing player with *no* art source -> None (hold: keep the current
      palette; there is nothing to resolve yet).

    An "art source" is a non-empty art_url OR a configured covers_dir for that
    player, so a jellyfin-tui line with empty art still resolves via its
    directory scan rather than holding.

    This drops select()'s cover-aware ranking (an older Playing-with-cover
    outranking a newer Playing-without-cover): that needs per-player resolution
    outcomes, which are 4c's state machine. See SECURITY_AUDIT.md SEC-018.
    """
    playing = [(name, p) for name, p in players.items() if p.status == "Playing"]
    if not playing:
        return Desired(target=None, mode=mode)
    name, p = max(playing, key=lambda np: np[1].seq)
    covers_dir = covers_dir_for(name)
    if p.art_url or covers_dir is not None:
        return Desired(target=CoverTarget(p.art_url, covers_dir), mode=mode)
    return None
