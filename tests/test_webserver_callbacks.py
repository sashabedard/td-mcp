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
