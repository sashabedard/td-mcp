"""td_visual_diff — image comparison metrics for the vibe loop."""
import base64
import io
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image


def _png(color, size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _gradient_png(size=(64, 64)) -> bytes:
    img = Image.new("RGB", size)
    for x in range(size[0]):
        v = int(255 * x / (size[0] - 1))
        for y in range(size[1]):
            img.putpixel((x, y), (v, v, v))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_identical_images_score_one():
    from td_mcp.tools.visual import compare_images

    a = _png((200, 30, 90))
    m = compare_images(a, a)
    assert m["similarity"] == pytest.approx(1.0, abs=1e-3)
    assert m["luminance_delta"] == pytest.approx(0.0, abs=1e-3)
    assert m["notes"] == []


def test_black_vs_white_reports_brightness_gap():
    from td_mcp.tools.visual import compare_images

    m = compare_images(_png((0, 0, 0)), _png((255, 255, 255)))
    assert m["similarity"] < 0.2
    assert m["luminance_delta"] < -0.9  # current much darker than reference
    assert any("darker" in n for n in m["notes"])


def test_color_shift_reported():
    from td_mcp.tools.visual import compare_images

    m = compare_images(_png((220, 30, 30)), _png((30, 30, 220)))
    assert any("red" in n or "blue" in n for n in m["notes"])


def test_flat_vs_gradient_reports_contrast():
    from td_mcp.tools.visual import compare_images

    m = compare_images(_png((128, 128, 128)), _gradient_png())
    assert m["contrast_delta"] < -0.1
    assert any("flat" in n or "contrast" in n for n in m["notes"])


def test_different_sizes_are_normalized():
    from td_mcp.tools.visual import compare_images

    m = compare_images(_png((10, 200, 10), size=(32, 32)), _png((10, 200, 10), size=(128, 96)))
    assert m["similarity"] == pytest.approx(1.0, abs=1e-2)


def test_downscale_png_caps_longest_side():
    from td_mcp.tools.visual import downscale_png

    small = downscale_png(_png((5, 5, 5), size=(128, 64)), max_size=32)
    img = Image.open(io.BytesIO(small))
    assert max(img.size) == 32
    assert img.size == (32, 16)


def test_downscale_png_never_upscales():
    from td_mcp.tools.visual import downscale_png

    data = _png((5, 5, 5), size=(16, 16))
    assert Image.open(io.BytesIO(downscale_png(data, max_size=512))).size == (16, 16)


@pytest.mark.asyncio
async def test_td_visual_diff_tool_snapshots_and_compares(tmp_path):
    from td_mcp import server

    ref_path = tmp_path / "ref.png"
    ref_path.write_bytes(_png((200, 30, 90)))
    snapshot_b64 = base64.b64encode(_png((200, 30, 90))).decode()

    with patch.object(server.bridge, "send",
                      new=AsyncMock(return_value={"base64": snapshot_b64, "width": 64, "height": 64})):
        result = await server.td_visual_diff("/project1/out1", str(ref_path))
    assert result["ok"] is True
    assert result["similarity"] > 0.99


@pytest.mark.asyncio
async def test_td_visual_diff_missing_reference(tmp_path):
    from td_mcp import server

    result = await server.td_visual_diff("/project1/out1", str(tmp_path / "nope.png"))
    assert result["ok"] is False
    assert "reference" in result["error"]
