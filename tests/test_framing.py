"""SEC-011 §3: bounded line framing for playerctl stdout.

The framer is the memory bound. GLib's read_line() buffers internally without a
cap, so a player that never emits a newline could grow that buffer without
limit; LineFramer caps a line at MAX_LINE_BYTES and — critically — resyncs by
discarding until the next newline, so the TAIL of an oversized line can never
re-frame as fresh pseudo-lines.
"""

import unittest

from mpris_chroma.framing import MAX_LINE_BYTES, LineFramer


def _framer(**kw):
    drops = []
    return LineFramer(on_drop=lambda detail: drops.append(detail), **kw), drops


class FramingTest(unittest.TestCase):
    def test_complete_line_is_returned_without_its_newline(self):
        f, _ = _framer()
        self.assertEqual(f.feed(b"spotify\tPlaying\thttps://x/a\n"),
                         ["spotify\tPlaying\thttps://x/a"])

    def test_multiple_lines_in_one_chunk(self):
        f, _ = _framer()
        self.assertEqual(f.feed(b"one\ntwo\nthree\n"), ["one", "two", "three"])

    def test_line_split_across_feeds_is_reassembled(self):
        f, _ = _framer()
        self.assertEqual(f.feed(b"spot"), [])
        self.assertEqual(f.feed(b"ify\tPlay"), [])
        self.assertEqual(f.feed(b"ing\t\n"), ["spotify\tPlaying\t"])

    def test_partial_line_is_withheld_until_its_newline(self):
        # Also the EOF case: playerctl dying mid-line leaves an unfinishable
        # tail, which is simply never emitted — nothing acts on half a line.
        f, _ = _framer()
        self.assertEqual(f.feed(b"spotify\tPlaying\thttps://x/a"), [])


class BoundaryTest(unittest.TestCase):
    def test_line_of_exactly_max_bytes_is_accepted(self):
        f, drops = _framer()
        line = b"a" * MAX_LINE_BYTES
        self.assertEqual(f.feed(line + b"\n"), ["a" * MAX_LINE_BYTES])
        self.assertEqual(drops, [])

    def test_line_one_byte_over_max_is_dropped(self):
        f, drops = _framer()
        self.assertEqual(f.feed(b"a" * (MAX_LINE_BYTES + 1) + b"\n"), [])
        self.assertEqual(len(drops), 1)

    def test_oversize_line_does_not_stop_the_next_line(self):
        f, _ = _framer()
        out = f.feed(b"a" * (MAX_LINE_BYTES + 1) + b"\nspotify\tPlaying\t\n")
        self.assertEqual(out, ["spotify\tPlaying\t"])


class ResyncTest(unittest.TestCase):
    """The correctness-critical rule: an over-cap line is discarded UNTIL the
    next newline. Merely dropping the buffered prefix would let the remainder
    re-frame — injection through the very mechanism meant to prevent it."""

    def test_tail_of_an_oversize_line_never_surfaces(self):
        # The adversary pads past the cap, then appends a well-formed forged
        # line inside the SAME (still unterminated) line. If the framer dropped
        # only its buffer and kept framing, the forged tail would emerge as a
        # line of its own.
        f, _ = _framer()
        forged = b"jellyfin-tui\tPlaying\thttps://evil/a"
        self.assertEqual(f.feed(b"a" * (MAX_LINE_BYTES + 1)), [])
        self.assertEqual(f.feed(forged + b"\n"), [])

    def test_framing_resumes_on_the_line_after_the_resync(self):
        f, _ = _framer()
        f.feed(b"a" * (MAX_LINE_BYTES + 1))
        f.feed(b"tail\n")                       # consumed by the resync
        self.assertEqual(f.feed(b"good\n"), ["good"])

    def test_oversize_spanning_many_feeds_does_not_accumulate(self):
        # The buffer must not grow with the flood: bytes past the cap are
        # dropped as they arrive, not held until a newline that may never come.
        f, _ = _framer()
        for _ in range(100):
            f.feed(b"a" * MAX_LINE_BYTES)
        self.assertLessEqual(f.buffered_bytes, MAX_LINE_BYTES)

    def test_oversize_reports_exactly_one_drop_per_line(self):
        f, drops = _framer()
        for _ in range(10):
            f.feed(b"a" * MAX_LINE_BYTES)   # one line, arriving in ten pieces
        f.feed(b"\n")
        self.assertEqual(len(drops), 1)


class DecodeTest(unittest.TestCase):
    def test_invalid_utf8_is_replaced_not_raised(self):
        # D-Bus guarantees valid UTF-8, but the framer is exactly where that
        # inherited assumption stops holding: a raw pipe is bytes.
        f, _ = _framer()
        out = f.feed(b"spotify\tPlaying\t\xff\xfe\n")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("spotify\tPlaying\t"))

    def test_multibyte_character_split_across_feeds_is_not_mangled(self):
        # Decoding happens per complete line, so a UTF-8 sequence straddling a
        # read boundary is reassembled before decode.
        f, _ = _framer()
        art = "https://x/café".encode()
        self.assertEqual(f.feed(b"spotify\tPlaying\t" + art[:-1]), [])
        self.assertEqual(f.feed(art[-1:] + b"\n"), ["spotify\tPlaying\thttps://x/café"])


if __name__ == "__main__":
    unittest.main()
