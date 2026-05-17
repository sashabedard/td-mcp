import json
import os
from unittest.mock import MagicMock, patch

import pytest

from td_mcp.ingest.shaders_shadertoy import (
    _shader_to_chunk,
    build_shadertoy_chunks,
    search_shaders,
)


def test_get_api_key_raises_when_missing(monkeypatch):
    monkeypatch.delenv("TD_MCP_SHADERTOY_API_KEY", raising=False)
    from td_mcp.ingest.shaders_shadertoy import _get_api_key
    with pytest.raises(RuntimeError, match="TD_MCP_SHADERTOY_API_KEY"):
        _get_api_key()


def test_shader_to_chunk_basic():
    shader = {
        "info": {
            "id": "ABC123",
            "name": "Plasma",
            "username": "iq",
            "description": "Simple plasma effect",
            "tags": ["plasma", "demo"],
        },
        "renderpass": [
            {"type": "image", "name": "Image", "code": "void mainImage(out vec4 c, vec2 p){}"},
        ],
    }
    chunk = _shader_to_chunk(shader)
    assert chunk is not None
    assert chunk.title == "Plasma"
    assert chunk.source == "shader_shadertoy"
    assert chunk.is_glsl is True
    assert "void mainImage" in chunk.text
    assert "Tags: plasma, demo" in chunk.text
    assert chunk.source_url.endswith("/view/ABC123")


def test_shader_to_chunk_returns_none_when_no_code():
    shader = {"info": {"id": "X"}, "renderpass": [{"code": ""}]}
    assert _shader_to_chunk(shader) is None


def test_search_shaders_returns_ids(monkeypatch):
    monkeypatch.setenv("TD_MCP_SHADERTOY_API_KEY", "fakekey")
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"Shaders": 2, "Results": ["id1", "id2"]}
    fake_client.get.return_value = fake_resp
    ids = search_shaders("plasma", client=fake_client)
    assert ids == ["id1", "id2"]


def test_build_chunks_from_cache(tmp_path):
    shader = {
        "info": {"id": "ZZZ", "name": "Test", "username": "x"},
        "renderpass": [{"type": "image", "name": "Image", "code": "void main(){}"}],
    }
    (tmp_path / "ZZZ.json").write_text(json.dumps(shader))
    chunks = list(build_shadertoy_chunks(cache_dir=tmp_path))
    assert len(chunks) == 1
    assert chunks[0].title == "Test"
