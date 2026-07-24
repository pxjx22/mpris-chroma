"""Main-thread coordinator for the async event pipeline (SEC-001, phase 4b).

GLib-free by construction (never imports gi/GLib): sync.py owns the GLib/dbus
wiring and calls these handlers. The coordinator parses events, updates player
state, decides the desired end-state, and submits it to the worker's mailbox —
it never blocks on resolution, download, or ctl. Results come back through
adopt() on the main thread, where the generation check makes stale adoption
impossible (design guarantee a).
"""

import logging
import random
from collections.abc import Callable
from pathlib import Path

from .select import decide
from .state import Mode, PlayerState
from .worker import COMMITTED, FAILED_RETRYABLE, REJECTED, Desired

_log = logging.getLogger("mpris_chroma.coordinator")
_log.addHandler(logging.NullHandler())

MPRIS_PREFIX = "org.mpris.MediaPlayer2."

# Retry backoff (SEC-018): capped exponential with jitter, then terminal. Three
# attempts at 1s/2s/4s ≈ 7s total, so a genuinely-absent cover (e.g. a player
# with covers disabled) burns a bounded window before going quiet.
RETRY_BASE_MS = 1000
RETRY_CAP_MS = 30_000
RETRY_MAX_ATTEMPTS = 3
RETRY_JITTER = 0.15


def _default_jitter() -> float:
    """Production jitter multiplier in [1-J, 1+J]; injectable in tests."""
    return random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)


def backoff_delay_ms(attempt: int, jitter: Callable[[], float]) -> int:
    """Delay before retry `attempt` (1-based): min(BASE * 2^(n-1), CAP) * jitter.
    Pure given the injected jitter, so the math is unit-testable exactly."""
    return int(min(RETRY_BASE_MS * 2 ** (attempt - 1), RETRY_CAP_MS) * jitter())


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
                 covers_dir_for: Callable[[str], Path | None], mode: Mode = "dark",
                 schedule: Callable[[int, Callable[[], None]], object],
                 cancel: Callable[[object], None],
                 jitter: Callable[[], float] = _default_jitter):
        self.players: dict[str, PlayerState] = {}
        self.seq = 0
        self.gen = 0
        self.mode: Mode = mode
        self.applied: str | None = None      # bookkeeping/observability only
        self.last_desired: Desired | None = None   # last decided (retone target)
        self.last_submitted: Desired | None = None  # dedup key
        self.stopping = False
        self._submit = submit
        self._covers_dir_for = covers_dir_for
        # Retry timer plumbing (SEC-018). schedule/cancel are injected so this
        # module stays GLib-free: sync.py wires GLib.timeout_add (one-shot) and
        # GLib.source_remove. Winner-only retry means at most ONE armed timer
        # exists at any time, so retry state is three scalars, not a map.
        self._schedule = schedule
        self._cancel = cancel
        self._jitter = jitter
        self._retry_handle: object | None = None   # armed GLib source, if any
        self._retry_desire: Desired | None = None  # desire the attempts count against
        self._retry_attempt = 0

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
        if result.outcome == FAILED_RETRYABLE:
            # §4.1 redefinition of the 4b failure-reset: the dedup key stays set
            # (an identical line dedups) and the backoff timer owns resubmission,
            # so recovery no longer depends on unrelated MPRIS events.
            self._arm_retry()
        elif result.outcome == REJECTED:
            # Terminal without a metadata change: no timer, and the kept dedup
            # key means repeats of the same line cannot spin.
            pass
        elif result.outcome == COMMITTED:
            self.applied = result.cover_id
            self._retry_desire, self._retry_attempt = None, 0  # backoff restarts
        # skipped_duplicate: already the desired state; nothing to record.

    def begin_shutdown(self) -> None:
        """Enter shutdown: stop scheduling, cancel any armed retry, and
        invalidate every in-flight result by bumping gen (sync.py then clears
        the mailbox, stops the worker, and does the final revert — design §5)."""
        self.stopping = True
        self.gen += 1
        self._cancel_retry()

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
        self._cancel_retry()  # guard 1: a new desired value supersedes any armed retry
        self.last_submitted = desired
        self._submit((self.gen, desired))

    # --- retry (SEC-018 §4) ---------------------------------------------------

    def _arm_retry(self) -> None:
        """A retryable failure for the current desire: count the attempt and arm
        the backoff timer, or go terminal past the cap."""
        desire = self.last_submitted
        if desire != self._retry_desire:
            self._retry_desire, self._retry_attempt = desire, 0  # new target: fresh count
        self._retry_attempt += 1
        if self._retry_attempt > RETRY_MAX_ATTEMPTS:
            if self._retry_attempt == RETRY_MAX_ATTEMPTS + 1:  # log the transition once
                _log.warning("giving up on %r after %d attempts",
                             desire, RETRY_MAX_ATTEMPTS)
            return
        delay = backoff_delay_ms(self._retry_attempt, self._jitter)
        self._retry_handle = self._schedule(delay, self._fire_retry)

    def _fire_retry(self) -> None:
        """The armed timer fired (main thread, via GLib). Force-resubmit the
        retried desire with a fresh gen — bypassing _maybe_submit's value-dedup,
        which would otherwise absorb it (its value equals last_submitted)."""
        self._retry_handle = None  # consumed by firing; never cancel it again
        if self.stopping:
            return
        if self._retry_desire is None or self._retry_desire != self.last_desired:
            return  # guard 2: the desired state moved on between arm and fire
        self.gen += 1  # a retry is a genuine new attempt; its result must be adoptable
        self._submit((self.gen, self._retry_desire))

    def _cancel_retry(self) -> None:
        if self._retry_handle is not None:
            self._cancel(self._retry_handle)
            self._retry_handle = None
