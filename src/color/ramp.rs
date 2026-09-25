//! Model of wlchroma's palette expansion, for measurement only (port of
//! `ramp.py`).
//!
//! wlchroma turns the three colors we send into twelve cells before rendering
//! (`src/render/palette.zig:buildPalette`), blending consecutive pairs at four
//! alphas. Apparent screen brightness is therefore the mean over this ramp,
//! not over the three colors. The daemon never calls this.

use super::oklab::{hex_channels, to_linear};

// (foreground, background) index pairs and the alphas, mirroring buildPalette.
const PAIRS: [(usize, usize); 3] = [(0, 1), (1, 2), (2, 0)];
const ALPHAS: [f64; 4] = [1.00, 0.72, 0.50, 0.28];

/// The twelve cells wlchroma will render for this palette.
pub fn expand(c1: &str, c2: &str, c3: &str) -> Vec<String> {
    let rgb = [c1, c2, c3].map(hex_channels);
    let mut cells = Vec::with_capacity(12);
    for (fg, bg) in PAIRS {
        for alpha in ALPHAS {
            // Blended in 8-bit sRGB, matching palette.zig's blend() exactly —
            // deliberately not gamma-correct. floor(x + 0.5) rounds half away
            // from zero like Zig's @round (Python's round() would not).
            let mixed: [u8; 3] = std::array::from_fn(|k| {
                (rgb[bg][k] as f64 * (1.0 - alpha) + rgb[fg][k] as f64 * alpha + 0.5).floor() as u8
            });
            cells.push(format!("#{:02x}{:02x}{:02x}", mixed[0], mixed[1], mixed[2]));
        }
    }
    cells
}

/// Mean relative luminance across the twelve rendered cells (0-1).
pub fn mean_luminance(c1: &str, c2: &str, c3: &str) -> f64 {
    let total: f64 = expand(c1, c2, c3)
        .iter()
        .map(|cell| {
            let [r, g, b] = hex_channels(cell).map(|v| to_linear(v as f64 / 255.0));
            0.2126 * r + 0.7152 * g + 0.0722 * b
        })
        .sum();
    total / 12.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn expands_to_twelve_cells() {
        assert_eq!(expand("#ff0000", "#00ff00", "#0000ff").len(), 12);
    }

    #[test]
    fn full_alpha_cells_are_the_endpoints() {
        let cells = expand("#ff0000", "#00ff00", "#0000ff");
        assert_eq!(cells[0], "#ff0000");
        assert_eq!(cells[4], "#00ff00");
        assert_eq!(cells[8], "#0000ff");
    }

    #[test]
    fn solid_palette_expands_to_one_color() {
        let cells: HashSet<_> = expand("#c81e5a", "#c81e5a", "#c81e5a")
            .into_iter()
            .collect();
        assert_eq!(cells, HashSet::from(["#c81e5a".to_string()]));
    }

    #[test]
    fn midpoint_cell_lies_between_its_endpoints() {
        let cells = expand("#000000", "#ffffff", "#000000");
        let mid = u8::from_str_radix(&cells[2][1..3], 16).unwrap();
        assert!(mid > 100 && mid < 155);
    }

    #[test]
    fn alpha_half_rounds_half_away_from_zero() {
        // (1,1,0)*0.5 + (0,0,1)*0.5 = (0.5,0.5,0.5): ties-to-even gives 0,
        // Zig's @round (and wlchroma) gives 1.
        let cells = expand("#010100", "#000001", "#000000");
        assert_eq!(cells[2], "#010101");
    }

    #[test]
    fn black_and_white_anchor_the_range() {
        assert!(mean_luminance("#000000", "#000000", "#000000").abs() < 1e-6);
        assert!((mean_luminance("#ffffff", "#ffffff", "#ffffff") - 1.0).abs() < 1e-6);
    }

    #[test]
    fn reference_palette_is_dark() {
        // witch_hour, the configured preset this work is calibrated against.
        assert!(mean_luminance("#120C14", "#4A2F5C", "#6D8F4F") < 0.12);
    }

    #[test]
    fn brighter_palette_scores_higher() {
        let dark = mean_luminance("#101010", "#181818", "#121212");
        let bright = mean_luminance("#f2f2f2", "#e8e8e8", "#fafafa");
        assert!(bright > dark);
    }
}
