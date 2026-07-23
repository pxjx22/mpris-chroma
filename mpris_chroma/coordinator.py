"""Main-thread coordinator for the async event pipeline (SEC-001, phase 4b).

GLib-free by construction (never imports gi/GLib): sync.py owns the GLib/dbus
wiring and calls these handlers. The coordinator parses events, updates player
state, decides the desired end-state, and submits it to the worker's mailbox —
it never blocks on resolution, download, or ctl. Results come back through
adopt() on the main thread, where the generation check makes stale adoption
impossible (design guarantee a).
"""

import logging
from collections.abc import Callable
from pathlib import Path

from .select import decide
from .state import Mode, PlayerState
from .worker import COMMITTED, FAILED_RETRYABLE, REJECTED, Desired

_log = logging.getLogger("mpris_chroma.coordinator")
_log.addHandler(logging.NullHandler())

MPRIS_PREFIX = "org.mpris.MediaPlayer2."


def mode_from_color_scheme(value: int) -> Mode:
    """Map the portal's color-scheme value to a palette mode. Anything that is
    not an explicit light preference (incl. 0 = no preference and future values)
    falls back to dark — the historical band."""
    return "light" if value == 2 else "dark"


def player_name_from_bus(bus_name: str) -> str | None:
    """Map a D-Bus name to playerctl's {{playerName}} key, or None if it is not
    an MPRIS player. playerctl keys players by the bus name minus the MPRIS
    prefix, so this is exactly the key used in the `players` dict."""
    if bus_name.startswith(MPRIS_PREFIX):
        return bus_name[len(MPRIS_PREFIX):]
    return None


class Coordinator:
    """Owns per-player state and the generation-versioned scheduling of worker
    jobs. All methods run on the GLib main thread."""

    def __init__(self, *, submit: Callable[[tuple[int, Desired]], None],
                 covers_dir_for: Callable[[str], Path | None], mode: Mode = "dark"):
        self.players: dict[str, PlayerState] = {}
        self.seq = 0
        self.gen = 0
        self.mode: Mode = mode
        self.applied: str | None = None      # bookkeeping/observability only
        self.last_desired: Desired | None = None   # last decided (retone target)
        self.last_submitted: Desired | None = None  # dedup key; reset on failure
        self.stopping = False
        self._submit = submit
        self._covers_dir_for = covers_dir_for

    # --- GLib-driven event handlers (main thread) ---------------------------

    def on_line(self, line: str) -> None:
        """Parse one 'name\\tstatus\\tartUrl' playerctl line, update that
        player's state, and (re)decide. Does no I/O."""
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            return
        name, status = parts[0], parts[1]
        art_url = parts[2] if len(parts) > 2 else ""
        self.seq += 1
        self.players[name] = PlayerState(status, art_url, self.seq)
        self._decide_and_submit()

    def on_vanish(self, bus_name: str) -> None:
        """A D-Bus name was lost. If it was a tracked player, evict it and
        re-decide — this is what reverts to the preset when the last player
        closes (playerctl --follow emits no line for a vanished player)."""
        name = player_name_from_bus(bus_name)
        if name is None or self.players.pop(name, None) is None:
            return
        self._decide_and_submit()

    def on_scheme(self, value: int) -> None:
        """The portal color-scheme changed. Adopt the new mode; if a cover is
        the current desired end-state, re-tone it under the new band. When the
        current desired is a revert (preset) or there is none, just record the
        mode for the next apply."""
        new_mode = mode_from_color_scheme(value)
        if new_mode == self.mode:
            return
        self.mode = new_mode
        if self.last_desired is not None and self.last_desired.target is not None:
            self._maybe_submit(Desired(self.last_desired.target, new_mode))

    def adopt(self, result) -> None:
        """Marshalled back from the worker via GLib.idle_add. Runs on the main
        thread, where gen only changes here — so the generation check is exact
        and a stale result can never corrupt state (guarantee a)."""
        if self.stopping or result.gen != self.gen:
            return
        if result.outcome in (FAILED_RETRYABLE, REJECTED):
            # Interim (4c unit 2): keep 4b's event-driven retry — reset the dedup
            # key so the next identical event resubmits. Unit 3 replaces this with
            # a backoff timer for failed_retryable and no resubmission for rejected.
            self.last_submitted = None
        elif result.outcome == COMMITTED:
            self.applied = result.cover_id
        # skipped_duplicate: already the desired state; nothing to record.

    def begin_shutdown(self) -> None:
        """Enter shutdown: stop scheduling and invalidate every in-flight result
        by bumping gen (sync.py then clears the mailbox, stops the worker, and
        does the final revert — design §5)."""
        self.stopping = True
        self.gen += 1

    # --- internals ----------------------------------------------------------

    def _decide_and_submit(self) -> None:
        desired = decide(self.players, self.mode, self._covers_dir_for)
        if desired is None:
            return  # hold: keep the current palette
        self._maybe_submit(desired)

    def _maybe_submit(self, desired: Desired) -> None:
        if self.stopping:
            return
        self.last_desired = desired
        if desired == self.last_submitted:
            # Same desired value. For a stable identity (revert or non-empty
            # art_url) that is a true duplicate — dedup. For a dir-scan identity
            # (empty art_url) the underlying file may have changed, so resubmit,
            # but reuse the current gen: no value change to bump on, so it cannot
            # preempt a running job (design §3).
            if desired.target is None or desired.target.art_url:
                return
            self._submit((self.gen, desired))
            return
        self.gen += 1
        self.last_submitted = desired
        self._submit((self.gen, desired))
