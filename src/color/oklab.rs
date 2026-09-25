//! Oklab / OkLCh color space (port of `oklab.py`).
//!
//! Palette work needs three things HSV cannot give: a lightness axis that
//! matches apparent brightness, a hue angle that can be held fixed while
//! lightness moves, and a chroma axis whose sRGB ceiling can be computed.
//! Transforms are Bjorn Ottosson's.
//!
//! Hue is in radians throughout. sRGB channels are 0-1, not 0-255.
//!
//! Exponents use `powf` even where Python wrote an integer power (`** 3`):
//! Python's float `**` is C `pow`, and `powi` can differ from it by an ulp.

pub type Lab = (f64, f64, f64);
pub type Lch = (f64, f64, f64);

/// sRGB gamma decode for one channel.
pub fn to_linear(c: f64) -> f64 {
    if c <= 0.04045 {
        c / 12.92
    } else {
        ((c + 0.055) / 1.055).powf(2.4)
    }
}

fn to_gamma(c: f64) -> f64 {
    if c <= 0.0031308 {
        12.92 * c
    } else {
        1.055 * c.powf(1.0 / 2.4) - 0.055
    }
}

pub fn srgb_to_oklab(r: f64, g: f64, b: f64) -> Lab {
    let (r, g, b) = (to_linear(r), to_linear(g), to_linear(b));
    // cbrt is a real odd root, so the slightly negative inputs bisection in
    // max_chroma can probe stay well-defined.
    let l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b).cbrt();
    let m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b).cbrt();
    let s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b).cbrt();
    (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )
}

/// Inverse transform stopping at linear light, so gamut tests can see
/// out-of-range values before gamma encoding clamps them away.
fn to_linear_rgb(l_: f64, a: f64, b: f64) -> (f64, f64, f64) {
    let l = (l_ + 0.3963377774 * a + 0.2158037573 * b).powf(3.0);
    let m = (l_ - 0.1055613458 * a - 0.0638541728 * b).powf(3.0);
    let s = (l_ - 0.0894841775 * a - 1.2914855480 * b).powf(3.0);
    (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )
}

/// True iff the color is representable in sRGB. The epsilon (1e-7) is
/// empirical and prevents bisection from wandering at lightness extremes.
pub fn in_gamut(l: f64, a: f64, b: f64) -> bool {
    let (r, g, b) = to_linear_rgb(l, a, b);
    [r, g, b].iter().all(|&c| (-1e-7..=1.0 + 1e-7).contains(&c))
}

pub fn oklab_to_srgb(l: f64, a: f64, b: f64) -> (f64, f64, f64) {
    let (r, g, b) = to_linear_rgb(l, a, b);
    let enc = |c: f64| to_gamma(c.clamp(0.0, 1.0)).clamp(0.0, 1.0);
    (enc(r), enc(g), enc(b))
}

pub fn to_lch(l: f64, a: f64, b: f64) -> Lch {
    (l, a.hypot(b), b.atan2(a))
}

pub fn from_lch(l: f64, c: f64, h: f64) -> Lab {
    (l, c * h.cos(), c * h.sin())
}

// sRGB's most chromatic color sits near C=0.32 in Oklab, so 0.5 is a safe
// upper bracket; 24 bisection steps resolve to ~3e-8, far finer than 8-bit.
const C_BRACKET: f64 = 0.5;
const BISECTIONS: u32 = 24;

/// Greatest chroma that is still in sRGB at this lightness and hue.
///
/// The ceiling varies about threefold across hue at fixed lightness, which is
/// why chroma targets are a fraction of this value rather than absolute.
///
/// The Python version is `lru_cache`d because it is the pipeline's hottest
/// function under the interpreter. Compiled, 24 bisection steps cost well
/// under a microsecond, so no memo until profiling says otherwise.
pub fn max_chroma(l: f64, h: f64) -> f64 {
    let (mut lo, mut hi) = (0.0, C_BRACKET);
    for _ in 0..BISECTIONS {
        let mid = (lo + hi) / 2.0;
        let (l_, a, b) = from_lch(l, mid, h);
        if in_gamut(l_, a, b) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    lo
}

/// Euclidean distance in Oklab, which approximates perceived difference.
pub fn delta_e(p: Lab, q: Lab) -> f64 {
    let (d0, d1, d2) = (p.0 - q.0, p.1 - q.1, p.2 - q.2);
    (d0 * d0 + d1 * d1 + d2 * d2).sqrt()
}

/// Parse `#rrggbb` into 0-255 channels. Callers pass validated hex (the
/// pipeline only ever feeds its own output back in); anything else panics.
pub(crate) fn hex_channels(value: &str) -> [u8; 3] {
    assert!(
        value.len() == 7 && value.starts_with('#'),
        "not a #rrggbb color: {value:?}"
    );
    let ch = |i: usize| u8::from_str_radix(&value[i..i + 2], 16).expect("hex digit");
    [ch(1), ch(3), ch(5)]
}

pub fn hex_to_lch(value: &str) -> Lch {
    let [r, g, b] = hex_channels(value).map(|c| c as f64 / 255.0);
    let (l, a, b) = srgb_to_oklab(r, g, b);
    to_lch(l, a, b)
}

pub fn lch_to_hex(l: f64, c: f64, h: f64) -> String {
    let (l, a, b) = from_lch(l, c, h);
    let (r, g, b) = oklab_to_srgb(l, a, b);
    let (r, g, b) = (channel_to_u8(r), channel_to_u8(g), channel_to_u8(b));
    format!("#{r:02x}{g:02x}{b:02x}")
}

/// Quantize a 0-1 channel to 8 bits the way Python's `round()` does: ties to
/// even. `f64::round` rounds ties away and would diverge on exact `x.5`.
fn channel_to_u8(v: f64) -> u8 {
    (v * 255.0).round_ties_even() as u8
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(got: f64, want: f64, eps: f64) {
        assert!((got - want).abs() <= eps, "{got} != {want} (eps {eps})");
    }

    fn lab_of(r: f64, g: f64, b: f64) -> Lab {
        srgb_to_oklab(r, g, b)
    }

    // --- round trips -----------------------------------------------------

    #[test]
    fn srgb_oklab_round_trip() {
        for rgb in [
            (0.0, 0.0, 0.0),
            (1.0, 1.0, 1.0),
            (0.2, 0.6, 0.9),
            (0.85, 0.06, 0.31),
            (0.5, 0.5, 0.5),
        ] {
            let (l, a, b) = lab_of(rgb.0, rgb.1, rgb.2);
            let back = oklab_to_srgb(l, a, b);
            close(back.0, rgb.0, 1e-6);
            close(back.1, rgb.1, 1e-6);
            close(back.2, rgb.2, 1e-6);
        }
    }

    #[test]
    fn lch_round_trip() {
        let lab = lab_of(0.2, 0.6, 0.9);
        let (l, c, h) = to_lch(lab.0, lab.1, lab.2);
        let back = from_lch(l, c, h);
        close(back.0, lab.0, 1e-9);
        close(back.1, lab.1, 1e-9);
        close(back.2, lab.2, 1e-9);
    }

    #[test]
    fn hex_round_trip() {
        // Quantization to 8 bits is the only permitted loss.
        for hex in ["#000000", "#ffffff", "#e01050", "#0284c4"] {
            let (l, c, h) = hex_to_lch(hex);
            assert_eq!(lch_to_hex(l, c, h), hex);
        }
    }

    // --- lightness -------------------------------------------------------

    #[test]
    fn black_and_white_anchor_the_scale() {
        close(lab_of(0.0, 0.0, 0.0).0, 0.0, 1e-6);
        close(lab_of(1.0, 1.0, 1.0).0, 1.0, 1e-6);
    }

    #[test]
    fn lightness_is_perceptual_not_hsv_value() {
        // #d9d9d9 and #0284c4 share HSV value ~0.8 but are nowhere near each
        // other in apparent lightness.
        let grey = hex_to_lch("#d9d9d9").0;
        let blue = hex_to_lch("#0284c4").0;
        assert!(grey - blue > 0.2);
    }

    #[test]
    fn neutral_has_zero_chroma() {
        for hex in ["#000000", "#808080", "#ffffff"] {
            assert!(hex_to_lch(hex).1 < 1e-6, "{hex}");
        }
    }

    // --- gamut -----------------------------------------------------------

    #[test]
    fn max_chroma_is_in_gamut_and_boundary_is_tight() {
        for l in [0.15, 0.35, 0.55, 0.75] {
            for h_deg in [29.0f64, 60.0, 110.0, 142.0, 195.0, 264.0, 328.0] {
                let h = h_deg.to_radians();
                let c = max_chroma(l, h);
                let inside = from_lch(l, c * 0.99, h);
                let outside = from_lch(l, c + 0.01, h);
                assert!(in_gamut(inside.0, inside.1, inside.2), "L={l} h={h_deg}");
                assert!(
                    !in_gamut(outside.0, outside.1, outside.2),
                    "L={l} h={h_deg}"
                );
            }
        }
    }

    #[test]
    fn max_chroma_is_hue_dependent() {
        let yellow = max_chroma(0.25, 110f64.to_radians());
        let blue = max_chroma(0.25, 264f64.to_radians());
        assert!(blue > yellow * 2.0);
    }

    #[test]
    fn max_chroma_vanishes_at_the_extremes() {
        for l in [0.0, 1.0] {
            for h_deg in [29.0f64, 142.0, 264.0] {
                assert!(max_chroma(l, h_deg.to_radians()) < 0.01, "L={l} h={h_deg}");
            }
        }
    }

    // --- delta E ---------------------------------------------------------

    #[test]
    fn identical_colors_have_zero_distance() {
        let lab = lab_of(0.2, 0.6, 0.9);
        close(delta_e(lab, lab), 0.0, 1e-9);
    }

    #[test]
    fn distance_is_symmetric_and_positive() {
        let a = lab_of(0.2, 0.6, 0.9);
        let b = lab_of(0.9, 0.2, 0.1);
        close(delta_e(a, b), delta_e(b, a), 1e-9);
        assert!(delta_e(a, b) > 0.0);
    }

    #[test]
    fn near_duplicates_score_below_the_separation_threshold() {
        // Two tans that the old RGB check passed must read as a collision.
        let a = lab_of(209.0 / 255.0, 169.0 / 255.0, 115.0 / 255.0);
        let b = lab_of(184.0 / 255.0, 146.0 / 255.0, 101.0 / 255.0);
        assert!(delta_e(a, b) < 0.10);
    }

    #[test]
    fn channel_quantization_rounds_ties_to_even_like_python() {
        // Gamma-encoded output essentially never lands on an exact tie, so the
        // golden corpus cannot see this; pin it directly. Python:
        // round(0.5)=0, round(1.5)=2, round(2.5)=2, round(254.5)=254.
        for (tie, want) in [(0.5, 0), (1.5, 2), (2.5, 2), (254.5, 254)] {
            assert_eq!(channel_to_u8(tie / 255.0), want, "{tie}");
        }
    }
}
