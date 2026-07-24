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
from dataclasses import dataclass
from pathlib import Path

from .select import decide
from .state import Mode, PlayerState
from .worker import (COMMITTED, FAILED_RETRYABLE, REJECTED, SKIPPED_DUPLICATE,
                     CoverTarget, Desired)

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


# Per-player cover status (SEC-018 §3). PENDING/READY/RETRYING are eligible for
# selection; EXHAUSTED and REJECTED are "definitively not ready" and fall back.
COVER_PENDING = "pending"      # never attempted (or reset); resolvable on selection
COVER_READY = "ready"          # last attempt confirmed a palette for this identity
COVER_RETRYING = "retrying"    # transient failure; the winner's timer owns retry
COVER_EXHAUSTED = "exhausted"  # transient failures past the cap; a fresh MPRIS
#                                event from the player re-opens a bounded window
COVER_REJECTED = "rejected"    # deterministic policy refusal; only an
#                                art-identity change unlocks it

_ELIGIBLE = frozenset({COVER_PENDING, COVER_READY, COVER_RETRYING})


@dataclass(slots=True)
class CoverState:
    """One player's cover-resolution state. Mutable and main-thread-only — a
    deliberate departure from PY-001's frozen records, because status is
    transitioned in place on each adopt rather than replaced wholesale.
    Narrower than the design §3 sketch: attempt/timer live on the coordinator
    (winner-only retry means one armed timer, not a map), and target/cover_id
    are derivable from `players`/`applied`."""

    identity: tuple    # ("url", art_url) or ("dir", covers_dir)
    status: str


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
        self.covers: dict[str, CoverState] = {}    # per-player resolution state
        self.seq = 0
        self.gen = 0
        self.mode: Mode = mode
        self.applied: str | None = None      # bookkeeping/observability only
        # The CoverTarget whose palette wlchroma is currently showing — the
        # retone anchor (design §5): set on a committed/skipped apply, cleared
        # by a committed revert (the preset is not ours to re-tone).
        self.applied_target: CoverTarget | None = None
        self.last_desired: Desired | None = None   # last decided value
        self.last_submitted: Desired | None = None  # dedup key
        self.stopping = False
        self._submit = submit
        self._covers_dir_for = covers_dir_for
        self._submitted_player: str | None = None  # winner of the last submit
        # Retry timer plumbing (SEC-018). schedule/cancel are injected so this
        # module stays GLib-free: sync.py wires GLib.timeout_add (one-shot) and
        # GLib.source_remove. Winner-only retry means at most ONE armed timer
        # exists at any time, so retry state is three scalars, not a map.
        self._schedule = schedule
        self._cancel = cancel
        self._jitter = jitter
        self._retry_handle: object | None = None   # armed GLib source, if any
        self._retry_desire: Desired | None = None  # desire the attempts count against
        self._retry_player: str | None = None      # player the armed retry is for
        self._retry_attempt = 0

    # --- GLib-driven event handlers (main thread) ---------------------------

    def on_line(self, line: str) -> None:
        """Parse one 'name\\tstatus\\tartUrl' playerctl line, update that
        player's state and cover-state, and (re)decide. Does no I/O."""
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            return
        name, status = parts[0], parts[1]
        art_url = parts[2] if len(parts) > 2 else ""
        self.seq += 1
        self.players[name] = PlayerState(status, art_url, self.seq)
        # Cover-state maintenance (§3): a new art identity resets to PENDING;
        # EXHAUSTED also resets on the player's own next event even with an
        # unchanged identity (dir-scan identities never change, and exhaustion
        # is "transient failures did not recover in the window", not policy —
        # a fresh event re-opens one bounded window). Policy REJECTED resets
        # only on an identity change, so identical lines cannot spin.
        ident = self._art_identity(name, art_url)
        st = self.covers.get(name)
        if ident is None:
            self.covers.pop(name, None)
        elif st is None or st.identity != ident:
            self.covers[name] = CoverState(ident, COVER_PENDING)
        elif st.status == COVER_EXHAUSTED:
            st.status = COVER_PENDING
            # F1: clear the dedup key too — a url player's identity (and hence
            # its desired value) is unchanged on its next line, so value-dedup
            # would otherwise absorb the resubmit and leave PENDING inert
            # (recovered network, same track, stale palette until track change).
            # Spin-safe: this fires only on the EXHAUSTED->PENDING edge, so
            # identical lines between exhaustion episodes still dedup — each
            # own-event buys exactly one bounded retry window. The gen bump on
            # the resubmit also resets the attempt counters via _cancel_retry.
            self.last_submitted = None
        self._decide_and_submit(dir_resubmit=True)

    def on_vanish(self, bus_name: str) -> None:
        """A D-Bus name was lost. If it was a tracked player, evict it and
        re-decide — this is what reverts to the preset when the last player
        closes (playerctl --follow emits no line for a vanished player)."""
        name = player_name_from_bus(bus_name)
        if name is None or self.players.pop(name, None) is None:
            return
        self.covers.pop(name, None)  # gone; a returning player starts PENDING
        self._decide_and_submit(dir_resubmit=False)

    def on_scheme(self, value: int) -> None:
        """The portal color-scheme changed. Adopt the new mode, then (§5):

        - if what is SHOWING (applied_target) is not the pipeline's winner —
          e.g. an older cover held while a newer player retries — re-tone the
          shown cover under the new band;
        - otherwise re-run selection under the new mode, which re-tones the
          winner and supersedes any in-flight old-mode job;
        - reverted/idle: just record the mode for the next apply (the preset is
          not ours to re-tone)."""
        new_mode = mode_from_color_scheme(value)
        if new_mode == self.mode:
            return
        self.mode = new_mode
        sel = decide(self.players, self.mode, self._covers_dir_for, self._eligible)
        winner_desired, winner_name = sel if sel is not None else (None, None)
        if (self.applied_target is not None
                and (winner_desired is None
                     or winner_desired.target != self.applied_target)):
            self._maybe_submit(Desired(self.applied_target, new_mode))
        elif winner_desired is not None and winner_desired.target is not None:
            self._maybe_submit(winner_desired, player=winner_name)

    def adopt(self, result) -> None:
        """Marshalled back from the worker via GLib.idle_add. Runs on the main
        thread, where gen only changes here — so the generation check is exact
        and a stale result can never corrupt state (guarantee a)."""
        if self.stopping or result.gen != self.gen:
            return
        if result.outcome in (COMMITTED, SKIPPED_DUPLICATE):
            # skipped_duplicate means the worker confirmed this content+mode is
            # already what wlchroma shows — success for bookkeeping purposes.
            self.applied = result.cover_id
            self.applied_target = (self.last_submitted.target
                                   if self.last_submitted is not None else None)
            self._retry_desire, self._retry_attempt = None, 0  # backoff restarts
            self._set_cover_status(COVER_READY)
        elif result.outcome == FAILED_RETRYABLE:
            # §4.1 redefinition of the 4b failure-reset: the dedup key stays set
            # (an identical line dedups) and the backoff timer owns resubmission,
            # so recovery no longer depends on unrelated MPRIS events.
            exhausted = self._arm_retry()
            self._set_cover_status(COVER_EXHAUSTED if exhausted else COVER_RETRYING)
        elif result.outcome == REJECTED:
            # Terminal without a metadata change: no timer, and the kept dedup
            # key means repeats of the same line cannot spin.
            self._set_cover_status(COVER_REJECTED)
        # §5 defect 3: re-run selection after every transition, so a terminal
        # transition falls back to an older ready cover WITHOUT an unrelated
        # event. Value-dedup absorbs the no-op cases (committed winner, still-
        # retrying winner); dir_resubmit=False so this cannot race the timer.
        self._decide_and_submit(dir_resubmit=False)

    def begin_shutdown(self) -> None:
        """Enter shutdown: stop scheduling, cancel any armed retry, and
        invalidate every in-flight result by bumping gen (sync.py then clears
        the mailbox, stops the worker, and does the final revert — design §5)."""
        self.stopping = True
        self.gen += 1
        self._cancel_retry()

    # --- internals ----------------------------------------------------------

    def _art_identity(self, name: str, art_url: str) -> tuple | None:
        """The cover-state key (§3): the art_url when present, else the player's
        covers_dir for dir-scan players, else None (no art source)."""
        if art_url:
            return ("url", art_url)
        covers_dir = self._covers_dir_for(name)
        if covers_dir is not None:
            return ("dir", covers_dir)
        return None

    def _eligible(self, name: str) -> bool:
        """Selection eligibility (§5): state-based only. No state yet means
        never attempted — eligible."""
        st = self.covers.get(name)
        return st is None or st.status in _ELIGIBLE

    def _set_cover_status(self, status: str) -> None:
        """Transition the cover state of the player whose job just reported.
        Retone submissions carry no player, so they transition nothing."""
        if self._submitted_player is None:
            return
        st = self.covers.get(self._submitted_player)
        if st is not None:
            st.status = status

    def _decide_and_submit(self, *, dir_resubmit: bool) -> None:
        sel = decide(self.players, self.mode, self._covers_dir_for, self._eligible)
        if sel is None:
            return  # hold: keep the current palette
        desired, winner = sel
        self._maybe_submit(desired, player=winner, dir_resubmit=dir_resubmit)

    def _maybe_submit(self, desired: Desired, *, player: str | None = None,
                      dir_resubmit: bool = True) -> None:
        if self.stopping:
            return
        self.last_desired = desired
        if desired == self.last_submitted:
            # Same desired value. For a stable identity (revert or non-empty
            # art_url) that is a true duplicate — dedup. For a dir-scan identity
            # (empty art_url) the underlying file may have changed, so a LINE
            # event resubmits, reusing the current gen: no value change to bump
            # on, so it cannot preempt a running job (design §3). Adopt-driven
            # re-selection passes dir_resubmit=False so it cannot duplicate jobs
            # or race an armed retry timer.
            if not dir_resubmit or desired.target is None or desired.target.art_url:
                return
            self._submitted_player = player
            self._submit((self.gen, desired))
            return
        self.gen += 1
        self._cancel_retry()  # guard 1: a new desired value supersedes any armed retry
        self.last_submitted = desired
        self._submitted_player = player
        self._submit((self.gen, desired))

    # --- retry (SEC-018 §4) ---------------------------------------------------

    def _arm_retry(self) -> bool:
        """A retryable failure for the current desire: count the attempt and arm
        the backoff timer. Returns True when past the cap (exhausted, terminal:
        no timer armed)."""
        desire = self.last_submitted
        if desire != self._retry_desire:
            self._retry_desire, self._retry_attempt = desire, 0  # new target: fresh count
        self._retry_attempt += 1
        if self._retry_attempt > RETRY_MAX_ATTEMPTS:
            if self._retry_attempt == RETRY_MAX_ATTEMPTS + 1:  # log the transition once
                _log.warning("giving up on %r after %d attempts",
                             desire, RETRY_MAX_ATTEMPTS)
            return True
        if self._retry_handle is not None:
            # Belt: a same-desire failure while a timer is still armed (e.g. a
            # dir-scan line resubmitted alongside it) replaces the timer rather
            # than leaking a second live GLib source.
            self._cancel(self._retry_handle)
            self._retry_handle = None
        delay = backoff_delay_ms(self._retry_attempt, self._jitter)
        self._retry_handle = self._schedule(delay, self._fire_retry)
        self._retry_player = self._submitted_player
        return False

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
        self._submitted_player = self._retry_player
        self._submit((self.gen, self._retry_desire))

    def _cancel_retry(self) -> None:
        """Cancel any armed retry (gen bump / shutdown). The superseded player
        loses its retry chain (winner-only retry), so demote it RETRYING ->
        PENDING: a future re-selection then attempts it fresh instead of leaving
        it stranded with a dead timer."""
        if self._retry_handle is not None:
            self._cancel(self._retry_handle)
            self._retry_handle = None
            st = (self.covers.get(self._retry_player)
                  if self._retry_player is not None else None)
            if st is not None and st.status == COVER_RETRYING:
                st.status = COVER_PENDING
        self._retry_desire, self._retry_player, self._retry_attempt = None, None, 0
