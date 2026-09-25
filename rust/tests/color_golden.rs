//! Parity with the Python pipeline over a seeded corpus.
//!
//! Fixtures come from `tools/dump_golden.py` (regenerate from the repo root
//! with `python tools/dump_golden.py > rust/tests/fixtures/color_golden.json`).
//! Hex strings, reasons and flags must match exactly; floats must match to
//! `EPS`, which allows for ulp-level libm differences (cbrt, pow, hypot)
//! between CPython and Rust while catching any real divergence.

use mpris_chroma::color::{oklab, ramp, render_palette, tone};
use mpris_chroma::state::Mode;
use serde::Deserialize;

const EPS: f64 = 1e-10;

#[derive(Deserialize)]
struct Golden {
    hex: Vec<HexCase>,
    max_chroma: Vec<MaxChromaCase>,
    render: Vec<RenderCase>,
    separate: Vec<SeparateCase>,
    ramp: Vec<RampCase>,
}

#[derive(Deserialize)]
struct HexCase {
    hex: String,
    lch: [f64; 3],
    round_trip: String,
}

#[derive(Deserialize)]
struct MaxChromaCase {
    #[serde(rename = "L")]
    l: f64,
    h: f64,
    max_chroma: f64,
}

#[derive(Deserialize)]
struct RenderCase {
    source_hex: Vec<String>,
    picks: Vec<[f64; 3]>,
    n_distinct: usize,
    mode: String,
    #[serde(rename = "toned_L")]
    toned_l: Vec<f64>,
    #[serde(rename = "separated_L")]
    separated_l: Vec<f64>,
    hex: Vec<String>,
    resolved: bool,
    reason: String,
    residual_de: f64,
}

#[derive(Deserialize)]
struct SeparateCase {
    mode: String,
    n_distinct: usize,
    input: Vec<[f64; 3]>,
    #[serde(rename = "separated_L")]
    separated_l: Vec<f64>,
    hex: Vec<String>,
    resolved: bool,
    reason: String,
    residual_de: f64,
}

#[derive(Deserialize)]
struct RampCase {
    palette: [String; 3],
    cells: Vec<String>,
    mean_luminance: f64,
}

fn golden() -> Golden {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/color_golden.json"
    );
    serde_json::from_str(&std::fs::read_to_string(path).expect("read fixture"))
        .expect("parse fixture")
}

fn assert_close(got: f64, want: f64, what: &str) {
    assert!(
        (got - want).abs() <= EPS,
        "{what}: got {got:e}, python {want:e} (diff {:e})",
        (got - want).abs()
    );
}

fn assert_all_close(got: &[f64], want: &[f64], what: &str) {
    assert_eq!(got.len(), want.len(), "{what}: length");
    for (i, (&g, &w)) in got.iter().zip(want).enumerate() {
        assert_close(g, w, &format!("{what}[{i}]"));
    }
}

fn lch(v: &[f64; 3]) -> oklab::Lch {
    (v[0], v[1], v[2])
}

#[test]
fn hex_conversion_matches_python() {
    for case in golden().hex {
        let got = oklab::hex_to_lch(&case.hex);
        assert_close(got.0, case.lch[0], &format!("{} L", case.hex));
        assert_close(got.1, case.lch[1], &format!("{} C", case.hex));
        // Hue of a neutral is atan2 of rounding noise (C ~ 1e-8 for #808080),
        // so an ulp of cbrt difference swings it by ~1e-9. It carries no color
        // there; the exact round-trip hex below is the meaningful check.
        if case.lch[1] > 1e-6 {
            assert_close(got.2, case.lch[2], &format!("{} h", case.hex));
        }
        assert_eq!(
            oklab::lch_to_hex(got.0, got.1, got.2),
            case.round_trip,
            "{}",
            case.hex
        );
    }
}

#[test]
fn max_chroma_matches_python() {
    for case in golden().max_chroma {
        let what = format!("max_chroma(L={}, h={})", case.l, case.h);
        assert_close(oklab::max_chroma(case.l, case.h), case.max_chroma, &what);
    }
}

#[test]
fn render_pipeline_matches_python() {
    for case in golden().render {
        let mode: Mode = case.mode.parse().unwrap();
        let what = format!("{:?} {}", case.source_hex, case.mode);
        let picks: Vec<_> = case.picks.iter().map(lch).collect();

        let toned = tone::tone(&picks, mode);
        let toned_l: Vec<f64> = toned.iter().map(|s| s.l).collect();
        assert_all_close(&toned_l, &case.toned_l, &format!("{what} toned L"));

        let (slots, result) = tone::separate(&toned, mode, case.n_distinct);
        let sep_l: Vec<f64> = slots.iter().map(|s| s.l).collect();
        assert_all_close(&sep_l, &case.separated_l, &format!("{what} separated L"));
        assert_eq!(result.reason.as_str(), case.reason, "{what} reason");
        assert_eq!(result.resolved, case.resolved, "{what} resolved");
        assert_close(
            result.residual_de,
            case.residual_de,
            &format!("{what} residual"),
        );

        let hex = render_palette(&picks, case.n_distinct, mode, "golden");
        assert_eq!(hex.to_vec(), case.hex, "{what} hex");
    }
}

#[test]
fn separate_on_raw_input_matches_python() {
    for (n, case) in golden().separate.into_iter().enumerate() {
        let mode: Mode = case.mode.parse().unwrap();
        let what = format!("separate case {n} ({})", case.mode);
        let slots: Vec<_> = case
            .input
            .iter()
            .map(|&[l, h, c_src]| tone::Toned { l, h, c_src })
            .collect();

        let (out, result) = tone::separate(&slots, mode, case.n_distinct);
        let got_l: Vec<f64> = out.iter().map(|s| s.l).collect();
        assert_all_close(&got_l, &case.separated_l, &what);
        let hex: Vec<String> = out.iter().map(|s| s.to_hex()).collect();
        assert_eq!(hex, case.hex, "{what} hex");
        assert_eq!(result.reason.as_str(), case.reason, "{what} reason");
        assert_eq!(result.resolved, case.resolved, "{what} resolved");
        assert_close(
            result.residual_de,
            case.residual_de,
            &format!("{what} residual"),
        );
    }
}

#[test]
fn ramp_matches_python() {
    for case in golden().ramp {
        let [a, b, c] = &case.palette;
        assert_eq!(ramp::expand(a, b, c), case.cells, "{:?}", case.palette);
        assert_close(
            ramp::mean_luminance(a, b, c),
            case.mean_luminance,
            &format!("{:?} luminance", case.palette),
        );
    }
}

#[test]
fn fixture_covers_every_separation_outcome() {
    // If a regenerated corpus stops reaching an outcome, the parity tests
    // above silently stop checking it; fail loudly instead.
    let g = golden();
    let reasons: std::collections::HashSet<String> = g
        .render
        .iter()
        .map(|c| c.reason.clone())
        .chain(g.separate.iter().map(|c| c.reason.clone()))
        .collect();
    for want in ["clear", "duplicates", "budget", "blocked", "passes"] {
        assert!(reasons.contains(want), "fixture never reaches {want:?}");
    }
}
