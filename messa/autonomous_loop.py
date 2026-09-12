"""Dynamic Autonomous Loop Engine (Plan -> Act -> Verify -> Pivot).

Empowers Messa to execute multi-step objectives end-to-end with:
1. Dynamic Plan Decomposition (Goal -> Sub-tasks without brittle hardcoding)
2. Ground-Truth Verification (Verify tool results against expected outcomes)
3. Dynamic Self-Correction / Pivot (If a step fails or produces empty/blocked data, adapt)
4. Milestone Updates (Keeps the user informed only on true milestone achievements)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from . import config, console, db, reliability


@dataclass
class StepRecord:
    step_number: int
    action_description: str
    tool_name: str | None
    tool_args: dict[str, Any] | None
    result_summary: str
    verified: bool
    notes: str = ""


@dataclass
class GoalExecutionResult:
    goal: str
    success: bool
    final_output: str
    steps_taken: list[StepRecord] = field(default_factory=list)
    needs_user_input: bool = False
    blocker_reason: str | None = None


class AutonomousLoopEngine:
    """Autonomous execution loop that ensures objectives are planned, executed,

    and verified against real tool outcomes before completing.
    """

    def __init__(
        self,
        user: config.UserContext,
        max_iterations: int = 6,
    ) -> None:
        self.user = user
        self.max_iterations = max_iterations

    def verify_tool_outcome(self, tool_name: str, tool_result: Any) -> tuple[bool, str]:
        """Inspects the ground truth output of a tool call to verify genuine success."""
        if tool_result is None:
            return False, "Tool returned no result."

        str_res = str(tool_result).strip()
        lower_res = str_res.lower()

        # Check for explicit failure markers or errors
        if any(err_marker in lower_res for err_marker in ("error:", "exception:", "failed to", "could not", "access denied", "blocked by captcha")):
            return False, f"Tool execution failed: {str_res[:200]}"

        # Positive verifications
        if tool_name in ("send_email", "send_messa_email", "send_gmail_email"):
            if "sent" in lower_res or "message id" in lower_res or "queued" in lower_res:
                return True, "Email verified dispatched."
            return False, f"Email delivery unconfirmed: {str_res[:200]}"

        if tool_name in ("create_project_capsule", "update_project_capsule"):
            if "capsule" in lower_res or "created" in lower_res or "updated" in lower_res:
                return True, "Project capsule state verified."
            return False, f"Capsule update unconfirmed: {str_res[:200]}"

        if tool_name in ("create_reminder", "add_task", "create_routine"):
            if "created" in lower_res or "scheduled" in lower_res or "added" in lower_res or "#" in lower_res:
                return True, "Action verified created in DB."
            return False, f"Creation unconfirmed: {str_res[:200]}"

        # Default for read/lookup tools
        if str_res:
            return True, "Information retrieved successfully."
        return False, "Empty result returned."
