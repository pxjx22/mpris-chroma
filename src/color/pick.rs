//! Ranking a quantized histogram and picking up to three distinct source
//! colors (port of `colors._vibrancy_score` / `colors._select`, plus the two
//! `colorsys` functions they depend on).

use super::oklab::{self, Lch};

/// How much chroma (s*v) counts vs. pixel coverage; 0.0 restores pure
/// most-pixels-wins ranking.
pub const VIBRANCY_WEIGHT: f64 = 0.5;
/// Coverage below this gets no vibrancy boost, so a vivid noise speck can't
/// jump the queue.
pub const VIBRANCY_MIN_POP: f64 = 0.01;
/// Minimum perceptual distance between two *source* picks. Lower than
/// `tone::MIN_DE` on purpose: separation repairs collisions toning creates,
/// so selection only rejects picks already near-identical at the source.
pub const SELECT_MIN_DE: f64 = 0.08;

pub type Hsv = (f64, f64, f64);

/// One quantized palette entry: how many sample pixels map to it, and its
/// color as HSV (0-1 channels).
pub type HistEntry = (u32, Hsv);

/// Python's float `%` for a positive divisor: the result takes the
/// divisor's sign, and an exact zero is +0.0.
fn py_mod1(x: f64) -> f64 {
    let m = x % 1.0;
    if m == 0.0 {
        0.0
    } else if m < 0.0 {
        m + 1.0
    } else {
        m
    }
}

/// `colorsys.rgb_to_hsv`, operation for operation.
pub fn rgb_to_hsv(r: f64, g: f64, b: f64) -> Hsv {
    let maxc = r.max(g).max(b);
    let minc = r.min(g).min(b);
    let rangec = maxc - minc;
    let v = maxc;
    if minc == maxc {
        return (0.0, 0.0, v);
    }
    let s = rangec / maxc;
    let rc = (maxc - r) / rangec;
    let gc = (maxc - g) / rangec;
    let bc = (maxc - b) / rangec;
    let h = if r == maxc {
        bc - gc
    } else if g == maxc {
        2.0 + rc - bc
    } else {
        4.0 + gc - rc
    };
    (py_mod1(h / 6.0), s, v)
}

/// `colorsys.hsv_to_rgb`, operation for operation.
pub fn hsv_to_rgb(h: f64, s: f64, v: f64) -> (f64, f64, f64) {
    if s == 0.0 {
        return (v, v, v);
    }
    let i = (h * 6.0).trunc();
    let f = h * 6.0 - i;
    let p = v * (1.0 - s);
    let q = v * (1.0 - s * f);
    let t = v * (1.0 - s * (1.0 - f));
    match (i as i64).rem_euclid(6) {
        0 => (v, t, p),
        1 => (q, v, p),
        2 => (p, v, t),
        3 => (p, q, v),
        4 => (t, p, v),
        _ => (v, p, q),
    }
}

/// Rank a histogram entry by coverage plus a vibrancy bonus.
///
/// Pure pixel-count ranking finds backgrounds, not identity: adding chroma
/// (s*v) lets a small vivid accent outrank a large drab region, while
/// grayscale entries (chroma 0) keep pure coverage ranking.
pub fn vibrancy_score(count: u32, total: u64, hsv: Hsv) -> f64 {
    let frac = count as f64 / total as f64;
    if frac < VIBRANCY_MIN_POP {
        return frac;
    }
    let (_, s, v) = hsv;
    frac + VIBRANCY_WEIGHT * s * v
}

/// Rank the histogram and pick up to three distinct source colors.
///
/// Returns the picks as OkLCh plus how many of them are *real*; fewer than
/// three are padded by repeating the last. Deliberately takes no mode: mode
/// may only re-tone the same picks, never change which ones they are.
/// An empty histogram returns `(vec![], 0)`.
pub fn select(hist: &[HistEntry]) -> (Vec<Lch>, usize) {
    if hist.is_empty() {
        return (Vec::new(), 0);
    }
    let total: u64 = hist.iter().map(|&(c, _)| c as u64).sum();
    let mut ranked: Vec<(f64, Hsv)> = hist
        .iter()
        .map(|&(count, hsv)| (vibrancy_score(count, total, hsv), hsv))
        .collect();
    // Stable and descending, as Python's sorted(..., reverse=True) is: equal
    // scores keep histogram (palette index) order.
    ranked.sort_by(|a, b| b.0.total_cmp(&a.0));

    let mut picked: Vec<Lch> = Vec::with_capacity(3);
    let mut labs: Vec<oklab::Lab> = Vec::with_capacity(3);
    for (_, (h, s, v)) in ranked {
        if picked.len() == 3 {
            break;
        }
        let (r, g, b) = hsv_to_rgb(h, s, v);
        let (l, a, bb) = oklab::srgb_to_oklab(r, g, b);
        let lch = oklab::to_lch(l, a, bb);
        let lab = oklab::from_lch(lch.0, lch.1, lch.2);
        if labs
            .iter()
            .all(|&p| oklab::delta_e(lab, p) >= SELECT_MIN_DE)
        {
            picked.push(lch);
            labs.push(lab);
        }
    }
    let n_distinct = picked.len();
    // Repeat the last real color rather than fabricate a hue that isn't there.
    let last = picked[n_distinct - 1];
    picked.resize(3, last);
    (picked, n_distinct)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hsv_of(hex: &str) -> Hsv {
        let ch = |i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap() as f64 / 255.0;
        rgb_to_hsv(ch(1), ch(3), ch(5))
    }

    #[test]
    fn hsv_matches_colorsys_reference_values() {
        // colorsys.rgb_to_hsv values, printed by CPython.
        assert_eq!(rgb_to_hsv(1.0, 0.0, 0.0), (0.0, 1.0, 1.0));
        assert_eq!(rgb_to_hsv(0.0, 0.0, 1.0), (2.0 / 3.0, 1.0, 1.0));
        assert_eq!(rgb_to_hsv(0.5, 0.5, 0.5), (0.0, 0.0, 0.5));
        // Magenta: bc-gc < 0 exercises the Python-style modulo.
        let (h, _, _) = rgb_to_hsv(1.0, 0.0, 0.5);
        assert!((h - 0.9166666666666666).abs() < 1e-15);
    }

    #[test]
    fn hsv_round_trips() {
        for rgb in [
            (0.2, 0.6, 0.9),
            (0.9, 0.1, 0.4),
            (0.0, 0.0, 0.0),
            (1.0, 1.0, 0.0),
        ] {
            let (h, s, v) = rgb_to_hsv(rgb.0, rgb.1, rgb.2);
            let back = hsv_to_rgb(h, s, v);
            assert!((back.0 - rgb.0).abs() < 1e-12);
            assert!((back.1 - rgb.1).abs() < 1e-12);
            assert!((back.2 - rgb.2).abs() < 1e-12);
        }
    }

    #[test]
    fn empty_histogram_yields_no_picks() {
        assert_eq!(select(&[]), (vec![], 0));
    }

    #[test]
    fn solid_color_is_padded_not_invented() {
        let (picks, n) = select(&[(10_000, hsv_of("#c81e5a"))]);
        assert_eq!(n, 1);
        assert_eq!(picks.len(), 3);
        assert!(picks.iter().all(|&p| p == picks[0]));
    }

    #[test]
    fn near_duplicates_are_collapsed() {
        let (_, n) = select(&[(5000, hsv_of("#808080")), (5000, hsv_of("#828282"))]);
        assert_eq!(n, 1);
    }

    #[test]
    fn small_vivid_accent_beats_large_drab_background() {
        // Background 0.50 (grey, no bonus) vs red 0.10 + 0.5*s*v ~ 0.59.
        let hist = [
            (5000, hsv_of("#202020")),
            (4000, hsv_of("#606060")),
            (1000, hsv_of("#ff2020")),
        ];
        let (picks, _) = select(&hist);
        let red = oklab::hex_to_lch("#ff2020");
        assert!(
            (picks[0].0 - red.0).abs() < 1e-9,
            "the red accent ranks first"
        );
    }

    #[test]
    fn speck_below_min_pop_gets_no_vibrancy_boost() {
        // 0.5% of pixels: vivid, but ranked by coverage alone.
        assert_eq!(vibrancy_score(50, 10_000, (0.0, 1.0, 1.0)), 0.005);
        assert_eq!(vibrancy_score(200, 10_000, (0.0, 1.0, 1.0)), 0.02 + 0.5);
    }
}
