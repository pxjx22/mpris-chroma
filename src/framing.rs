//! Bounded newline framing for playerctl stdout (port of `framing.py`,
//! SEC-011 §3).
//!
//! Pure: the event source does one bounded read per wakeup and hands the bytes
//! here. Framing is a byte-level concern with its own failure modes, kept apart
//! from the coordinator's semantic trust boundary.

/// Generous for any legitimate artUrl (a path or an https URL); data-URI
/// artwork would blow any cap and is unsupported by the resolver anyway.
pub const MAX_LINE_BYTES: usize = 8192;

/// One read of this size per wakeup — never a read-until-EAGAIN loop, which
/// would reintroduce the unbounded drain the framer exists to prevent.
pub const READ_CHUNK: usize = 65536;

type OnDrop = Box<dyn FnMut(String) + Send>;

/// Accumulate bytes, emit complete decoded lines, cap what a single line may
/// cost.
pub struct LineFramer {
    buf: Vec<u8>,
    max: usize,
    on_drop: Option<OnDrop>,
    /// Resync state: everything up to the next newline belongs to a line we
    /// already refused, so it is discarded rather than framed.
    discarding: bool,
}

impl Default for LineFramer {
    fn default() -> Self {
        Self::new(MAX_LINE_BYTES, None)
    }
}

impl LineFramer {
    pub fn new(max_line_bytes: usize, on_drop: Option<OnDrop>) -> Self {
        Self {
            buf: Vec::new(),
            max: max_line_bytes,
            on_drop,
            discarding: false,
        }
    }

    /// Bytes held for the in-progress line (test/observability hook).
    pub fn buffered_bytes(&self) -> usize {
        self.buf.len()
    }

    /// Append `data` and return every line it completed. A partial tail is
    /// kept for the next feed; at EOF it is simply never emitted.
    pub fn feed(&mut self, data: &[u8]) -> Vec<String> {
        let mut lines = Vec::new();
        self.buf.extend_from_slice(data);
        let mut start = 0;
        while let Some(off) = self.buf[start..].iter().position(|&b| b == b'\n') {
            let (lo, nl) = (start, start + off);
            start = nl + 1;
            if self.discarding {
                // The remainder of a refused line ends here: consume it and
                // resume framing after the newline. Emitting it would let the
                // TAIL of an oversized line re-frame as a fresh pseudo-line —
                // injection through the very mechanism meant to prevent it.
                self.discarding = false;
                continue;
            }
            let len = nl - lo;
            if len > self.max {
                self.drop_line(len);
                continue;
            }
            lines.push(String::from_utf8_lossy(&self.buf[lo..nl]).into_owned());
        }
        self.buf.drain(..start);
        if self.buf.len() > self.max {
            // Over the cap with no newline in sight. Refuse now and discard
            // until one arrives, so the buffer cannot grow with the flood.
            if !self.discarding {
                self.drop_line(self.buf.len()); // one report per refused line
            }
            self.discarding = true;
        }
        if self.discarding {
            self.buf.clear();
        }
        lines
    }

    fn drop_line(&mut self, size: usize) {
        if let Some(on_drop) = self.on_drop.as_mut() {
            on_drop(format!("line exceeded {} bytes ({size}+)", self.max));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    fn framer() -> (LineFramer, Arc<Mutex<Vec<String>>>) {
        let drops = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&drops);
        let on_drop: OnDrop = Box::new(move |d| sink.lock().unwrap().push(d));
        (LineFramer::new(MAX_LINE_BYTES, Some(on_drop)), drops)
    }

    fn a(n: usize) -> Vec<u8> {
        vec![b'a'; n]
    }

    fn cat(parts: &[&[u8]]) -> Vec<u8> {
        parts.concat()
    }

    // --- framing ---------------------------------------------------------

    #[test]
    fn complete_line_is_returned_without_its_newline() {
        let (mut f, _) = framer();
        assert_eq!(
            f.feed(b"spotify\tPlaying\thttps://x/a\n"),
            ["spotify\tPlaying\thttps://x/a"]
        );
    }

    #[test]
    fn multiple_lines_in_one_chunk() {
        let (mut f, _) = framer();
        assert_eq!(f.feed(b"one\ntwo\nthree\n"), ["one", "two", "three"]);
    }

    #[test]
    fn line_split_across_feeds_is_reassembled() {
        let (mut f, _) = framer();
        assert!(f.feed(b"spot").is_empty());
        assert!(f.feed(b"ify\tPlay").is_empty());
        assert_eq!(f.feed(b"ing\t\n"), ["spotify\tPlaying\t"]);
    }

    #[test]
    fn partial_line_is_withheld_until_its_newline() {
        // Also the EOF case: nothing acts on half a line.
        let (mut f, _) = framer();
        assert!(f.feed(b"spotify\tPlaying\thttps://x/a").is_empty());
    }

    // --- boundaries ------------------------------------------------------

    #[test]
    fn line_of_exactly_max_bytes_is_accepted() {
        let (mut f, drops) = framer();
        let out = f.feed(&cat(&[&a(MAX_LINE_BYTES), b"\n"]));
        assert_eq!(out, ["a".repeat(MAX_LINE_BYTES)]);
        assert!(drops.lock().unwrap().is_empty());
    }

    #[test]
    fn line_one_byte_over_max_is_dropped() {
        let (mut f, drops) = framer();
        assert!(f.feed(&cat(&[&a(MAX_LINE_BYTES + 1), b"\n"])).is_empty());
        assert_eq!(drops.lock().unwrap().len(), 1);
    }

    #[test]
    fn oversize_line_does_not_stop_the_next_line() {
        let (mut f, _) = framer();
        let out = f.feed(&cat(&[&a(MAX_LINE_BYTES + 1), b"\nspotify\tPlaying\t\n"]));
        assert_eq!(out, ["spotify\tPlaying\t"]);
    }

    // --- resync ----------------------------------------------------------

    #[test]
    fn tail_of_an_oversize_line_never_surfaces() {
        // Pad past the cap, then append a well-formed forged line inside the
        // SAME still-unterminated line; it must not emerge on its own.
        let (mut f, _) = framer();
        assert!(f.feed(&a(MAX_LINE_BYTES + 1)).is_empty());
        assert!(
            f.feed(b"jellyfin-tui\tPlaying\thttps://evil/a\n")
                .is_empty()
        );
    }

    #[test]
    fn framing_resumes_on_the_line_after_the_resync() {
        let (mut f, _) = framer();
        f.feed(&a(MAX_LINE_BYTES + 1));
        f.feed(b"tail\n"); // consumed by the resync
        assert_eq!(f.feed(b"good\n"), ["good"]);
    }

    #[test]
    fn oversize_spanning_many_feeds_does_not_accumulate() {
        let (mut f, _) = framer();
        for _ in 0..100 {
            f.feed(&a(MAX_LINE_BYTES));
        }
        assert!(f.buffered_bytes() <= MAX_LINE_BYTES);
    }

    #[test]
    fn oversize_reports_exactly_one_drop_per_line() {
        let (mut f, drops) = framer();
        for _ in 0..10 {
            f.feed(&a(MAX_LINE_BYTES)); // one line, arriving in ten pieces
        }
        f.feed(b"\n");
        assert_eq!(drops.lock().unwrap().len(), 1);
    }

    // --- decoding --------------------------------------------------------

    #[test]
    fn invalid_utf8_is_replaced_not_rejected() {
        // A raw pipe is bytes; D-Bus's UTF-8 guarantee stops holding here.
        let (mut f, _) = framer();
        let out = f.feed(b"spotify\tPlaying\t\xff\xfe\n");
        assert_eq!(out.len(), 1);
        assert!(out[0].starts_with("spotify\tPlaying\t"));
    }

    #[test]
    fn multibyte_character_split_across_feeds_is_not_mangled() {
        let (mut f, _) = framer();
        let line = "spotify\tPlaying\thttps://x/café".as_bytes();
        let cut = line.len() - 1; // inside the two-byte é
        assert!(f.feed(&line[..cut]).is_empty());
        assert_eq!(
            f.feed(&cat(&[&line[cut..], b"\n"])),
            ["spotify\tPlaying\thttps://x/café"]
        );
    }
}
