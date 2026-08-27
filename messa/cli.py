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
from .tools.browser_tools import BrowserToolProvider


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


async def main_async(session_id: str) -> None:
    console.system(f"Session: {session_id}  (Ctrl+C or 'exit' to quit)")

    user_row = await db.get_or_create_user(
        config.DEFAULT_CLI_PHONE, config.DEFAULT_CLI_NAME, config.DEFAULT_TIMEZONE
    )
    user = config.UserContext(
        user_id=user_row["id"],
        phone_number=user_row["phone_number"],
        name=user_row["name"],
        timezone=user_row["timezone"],
        channel="cli",
    )

    approval_gate = CLIApprovalGate()
    bg_tasks: list[asyncio.Task] = []

    async with BrowserToolProvider(approval_gate) as browser_provider:
        agent = await build_orchestrator(user, browser_provider.tools, approval_gate)
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

                session_store.append(session_id, "user", user_input)
                await db.append_message(user.user_id, "user", user_input, channel="cli")
                history.append({"role": "user", "content": user_input})

                history = await run_turn(agent, history)

                # Persist only the final assistant reply text for the local
                # transcript + DB history (intermediate tool/AI chatter stays
                # in the live console, not the durable log).
                last_ai_text = ""
                for m in reversed(history):
                    if getattr(m, "type", None) == "ai" and getattr(m, "content", None):
                        last_ai_text = m.content if isinstance(m.content, str) else str(m.content)
                        break
                if last_ai_text:
                    session_store.append(session_id, "assistant", last_ai_text)
                    await db.append_message(user.user_id, "assistant", last_ai_text, channel="cli")
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
