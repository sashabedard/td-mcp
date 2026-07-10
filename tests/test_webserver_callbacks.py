"""TD-side bridge script tests — the parts that don't need a live TD.

The script lives outside the package (it's DAT text), so load it by path.
"""
import importlib.util
from pathlib import Path

import pytest


def _load_callbacks():
    path = Path(__file__).parent.parent / "td_bridge_tox" / "webserver_callbacks.py"
    spec = importlib.util.spec_from_file_location("webserver_callbacks_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_script_functions_can_call_each_other():
    """The classic exec-in-function trap: names defined at the script's top
    level must be visible from inside functions the script defines."""
    mod = _load_callbacks()
    code = (
        "def helper():\n"
        "    return 1\n"
        "def main():\n"
        "    return helper() + 1\n"
        "print(main())\n"
    )
    result = mod._dispatch("run_script", {"code": code})
    assert result["output"] == "2\n"


def test_run_script_error_carries_partial_stdout():
    """A script that prints diagnostics then fails must not lose the
    diagnostics — they say exactly how far it got."""
    mod = _load_callbacks()
    code = "print('created op 12 of 15')\nraise ValueError('op 13 exploded')\n"
    with pytest.raises(Exception) as exc_info:
        mod._dispatch("run_script", {"code": code})
    assert "created op 12 of 15" in str(exc_info.value)
    assert "op 13 exploded" in str(exc_info.value)


def test_run_script_does_not_pollute_bridge_namespace():
    """User code rebinding a bridge-internal name must not break the next
    dispatch (exec must run in a copy, not the module globals)."""
    mod = _load_callbacks()
    mod._dispatch("run_script", {"code": "_dispatch = None\njson = None\n"})
    # Next call still works — the module's own names were untouched.
    result = mod._dispatch("run_script", {"code": "print('still alive')\n"})
    assert result["output"] == "still alive\n"


# ─────────────────────────── token auth ──────────────────────────────────────


class _FakeWebServer:
    def __init__(self):
        self.sent = []

    def webSocketSendText(self, client, text):
        import json as _json
        self.sent.append(_json.loads(text))


def _receive(mod, payload: dict) -> dict:
    import json as _json
    ws = _FakeWebServer()
    mod.onWebSocketReceiveText(ws, "client1", _json.dumps(payload))
    return ws.sent[-1]


def test_missing_token_rejected_when_token_file_exists(tmp_path):
    """As soon as the same-machine token file exists (the MCP server creates
    it at connect), a message without the token must be rejected — eval/exec
    must not be open to anyone on the LAN."""
    mod = _load_callbacks()
    token_file = tmp_path / "bridge_token"
    token_file.write_text("s3cret")
    mod.TOKEN_FILE = token_file

    resp = _receive(mod, {"id": 1, "action": "eval", "data": {"expression": "1+1"}})
    assert resp["ok"] is False
    assert "Unauthorized" in resp["error"]["message"]


def test_matching_token_accepted(tmp_path):
    mod = _load_callbacks()
    token_file = tmp_path / "bridge_token"
    token_file.write_text("s3cret\n")
    mod.TOKEN_FILE = token_file

    resp = _receive(mod, {"id": 1, "action": "run_script",
                          "data": {"code": "print('hi')"}, "token": "s3cret"})
    assert resp["ok"] is True
    assert resp["result"]["output"] == "hi\n"


def test_no_token_file_allows_local_dev(tmp_path):
    """Without a token file (server never connected on this machine) the
    bridge keeps working — enforcement starts as soon as td_connect creates
    the file."""
    mod = _load_callbacks()
    mod.TOKEN_FILE = tmp_path / "does_not_exist"

    resp = _receive(mod, {"id": 1, "action": "run_script", "data": {"code": "print('ok')"}})
    assert resp["ok"] is True


# ─────────────────────────── set_param / pulse errors ────────────────────────


class _FakeParCollection:
    """Mimics td's OP.par: subscript with an unknown name returns None."""
    def __getitem__(self, name):
        return None


class _FakeOp:
    path = "/project1/fake"
    def __init__(self):
        self.par = _FakeParCollection()


def test_set_param_unknown_param_is_a_named_error():
    """par[unknown] returns None in TD; '.val' on it raised the opaque
    "'NoneType' object has no attribute 'val'" — the error must instead
    name the param and the op so catalog suggestions can kick in."""
    mod = _load_callbacks()
    mod.op = lambda path: _FakeOp()
    with pytest.raises(Exception) as exc_info:
        mod._dispatch("set_param", {"path": "/project1/fake",
                                    "param": "wrongname", "value": 1})
    msg = str(exc_info.value)
    assert "wrongname" in msg
    assert "/project1/fake" in msg


def test_pulse_unknown_param_is_a_named_error():
    mod = _load_callbacks()
    mod.op = lambda path: _FakeOp()
    with pytest.raises(Exception) as exc_info:
        mod._dispatch("pulse", {"path": "/project1/fake", "param": "resett"})
    assert "resett" in str(exc_info.value)


# ─────────────────────────── load_tox ─────────────────────────────────────────


def test_load_tox_missing_file_is_a_named_error(tmp_path):
    """A bad path must fail loud BEFORE calling loadTox — TD-side loadTox
    on a missing file drops an empty COMP silently."""
    mod = _load_callbacks()

    class _Parent:
        def loadTox(self, path):
            raise AssertionError("loadTox must not be reached")

    mod.op = lambda path: _Parent()
    with pytest.raises(Exception) as exc_info:
        mod._dispatch("load_tox", {"parent": "/project1",
                                   "file": str(tmp_path / "ghost.tox")})
    assert "ghost.tox" in str(exc_info.value)


def test_load_tox_loads_renames_and_positions(tmp_path):
    mod = _load_callbacks()
    tox = tmp_path / "thing.tox"
    tox.write_bytes(b"tox")

    class _NewOp:
        path = "/project1/thing"
        type = "base"
        name = "thing"
        nodeX = 0
        nodeY = 0

    new_op = _NewOp()

    class _Parent:
        def loadTox(self, path):
            assert path == str(tox)
            return new_op

    mod.op = lambda path: _Parent()
    result = mod._dispatch("load_tox", {"parent": "/project1", "file": str(tox),
                                        "name": "my_thing", "x": 100, "y": -50})
    assert new_op.name == "my_thing"
    assert (new_op.nodeX, new_op.nodeY) == (100, -50)
    assert result["tox"] == str(tox)
