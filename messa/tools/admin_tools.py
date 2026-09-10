"""Admin-only tools: broadcast a message to every user (preview -> approval
-> execute), and manage the new-user waitlist (messa/waitlist.py,
migrations/025_new_user_cap_waitlist.sql / 026_admin_broadcast.sql).

Only ever wired into the orchestrator's subagent list when
user.is_admin is true (see agents/registry.py's build_orchestrator) -- a
non-admin user has no path to any of this, not in their tool list and not
even mentioned in their system prompt. is_admin itself is the same quiet,
unadvertised flag usage.py already uses for the metering bypass -- set with
a manual `UPDATE users SET is_admin = true`, never surfaced anywhere a
regular user could see or reach it.

Broadcasting reuses the SAME pending_actions/confirm_pending_action gate as
routines_agent's scheduling -- see db.py's own comment on GATED_ACTION_TYPES
for why broadcast_message earned a place alongside scheduling there. The
actual send happens asynchronously in server.py's _production_broadcast_loop
once confirmed, not inline in this tool call -- see that loop's docstring
for why (a large user base can take a while to fan out to).
"""
from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool

from .. import config, db, reliability
from .common import last_ai_text, run_inner_agent_with_claim_check, trace_all
from .integration_circuit_breaker import ToolFailureLadderMiddleware
from .scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block

LABEL = "admin_agent"


def build_admin_tools(user: config.UserContext) -> list[BaseTool]:
    uid = user.user_id

    @tool
    async def propose_broadcast_message(message_text: str) -> str:
        """Propose broadcasting a text to EVERY user currently in the
        database (all of them -- no region/plan/active-only targeting yet,
        that's a future addition). This only stages it for confirmation:
        relay the exact message text and the recipient count back to the
        admin and get an explicit yes before calling confirm_pending_action
        -- never confirm without them having just said yes to that specific
        text. Once confirmed, delivery happens in the background (it can
        take a while for a large user base); list_broadcasts shows status
        and the admin gets a text when it's done either way."""
        recipient_count = await db.count_all_users()
        row = await db.propose_action(uid, "broadcast_message", {"message_text": message_text})
        return (
            f"Proposed (pending confirmation, id #{row['id']}): broadcast to {recipient_count} "
            f"user(s), exact text below -- relay this back verbatim so the admin can confirm the "
            f"actual wording:\n\n{message_text}"
        )

    @tool
    async def list_broadcasts() -> str:
        """List recent broadcasts and their delivery status."""
        rows = await db.list_broadcasts()
        if not rows:
            return "No broadcasts sent yet."
        lines = []
        for r in rows:
            preview = r["message_text"][:60] + ("..." if len(r["message_text"]) > 60 else "")
            if r["status"] == "completed":
                lines.append(
                    f"#{r['id']} [completed] sent {r['sent_count']}/{r['total_recipients']} "
                    f"({r['failed_count']} failed): {preview}"
                )
            else:
                lines.append(f"#{r['id']} [{r['status']}]: {preview}")
        return "\n".join(lines)

    @tool
    async def get_new_user_cap_status() -> str:
        """Check the new-user signup cap: how many new users have signed up
        since the cap was turned on, out of the configured limit, and how
        many are currently on the waitlist."""
        status = await db.get_new_user_cap_summary()
        if status["cap"] <= 0:
            return "No new-user cap is configured right now -- signups are unlimited."
        return (
            f"New-user cap: {status['count']}/{status['cap']} used since the cap was enabled. "
            f"{status['waitlist_count']} currently on the waitlist."
        )

    @tool
    async def list_waitlist() -> str:
        """List everyone currently on the new-user waitlist, oldest request
        first, with whether each has already been admitted (waiting on
        them to text again) or is still waiting."""
        rows = await db.list_waitlist()
        if not rows:
            return "Waitlist is empty."
        lines = []
        for r in rows:
            state = "admitted -- waiting on them to text again" if r.get("admitted_at") else "waiting"
            lines.append(f"{r['phone_number']} -- requested {r['requested_at']} ({state})")
        return "\n".join(lines)

    @tool
    async def admit_from_waitlist(phone_number: str) -> str:
        """Admit one specific person off the waitlist -- the next time they
        text in, they'll be let in as a real user regardless of whether the
        cap is still full. Does NOT raise the cap itself or affect anyone
        else on the waitlist; it's a one-person exception. The cap itself
        is raised via the MESSA_NEW_USER_CAP environment variable, not from
        chat."""
        row = await db.admit_from_waitlist(phone_number)
        if not row:
            return f"No waitlist entry found for {phone_number}."
        return f"Admitted {phone_number} off the waitlist -- they'll be let in the next time they text."

    raw_tools: list[BaseTool] = [
        propose_broadcast_message, list_broadcasts, get_new_user_cap_status,
        list_waitlist, admit_from_waitlist,
    ]
    return trace_all(raw_tools, LABEL)


ADMIN_SYSTEM_PROMPT = (
    "You are the admin specialist: broadcasting messages to all users and managing the "
    "new-user waitlist. You are only ever delegated to for an admin account -- if you're "
    "running, this user IS an admin.\n"
    "- Broadcasting goes through propose_broadcast_message -- it only stages the broadcast, "
    "returning the exact text and how many users it'll reach. Relay both back to the admin "
    "in plain language and get an explicit yes before calling confirm_pending_action; call "
    "reject_pending_action if they decline or want to change the wording -- never confirm "
    "without them having just said yes to that specific text. Once confirmed, delivery runs "
    "in the background (a large user base can take a while) -- use list_broadcasts to check "
    "status, and the admin also gets a text automatically once it's done either way.\n"
    "- get_new_user_cap_status and list_waitlist are read-only status checks. "
    "admit_from_waitlist lets one specific waitlisted person in past the current cap -- it "
    "doesn't raise the cap itself (that's the MESSA_NEW_USER_CAP environment variable, not "
    "something you can change).\n"
)


def build_admin_subagent(user: config.UserContext, model: BaseChatModel) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec (feature/agentic-upgrade
    plan) -- same shape/reasoning as email_tools.build_email_subagent.
    admin_agent used to be a plain declarative SubAgent dict (tools/
    system_prompt frozen at build_orchestrator's start) -- see
    tools/integration_tools.py's build_integration_subagent docstring for
    the concrete "artifact created earlier this turn is invisible to a
    later delegation" bug this fixes. Only ever registered for
    user.is_admin (see agents/registry.py's build_orchestrator) --
    unchanged by this conversion."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state["messages"])
        tools = build_admin_tools(user)
        system_prompt = ADMIN_SYSTEM_PROMPT + reliability.RELIABILITY_GUARDRAIL_STR
        if config.SCRATCHPAD_AND_SKILLS_ENABLED:
            tools = tools + build_scratchpad_tools(user, "admin_agent")
            system_prompt = system_prompt + await scratchpad_prompt_block(user, "admin_agent")
        run_config = {"recursion_limit": config.RECURSION_LIMIT}
        inner_agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[ToolFailureLadderMiddleware("propose_broadcast_message")],
        )
        final_messages = await run_inner_agent_with_claim_check(
            inner_agent, messages, run_config, label="admin_agent"
        )
        return {"messages": [AIMessage(content=last_ai_text(final_messages))]}

    return {
        "name": "admin_agent",
        "description": (
            "Admin-only: broadcast a message to every user (preview -> approval -> "
            "execute), and manage the new-user waitlist/signup cap. Only reachable "
            "for admin accounts."
        ),
        "runnable": RunnableLambda(_run),
    }
