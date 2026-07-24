from collections.abc import Callable
from pathlib import Path

from .state import Mode, PlayerState
from .worker import CoverTarget, Desired

# decide()'s return: (Desired, winner_name) for an apply, (Desired(None), None)
# for a revert, None for hold. The winner name lets adopt() transition that
# player's cover state (SEC-018).
Selection = tuple[Desired, str | None] | None


def decide(players: dict[str, PlayerState], mode: Mode,
           covers_dir_for: Callable[[str], Path | None],
           eligible: Callable[[str], bool]) -> Selection:
    """Pick the desired end-state from the per-player states (4b SEC-001,
    ranking restored in 4c SEC-018).

    Resolution happens off-thread, so this decides from *unresolved* state —
    status, art_url, and a per-player eligibility predicate fed by the
    coordinator's cover-state machine (rejected/exhausted players are
    ineligible):

    - No player Playing -> revert: (Desired(target=None), None).
    - Newest Playing player (by seq) with an *art source* that is eligible ->
      apply its cover: (Desired(target), name). A newest player that is
      ineligible or source-less falls back to the next-newest — this restores
      select()'s cover-aware ranking from cached outcomes instead of inline
      resolution.
    - Playing players exist but none is actionable -> None (hold: keep the
      current palette).

    An "art source" is a non-empty art_url OR a configured covers_dir for that
    player, so a jellyfin-tui line with empty art still resolves via its
    directory scan rather than holding.
    """
    playing = [(name, p) for name, p in players.items() if p.status == "Playing"]
    if not playing:
        return Desired(target=None, mode=mode), None
    for name, p in sorted(playing, key=lambda np: np[1].seq, reverse=True):
        covers_dir = covers_dir_for(name)
        if (p.art_url or covers_dir is not None) and eligible(name):
            return Desired(CoverTarget(p.art_url, covers_dir), mode), name
    return None
