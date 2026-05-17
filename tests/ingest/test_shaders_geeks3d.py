from unittest.mock import MagicMock

from td_mcp.ingest.shaders_geeks3d import (
    _extract_text_and_code,
    _extract_title,
    build_geeks3d_chunks,
    discover_article_urls,
)


def test_extract_title():
    html = "<html><head><title>GLSL Hacker  -  Pixel Shader</title></head></html>"
    assert _extract_title(html) == "GLSL Hacker - Pixel Shader"


def test_extract_text_and_code_keeps_code_blocks():
    html = """
    <html><body>
    <p>Description of the shader effect.</p>
    <pre>void main() { gl_FragColor = vec4(1.0); }</pre>
    <p>More words.</p>
    </body></html>
    """
    result = _extract_text_and_code(html)
    assert "Description of the shader effect" in result
    assert "gl_FragColor" in result
    assert "```glsl" in result


def test_discover_article_urls_parses_index(tmp_path):
    fake_html = '''
    <a href="https://www.geeks3d.com/20100525/glsl-pixel-shader/">link1</a>
    <a href="https://www.geeks3d.com/20120615/another-shader/">link2</a>
    <a href="https://www.geeks3d.com/about/">about page (not article)</a>
    '''
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=200, text=fake_html)
    fake_client.get.return_value = fake_resp
    urls = discover_article_urls(fake_client)
    assert len(urls) == 2
    assert all("/2010" in u or "/2012" in u for u in urls)


def test_build_chunks_from_cache(tmp_path):
    import json
    cache_file = tmp_path / "deadbeef.json"
    cache_file.write_text(json.dumps({
        "url": "https://www.geeks3d.com/2020/test/",
        "title": "Test Shader",
        "text": "Some description\n\n```glsl\nvoid main(){}\n```",
    }))
    chunks = list(build_geeks3d_chunks(cache_dir=tmp_path))
    assert len(chunks) == 1
    assert chunks[0].source == "shader_geeks3d"
    assert chunks[0].is_glsl is True
    assert chunks[0].title == "Test Shader"
