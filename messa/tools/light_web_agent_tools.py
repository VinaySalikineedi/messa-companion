"""Light-Web-Agent Subagent Wrapper for Messa.

Exposes the light-web-agent engine (messa/channels/light_web_agent.py) as a
deepagents CompiledSubAgent, so the orchestrator can delegate real-browser tasks
(Instacart checkout, Walmart cart, form-filling, sign-ups) to it via the `task`
tool -- exactly the same delegation contract as deepsearch/grocery_agent.

Responsibilities:
  1. build_light_web_agent_subagent -- CompiledSubAgent dict wired into registry.py.
     Entry point for NEW tasks delegated by the orchestrator.

Design notes:
  - Usage-plan gate: one FEATURE_BROWSE_ACTIONS unit per new task (same as
    deepsearch). Resume calls do NOT consume a unit -- the user is completing
    something already in flight, not starting a new session.
  - Access gate: light_web_agent shares deepsearch's access guard
    (user.has_deepsearch_access) since both open live cloud browser sessions.
  - TTL: HumanCheckpoint.timeout_seconds drives session keepalive (300 s
    default); browser_session_manager.suspend_session now honours this value
    (see channels/browser_session_manager.py's suspend_session).
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from .. import config, console
from ..approval import ApprovalGate

logger = logging.getLogger(__name__)

_LWA_DESCRIPTION = (
    "Handles real-browser tasks on live websites: filling out forms, adding items "
    "to an Instacart or Amazon cart, signing up for accounts, completing checkout "
    "flows, solving verification steps (SMS OTP, CAPTCHA) with the user's help. "
    "Use whenever the user wants Messa to actually DO something on a website -- "
    "navigate, click, type, submit -- rather than just researching or generating "
    "a link. Prefer light_web_agent over deepsearch for transactional tasks "
    "(cart/checkout/sign-up) on a SINGLE website; prefer deepsearch for multi-site "
    "research or tasks that require reading many pages."
)


def build_light_web_agent_subagent(
    user: "config.UserContext",
    approval_gate: "ApprovalGate | None" = None,
) -> dict[str, Any]:
    """Return a deepagents ``CompiledSubAgent`` spec for light_web_agent.

    The agent is gated on ``config.LIGHT_WEB_AGENT_ENABLED`` (registry.py
    checks this before calling us) and on ``user.has_deepsearch_access`` (same
    gate deepsearch uses -- both open live cloud browser sessions).

    Usage metering: one FEATURE_BROWSE_ACTIONS unit is consumed per new task,
    same as deepsearch. Resume flows (already-suspended sessions) are exempt.
    """

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        # ------------------------------------------------------------------ #
        # Gate 1: deepsearch access guard (shared with deepsearch -- both open
        # a live cloud browser session that costs real money).
        # ------------------------------------------------------------------ #
        if not user.has_deepsearch_access:
            console.system(
                f"[light_web_agent] declined for user #{user.user_id} -- "
                "browsing/deepsearch access not enabled (beta-gated)."
            )
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "Live browser tasks aren't available on your account yet -- "
                            "they're in a limited beta while we get them fully ready. "
                            "I can still help you plan the steps or research options!"
                        )
                    )
                ]
            }

        # ------------------------------------------------------------------ #
        # Gate 2: global feature flag (LIGHT_WEB_AGENT_ENABLED, default OFF).
        # ------------------------------------------------------------------ #
        if not getattr(config, "LIGHT_WEB_AGENT_ENABLED", False):
            return {
                "messages": [
                    AIMessage(
                        content="Live browser navigation isn't enabled yet -- coming soon!"
                    )
                ]
            }

        # ------------------------------------------------------------------ #
        # Gate 3: usage plan check (one FEATURE_BROWSE_ACTIONS unit).
        # ------------------------------------------------------------------ #
        from .. import usage
        limit_result = await usage.check_and_consume(user, usage.FEATURE_BROWSE_ACTIONS)
        if not limit_result.allowed:
            return {"messages": [AIMessage(content=limit_result.upgrade_message)]}

        # ------------------------------------------------------------------ #
        # Extract the delegated task description from the orchestrator message.
        # ------------------------------------------------------------------ #
        messages = list(state.get("messages", []))
        task_description = ""
        for m in reversed(messages):
            text = ""
            if hasattr(m, "content"):
                text = m.content if isinstance(m.content, str) else str(m.content)
            if text.strip():
                task_description = text.strip()
                break

        if not task_description:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "I need a description of what to do on the website. "
                            "Please tell me the goal (e.g. 'add 3 apples to my Instacart cart')."
                        )
                    )
                ]
            }

        console.system(
            f"[light_web_agent] Starting task for user #{user.user_id}: "
            f"{task_description[:100]}"
        )

        # ------------------------------------------------------------------ #
        # Run the light-web-agent engine.
        # ------------------------------------------------------------------ #
        from ..channels.browser import run_light_web_task

        try:
            result = await run_light_web_task(
                goal=task_description,
                user_id=user.user_id,
            )
        except Exception as e:
            logger.exception(f"[light_web_agent] Task failed with exception: {e}")
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "Something went wrong starting the browser session. "
                            f"Details: {e}"
                        )
                    )
                ]
            }

        status = result.get("status", "FAILED")
        summary = result.get("result_summary") or ""
        steps = result.get("steps_taken", 0)
        live_view = result.get("live_view_url") or ""
        checkpoint = result.get("checkpoint")

        # ------------------------------------------------------------------ #
        # Build the reply back to the orchestrator.
        # ------------------------------------------------------------------ #
        if status == "DONE":
            reply = (
                f"✅ Done ({steps} steps). {summary}"
                if summary
                else f"✅ Completed in {steps} steps."
            )

        elif status == "NEEDS_HUMAN":
            # Task is suspended, waiting for the user's input (OTP, CAPTCHA…).
            # The orchestrator's turn reply will include the checkpoint's
            # prompt_to_user so Messa forwards it to the user via SMS.
            checkpoint_prompt = ""
            if isinstance(checkpoint, dict):
                checkpoint_prompt = checkpoint.get("prompt_to_user") or ""
            elif checkpoint is not None and hasattr(checkpoint, "prompt_to_user"):
                checkpoint_prompt = checkpoint.prompt_to_user or ""

            reply = (
                f"⏸️ Paused -- need your help to continue.\n"
                f"{checkpoint_prompt}"
            )
            if live_view:
                reply += f"\nLive view: {live_view}"

        elif status in ("BLOCKED_MODAL", "BLOCKED_INJECTION"):
            reply = (
                f"⚠️ Task blocked after {steps} steps: {summary or status}. "
                "You may need to complete this manually."
            )

        else:
            reply = (
                f"❌ Task could not be completed after {steps} steps. "
                f"{summary or 'An unknown error occurred.'}"
            )

        console.system(
            f"[light_web_agent] Task finished: status={status}, steps={steps}"
        )
        return {"messages": [AIMessage(content=reply)]}

    return {
        "name": "light_web_agent",
        "description": _LWA_DESCRIPTION,
        "runnable": RunnableLambda(_run),
    }
