"""Pluggable human-approval gate for actions that shouldn't run unattended.

Two different confirmation mechanisms exist in this project, deliberately:

1. Synchronous, in-process actions (browser clicks/keypresses, sending an
   email) -- these need a yes/no *right now*, mid tool-call. `ApprovalGate`
   below covers this; Phase 1's `CLIApprovalGate` uses stdin. A future
   channel-backed gate (SMS/email) would instead have to interrupt the
   agent run entirely and wait for the user's next message -- which is
   exactly what...
2. ...`pending_actions` in the database is for: task/reminder/calendar/note
   mutations get proposed, the agent asks the user in the same reply, and a
   *later* message confirms or rejects it. That flow lives in db.py /
   executive_tools.py and doesn't use this module.
"""
from __future__ import annotations

import asyncio
from typing import Any, Protocol

from . import console


class ApprovalGate(Protocol):
    async def confirm(self, label: str, tool_name: str, args: dict[str, Any]) -> bool: ...


class CLIApprovalGate:
    """Blocks on stdin. Fine for a single-user interactive CLI (Phase 1)."""

    async def confirm(self, label: str, tool_name: str, args: dict[str, Any]) -> bool:
        console.system(f"[{label}] wants to run '{tool_name}' with args: {args}")
        answer = await asyncio.to_thread(input, "Allow this action? (y/n): ")
        return answer.strip().lower() == "y"


class AutoApproveGate:
    """No-op gate that always approves. Useful for tests / non-interactive runs."""

    async def confirm(self, label: str, tool_name: str, args: dict[str, Any]) -> bool:
        return True
