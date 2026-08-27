"""CLI entrypoint: chat with Messa, see every agent delegation and tool call,
with each session's transcript stored locally so you can resume it later.

Run: python -m messa.cli [--session NAME] [--list-sessions]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback

from . import background, config, console, db, session_store
from .agents.registry import build_orchestrator
from .approval import CLIApprovalGate


async def load_user_context(
    phone_number: str, name: str | None = None, channel: str = "cli"
) -> config.UserContext:
    """(Re)reads the user row from the DB. Called at startup and again before
    every turn, so onboarding fields saved mid-conversation (see
    agents/registry.py's save_profile_info) are reflected in the next turn's
    system prompt -- deepagents bakes system_prompt in at agent-build time,
    so the orchestrator gets rebuilt each turn with fresh context rather than
    reused across the whole session.

    Shared by the CLI (fixed dev phone number) and the Sendblue webhook
    server (real inbound phone_number, one per sender) -- see run_message
    below for the rest of what the server reuses from here."""
    user_row = await db.get_or_create_user(phone_number, name, config.DEFAULT_TIMEZONE)
    live_view_token = await db.get_or_create_live_share_token(user_row["id"])
    return config.UserContext(
        user_id=user_row["id"],
        phone_number=user_row["phone_number"],
        name=user_row["name"],
        email=user_row.get("email"),
        city=user_row.get("city"),
        timezone=user_row["timezone"],
        onboarding_step=user_row["onboarding_step"],
        channel=channel,
        live_view_token=live_view_token,
    )


async def _load_user_context(channel: str = "cli") -> config.UserContext:
    """CLI convenience wrapper: always the fixed local dev identity."""
    return await load_user_context(config.DEFAULT_CLI_PHONE, config.DEFAULT_CLI_NAME, channel)


async def run_turn(agent, messages: list[dict]) -> list[dict]:
    """Stream one agent turn, printing Messa's own thoughts and delegations.

    Per-tool call/result tracing (including everything a subagent does) is
    printed synchronously by the trace_tool wrapper as each tool actually
    runs -- see messa/console.py's module docstring for why that's more
    reliable here than parsing nested LangGraph subgraph stream events.
    """
    final_messages = list(messages)
    async for chunk in agent.astream(
        {"messages": messages},
        config={"recursion_limit": config.RECURSION_LIMIT},
        stream_mode="updates",
    ):
        for _node, update in chunk.items():
            if not update:
                continue
            for m in update.get("messages", []):
                content = getattr(m, "content", "")
                tool_calls = getattr(m, "tool_calls", None) or []
                for tc in tool_calls:
                    if tc.get("name") == "task":
                        args = tc.get("args", {})
                        console.delegation(
                            "messa", args.get("subagent_type", "?"), args.get("description", "")
                        )
                if content and getattr(m, "type", None) == "ai":
                    console.agent_say("messa", content if isinstance(content, str) else str(content))
                final_messages.append(m)
    return final_messages


def last_ai_text(messages: list) -> str:
    """The final assistant reply text out of a message list -- what actually
    goes back to the user (SMS/iMessage reply, or the CLI's own persisted
    transcript line), as opposed to the full intermediate agent chatter."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "ai" and getattr(m, "content", None):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


async def run_message(user: config.UserContext, agent, text: str) -> str:
    """One full, stateless turn for `user`: loads recent DB history for
    context, runs it, persists both sides, returns the reply text.

    This is what the Sendblue webhook server uses -- each inbound webhook
    is its own HTTP request with no long-lived process holding conversation
    state, unlike the CLI's `main_async` loop below, which keeps its own
    in-memory `history` list (plus a local session_store transcript) across
    the whole run. Loading from db.get_recent_messages() here means both
    paths ultimately draw on the same durable history, they just do it
    differently."""
    recent = await db.get_recent_messages(user.user_id, limit=20)
    history = [{"role": r["role"], "content": r["content"]} for r in recent]
    history.append({"role": "user", "content": text})
    await db.append_message(user.user_id, "user", text, channel=user.channel)

    final = await run_turn(agent, history)

    reply = last_ai_text(final)
    if reply:
        await db.append_message(user.user_id, "assistant", reply, channel=user.channel)
    return reply


async def main_async(session_id: str) -> None:
    console.system(f"Session: {session_id}  (Ctrl+C or 'exit' to quit)")

    user = await _load_user_context()
    if not user.onboarding_complete:
        console.system("New user -- Messa will ask for name/email/location casually as you chat.")

    approval_gate = CLIApprovalGate()
    bg_tasks = background.start_background_pollers()

    history = session_store.load(session_id)
    if history:
        console.system(f"Resumed {len(history)} prior message(s) from this session.")

    print("\n=== Messa CLI ===")
    print("Type your message, or 'exit' to quit.\n")

    try:
        while True:
            try:
                user_input = await asyncio.to_thread(input, "You: ")
            except EOFError:
                break
            user_input = user_input.strip()
            if user_input.lower() in ("exit", "quit"):
                break
            if not user_input:
                continue

            # Refresh user context each turn so onboarding progress and any
            # profile fields saved last turn are reflected in this turn's
            # system prompt (see _load_user_context's docstring).
            user = await _load_user_context()
            agent = await build_orchestrator(user, approval_gate)

            session_store.append(session_id, "user", user_input)
            await db.append_message(user.user_id, "user", user_input, channel="cli")
            history.append({"role": "user", "content": user_input})

            history = await run_turn(agent, history)

            # Persist only the final assistant reply text for the local
            # transcript + DB history (intermediate tool/AI chatter stays
            # in the live console, not the durable log).
            reply = last_ai_text(history)
            if reply:
                session_store.append(session_id, "assistant", reply)
                await db.append_message(user.user_id, "assistant", reply, channel="cli")
    except Exception:
        console.system("!!! EXCEPTION !!!")
        traceback.print_exc()
    finally:
        for t in bg_tasks:
            t.cancel()
        await db.close_pool()
        console.system("Session ended.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with Messa on the CLI.")
    parser.add_argument("--session", help="Session id to start or resume.")
    parser.add_argument("--list-sessions", action="store_true", help="List saved sessions and exit.")
    args = parser.parse_args()

    if args.list_sessions:
        for s in session_store.list_sessions():
            print(s)
        sys.exit(0)

    session_id = args.session or session_store.new_session_id()
    asyncio.run(main_async(session_id))


if __name__ == "__main__":
    main()
