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


async def run_turn(agent, messages: list[dict], on_ai_message=None) -> list[dict]:
    """Stream one agent turn, printing Messa's own thoughts and delegations.

    Per-tool call/result tracing (including everything a subagent does) is
    printed synchronously by the trace_tool wrapper as each tool actually
    runs -- see messa/console.py's module docstring for why that's more
    reliable here than parsing nested LangGraph subgraph stream events.

    on_ai_message: optional async callback(text: str, delegating_to: str | None),
    awaited inline for every AI message *or* delegation the moment it's
    produced -- not just the turn's final message. The CLI doesn't need
    this (the terminal already shows everything live via console.agent_say/
    console.delegation below, which always run regardless of this
    callback); it exists for run_message/the Sendblue webhook path, so an
    acknowledgment before a subagent delegation actually reaches the user's
    phone as its own text the moment Messa says it, instead of only ever
    showing up in the server log while just the turn's last message gets
    texted (see run_message's docstring for the bug this fixes). Awaited
    inline (not fire-and-forget) so the ack is confirmed sent to Sendblue
    before the graph moves on to the possibly-long-running delegation that
    follows it.

    `delegating_to` is the subagent_type ("deepsearch", etc.) if this exact
    message also carries a `task` tool call, else None -- fired even when
    `text` is empty (the model went straight to the tool call with no
    acknowledgment at all), because run_message needs to know about a
    deepsearch delegation regardless of whether the model said anything,
    to attach the live-view link itself rather than trusting the model to
    write a correct, complete URL into its own free-form text (see
    run_message's docstring for why that trust turned out to be misplaced).
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
                delegating_to = None
                for tc in tool_calls:
                    if tc.get("name") == "task":
                        args = tc.get("args", {})
                        subagent_type = args.get("subagent_type", "?")
                        console.delegation("messa", subagent_type, args.get("description", ""))
                        delegating_to = subagent_type
                is_ai = getattr(m, "type", None) == "ai"
                text = (content if isinstance(content, str) else str(content)) if content else ""
                if is_ai and (text or delegating_to):
                    if text:
                        console.agent_say("messa", text)
                    if on_ai_message:
                        await on_ai_message(text, delegating_to)
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


async def run_message(user: config.UserContext, agent, text: str, send=None) -> str:
    """One full, stateless turn for `user`: loads recent DB history for
    context, runs it, persists both sides, returns the final reply text.

    This is what the Sendblue webhook server uses -- each inbound webhook
    is its own HTTP request with no long-lived process holding conversation
    state, unlike the CLI's `main_async` loop below, which keeps its own
    in-memory `history` list (plus a local session_store transcript) across
    the whole run. Loading from db.get_recent_messages() here means both
    paths ultimately draw on the same durable history, they just do it
    differently.

    send: optional async callable(text: str) -> None. When given, it's
    invoked immediately for *every* AI message Messa produces this turn, in
    order -- not just the last one. Without this, a pre-delegation
    acknowledgment would only ever get logged to the console (via
    run_turn's own tracing), never actually reach the user: the caller used
    to send only whatever this function returned, which is exclusively the
    turn's *final* AI message -- so over SMS, Messa's live-view link (meant
    to go out as part of the acknowledgment right before delegating to
    deepsearch) was silently dropped, and only her post-research summary
    ever arrived as a text. server.py passes Sendblue's send_message here
    so each of Messa's utterances goes out as its own SMS the moment she
    says it, matching how a person actually texts rather than batching
    everything into one message at the end of a possibly multi-minute
    browsing task. Each sent message is persisted to message_history as it
    goes out (not just the final one), so this turn's own interim texts are
    correctly present in the next turn's conversation history too.

    The live-view link itself is attached here in code, not by the model:
    an earlier version had the system prompt tell Messa to write the exact
    URL into her own acknowledgment sentence, which mostly worked but
    failed exactly the way free-text LLM output eventually always does --
    a real run trailed off mid-sentence ("checking a") right as the model
    fired its tool call, before ever reaching the link. stream_mode
    "updates" only ever hands back complete messages, never partial
    tokens, so that wasn't a streaming/truncation bug in this code -- it
    was genuinely the full, final `content` the model chose to generate for
    that turn. Trusting a model to reliably finish a sentence *and*
    reproduce a URL correctly, every single time, right before it switches
    into tool-call mode, isn't reliable enough for something that's
    supposed to always be there. So the model is no longer asked to write
    the link at all (see agents/registry.py) -- run_turn instead tells us
    whenever a message carries a deepsearch delegation (`delegating_to`,
    fired even when the model said nothing at all), and the link is
    appended deterministically below, guaranteed correct and complete
    regardless of what the model's own text looks like."""
    recent = await db.get_recent_messages(user.user_id, limit=20)
    history = [{"role": r["role"], "content": r["content"]} for r in recent]
    history.append({"role": "user", "content": text})
    await db.append_message(user.user_id, "user", text, channel=user.channel)

    sent_texts: list[str] = []

    async def _on_ai_message(msg_text: str, delegating_to: str | None = None) -> None:
        share_url = user.live_view_share_url
        if delegating_to == "deepsearch":
            if share_url and share_url not in msg_text:
                link_line = f"Watch it live: {share_url}"
                msg_text = f"{msg_text.rstrip()} {link_line}" if msg_text.strip() else link_line
            elif not share_url:
                # Confirmed (by direct integration test against the real
                # deepagents/create_deep_agent harness) that `delegating_to`
                # detection itself is reliable, in both the "model wrote an
                # acknowledgment" and "model went straight to the tool call
                # with zero text" cases -- so if a deepsearch delegation
                # ever ships with no link again, it's one of exactly two
                # things and this line says which: either migration 006
                # hasn't run yet against this DB (live_view_token is None --
                # get_or_create_live_share_token returns None whenever
                # users.live_share_token doesn't exist yet) or it has run
                # but something about token generation/lookup itself failed
                # silently. Previously this was a completely silent no-op,
                # which is exactly why the last two rounds of this bug took
                # multiple back-and-forths to even localize.
                console.system(
                    f"Deepsearch delegation for user #{user.user_id} has no live-view link to "
                    f"attach -- live_view_token={user.live_view_token!r}, "
                    f"LIVE_VIEW_BASE_URL={config.LIVE_VIEW_BASE_URL!r}. "
                    + (
                        "live_view_token is None: migration 006_live_view.sql likely hasn't "
                        "run against this DB yet (or get_or_create_live_share_token failed)."
                        if not user.live_view_token
                        else "LIVE_VIEW_BASE_URL is empty: set MESSA_LIVE_VIEW_BASE_URL."
                        if not config.LIVE_VIEW_BASE_URL
                        else "both look set -- check live_view_share_url's own logic."
                    )
                )
        if not msg_text:
            return  # nothing to say and no link to attach -- e.g. a silent, non-deepsearch delegation
        sent_texts.append(msg_text)
        await db.append_message(user.user_id, "assistant", msg_text, channel=user.channel)
        if send:
            await send(msg_text)

    final = await run_turn(agent, history, on_ai_message=_on_ai_message)

    if sent_texts:
        return sent_texts[-1]

    # _on_ai_message above never fired at all -- the turn produced zero
    # AI-with-content messages (rare, but possible). Nothing to persist or
    # send either; last_ai_text(final) will also be "" here.
    return last_ai_text(final)


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
