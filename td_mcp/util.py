"""Small shared helpers."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, obj: Any, indent: int | None = None) -> None:
    """Serialize `obj` to `path` via temp-file + os.replace.

    A plain write_text can be interrupted mid-write, leaving truncated JSON
    that breaks every subsequent resume-from-cache load (transcripts,
    techniques, manifests, curated KBs). os.replace is atomic on POSIX and
    Windows for same-directory renames.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def get_bridge_token() -> str:
    """Same-machine shared secret for the TD bridge, created on first use.

    Both sides read the same file: td_connect sends its content as the WS
    token, and the TD-side callbacks require it once the file exists. A
    LAN attacker can reach the WebServer DAT's port but not this file —
    which is what keeps eval/exec off the open network. Override the
    location with TD_MCP_TOKEN_FILE."""
    path = Path(
        os.environ.get(
            "TD_MCP_TOKEN_FILE",
            str(Path.home() / ".cache" / "td-mcp" / "bridge_token"),
        )
    )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(16))
        try:
            path.chmod(0o600)
        except OSError:
            pass  # e.g. some Windows filesystems
    return path.read_text().strip()
