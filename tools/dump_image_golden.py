"""Generate synthetic covers and record Pillow's pipeline on them, for the Rust
port's image parity tests (rust/tests/image_golden.rs).

Each synthetic cover is written to rust/tests/fixtures/images/. For each one
this records what mpris_chroma.colors does with it, stage by stage:

- the 100x100 RGB sample (after draft/convert/resize), saved losslessly to
  rust/tests/fixtures/samples/<name>.png so the quantizer can be tested on
  Pillow's exact input even where the Rust decoder differs (JPEG, lossy WebP);
- the quantized palette and getcolors() histogram;
- the picks and n_distinct from colors._select;
- extract_colors() in both modes.

The images are synthetic on purpose: no album art in the repo, and each one is
shaped to reach a specific branch (see COVERS). Fixtures were generated with
the Pillow version recorded in the JSON; regenerate after a Pillow upgrade
only if the Rust port is meant to track it.

Run from the repo root:

    python tools/dump_image_golden.py
"""

import hashlib
import json
import math
import random
import sys
from pathlib import Path

import PIL
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mpris_chroma import colors  # noqa: E402

FIXTURES = ROOT / "rust" / "tests" / "fixtures"
IMAGES = FIXTURES / "images"
SAMPLES = FIXTURES / "samples"
SEED = 20260925


def _radial(size, inner, outer):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    cx, cy, rmax = w / 2, h / 2, math.hypot(w / 2, h / 2)
    for y in range(h):
        for x in range(w):
            t = math.hypot(x - cx, y - cy) / rmax
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(inner, outer))
    return img


def _stripes(size, palette, blur):
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    band = w / len(palette)
    for i, c in enumerate(palette):
        draw.rectangle([round(i * band), 0, round((i + 1) * band), h], fill=c)
    return img.filter(ImageFilter.GaussianBlur(blur)) if blur else img


def _blobs(rng, size, background, n):
    img = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(img)
    w, h = size
    for _ in range(n):
        r = rng.randint(w // 12, w // 4)
        x, y = rng.randint(0, w), rng.randint(0, h)
        c = tuple(rng.randint(0, 255) for _ in range(3))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=c)
    return img.filter(ImageFilter.GaussianBlur(4))


def _logo(size, background, accent, frac):
    img = Image.new("RGB", size, background)
    w, h = size
    s = round(w * frac)
    ImageDraw.Draw(img).rectangle([w // 2 - s // 2, h // 2 - s // 2,
                                   w // 2 + s // 2, h // 2 + s // 2], fill=accent)
    return img


def _noise(rng, size, tint):
    w, h = size
    data = bytes(min(255, max(0, t + rng.randint(-60, 60)))
                 for _ in range(w * h) for t in tint)
    return Image.frombytes("RGB", size, data)


def _waves(size):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (
                round(127 + 110 * math.sin(x / 37) * math.cos(y / 53)),
                round(127 + 100 * math.sin((x + y) / 71)),
                round(127 + 90 * math.cos(x / 23 - y / 41)),
            )
    return img


def _covers(rng):
    """(name, image, save kwargs). Each targets a branch of the pipeline."""
    blobs = _blobs(rng, (480, 480), (18, 12, 24), 9)
    stripes = _stripes((300, 300), [(209, 169, 115), (184, 146, 101),
                                    (157, 113, 86), (40, 30, 60), (230, 60, 40)], 3)
    return [
        # Smooth two-hue gradient: many near colours, median cut does real work.
        ("radial.png", _radial((256, 256), (230, 90, 30), (20, 30, 90)), {}),
        # Tan near-collisions (the corpus case separation exists for).
        ("stripes.png", stripes, {}),
        ("stripes_lossless.webp", stripes, {"lossless": True}),
        # Dark background with vivid blobs: the vibrancy ranking.
        ("blobs.png", blobs, {}),
        ("blobs.jpg", blobs, {"quality": 88}),
        ("blobs_lossy.webp", blobs, {"quality": 80}),
        # Small vivid accent on black.
        ("logo.png", _logo((320, 320), (8, 8, 8), (230, 20, 40), 0.3), {}),
        # Heavy noise at an odd size: thousands of distinct colours.
        ("noise.png", _noise(rng, (173, 131), (90, 140, 200)), {}),
        # Photo-like, large enough that JPEG draft scales by 4 on decode.
        ("waves.jpg", _waves((640, 640)), {"quality": 90}),
        ("waves.png", _waves((240, 240)), {}),
        # Mode conversions: greyscale, palette, alpha.
        ("grey.png", _radial((200, 200), (240, 240, 240), (15, 15, 15)).convert("L"), {}),
        ("palette.png", blobs.resize((200, 200)).quantize(64), {}),
        ("rgba.png", _logo((128, 128), (30, 90, 160), (250, 210, 30), 0.5)
         .convert("RGBA"), {}),
        # Upscaling (source smaller than the sample).
        ("tiny.png", _stripes((40, 30), [(200, 30, 30), (30, 200, 30), (30, 30, 200)], 0), {}),
        # h > 100 * w: Image.resize's two-step path.
        ("tall.png", _stripes((3, 400), [(250, 250, 0), (0, 120, 250), (250, 0, 120)], 0)
         .rotate(90, expand=True).resize((3, 400)), {}),
        # A solid cover: one real colour, padded.
        ("solid.png", Image.new("RGB", (64, 64), (200, 30, 90)), {}),
        # Multi-frame: only the first frame is decoded.
        ("anim.webp", Image.new("RGB", (64, 64), (224, 16, 80)),
         {"save_all": True, "duration": 100,
          "append_images": [Image.new("RGB", (64, 64), (16, 80, 224))]}),
        # CMYK JPEG: must still come out as a valid sRGB palette.
        ("cmyk.jpg", Image.new("CMYK", (64, 64), (0, 255, 255, 0)), {}),
    ]


def _sample(path):
    """colors._histogram's decode stages, stopping before quantize."""
    with Image.open(path) as img:
        img.draft("RGB", colors._SAMPLE)
        return img.format, img.size, img.convert("RGB").resize(colors._SAMPLE)


def main():
    rng = random.Random(SEED)
    IMAGES.mkdir(parents=True, exist_ok=True)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    cases = []
    for name, img, kwargs in _covers(rng):
        path = IMAGES / name
        img.save(path, **kwargs)
        fmt, size, sample = _sample(path)
        stem = name.replace(".", "_")
        sample.save(SAMPLES / f"{stem}.png")

        quantized = sample.quantize(colors=colors._QUANTIZE_COLORS)
        pal = quantized.getpalette()
        hist_counts = quantized.getcolors()
        picks, n_distinct = colors._select(colors._histogram(path))
        cases.append({
            "name": name,
            "format": fmt,
            "size": list(size),
            "sample": f"samples/{stem}.png",
            "sample_sha256": hashlib.sha256(sample.tobytes()).hexdigest(),
            "palette": [pal[i * 3:i * 3 + 3] for i in range(len(pal) // 3)],
            "colors": [[c, i] for c, i in hist_counts],
            "picks": picks,
            "n_distinct": n_distinct,
            "dark": list(colors.extract_colors(path, "dark")),
            "light": list(colors.extract_colors(path, "light")),
        })
    golden = {"generator": "tools/dump_image_golden.py", "seed": SEED,
              "pillow": PIL.__version__, "cases": cases}
    out = FIXTURES / "image_golden.json"
    out.write_text(json.dumps(golden, indent=1) + "\n")
    print(f"wrote {len(cases)} cases to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
