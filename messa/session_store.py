"""Local, per-CLI-session transcript persistence (JSONL, one line per message).

Not the same thing as the DB's message_history table (that's per *user*,
shared across every channel and kept forever). This is a local debugging/
resume convenience for the CLI: `python -m messa.cli --session foo` re-loads
whatever was said in a previous run under that id.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import config


def _sessions_dir() -> Path:
    p = Path(config.SESSIONS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def session_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return _sessions_dir() / f"{safe}.jsonl"


def append(session_id: str, role: str, content: str) -> None:
    path = session_path(session_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": time.time()}) + "\n")


def load(session_id: str) -> list[dict[str, Any]]:
    path = session_path(session_id)
    if not path.exists():
        return []
    messages = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            messages.append({"role": rec["role"], "content": rec["content"]})
    return messages


def list_sessions() -> list[str]:
    return sorted(p.stem for p in _sessions_dir().glob("*.jsonl"))
