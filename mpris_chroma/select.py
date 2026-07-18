def select(players: dict[str, tuple[str, str | None, int]]) -> tuple[str, str | None]:
    """Pick what the desktop should show from the per-player states.

    players maps player name -> (status, cover_id, seq), where seq is a monotonic
    recency counter (higher = updated more recently).

    - Any player Playing with a resolved cover -> apply the most-recent one.
    - Players Playing but with no cover yet -> hold (cover may still resolve).
    - Nothing Playing (paused, stopped, or closed) -> revert to the default
      palette: paused music has stopped mattering to the desktop.
    """
    playing = [(seq, cover) for (status, cover, seq) in players.values()
               if status == "Playing"]
    if playing:
        with_cover = [(seq, cover) for (seq, cover) in playing if cover]
        if with_cover:
            _, cover = max(with_cover)
            return "apply", cover
        return "hold", None
    return "revert", None
