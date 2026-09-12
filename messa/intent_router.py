"""Front-Door Intent Router (Message Triage) for Messa.

Triages incoming inbound messages before execution to determine:
1. CHAT_BANTER: Chit-chat, greetings, casual acknowledgments ('thanks', 'cool', 'sounds good').
   Fast-path response without heavy tool loading or ghost approval triggering.
2. ACTIVE_TASK_INPUT: Input to an active workflow, such as an SMS verification code (OTP),
   a clarification answer, or a specific confirmation for a pending action.
3. STATUS_QUERY: Queries on in-flight or recent tasks ('status?', 'how is the search going?', 'any update?').
   Answers from live state without launching duplicate tasks.
4. NEW_GOAL: Multi-step objectives, searches, email workflows, bookings, routines.
   Routed to the dynamic autonomous loop / orchestrator.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

from . import config, db, deepsearch_control, turn_control


class IntentType(str, enum.Enum):
    CHAT_BANTER = "chat_banter"
    ACTIVE_TASK_INPUT = "active_task_input"
    STATUS_QUERY = "status_query"
    NEW_GOAL = "new_goal"


@dataclass
class IntentDecision:
    intent: IntentType
    confidence: float
    context_hint: str
    active_entity: dict[str, Any] | None = None
    is_fast_path: bool = False


# Regex patterns for fast-path triage (<1ms)
_BANTER_PATTERN = re.compile(
    r"^[\s,.]*(?:hey|hi|hello|yo|sup|good\s+morning|good\s+night|gn|gm|thanks|thank\s+you|thx|ty|cool|ok|okay|k|nice|"
    r"sounds\s+good|sounds\s+great|all\s+good|no\s+worries|no\s+problem|np|perfect|awesome|great|got\s+it|alright|sweet|lol|haha|bet)"
    r"(?:[\s,.]+(?:hey|hi|hello|yo|sup|thanks|thank\s+you|thx|ty|cool|ok|okay|k|nice|sounds\s+good|sounds\s+great|perfect|awesome|great|got\s+it|alright|sweet|lol|haha|bet))*[\s.!]*$",
    re.IGNORECASE,
)

_STATUS_PATTERN = re.compile(
    r"\b(status|any\s+update|how('s|\s+is)\s+it\s+going|did\s+you\s+finish|is\s+it\s+done|"
    r"what('s|\s+is)\s+running|what\s+are\s+you\s+doing|progress)\b",
    re.IGNORECASE,
)

_OTP_PATTERN = re.compile(
    r"(\b\d{4,8}\b|code\s*(?:is|:)?\s*\d{4,8}|verification\s*code)",
    re.IGNORECASE,
)


async def triage_incoming_message(
    user: config.UserContext,
    text: str,
) -> IntentDecision:
    """Classifies an incoming user message against real-time state.

    Fast, non-blocking, and authoritative.
    """
    clean_text = (text or "").strip()
    if not clean_text:
        return IntentDecision(
            intent=IntentType.CHAT_BANTER,
            confidence=1.0,
            context_hint="Empty message.",
            is_fast_path=True,
        )

    # 1. Check for active OTP expectations or ongoing verification needs
    active_otps = await db.list_active_otp_expectations(user.user_id)
    if active_otps and _OTP_PATTERN.search(clean_text):
        return IntentDecision(
            intent=IntentType.ACTIVE_TASK_INPUT,
            confidence=0.95,
            context_hint=f"User provided verification code for pending OTP request #{active_otps[0]['id']}.",
            active_entity={"otp_id": active_otps[0]["id"]},
            is_fast_path=True,
        )

    # 2. Check for active pending actions requiring explicit user input
    pending_actions = await db.list_pending_actions(user.user_id)
    if pending_actions:
        # If the text is specifically answering yes/no/confirm/cancel to an active pending action
        if re.search(r"\b(yes|no|confirm|approve|reject|deny|cancel|go\s+ahead|do\s+it|don't)\b", clean_text, re.IGNORECASE):
            return IntentDecision(
                intent=IntentType.ACTIVE_TASK_INPUT,
                confidence=0.90,
                context_hint=f"User responded to pending action #{pending_actions[0]['id']} ({pending_actions[0]['action_type']}).",
                active_entity={"pending_action": pending_actions[0]},
                is_fast_path=True,
            )

    # 3. Status queries
    if _STATUS_PATTERN.search(clean_text):
        in_flight_tasks = deepsearch_control.active_count(user.user_id)
        return IntentDecision(
            intent=IntentType.STATUS_QUERY,
            confidence=0.90,
            context_hint=f"User checking system/task status. Active background tasks in memory: {in_flight_tasks}.",
            is_fast_path=True,
        )

    # 4. Pure banter / conversational pleasantry
    if _BANTER_PATTERN.match(clean_text):
        return IntentDecision(
            intent=IntentType.CHAT_BANTER,
            confidence=0.95,
            context_hint="Casual conversational greeting or acknowledgement.",
            is_fast_path=True,
        )

    # 5. Default to NEW_GOAL with autonomous reasoning
    return IntentDecision(
        intent=IntentType.NEW_GOAL,
        confidence=0.85,
        context_hint="New objective or active query requiring orchestrator problem-solving.",
        is_fast_path=False,
    )
