//! Image pipeline parity with Pillow, on synthetic covers.
//!
//! The fixtures were recorded from the Python implementation this crate
//! replaced (`tools/dump_image_golden.py` at commit 992678e; sources in
//! `fixtures/images/`, Pillow's 100x100 samples in `fixtures/samples/`). Three
//! layers, so a failure says which stage diverged:
//!
//! 1. On Pillow's own sample: quantize, pick and render must match exactly.
//!    This isolates the quantizer and selection from decoder differences.
//! 2. Lossless sources (PNG, lossless WebP): decode + convert + resize must
//!    reproduce Pillow's sample byte for byte, so the whole pipeline matches.
//! 3. Lossy sources (JPEG, lossy WebP): different decoders (and an emulated
//!    JPEG draft) shift the sample by a few levels; the final palette must
//!    stay within `LOSSY_MAX_DE` of Pillow's.

use std::path::{Path, PathBuf};

use mpris_chroma::color::{self, decode, oklab, pick, quantize, render_palette};
use mpris_chroma::state::Mode;
use serde::Deserialize;
use sha2::{Digest, Sha256};

#[derive(Deserialize)]
struct Golden {
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    format: String,
    sample: String,
    sample_sha256: String,
    palette: Vec<[u8; 3]>,
    colors: Vec<(u32, usize)>,
    picks: Vec<[f64; 3]>,
    n_distinct: usize,
    dark: [String; 3],
    light: [String; 3],
}

impl Case {
    fn lossless(&self) -> bool {
        self.format == "PNG" || self.name.ends_with("_lossless.webp")
    }
}

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn golden() -> Golden {
    let text = std::fs::read_to_string(fixtures().join("image_golden.json")).expect("fixture");
    serde_json::from_str(&text).expect("parse fixture")
}

fn sha(pixels: &[[u8; 3]]) -> String {
    let mut h = Sha256::new();
    for p in pixels {
        h.update(p);
    }
    format!("{:x}", h.finalize())
}

fn load_sample(case: &Case) -> Vec<[u8; 3]> {
    let img = image::open(fixtures().join(&case.sample))
        .expect("sample")
        .to_rgb8();
    assert_eq!(img.dimensions(), decode::SAMPLE, "{}", case.name);
    img.pixels().map(|p| p.0).collect()
}

#[test]
fn quantize_pick_render_match_pillow_on_its_own_sample() {
    for case in golden().cases {
        let n = &case.name;
        let px = load_sample(&case);
        assert_eq!(sha(&px), case.sample_sha256, "{n}: sample fixture read");

        let q = quantize::quantize(&px, color::QUANTIZE_COLORS);
        assert_eq!(q.palette, case.palette, "{n}: palette");
        assert_eq!(q.colors(), case.colors, "{n}: getcolors");

        let (picks, n_distinct) = pick::select(&color::histogram_of_sample(&px));
        assert_eq!(n_distinct, case.n_distinct, "{n}: n_distinct");
        for (got, want) in picks.iter().zip(&case.picks) {
            let got = [got.0, got.1, got.2];
            // Hue of a neutral (C ~ 1e-9) is atan2 of rounding noise; it
            // carries no color, and the exact hex checks below cover it.
            let dims = if want[1] > 1e-6 { 3 } else { 2 };
            for k in 0..dims {
                assert!(
                    (got[k] - want[k]).abs() < 1e-12,
                    "{n}: pick {got:?} vs {want:?}"
                );
            }
        }
        assert_eq!(
            render_palette(&picks, n_distinct, Mode::Dark, n),
            case.dark,
            "{n}: dark"
        );
        assert_eq!(
            render_palette(&picks, n_distinct, Mode::Light, n),
            case.light,
            "{n}: light"
        );
    }
}

#[test]
fn lossless_sources_reproduce_pillows_sample_and_palette_exactly() {
    let mut checked = 0;
    for case in golden().cases.into_iter().filter(Case::lossless) {
        let n = &case.name;
        let src = fixtures().join("images").join(n);
        let sample = decode::sample_file(&src, decode::MAX_DECODE_BYTES).expect(n);
        assert_eq!(
            sha(&sample.pixels),
            case.sample_sha256,
            "{n}: decode+resize"
        );
        assert_eq!(
            color::extract_colors(&src, Mode::Dark),
            case.dark,
            "{n}: dark"
        );
        assert_eq!(
            color::extract_colors(&src, Mode::Light),
            case.light,
            "{n}: light"
        );
        checked += 1;
    }
    assert!(checked >= 12, "only {checked} lossless cases");
}

/// Largest Oklab distance allowed between a Rust and a Pillow palette slot
/// for lossy sources. Just-noticeable is ~0.02; see the test for measured
/// values.
const LOSSY_MAX_DE: f64 = 0.03;

fn slot_de(a: &str, b: &str) -> f64 {
    let lab = |h: &str| {
        let (l, c, hh) = oklab::hex_to_lch(h);
        oklab::from_lch(l, c, hh)
    };
    oklab::delta_e(lab(a), lab(b))
}

#[test]
fn lossy_sources_land_close_to_pillows_palette() {
    let mut checked = 0;
    for case in golden().cases.into_iter().filter(|c| !c.lossless()) {
        let n = &case.name;
        let src = fixtures().join("images").join(n);
        let sample = decode::sample_file(&src, decode::MAX_DECODE_BYTES).expect(n);
        let want = load_sample(&case);
        let mad = sample
            .pixels
            .iter()
            .zip(&want)
            .flat_map(|(a, b)| (0..3).map(move |k| (a[k] as i32 - b[k] as i32).abs()))
            .sum::<i32>() as f64
            / (want.len() * 3) as f64;
        for (mode, expected) in [(Mode::Dark, &case.dark), (Mode::Light, &case.light)] {
            let got = color::extract_colors(&src, mode);
            let worst = got
                .iter()
                .zip(expected)
                .map(|(g, e)| slot_de(g, e))
                .fold(0.0, f64::max);
            eprintln!(
                "{n} {}: sample MAD {mad:.2}, worst slot dE {worst:.4} ({got:?} vs {expected:?})",
                mode.as_str()
            );
            assert!(worst <= LOSSY_MAX_DE, "{n} {}: dE {worst}", mode.as_str());
        }
        checked += 1;
    }
    assert!(checked >= 4, "only {checked} lossy cases");
}
