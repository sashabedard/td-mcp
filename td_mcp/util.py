"""Small shared helpers."""
from __future__ import annotations

import json
import os
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
