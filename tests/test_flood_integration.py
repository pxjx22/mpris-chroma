"""Opt-in GLib integration test for the input-side bound (SEC-011 §4).

Different axis from the 4b heartbeat test: that one proves slow WORK on the
worker cannot stall the loop; this one proves a flood of INPUT cannot. Enable:

    MPRIS_CHROMA_INTEGRATION=1 python -m unittest tests.test_flood_integration

A real pipe, a real main loop, and the production reader callback. The claim is
bounded LATENCY, not eventual progress: with exactly one bounded read per
dispatch, a 10 ms timer must keep beating at a rate derived from the measured
cost of processing one chunk — so the floor is calibrated, not guessed.

Measured on the maintainer's box (2026-07-27): a 64 KiB chunk holds ~1820
lines and costs ~11 ms to frame + validate + decide, giving ~24 ideal beats
per 0.5 s window and an asserted floor of 16. The test was verified to bite by
swapping in the forbidden drain-until-EAGAIN reader: under a flood that keeps
the pipe permanently readable, that loop never reaches EAGAIN, so the callback
never returns and the timer never fires ONCE. The bound is not a nicety.
"""

import fcntl
import os
import subprocess
import sys
import time
import unittest

from mpris_chroma.coordinator import Coordinator
from mpris_chroma.framing import READ_CHUNK, LineFramer
from mpris_chroma.sync import PLAYERS, _make_io_reader

_ENABLED = os.environ.get("MPRIS_CHROMA_INTEGRATION")

_LINE = b"spotify\tPlaying\thttps://x/cover.jpg\n"
TIMER_MS = 10
WINDOW_S = 0.5
# The flood must stay AHEAD of the reader or the test cannot tell a bounded
# read from an unbounded drain: a same-process writer thread loses the GIL to
# the reader and never builds a backlog. So: a separate process, and a pipe
# deep enough that one drain-until-EAGAIN dispatch would swallow many chunks.
PIPE_BYTES = 1 << 20
F_SETPIPE_SZ = 1031  # linux/fcntl.h; absent from the fcntl module


def _coordinator():
    return Coordinator(submit=lambda item: None, covers_dir_for=lambda n: None,
                       schedule=lambda d, f: None, cancel=lambda h: None,
                       allowed_players=PLAYERS)


def _measure_chunk_cost() -> float:
    """Seconds to frame + validate + decide one full READ_CHUNK of lines."""
    chunk = (_LINE * (READ_CHUNK // len(_LINE) + 1))[:READ_CHUNK]
    best = float("inf")
    for _ in range(3):
        coord, framer = _coordinator(), LineFramer()
        start = time.monotonic()
        for line in framer.feed(chunk):
            coord.on_line(line)
        best = min(best, time.monotonic() - start)
    return best


@unittest.skipUnless(_ENABLED, "real GLib loop; set MPRIS_CHROMA_INTEGRATION=1")
class InputFloodTest(unittest.TestCase):
    def test_a_saturated_pipe_does_not_starve_the_timer(self):
        from gi.repository import GLib, GLibUnix

        chunk_cost = _measure_chunk_cost()
        # One dispatch costs at most one chunk, so the timer's period stretches
        # to at most interval + chunk_cost — that is the bound being claimed.
        # Anything that drains further per dispatch (a fill-loop) multiplies the
        # chunk term and blows straight through this floor.
        ideal = WINDOW_S / (TIMER_MS / 1000 + chunk_cost)
        floor = max(2, int(ideal * 0.7))

        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        try:
            fcntl.fcntl(write_fd, F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            pass    # not permitted: the test still runs, just less sharply
        flooder = subprocess.Popen(
            [sys.executable, "-c",
             "import os\n"
             f"payload = {_LINE!r} * 2048\n"
             "while True:\n"
             "    os.write(1, payload)\n"],
            stdout=write_fd)
        os.close(write_fd)   # the child owns the write end now

        coord, framer = _coordinator(), LineFramer()
        seen, beats = [0], [0]

        def _count_line(line):
            coord.on_line(line)
            seen[0] += 1

        loop = GLib.MainLoop()
        reader = _make_io_reader(
            read_fd, framer, _count_line,
            on_hangup=loop.quit,
            hup_err_mask=GLib.IOCondition.HUP | GLib.IOCondition.ERR)
        GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, read_fd,
                             GLib.IOCondition.IN | GLib.IOCondition.HUP
                             | GLib.IOCondition.ERR, reader)

        def _beat():
            beats[0] += 1
            return True

        GLib.timeout_add(TIMER_MS, _beat)
        GLib.timeout_add(int(WINDOW_S * 1000), lambda: (loop.quit(), False)[1])

        try:
            loop.run()
        finally:
            flooder.terminate()
            flooder.wait(5)
            os.close(read_fd)

        self.assertGreater(seen[0], 0, "reader consumed nothing")
        self.assertGreaterEqual(
            beats[0], floor,
            f"timer starved: {beats[0]} beats < floor {floor} "
            f"(chunk cost {chunk_cost * 1000:.2f} ms, {seen[0]} lines consumed)")


if __name__ == "__main__":
    unittest.main()
