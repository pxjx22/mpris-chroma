//! Cover decoding for palette extraction (port of the decode half of
//! `colors._histogram`, SEC-005/SEC-006).
//!
//! Only JPEG/PNG/WebP *content* is decoded, identified by signature, never by
//! extension. A file over the byte budget is refused before it is read in
//! full, and an image whose header declares more pixels than the pixel
//! budget is refused before its body is decoded. The result is the 100x100
//! sample the quantizer works on.
//!
//! Parity with Pillow: PNG and lossless WebP decode to the same pixels, and
//! the mode conversions below copy Pillow's, so their samples match
//! bit-for-bit. JPEG and lossy WebP go through different decoders (and
//! JPEG's draft is emulated, see `draft_scale`), so their samples differ by a
//! few levels; `tests/image_golden.rs` bounds how far that moves the palette.

use std::fmt;
use std::fs::File;
use std::io::{Cursor, Read};
use std::path::Path;

use image::{DynamicImage, ImageFormat, ImageReader, Limits};

use super::resize::{Rgb8, resize};

/// Downsample target, mirroring the old ImageMagick `-resize 100x100`.
pub const SAMPLE: (u32, u32) = (100, 100);
/// 16 MiB: bounds local covers, which the download path does not size-cap.
pub const MAX_DECODE_BYTES: u64 = 16 * 1024 * 1024;
/// ~16 MP declared-dimension ceiling: the decompression-bomb guard.
pub const MAX_PIXELS: u64 = 16_000_000;

const ACCEPTED: [ImageFormat; 3] = [ImageFormat::Jpeg, ImageFormat::Png, ImageFormat::WebP];

#[derive(Debug)]
pub enum DecodeError {
    Io(std::io::Error),
    /// Larger than the byte budget.
    TooLarge,
    /// Not JPEG/PNG/WebP by signature (HTML, SVG, PDF, ...).
    Unaccepted,
    /// Header declares more pixels than the pixel budget.
    TooManyPixels,
    /// Accepted format, but the data does not decode.
    Corrupt(image::ImageError),
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(e) => write!(f, "read failed: {e}"),
            Self::TooLarge => write!(f, "over {MAX_DECODE_BYTES} bytes"),
            Self::Unaccepted => write!(f, "not a JPEG/PNG/WebP"),
            Self::TooManyPixels => write!(f, "over {MAX_PIXELS} pixels"),
            Self::Corrupt(e) => write!(f, "undecodable: {e}"),
        }
    }
}

impl std::error::Error for DecodeError {}

/// Read a cover from disk within `max_bytes`, then decode it to the sample.
pub fn sample_file(path: &Path, max_bytes: u64) -> Result<Rgb8, DecodeError> {
    let file = File::open(path).map_err(DecodeError::Io)?;
    if file.metadata().map_err(DecodeError::Io)?.len() > max_bytes {
        return Err(DecodeError::TooLarge);
    }
    // Bounded read as well: the file may grow between the stat and the read.
    let mut bytes = Vec::new();
    file.take(max_bytes + 1)
        .read_to_end(&mut bytes)
        .map_err(DecodeError::Io)?;
    if bytes.len() as u64 > max_bytes {
        return Err(DecodeError::TooLarge);
    }
    sample_bytes(&bytes)
}

/// Decode an in-memory cover to the 100x100 sample.
pub fn sample_bytes(bytes: &[u8]) -> Result<Rgb8, DecodeError> {
    let decoded = decode(bytes)?;
    Ok(resize(&decoded, SAMPLE.0, SAMPLE.1))
}

/// The decoded, RGB-converted cover (after JPEG draft), before resizing.
pub fn decode(bytes: &[u8]) -> Result<Rgb8, DecodeError> {
    let format = image::guess_format(bytes).map_err(|_| DecodeError::Unaccepted)?;
    if !ACCEPTED.contains(&format) {
        return Err(DecodeError::Unaccepted);
    }
    // Header only: no pixel data is decoded before the budget check.
    let (w, h) = ImageReader::with_format(Cursor::new(bytes), format)
        .into_dimensions()
        .map_err(DecodeError::Corrupt)?;
    if w as u64 * h as u64 > MAX_PIXELS {
        return Err(DecodeError::TooManyPixels);
    }
    let mut reader = ImageReader::with_format(Cursor::new(bytes), format);
    let mut limits = Limits::default();
    limits.max_image_width = Some(w);
    limits.max_image_height = Some(h);
    reader.limits(limits);
    let img = reader.decode().map_err(DecodeError::Corrupt)?;
    let rgb = to_rgb_like_pillow(img);
    if format == ImageFormat::Jpeg {
        let s = draft_scale(w, h);
        if s > 1 {
            return Ok(box_reduce(&rgb, s));
        }
    }
    Ok(rgb)
}

/// `JpegImageFile.draft("RGB", (100, 100))`: the largest DCT scale in
/// {8, 4, 2, 1} that keeps both sides at or above the sample size.
pub fn draft_scale(w: u32, h: u32) -> u32 {
    let scale = (w / SAMPLE.0).min(h / SAMPLE.1);
    [8, 4, 2].into_iter().find(|&s| scale >= s).unwrap_or(1)
}

/// Emulates libjpeg's scaled decode with a block average to
/// `ceil(w / s) x ceil(h / s)`. libjpeg scales inside the IDCT, so this is an
/// approximation (a few levels); the sample size, and so everything
/// downstream of it, matches Pillow exactly.
fn box_reduce(src: &Rgb8, s: u32) -> Rgb8 {
    let (ow, oh) = (src.width.div_ceil(s), src.height.div_ceil(s));
    let mut out = Vec::with_capacity((ow * oh) as usize);
    for by in 0..oh {
        for bx in 0..ow {
            let mut sum = [0u32; 3];
            let mut n = 0;
            for y in by * s..((by + 1) * s).min(src.height) {
                for x in bx * s..((bx + 1) * s).min(src.width) {
                    let p = src.pixels[(y * src.width + x) as usize];
                    for c in 0..3 {
                        sum[c] += p[c] as u32;
                    }
                    n += 1;
                }
            }
            out.push(sum.map(|v| ((v + n / 2) / n) as u8));
        }
    }
    Rgb8::new(ow, oh, out)
}

/// `Image.convert("RGB")` as Pillow does it for what these decoders yield:
/// alpha is dropped (not composited), grey is replicated, and 16-bit data
/// follows Pillow's PNG raw modes: colour keeps the high byte ("RGB;16B"),
/// while 16-bit grey opens as "I;16" and clips to 255 on conversion.
fn to_rgb_like_pillow(img: DynamicImage) -> Rgb8 {
    let (w, h) = (img.width(), img.height());
    let pixels: Vec<[u8; 3]> = match img {
        DynamicImage::ImageLuma8(b) => b.pixels().map(|p| [p[0]; 3]).collect(),
        DynamicImage::ImageLumaA8(b) => b.pixels().map(|p| [p[0]; 3]).collect(),
        DynamicImage::ImageRgb8(b) => b.pixels().map(|p| p.0).collect(),
        DynamicImage::ImageRgba8(b) => b.pixels().map(|p| [p[0], p[1], p[2]]).collect(),
        DynamicImage::ImageLuma16(b) => b.pixels().map(|p| [p[0].min(255) as u8; 3]).collect(),
        DynamicImage::ImageLumaA16(b) => b.pixels().map(|p| [(p[0] >> 8) as u8; 3]).collect(),
        DynamicImage::ImageRgb16(b) => b.pixels().map(|p| p.0.map(|c| (c >> 8) as u8)).collect(),
        DynamicImage::ImageRgba16(b) => b
            .pixels()
            .map(|p| [p[0], p[1], p[2]].map(|c| (c >> 8) as u8))
            .collect(),
        other => other.to_rgb8().pixels().map(|p| p.0).collect(),
    };
    Rgb8::new(w, h, pixels)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn draft_scale_matches_pillow() {
        assert_eq!(draft_scale(640, 640), 4); // 6 -> 4
        assert_eq!(draft_scale(480, 480), 4);
        assert_eq!(draft_scale(1000, 3000), 8);
        assert_eq!(draft_scale(250, 900), 2);
        assert_eq!(draft_scale(199, 199), 1);
        assert_eq!(draft_scale(64, 64), 1); // 0 -> 1
    }

    #[test]
    fn box_reduce_rounds_up_partial_edge_blocks() {
        let img = Rgb8::new(5, 3, vec![[10, 20, 30]; 15]);
        let out = box_reduce(&img, 2);
        assert_eq!((out.width, out.height), (3, 2));
        assert!(out.pixels.iter().all(|&p| p == [10, 20, 30]));
    }

    #[test]
    fn non_images_are_refused_by_signature() {
        for bytes in [
            &b"<!DOCTYPE html>\n<html><body>not an image</body></html>"[..],
            b"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"64\" height=\"64\"/>",
            b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF",
            b"",
        ] {
            assert!(matches!(decode(bytes), Err(DecodeError::Unaccepted)));
        }
    }

    #[test]
    fn truncated_png_is_corrupt_not_a_panic() {
        let mut bytes = b"\x89PNG\r\n\x1a\n".to_vec();
        bytes.extend([0u8; 32]);
        assert!(matches!(decode(&bytes), Err(DecodeError::Corrupt(_))));
    }
}
