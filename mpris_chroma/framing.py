"""Bounded newline framing for playerctl stdout (SEC-011 §3).

Pure and GLib-free: sync.py does one bounded read per dispatch and hands the
bytes here. Kept out of coordinator.py because framing is a byte-level concern
with its own failure modes, while the coordinator's trust boundary is semantic.
"""

from collections.abc import Callable

# Generous for any legitimate artUrl (a real one is a path or an https URL);
# data-URI artwork would blow any cap and is unsupported by the resolver anyway.
MAX_LINE_BYTES = 8192

# One read of this size per GLib dispatch — never a read-until-EAGAIN loop,
# which would reintroduce the unbounded drain the framer exists to prevent.
READ_CHUNK = 65536


class LineFramer:
    """Accumulate bytes, emit complete decoded lines, cap what a single line
    may cost."""

    def __init__(self, *, max_line_bytes: int = MAX_LINE_BYTES,
                 on_drop: Callable[[str], None] | None = None):
        self._buf = b""
        self._max = max_line_bytes
        self._on_drop = on_drop
        # Resync state: everything up to the next newline belongs to a line we
        # already refused, so it is discarded rather than framed.
        self._discarding = False

    @property
    def buffered_bytes(self) -> int:
        """Bytes held for the in-progress line (test/observability hook)."""
        return len(self._buf)

    def feed(self, data: bytes) -> list[str]:
        """Append `data` and return every line it completed. A partial tail is
        kept for the next feed; at EOF it is simply never emitted."""
        lines = []
        self._buf += data
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            raw, self._buf = self._buf[:nl], self._buf[nl + 1:]
            if self._discarding:
                # The remainder of a refused line ends here: consume it and
                # resume framing at the newline. Emitting it would let the TAIL
                # of an oversized line re-frame as a fresh pseudo-line — one of
                # which could parse — i.e. injection through the very mechanism
                # meant to prevent it.
                self._discarding = False
                continue
            if len(raw) > self._max:
                self._drop(len(raw))
                continue
            lines.append(raw.decode("utf-8", errors="replace"))
        if len(self._buf) > self._max:
            # Over the cap with no newline in sight. Refuse now and discard
            # until one arrives, so the buffer cannot grow with the flood.
            if not self._discarding:
                self._drop(len(self._buf))   # one report per refused line
            self._discarding = True
        if self._discarding:
            self._buf = b""
        return lines

    def _drop(self, size: int) -> None:
        if self._on_drop is not None:
            self._on_drop(f"line exceeded {self._max} bytes ({size}+)")
