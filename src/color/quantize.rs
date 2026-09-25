//! Pillow's median-cut `Image.quantize`, bit-exact (port of
//! `libImaging/Quant.c` `quantize()` with `kmeans=0`, and `QuantHeap.c`,
//! Pillow 12.3).
//!
//! What must match for the palette pipeline: the palette entries, and how
//! many sample pixels map to each index (`getcolors()`, in index order). The
//! C builds linked lists sorted per axis; the result depends only on axis
//! *values*, never on the order of equal values within a list, so boxes here
//! are plain vectors. Everything order-sensitive is kept as in the C: the
//! heap and its tie-breaking, the depth-first leaf numbering, the averaging
//! and rounding, and the distance-table nearest-neighbour search.

use std::collections::HashMap;

/// Pillow's `PIXEL_HASH`. The C colour hash table compares keys by this
/// value, so two distinct colours with equal hashes are one entry (the first
/// inserted keeps its colour). Rare, but reproduced.
fn pixel_hash(p: [u8; 3]) -> u32 {
    (p[0] as u32).wrapping_mul(463)
        ^ ((p[1] as u32) << 8).wrapping_mul(10069)
        ^ ((p[2] as u32) << 16).wrapping_mul(64997)
}

/// `create_pixel_hash` rehashes at reduced precision past this many distinct
/// colours. The pipeline only quantizes a 100x100 sample, which can never
/// reach it, so the rehash path is not ported and larger inputs are refused.
const MAX_HASH_ENTRIES: usize = 65536;

fn dist_sq(a: [u8; 3], b: [u8; 3]) -> u32 {
    (0..3)
        .map(|i| {
            let d = a[i] as i32 - b[i] as i32;
            (d * d) as u32
        })
        .sum()
}

#[derive(Clone, Copy)]
struct Entry {
    color: [u8; 3],
    count: u32,
}

struct BoxNode {
    entries: Vec<Entry>,
    pixel_count: u32,
    children: Option<(usize, usize)>,
}

impl BoxNode {
    fn range(&self, axis: usize) -> (u8, u8) {
        let lo = self
            .entries
            .iter()
            .map(|e| e.color[axis])
            .min()
            .unwrap_or(0);
        let hi = self
            .entries
            .iter()
            .map(|e| e.color[axis])
            .max()
            .unwrap_or(0);
        (lo, hi)
    }

    /// `compute_box_volume`.
    fn volume(&self) -> i32 {
        if self.entries.is_empty() {
            return 0;
        }
        (0..3)
            .map(|a| {
                let (lo, hi) = self.range(a);
                hi as i32 - lo as i32 + 1
            })
            .product()
    }
}

/// `QuantHeap.c`: a 1-based binary max-heap on `pixelCount`, whose sift
/// rules decide which of two equal-count boxes is split first.
struct Heap {
    slots: Vec<usize>, // slots[0] unused
}

impl Heap {
    fn cmp(boxes: &[BoxNode], a: usize, b: usize) -> i32 {
        // (int)a->pixelCount - (int)b->pixelCount
        (boxes[a].pixel_count as i32).wrapping_sub(boxes[b].pixel_count as i32)
    }

    fn add(&mut self, boxes: &[BoxNode], val: usize) {
        self.slots.push(val);
        let mut k = self.slots.len() - 1;
        while k != 1 {
            if Self::cmp(boxes, val, self.slots[k / 2]) <= 0 {
                break;
            }
            self.slots[k] = self.slots[k / 2];
            k >>= 1;
        }
        self.slots[k] = val;
    }

    fn remove(&mut self, boxes: &[BoxNode]) -> Option<usize> {
        let count = self.slots.len() - 1;
        if count == 0 {
            return None;
        }
        let top = self.slots[1];
        let v = self.slots.pop().expect("non-empty");
        let count = count - 1;
        if count == 0 {
            return Some(top);
        }
        let mut k = 1;
        while k * 2 <= count {
            let mut l = k * 2;
            if l < count && Self::cmp(boxes, self.slots[l], self.slots[l + 1]) < 0 {
                l += 1;
            }
            if Self::cmp(boxes, v, self.slots[l]) > 0 {
                break;
            }
            self.slots[k] = self.slots[l];
            k = l;
        }
        self.slots[k] = v;
        Some(top)
    }
}

/// `split` + `splitlists`: cut along the axis with the largest
/// luminance-weighted extent, at the pixel-count median, keeping every entry
/// that shares the median's value on the left.
fn split(node: &BoxNode) -> (BoxNode, BoxNode) {
    let weights = [77, 150, 29];
    let mut best = i32::MIN;
    let mut axis = 0;
    for (i, w) in weights.iter().enumerate() {
        let (lo, hi) = node.range(i);
        let f = (hi as i32 - lo as i32) * w;
        if best < f {
            best = f;
            axis = i;
        }
    }

    let mut sorted = node.entries.clone();
    sorted.sort_by(|a, b| b.color[axis].cmp(&a.color[axis])); // descending
    let mut left_len = 0;
    let mut left: u32 = 0;
    while left_len < sorted.len() {
        left += sorted[left_len].count;
        left_len += 1;
        if left.wrapping_mul(2) > node.pixel_count {
            break;
        }
    }
    if left_len < sorted.len() {
        let split_val = sorted[left_len - 1].color[axis];
        while left_len < sorted.len() && sorted[left_len].color[axis] == split_val {
            left_len += 1;
        }
    }
    if left_len == sorted.len() {
        // No right side: move every entry sharing the tail (minimum) value.
        let tail_val = sorted[sorted.len() - 1].color[axis];
        while left_len > 0 && sorted[left_len - 1].color[axis] == tail_val {
            left_len -= 1;
        }
    }
    let right_entries = sorted.split_off(left_len);
    let sum = |v: &[Entry]| v.iter().map(|e| e.count).sum::<u32>();
    let l = BoxNode {
        pixel_count: sum(&sorted),
        entries: sorted,
        children: None,
    };
    let r = BoxNode {
        pixel_count: sum(&right_entries),
        entries: right_entries,
        children: None,
    };
    (l, r)
}

/// Palette plus per-pixel palette indices, like a mode "P" image.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Quantized {
    pub palette: Vec<[u8; 3]>,
    pub indices: Vec<u8>,
}

impl Quantized {
    /// `Image.getcolors()` on the result: `(count, index)` for every index
    /// that occurs, in index order.
    pub fn colors(&self) -> Vec<(u32, usize)> {
        let mut counts = vec![0u32; self.palette.len()];
        for &i in &self.indices {
            counts[i as usize] += 1;
        }
        counts
            .into_iter()
            .enumerate()
            .filter(|&(_, c)| c > 0)
            .map(|(i, c)| (c, i))
            .collect()
    }
}

/// `ImagingQuantize(im, colors, method=MEDIANCUT, kmeans=0)` for RGB input.
///
/// # Panics
/// If `colors` is outside 1..=256 or the input has more distinct colours than
/// the unported rehash path handles (see `MAX_HASH_ENTRIES`).
pub fn quantize(pixels: &[[u8; 3]], colors: usize) -> Quantized {
    assert!((1..=256).contains(&colors), "bad number of colors");
    if pixels.is_empty() {
        return Quantized {
            palette: Vec::new(),
            indices: Vec::new(),
        };
    }

    // 1. Colour histogram, keyed like the C hash table.
    let mut order: Vec<u32> = Vec::new();
    let mut table: HashMap<u32, Entry> = HashMap::new();
    for &p in pixels {
        let h = pixel_hash(p);
        table
            .entry(h)
            .and_modify(|e| e.count += 1)
            .or_insert_with(|| {
                order.push(h);
                Entry { color: p, count: 1 }
            });
    }
    assert!(
        table.len() <= MAX_HASH_ENTRIES,
        "quantize: {} distinct colours needs the unported rehash path",
        table.len()
    );

    // 2-3. Median cut.
    let mut boxes = vec![BoxNode {
        entries: order.iter().map(|h| table[h]).collect(),
        pixel_count: pixels.len() as u32,
        children: None,
    }];
    let mut heap = Heap { slots: vec![0] };
    heap.add(&boxes, 0);
    'cut: for _ in 1..colors {
        let node = loop {
            match heap.remove(&boxes) {
                None => break 'cut,
                Some(n) if boxes[n].volume() == 1 => continue,
                Some(n) => break n,
            }
        };
        let (l, r) = split(&boxes[node]);
        let (li, ri) = (boxes.len(), boxes.len() + 1);
        boxes.push(l);
        boxes.push(r);
        boxes[node].children = Some((li, ri));
        heap.add(&boxes, li);
        heap.add(&boxes, ri);
    }

    // 4. Number the leaves depth-first, left first (annotate_hash_table).
    let mut box_of: HashMap<u32, u32> = HashMap::new();
    let mut n_entries: u32 = 0;
    let mut stack = vec![0usize];
    while let Some(n) = stack.pop() {
        if let Some((l, r)) = boxes[n].children {
            stack.push(r);
            stack.push(l);
            continue;
        }
        for e in &boxes[n].entries {
            box_of.insert(pixel_hash(e.color), n_entries);
        }
        if !boxes[n].entries.is_empty() {
            n_entries += 1;
        }
    }
    let n = n_entries as usize;

    // 5-6. Palette = rounded mean of the pixels in each box.
    let mut sums = vec![[0u32; 3]; n];
    let mut counts = vec![0u32; n];
    for &p in pixels {
        let b = box_of[&pixel_hash(p)] as usize;
        for ch in 0..3 {
            sums[b][ch] += p[ch] as u32;
        }
        counts[b] += 1;
    }
    let palette: Vec<[u8; 3]> = (0..n)
        .map(|i| sums[i].map(|s| (0.5 + s as f64 / counts[i] as f64) as u8))
        .collect();

    // 7. Map each pixel to its nearest palette entry, searching outward from
    //    its own box in (distance, index) order (build_distance_tables +
    //    map_image_pixels_from_median_box).
    let avg_dist: Vec<u32> = (0..n * n)
        .map(|ij| dist_sq(palette[ij / n], palette[ij % n]))
        .collect();
    let sort_key: Vec<Vec<usize>> = (0..n)
        .map(|i| {
            let mut row: Vec<usize> = (0..n).collect();
            row.sort_by_key(|&j| (avg_dist[i * n + j], j));
            row
        })
        .collect();
    let mut cache: HashMap<[u8; 3], u8> = HashMap::new();
    let indices = pixels
        .iter()
        .map(|&p| {
            *cache.entry(p).or_insert_with(|| {
                let own = box_of[&pixel_hash(p)] as usize;
                let mut best = own;
                let mut best_dist = dist_sq(palette[own], p);
                let limit = best_dist << 2;
                for &idx in &sort_key[own] {
                    if avg_dist[own * n + idx] > limit {
                        break;
                    }
                    let d = dist_sq(palette[idx], p);
                    if d < best_dist {
                        best_dist = d;
                        best = idx;
                    }
                }
                best as u8
            })
        })
        .collect();

    Quantized { palette, indices }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn solid_image_is_one_entry() {
        let q = quantize(&[[224, 16, 80]; 100], 16);
        assert_eq!(q.palette, vec![[224, 16, 80]]);
        assert_eq!(q.colors(), vec![(100, 0)]);
    }

    #[test]
    fn few_colours_are_kept_exactly() {
        let mut px = vec![[255, 0, 0]; 50];
        px.extend([[0, 0, 255]; 30]);
        px.extend([[0, 255, 0]; 20]);
        let q = quantize(&px, 16);
        let mut got: Vec<([u8; 3], u32)> = q
            .colors()
            .into_iter()
            .map(|(c, i)| (q.palette[i], c))
            .collect();
        got.sort();
        assert_eq!(
            got,
            vec![([0, 0, 255], 30), ([0, 255, 0], 20), ([255, 0, 0], 50)]
        );
    }

    #[test]
    fn never_more_entries_than_requested() {
        let px: Vec<[u8; 3]> = (0..10_000u32)
            .map(|i| [(i % 256) as u8, (i / 40 % 256) as u8, (i * 7 % 256) as u8])
            .collect();
        let q = quantize(&px, 16);
        assert!(q.palette.len() <= 16);
        assert_eq!(q.colors().iter().map(|c| c.0).sum::<u32>(), 10_000);
    }

    #[test]
    fn heap_pops_largest_count_first() {
        let boxes: Vec<BoxNode> = [5u32, 9, 1, 9, 3]
            .iter()
            .map(|&c| BoxNode {
                entries: vec![],
                pixel_count: c,
                children: None,
            })
            .collect();
        let mut heap = Heap { slots: vec![0] };
        for i in 0..boxes.len() {
            heap.add(&boxes, i);
        }
        let popped: Vec<u32> = std::iter::from_fn(|| heap.remove(&boxes))
            .map(|i| boxes[i].pixel_count)
            .collect();
        assert_eq!(popped, vec![9, 9, 5, 3, 1]);
    }
}
