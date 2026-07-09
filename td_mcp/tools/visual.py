"""Image comparison for the vibe loop — snapshot vs reference.

Pure-PIL metrics (fast, always available) plus an optional CLIP cosine
similarity when open_clip is installed ([vj] extra). The metrics are
deliberately coarse and *verbal*: the agent needs "current is much darker
and flatter than the reference, shifted toward blue" to decide the next
mutation, not a raw tensor.
"""
from __future__ import annotations

import io

ANALYSIS_SIZE = (128, 128)
GRID = 3  # 3x3 zones for localized luminance notes

_ZONE_NAMES = [
    "top-left", "top", "top-right",
    "left", "center", "right",
    "bottom-left", "bottom", "bottom-right",
]


def _load_rgb(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data)).convert("RGB").resize(ANALYSIS_SIZE)


def _stats(img) -> dict:
    from PIL import ImageStat

    stat = ImageStat.Stat(img)
    r, g, b = (v / 255.0 for v in stat.mean)
    lum_img = img.convert("L")
    lum_stat = ImageStat.Stat(lum_img)
    return {
        "mean_rgb": (r, g, b),
        "luminance": lum_stat.mean[0] / 255.0,
        "contrast": lum_stat.stddev[0] / 255.0,
        "lum_img": lum_img,
    }


def _zone_luminances(lum_img) -> list[float]:
    from PIL import ImageStat

    w, h = lum_img.size
    zones = []
    for row in range(GRID):
        for col in range(GRID):
            box = (
                col * w // GRID, row * h // GRID,
                (col + 1) * w // GRID, (row + 1) * h // GRID,
            )
            zones.append(ImageStat.Stat(lum_img.crop(box)).mean[0] / 255.0)
    return zones


def compare_images(current: bytes, reference: bytes) -> dict:
    """Compare a snapshot (`current`) against a reference image.

    Returns similarity in [0, 1] (pixel MSE-based), signed deltas
    (current - reference) for luminance/contrast/RGB, verbal `notes`
    describing the biggest gaps, and `clip_similarity` when open_clip is
    installed.
    """
    cur = _load_rgb(current)
    ref = _load_rgb(reference)
    cs, rs = _stats(cur), _stats(ref)

    # Pixelwise MSE on the downscaled pair — cheap and monotonic enough to
    # tell "getting closer" from "getting further" between iterations.
    cur_px, ref_px = cur.tobytes(), ref.tobytes()
    mse = sum((a - b) ** 2 for a, b in zip(cur_px, ref_px)) / (len(cur_px) * 255.0 * 255.0)
    similarity = 1.0 - min(1.0, mse * 4.0)  # x4: full-scale opposites → 0

    lum_delta = cs["luminance"] - rs["luminance"]
    contrast_delta = cs["contrast"] - rs["contrast"]
    rgb_delta = tuple(c - r for c, r in zip(cs["mean_rgb"], rs["mean_rgb"]))

    notes: list[str] = []
    if lum_delta < -0.15:
        notes.append("current is darker than the reference")
    elif lum_delta > 0.15:
        notes.append("current is brighter than the reference")
    if contrast_delta < -0.08:
        notes.append("current is flatter (less contrast) than the reference")
    elif contrast_delta > 0.08:
        notes.append("current has more contrast than the reference")
    channel_names = ("red", "green", "blue")
    for name, d in zip(channel_names, rgb_delta):
        if d > 0.15:
            notes.append(f"current is shifted toward {name}")
        elif d < -0.15:
            notes.append(f"current lacks {name} vs the reference")

    # Localized: which zones differ most in luminance.
    zone_deltas = [
        c - r for c, r in zip(_zone_luminances(cs["lum_img"]), _zone_luminances(rs["lum_img"]))
    ]
    for name, d in zip(_ZONE_NAMES, zone_deltas):
        if abs(d) > 0.3:
            notes.append(f"{name} zone is much {'brighter' if d > 0 else 'darker'} than the reference")

    result = {
        "similarity": round(similarity, 4),
        "luminance_delta": round(lum_delta, 4),
        "contrast_delta": round(contrast_delta, 4),
        "rgb_delta": [round(d, 4) for d in rgb_delta],
        "notes": notes,
    }

    clip = _clip_similarity(current, reference)
    if clip is not None:
        result["clip_similarity"] = clip
    return result


def _clip_similarity(a: bytes, b: bytes) -> float | None:
    """CLIP cosine similarity, or None when open_clip isn't installed.
    Semantic: robust to layout shifts the pixel metrics punish."""
    try:
        import open_clip
        import torch
        from PIL import Image
    except ImportError:
        return None
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="openai"
    )
    model.train(False)
    with torch.no_grad():
        feats = model.encode_image(torch.stack([
            preprocess(Image.open(io.BytesIO(d)).convert("RGB")) for d in (a, b)
        ]))
        feats = feats / feats.norm(dim=-1, keepdim=True)
        return round(float((feats[0] @ feats[1]).item()), 4)


def downscale_png(data: bytes, max_size: int) -> bytes:
    """Downscale a PNG so its longest side is max_size. Never upscales."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if max(img.size) <= max_size:
        return data
    img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
