//! Color pipeline (port of `colors.py` and the modules it builds on).
//!
//! Extraction runs in two halves:
//!
//! - [`select_palette`]: decode, downsample, quantize, rank, pick. Depends only
//!   on the cover's pixels, never on the mode, so its result can be reused
//!   across theme flips ([`PaletteMemo`]).
//! - [`render_palette`]: tone the picks into the mode's envelope and separate
//!   collisions.
//!
//! [`extract_colors`] is their uncached composition and the reference the
//! memo must agree with.

pub mod decode;
pub mod oklab;
pub mod pick;
pub mod quantize;
pub mod ramp;
pub mod resize;
pub mod tone;

use std::path::Path;

use crate::state::Mode;
use oklab::Lch;
use pick::HistEntry;
use tone::{SeparationReason, separate, tone};

/// Returned for a cover that yielded no usable picks.
pub const DEFAULT_ACCENT: &str = "#a48ec7";
/// Palette size, mirroring the old ImageMagick `-colors 16`.
pub const QUANTIZE_COLORS: usize = 16;

/// Source picks as OkLCh, plus how many of them are real (the rest repeat).
pub type Picks = (Vec<Lch>, usize);

/// A cover's content identity: `(size, mtime_ns)`, as cover resolution
/// derives it (SEC-018).
pub type ContentId = (u64, i128);

fn label(path: &Path) -> String {
    path.file_name().map_or_else(
        || path.display().to_string(),
        |n| n.to_string_lossy().into_owned(),
    )
}

/// `(count, hsv)` for each colour of the quantized 100x100 sample, in palette
/// index order. Empty for anything that cannot be decoded, which the caller
/// maps to the default palette.
pub fn histogram(path: &Path) -> Vec<HistEntry> {
    match decode::sample_file(path, decode::MAX_DECODE_BYTES) {
        Ok(sample) => histogram_of_sample(&sample.pixels),
        Err(e) => {
            log::debug!("{}: {e}", label(path));
            Vec::new()
        }
    }
}

/// The quantize-and-convert half of [`histogram`], for an already-decoded
/// sample.
pub fn histogram_of_sample(pixels: &[[u8; 3]]) -> Vec<HistEntry> {
    let q = quantize::quantize(pixels, QUANTIZE_COLORS);
    q.colors()
        .into_iter()
        .map(|(count, index)| {
            let [r, g, b] = q.palette[index].map(|c| c as f64 / 255.0);
            (count, pick::rgb_to_hsv(r, g, b))
        })
        .collect()
}

/// Content-derived half of extraction: decode, quantize, rank, pick.
///
/// Mode-free by construction. Returns `(vec![], 0)` for a cover that yields
/// no extractable colors; [`render_palette`] maps that to the default accent.
pub fn select_palette(path: &Path) -> Picks {
    let hist = histogram(path);
    if hist.is_empty() {
        // Logged here rather than at the caller so a memoized failure is
        // reported once per cover instead of once per theme flip.
        log::warn!(
            "no extractable colors from {}; using default palette",
            label(path)
        );
        return (Vec::new(), 0);
    }
    pick::select(&hist)
}

/// Mode-derived half of extraction: tone into the mode's Oklab envelope, then
/// separate. `picks` are source OkLCh colors from selection; the first
/// `n_distinct` are real, any others repeat the last real one. `label` names
/// the cover in diagnostics only.
pub fn render_palette(picks: &[Lch], n_distinct: usize, mode: Mode, label: &str) -> [String; 3] {
    if picks.is_empty() {
        return [DEFAULT_ACCENT; 3].map(String::from);
    }
    let (slots, result) = separate(&tone(picks, mode), mode, n_distinct);
    if !result.resolved && result.reason != SeparationReason::Duplicates {
        // An unseparable palette is an accepted outcome, but never a silent one.
        log::debug!(
            "palette for {label} left a pair at dE {:.3} ({})",
            result.residual_de,
            result.reason.as_str()
        );
    }
    std::array::from_fn(|i| slots[i].to_hex())
}

/// The three most prominent, visibly distinct colors of a cover, toned for
/// `mode`. Uncached: the reference [`PaletteMemo`] must agree with.
pub fn extract_colors(path: &Path, mode: Mode) -> [String; 3] {
    let (picks, n_distinct) = select_palette(path);
    render_palette(&picks, n_distinct, mode, &label(path))
}

type SelectFn = fn(&Path) -> Picks;
type RenderFn = fn(&[Lch], usize, Mode, &str) -> [String; 3];

/// One-slot memo over the mode-free half of extraction.
///
/// A theme flip re-runs extraction for an unchanged cover (the worker's dedup
/// key includes the mode), but selection does not depend on the mode, so its
/// result is reusable: a flip becomes a retone instead of a decode. One slot,
/// because a flip's access pattern is exactly (cover X, dark) -> (cover X,
/// light). Keyed on content identity, not the path, so an in-place overwrite
/// misses.
///
/// The value is computed before the key is stored: if selection panics, the
/// old key and value stay consistent with each other rather than the new key
/// pointing at the previous cover's picks.
pub struct PaletteMemo<S = SelectFn, R = RenderFn> {
    select: S,
    render: R,
    slot: Option<(ContentId, Picks)>,
}

impl PaletteMemo {
    /// The production wiring: real [`select_palette`] and [`render_palette`].
    pub fn new() -> Self {
        Self::with(select_palette, render_palette)
    }
}

impl Default for PaletteMemo {
    fn default() -> Self {
        Self::new()
    }
}

impl<S, R> PaletteMemo<S, R>
where
    S: FnMut(&Path) -> Picks,
    R: Fn(&[Lch], usize, Mode, &str) -> [String; 3],
{
    /// A memo over injected halves (tests exercise the slot with fakes).
    pub fn with(select: S, render: R) -> Self {
        Self {
            select,
            render,
            slot: None,
        }
    }

    pub fn get(&mut self, path: &Path, mode: Mode, content_id: ContentId) -> [String; 3] {
        if self.slot.as_ref().map(|(k, _)| *k) != Some(content_id) {
            let value = (self.select)(path);
            self.slot = Some((content_id, value));
        }
        let (_, (picks, n)) = self.slot.as_ref().expect("slot filled above");
        (self.render)(picks, *n, mode, &label(path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{ImageBuffer, ImageFormat, Rgb, RgbImage};
    use std::path::PathBuf;
    use tone::{MIN_DE, NEUTRAL_C, envelope};

    fn rgb(hex: &str) -> Rgb<u8> {
        Rgb(oklab::hex_channels(hex))
    }

    fn hsv(hex: &str) -> pick::Hsv {
        let [r, g, b] = oklab::hex_channels(hex).map(|c| c as f64 / 255.0);
        pick::rgb_to_hsv(r, g, b)
    }

    /// Vertical bands of the given widths on a 64x64 canvas.
    fn bands(parts: &[(&str, u32)]) -> RgbImage {
        let mut edges = Vec::new();
        let mut x0 = 0;
        for &(c, w) in parts {
            edges.push((x0, x0 + w, rgb(c)));
            x0 += w;
        }
        ImageBuffer::from_fn(64, 64, |x, _| {
            edges.iter().find(|e| x >= e.0 && x < e.1).expect("band").2
        })
    }

    fn solid(c: &str) -> RgbImage {
        bands(&[(c, 64)])
    }
    fn halves(a: &str, b: &str) -> RgbImage {
        bands(&[(a, 32), (b, 32)])
    }
    fn thirds(a: &str, b: &str, c: &str) -> RgbImage {
        bands(&[(a, 22), (b, 22), (c, 20)])
    }

    struct Tmp(PathBuf);
    impl Tmp {
        fn new(tag: &str) -> Self {
            let dir = std::env::temp_dir()
                .join(format!("mpris-chroma-color-{tag}-{}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            Tmp(dir)
        }
        fn save(&self, name: &str, img: &RgbImage) -> PathBuf {
            let p = self.0.join(name);
            img.save(&p).unwrap();
            p
        }
        fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
            let p = self.0.join(name);
            std::fs::write(&p, bytes).unwrap();
            p
        }
    }
    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    const DEFAULT: [&str; 3] = [DEFAULT_ACCENT; 3];

    // --- extract (ExtractTest) -------------------------------------------

    #[test]
    fn extract_returns_three_valid_hex() {
        let t = Tmp::new("valid");
        for c in extract_colors(&t.save("v.png", &solid("#e01050")), Mode::Dark) {
            assert_eq!(c.len(), 7);
            assert!(c.starts_with('#') && u32::from_str_radix(&c[1..], 16).is_ok());
        }
    }

    #[test]
    fn dark_colored_cover_is_lifted_to_readable() {
        let t = Tmp::new("dark");
        let [c1, _, _] = extract_colors(&t.save("d.png", &solid("#3a0d0d")), Mode::Dark);
        let (l, c, _) = oklab::hex_to_lch(&c1);
        let (lo, hi) = envelope(Mode::Dark);
        assert!(l >= lo - 1e-9 && l <= hi + 1e-9);
        assert!(c > NEUTRAL_C);
    }

    #[test]
    fn grayscale_cover_stays_neutral() {
        let t = Tmp::new("gray");
        let p = t.save("g.png", &thirds("#202020", "#808080", "#d0d0d0"));
        for c in extract_colors(&p, Mode::Dark) {
            assert!(oklab::hex_to_lch(&c).1 < NEUTRAL_C);
        }
    }

    #[test]
    fn two_tone_cover_yields_distinct_hues() {
        let t = Tmp::new("two");
        let [c1, c2, _] =
            extract_colors(&t.save("t.png", &halves("#e01010", "#1010e0")), Mode::Dark);
        assert!((hsv(&c1).0 - hsv(&c2).0).abs() > 0.05);
    }

    #[test]
    fn three_color_cover_yields_three_distinct_slots() {
        let t = Tmp::new("three");
        let p = t.save("t.png", &thirds("#e01010", "#10e010", "#1010e0"));
        let [a, b, c] = extract_colors(&p, Mode::Dark);
        assert!(a != b && b != c && a != c);
    }

    #[test]
    fn light_mode_same_hues_brighter_values() {
        let t = Tmp::new("lm");
        let p = t.save("t.png", &thirds("#e01010", "#10e010", "#1010e0"));
        let dark = extract_colors(&p, Mode::Dark);
        let light = extract_colors(&p, Mode::Light);
        for (d, l) in dark.iter().zip(&light) {
            assert!((oklab::hex_to_lch(d).2 - oklab::hex_to_lch(l).2).abs() < 0.05);
        }
        let (lo, _) = envelope(Mode::Light);
        for l in &light {
            assert!(oklab::hex_to_lch(l).0 >= lo - 1e-9);
        }
    }

    #[test]
    fn small_vivid_accent_makes_the_palette() {
        let t = Tmp::new("accent");
        let mut img = thirds("#202a33", "#332028", "#2a3320");
        for y in 27..37 {
            for x in 27..37 {
                img.put_pixel(x, y, rgb("#ff6a00"));
            }
        }
        let accent = hsv("#ff6a00").0;
        let hues: Vec<f64> = extract_colors(&t.save("a.png", &img), Mode::Dark)
            .iter()
            .map(|c| hsv(c).0)
            .collect();
        assert!(
            hues.iter().any(|h| (h - accent).abs() < 0.04),
            "orange missing from {hues:?}"
        );
    }

    #[test]
    fn solid_cover_repeats_not_invents() {
        let t = Tmp::new("solid");
        let [a, b, c] = extract_colors(&t.save("s.png", &solid("#c81e5a")), Mode::Dark);
        assert!(a == b && b == c);
    }

    // --- mode independence -----------------------------------------------

    #[test]
    fn both_modes_tone_the_same_picked_hues() {
        // pick::select has no mode parameter at all, which is the structural
        // guarantee; this checks the rendered hues stay on the picks' hues.
        let hist = [
            (100, (0.70, 0.60, 0.30)),
            (50, (0.70, 0.60, 0.62)),
            (10, (0.10, 0.80, 0.50)),
        ];
        let (picks, n) = pick::select(&hist);
        for mode in [Mode::Dark, Mode::Light] {
            for (src, out) in picks.iter().zip(render_palette(&picks, n, mode, "h")) {
                assert!((oklab::hex_to_lch(&out).2 - src.2).abs() < 0.01, "{out}");
            }
        }
    }

    // --- split pipeline --------------------------------------------------

    #[test]
    fn select_palette_returns_picks_with_count() {
        let t = Tmp::new("sp");
        let (picks, n) = select_palette(&t.save("s.png", &thirds("#d12b2b", "#2b7fd1", "#e0d020")));
        assert_eq!((picks.len(), n), (3, 3));
    }

    #[test]
    fn select_palette_reports_empty_for_an_unreadable_cover() {
        let t = Tmp::new("unread");
        assert_eq!(
            select_palette(&t.write("not.png", b"this is not an image")),
            (vec![], 0)
        );
    }

    #[test]
    fn render_palette_maps_the_empty_selection_to_the_default() {
        assert_eq!(render_palette(&[], 0, Mode::Dark, "x"), DEFAULT);
    }

    #[test]
    fn render_palette_composes_back_into_extract_colors() {
        let t = Tmp::new("compose");
        let p = t.save("c.png", &thirds("#d12b2b", "#2b7fd1", "#e0d020"));
        for mode in [Mode::Dark, Mode::Light] {
            let (picks, n) = select_palette(&p);
            assert_eq!(
                render_palette(&picks, n, mode, "c"),
                extract_colors(&p, mode)
            );
        }
    }

    #[test]
    fn the_default_memo_wiring_matches_uncached_extract() {
        let t = Tmp::new("wiring");
        let p = t.save("w.png", &thirds("#d12b2b", "#2b7fd1", "#e0d020"));
        let mut memo = PaletteMemo::new();
        for mode in [Mode::Dark, Mode::Light] {
            assert_eq!(memo.get(&p, mode, (1, 1)), extract_colors(&p, mode));
        }
    }

    // --- pipeline invariants ---------------------------------------------

    #[test]
    fn distinct_cover_yields_separated_slots() {
        let t = Tmp::new("sep");
        let p = t.save("d.png", &thirds("#d1a973", "#b89265", "#9d7156"));
        let labs: Vec<_> = extract_colors(&p, Mode::Dark)
            .iter()
            .map(|c| {
                let (l, ch, h) = oklab::hex_to_lch(c);
                oklab::from_lch(l, ch, h)
            })
            .collect();
        for i in 0..3 {
            for j in i + 1..3 {
                assert!(oklab::delta_e(labs[i], labs[j]) >= MIN_DE - 1e-9);
            }
        }
    }

    #[test]
    fn bright_cover_no_longer_washes_out() {
        let t = Tmp::new("bright");
        let [a, b, c] = extract_colors(
            &t.save("b.png", &thirds("#f2f2f2", "#e8e8e8", "#fafafa")),
            Mode::Dark,
        );
        assert!(ramp::mean_luminance(&a, &b, &c) < 0.25);
    }

    #[test]
    fn every_slot_is_in_gamut() {
        let t = Tmp::new("gamut");
        let p = t.save("g.png", &thirds("#ffee00", "#00e5ff", "#1010e0"));
        for mode in [Mode::Dark, Mode::Light] {
            let (picks, n) = pick::select(&histogram(&p));
            let (slots, _) = separate(&tone(&picks, mode), mode, n);
            for s in slots {
                let (l, a, b) = s.to_lab();
                assert!(oklab::in_gamut(l, a, b));
                let (hl, hc, hh) = oklab::hex_to_lch(&s.to_hex());
                assert!(oklab::delta_e(s.to_lab(), oklab::from_lch(hl, hc, hh)) < 0.005);
            }
        }
    }

    // --- format allowlist (SEC-005) --------------------------------------

    #[test]
    fn accepts_png_jpeg_and_webp() {
        let t = Tmp::new("formats");
        let img = solid("#e01050");
        for (name, fmt) in [
            ("c.png", ImageFormat::Png),
            ("c.jpg", ImageFormat::Jpeg),
            ("c.webp", ImageFormat::WebP),
        ] {
            let p = t.0.join(name);
            img.save_with_format(&p, fmt).unwrap();
            let got = extract_colors(&p, Mode::Dark);
            assert_ne!(got, DEFAULT, "{name}");
        }
    }

    #[test]
    fn rejects_non_images_whatever_the_extension() {
        let t = Tmp::new("reject");
        for (name, bytes) in [
            ("evil.jpg", &b"<!DOCTYPE html>\n<html><body>not an image</body></html>"[..]),
            (
                "vector.svg",
                b"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"64\" height=\"64\"><rect width=\"64\" height=\"64\" fill=\"#e01050\"/></svg>",
            ),
            ("doc.pdf", b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"),
            ("broken.png", b"\x89PNG\r\n\x1a\n\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0"),
        ] {
            assert_eq!(extract_colors(&t.write(name, bytes), Mode::Dark), DEFAULT, "{name}");
        }
    }

    // --- decode bounds (SEC-006) -----------------------------------------

    /// A PNG whose IHDR declares `w` x `h` followed by an empty IDAT: no
    /// pixel data at all, so only a header-first check can refuse it for its
    /// size (a decode would fail as corrupt instead).
    fn png_header_only(w: u32, h: u32) -> Vec<u8> {
        let chunk = |kind: &[u8], data: &[u8]| {
            let mut body = kind.to_vec();
            body.extend(data);
            let mut out = (data.len() as u32).to_be_bytes().to_vec();
            out.extend(&body);
            out.extend(crc32(&body).to_be_bytes());
            out
        };
        let mut ihdr = w.to_be_bytes().to_vec();
        ihdr.extend(h.to_be_bytes());
        ihdr.extend([8, 2, 0, 0, 0]); // 8-bit RGB
        let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
        out.extend(chunk(b"IHDR", &ihdr));
        out.extend(chunk(b"IDAT", &[]));
        out
    }

    fn crc32(data: &[u8]) -> u32 {
        let mut c = 0xffff_ffffu32;
        for &b in data {
            c ^= b as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 {
                    0xedb8_8320 ^ (c >> 1)
                } else {
                    c >> 1
                };
            }
        }
        !c
    }

    #[test]
    fn rejects_oversized_dimensions_from_the_header() {
        const { assert!(4100u64 * 4100 > decode::MAX_PIXELS) };
        let bytes = png_header_only(4100, 4100);
        assert!(matches!(
            decode::decode(&bytes),
            Err(decode::DecodeError::TooManyPixels)
        ));
        let t = Tmp::new("bomb");
        assert_eq!(
            extract_colors(&t.write("bomb.png", &bytes), Mode::Dark),
            DEFAULT
        );
    }

    #[test]
    fn rejects_oversized_file_bytes() {
        let t = Tmp::new("huge");
        let p = t.save("h.png", &solid("#e01050"));
        assert!(matches!(
            decode::sample_file(&p, 8),
            Err(decode::DecodeError::TooLarge)
        ));
    }

    #[test]
    fn within_budget_still_extracts() {
        let t = Tmp::new("ok");
        let img: RgbImage = ImageBuffer::from_pixel(640, 640, rgb("#e01050"));
        assert_ne!(extract_colors(&t.save("ok.png", &img), Mode::Dark), DEFAULT);
    }

    #[test]
    fn dash_prefixed_path_is_a_file_not_an_option() {
        let t = Tmp::new("dash");
        assert_ne!(
            extract_colors(&t.save("-dash.png", &solid("#e01050")), Mode::Dark),
            DEFAULT
        );
    }

    // --- palette memo (test_palette_memo.py) -----------------------------

    fn fake_render(_: &[Lch], _: usize, mode: Mode, _: &str) -> [String; 3] {
        [mode.as_str(); 3].map(String::from)
    }

    fn fake_pick() -> Picks {
        (vec![(0.5, 0.1, 1.0)], 1)
    }

    #[test]
    fn same_content_id_selects_once_across_two_modes() {
        let mut calls = 0;
        let mut memo = PaletteMemo::with(
            |_: &Path| {
                calls += 1;
                fake_pick()
            },
            fake_render,
        );
        let p = Path::new("/c/a.jpg");
        assert_eq!(memo.get(p, Mode::Dark, (10, 100)), ["dark"; 3]);
        assert_eq!(memo.get(p, Mode::Light, (10, 100)), ["light"; 3]);
        drop(memo);
        assert_eq!(calls, 1);
    }

    #[test]
    fn misses_on_new_content_id_even_for_the_same_path() {
        let mut calls = 0;
        let mut memo = PaletteMemo::with(
            |_: &Path| {
                calls += 1;
                fake_pick()
            },
            fake_render,
        );
        memo.get(Path::new("/c/a.jpg"), Mode::Dark, (10, 100));
        memo.get(Path::new("/c/a.jpg"), Mode::Dark, (10, 999)); // overwritten in place
        memo.get(Path::new("/c/b.jpg"), Mode::Dark, (20, 200));
        drop(memo);
        assert_eq!(calls, 3);
    }

    #[test]
    fn identity_is_the_key_not_the_path() {
        let mut calls = 0;
        let mut memo = PaletteMemo::with(
            |_: &Path| {
                calls += 1;
                fake_pick()
            },
            fake_render,
        );
        memo.get(Path::new("/c/a.jpg"), Mode::Dark, (10, 100));
        memo.get(Path::new("/c/b.jpg"), Mode::Dark, (10, 100));
        drop(memo);
        assert_eq!(calls, 1);
    }

    #[test]
    fn an_unextractable_cover_is_cached_as_the_default() {
        let mut calls = 0;
        let mut memo = PaletteMemo::with(
            |_: &Path| {
                calls += 1;
                (Vec::new(), 0)
            },
            render_palette,
        );
        let p = Path::new("/c/bad.jpg");
        assert_eq!(memo.get(p, Mode::Dark, (20, 1)), DEFAULT);
        assert_eq!(memo.get(p, Mode::Light, (20, 1)), DEFAULT);
        drop(memo);
        assert_eq!(calls, 1);
    }

    #[test]
    fn a_panicking_select_does_not_poison_the_slot() {
        use std::cell::Cell;
        use std::panic::{AssertUnwindSafe, catch_unwind};
        let calls = Cell::new(0);
        let boom = Cell::new(false);
        let mut memo = PaletteMemo::with(
            |_: &Path| {
                calls.set(calls.get() + 1);
                assert!(!boom.get(), "decode blew up");
                (vec![(0.5, 0.1, 1.0)], calls.get())
            },
            |_: &[Lch], n: usize, _: Mode, _: &str| [n.to_string(), n.to_string(), n.to_string()],
        );
        let first = memo.get(Path::new("/a.jpg"), Mode::Dark, (10, 100));
        boom.set(true);
        let r = catch_unwind(AssertUnwindSafe(|| {
            memo.get(Path::new("/b.jpg"), Mode::Dark, (20, 200))
        }));
        assert!(r.is_err());
        boom.set(false);
        // A's key was never overwritten: still a hit.
        assert_eq!(memo.get(Path::new("/a.jpg"), Mode::Light, (10, 100)), first);
        assert_eq!(calls.get(), 2);
        // And B was not recorded as selected: it re-selects.
        memo.get(Path::new("/b.jpg"), Mode::Dark, (20, 200));
        assert_eq!(calls.get(), 3);
    }
}
