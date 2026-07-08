import json

from td_mcp.util import write_json_atomic


def test_write_json_atomic_roundtrip(tmp_path):
    target = tmp_path / "data.json"
    write_json_atomic(target, {"a": 1, "accents": "éléphant"})
    assert json.loads(target.read_text()) == {"a": 1, "accents": "éléphant"}


def test_write_json_atomic_overwrites_existing(tmp_path):
    target = tmp_path / "data.json"
    target.write_text('{"old": true}')
    write_json_atomic(target, {"new": True})
    assert json.loads(target.read_text()) == {"new": True}


def test_write_json_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "data.json"
    write_json_atomic(target, [1, 2, 3])
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_write_json_atomic_indent(tmp_path):
    target = tmp_path / "data.json"
    write_json_atomic(target, {"a": 1}, indent=2)
    assert "\n" in target.read_text()
