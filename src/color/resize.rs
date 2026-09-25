//! Pillow's bicubic `Image.resize`, bit-exact (port of `libImaging/Resample.c`
//! 8bpc path plus the Python/C wrappers' shortcuts, Pillow 12.3).
//!
//! The palette pipeline downsamples every cover to 100x100 before quantizing,
//! and the quantizer's output depends on every sample byte, so a "close
//! enough" resampler would change which colors get picked. This reproduces
//! Pillow's two-pass separable convolution with its fixed-point coefficients
//! and rounding exactly.

/// An 8-bit RGB raster, row-major.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Rgb8 {
    pub width: u32,
    pub height: u32,
    pub pixels: Vec<[u8; 3]>,
}

impl Rgb8 {
    pub fn new(width: u32, height: u32, pixels: Vec<[u8; 3]>) -> Self {
        assert_eq!(
            pixels.len(),
            width as usize * height as usize,
            "raster size"
        );
        Self {
            width,
            height,
            pixels,
        }
    }

    fn at(&self, x: usize, y: usize) -> [u8; 3] {
        self.pixels[y * self.width as usize + x]
    }
}

/// Pillow's coefficients carry 32 - 8 - 2 fractional bits: 8 for the result,
/// 2 for overshoot from the filter's negative lobes.
const PRECISION_BITS: u32 = 32 - 8 - 2;
const BICUBIC_SUPPORT: f64 = 2.0;

fn bicubic_filter(x: f64) -> f64 {
    const A: f64 = -0.5;
    let x = x.abs();
    if x < 1.0 {
        return ((A + 2.0) * x - (A + 3.0)) * x * x + 1.0;
    }
    if x < 2.0 {
        return (((x - 5.0) * x + 8.0) * x - 4.0) * A;
    }
    0.0
}

/// Per output pixel: first source index and tap count, plus fixed-point taps.
struct Coeffs {
    ksize: usize,
    bounds: Vec<(usize, usize)>,
    kk: Vec<i32>,
}

/// `precompute_coeffs` + `normalize_coeffs_8bpc`. `in0`/`in1` are `f32` as in
/// the C (`float box[4]`), and `in1 - in0` is a float subtraction.
fn precompute_coeffs(in_size: u32, in0: f32, in1: f32, out_size: u32) -> Coeffs {
    let scale = (in1 - in0) as f64 / out_size as f64;
    let filterscale = scale.max(1.0);
    let support = BICUBIC_SUPPORT * filterscale;
    let ksize = support.ceil() as usize * 2 + 1;
    let inv_filterscale = 1.0 / filterscale;

    let mut bounds = Vec::with_capacity(out_size as usize);
    let mut pre = vec![0.0f64; out_size as usize * ksize];
    for xx in 0..out_size as usize {
        let center = in0 as f64 + (xx as f64 + 0.5) * scale;
        // C casts truncate toward zero, which `as i64` also does.
        let xmin = ((center - support + 0.5) as i64).max(0);
        let xmax = ((center + support + 0.5) as i64).min(in_size as i64) - xmin;
        let k = &mut pre[xx * ksize..(xx + 1) * ksize];
        let taps = xmax.max(0) as usize;
        let mut ww = 0.0;
        for (x, slot) in k[..taps].iter_mut().enumerate() {
            let w = bicubic_filter(((x as i64 + xmin) as f64 - center + 0.5) * inv_filterscale);
            *slot = w;
            ww += w;
        }
        if ww != 0.0 {
            for w in &mut k[..taps] {
                *w /= ww;
            }
        }
        bounds.push((xmin as usize, taps));
    }
    let one = (1u32 << PRECISION_BITS) as f64;
    let kk = pre
        .iter()
        .map(|&k| {
            if k < 0.0 {
                (-0.5 + k * one) as i32
            } else {
                (0.5 + k * one) as i32
            }
        })
        .collect();
    Coeffs { ksize, bounds, kk }
}

fn clip8(ss: i32) -> u8 {
    (ss >> PRECISION_BITS).clamp(0, 255) as u8
}

const ROUND: i32 = 1 << (PRECISION_BITS - 1);

fn horizontal(src: &Rgb8, offset: usize, rows: usize, out_w: u32, c: &Coeffs) -> Rgb8 {
    let mut out = Vec::with_capacity(out_w as usize * rows);
    for yy in 0..rows {
        for xx in 0..out_w as usize {
            let (xmin, xmax) = c.bounds[xx];
            let k = &c.kk[xx * c.ksize..xx * c.ksize + xmax];
            let mut ss = [ROUND; 3];
            for (x, &kx) in k.iter().enumerate() {
                let p = src.at(x + xmin, yy + offset);
                for (acc, &v) in ss.iter_mut().zip(&p) {
                    *acc += v as i32 * kx;
                }
            }
            out.push(ss.map(clip8));
        }
    }
    Rgb8::new(out_w, rows as u32, out)
}

fn vertical(src: &Rgb8, out_h: u32, c: &Coeffs) -> Rgb8 {
    let mut out = Vec::with_capacity(src.width as usize * out_h as usize);
    for yy in 0..out_h as usize {
        let (ymin, ymax) = c.bounds[yy];
        let k = &c.kk[yy * c.ksize..yy * c.ksize + ymax];
        for xx in 0..src.width as usize {
            let mut ss = [ROUND; 3];
            for (y, &ky) in k.iter().enumerate() {
                let p = src.at(xx, y + ymin);
                for (acc, &v) in ss.iter_mut().zip(&p) {
                    *acc += v as i32 * ky;
                }
            }
            out.push(ss.map(clip8));
        }
    }
    Rgb8::new(src.width, out_h, out)
}

/// `ImagingResampleInner` with the bicubic filter.
fn resample(src: &Rgb8, out_w: u32, out_h: u32, bx: [f32; 4]) -> Rgb8 {
    let need_horizontal = out_w != src.width || bx[0] != 0.0 || bx[2] != out_w as f32;
    let need_vertical = out_h != src.height || bx[1] != 0.0 || bx[3] != out_h as f32;

    let mut vert = precompute_coeffs(src.height, bx[1], bx[3], out_h);
    let ybox_first = vert.bounds[0].0;
    let (last_min, last_len) = vert.bounds[out_h as usize - 1];
    let ybox_last = last_min + last_len;

    let mut current = None;
    if need_horizontal {
        let horiz = precompute_coeffs(src.width, bx[0], bx[2], out_w);
        for b in &mut vert.bounds {
            b.0 -= ybox_first;
        }
        current = Some(horizontal(
            src,
            ybox_first,
            ybox_last - ybox_first,
            out_w,
            &horiz,
        ));
    }
    if need_vertical {
        let input = current.as_ref().unwrap_or(src);
        return vertical(input, out_h, &vert);
    }
    current.unwrap_or_else(|| src.clone())
}

/// The C `_resize` wrapper: an integer box whose size equals the requested
/// size is a plain crop; anything else is resampled.
fn im_resize(src: &Rgb8, out_w: u32, out_h: u32, bx: [f32; 4]) -> Rgb8 {
    let integral = |v: f32| v - (v as i32) as f32 == 0.0;
    if integral(bx[0])
        && bx[2] - bx[0] == out_w as f32
        && integral(bx[1])
        && bx[3] - bx[1] == out_h as f32
    {
        let (x0, y0) = (bx[0] as usize, bx[1] as usize);
        let pixels = (0..out_h as usize)
            .flat_map(|y| (0..out_w as usize).map(move |x| (x + x0, y + y0)))
            .map(|(x, y)| src.at(x, y))
            .collect();
        return Rgb8::new(out_w, out_h, pixels);
    }
    resample(src, out_w, out_h, bx)
}

/// `Image.resize(size)` with the default filter (bicubic), no box and no
/// `reducing_gap`, as `colors._histogram` calls it.
pub fn resize(src: &Rgb8, out_w: u32, out_h: u32) -> Rgb8 {
    assert!(out_w > 0 && out_h > 0, "height and width must be > 0");
    let (w, h) = (src.width, src.height);
    if (w, h) == (out_w, out_h) {
        return src.clone();
    }
    let full = [0.0, 0.0, w as f32, h as f32];
    // Very tall images are resized in two steps, height first (Image.resize).
    if h as u64 > w as u64 * 100 && out_h < h {
        let tall = im_resize(src, w, out_h, [0.0, full[1], w as f32, full[3]]);
        return im_resize(&tall, out_w, out_h, [full[0], 0.0, full[2], out_h as f32]);
    }
    im_resize(src, out_w, out_h, full)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid(w: u32, h: u32, px: [u8; 3]) -> Rgb8 {
        Rgb8::new(w, h, vec![px; (w * h) as usize])
    }

    #[test]
    fn solid_color_survives_any_resize() {
        // Normalized taps sum to 1, so a flat field stays flat (no overshoot).
        for (w, h) in [(64, 64), (640, 640), (37, 211), (100, 3)] {
            let out = resize(&solid(w, h, [224, 16, 80]), 100, 100);
            assert!(out.pixels.iter().all(|&p| p == [224, 16, 80]), "{w}x{h}");
        }
    }

    #[test]
    fn same_size_is_a_copy() {
        let img = Rgb8::new(2, 1, vec![[1, 2, 3], [4, 5, 6]]);
        assert_eq!(resize(&img, 2, 1), img);
    }

    #[test]
    fn output_has_the_requested_size() {
        let out = resize(&solid(640, 480, [0, 0, 0]), 100, 100);
        assert_eq!(
            (out.width, out.height, out.pixels.len()),
            (100, 100, 10_000)
        );
    }

    #[test]
    fn very_tall_image_takes_the_two_step_path() {
        // h > 100 * w: Pillow resizes height first; must not panic and must
        // keep a flat field flat.
        let out = resize(&solid(2, 300, [9, 9, 9]), 100, 100);
        assert!(out.pixels.iter().all(|&p| p == [9, 9, 9]));
    }
}
