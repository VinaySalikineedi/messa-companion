"""CLI entrypoint: chat with Messa, see every agent delegation and tool call,
with each session's transcript stored locally so you can resume it later.

Run: python -m messa.cli [--session NAME] [--list-sessions]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import traceback

from langchain_core.messages import HumanMessage

from . import background, config, console, db, session_store, usage
from .agents.registry import build_orchestrator
from .approval import CLIApprovalGate

# Heuristic backstop for the "empty promise" failure mode (see run_turn's
# docstring): the system prompt explicitly tells Messa to open with a short
# acknowledgment like "Checking flights now..." or "Give me a few" right
# before a task-tool call, in the SAME response. When the model produces
# that acknowledgment but drops the tool call, the phrasing it used is
# almost always drawn from that exact instruction -- so a reply that (a)
# matches this and (b) made zero tool calls all turn is a strong signal
# something was promised but never actually started, not a case of "the
# reply legitimately needed no tool call." False positives here just cost
# one extra model call, which is cheap next to leaving the user's actual
# request silently dropped.
_STALL_PATTERN = re.compile(
    r"\b(checking|looking into|give (?:you |me )?(?:a|one) (?:moment|sec|second|few|minute)s?|"
    r"hold on|one (?:moment|sec|second)|i'?ll (?:check|look|find)|let me (?:check|look|find)|"
    r"\bon it\b|sending (?:this|that|it) (?:to|over|back)|working on it)\b",
    re.IGNORECASE,
)


async def _context_from_row(user_row: dict, channel: str) -> config.UserContext:
    """Shared tail end of both load_user_context (below) and
    load_user_context_by_id: everything from here on only needs the user's
    row, not how it was looked up (phone number vs a bare id) -- see each
    caller's own docstring for why there are now two entry points.

    `db.ensure_timezone_resolved` runs on every call: a no-op the moment
    timezone_confirmed is true (the common case, one cheap column check),
    but for any user created before that feature existed -- or whose city
    was saved before it resolved successfully -- this retries resolution
    against the city already on file, so those accounts self-heal onto the
    correct timezone without needing to re-answer onboarding.

    `db.ensure_default_briefings` runs right after, once the timezone above
    is as resolved as it's going to get this turn -- same self-healing
    pattern, this time backfilling the morning/evening briefing cron jobs
    (see config.DEFAULT_BRIEFINGS) for any user who doesn't already have
    one of each, and correcting either one's local-time meaning if it was
    provisioned before this user's timezone was confirmed.

    `db.get_or_create_messa_email_local_part` is the same kind of
    self-healing/backfill call as the two above, just for a user's own
    <local-part>@config.TEXTMESSA_EMAIL_DOMAIN address (migrations/
    013_personal_email.sql) -- cheap after the first time, same shape as
    get_or_create_live_share_token."""
    user_row = await db.ensure_timezone_resolved(user_row)
    await db.ensure_default_briefings(user_row)
    live_view_token = await db.get_or_create_live_share_token(user_row["id"])
    messa_email_local_part = await db.get_or_create_messa_email_local_part(
        user_row["id"], user_row.get("name")
    )
    return config.UserContext(
        user_id=user_row["id"],
        phone_number=user_row["phone_number"],
        name=user_row["name"],
        email=user_row.get("email"),
        city=user_row.get("city"),
        timezone=user_row["timezone"],
        timezone_confirmed=bool(user_row.get("timezone_confirmed", False)),
        onboarding_step=user_row["onboarding_step"],
        channel=channel,
        live_view_token=live_view_token,
        email_connected=bool(user_row.get("email_connected", False)),
        messa_email_local_part=messa_email_local_part,
        default_email_provider=user_row.get("default_email_provider") or "messa",
        plan_id=user_row.get("plan_id") or config.plans.DEFAULT_PLAN_ID,
        is_admin=bool(user_row.get("is_admin", False)),
        memory_profile=user_row.get("memory_profile"),
    )


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
    return await _context_from_row(user_row, channel)


async def load_user_context_by_id(user_id: int, channel: str = "sms") -> config.UserContext | None:
    """Same as load_user_context, but for a turn that starts from an
    already-known user id rather than an inbound phone number -- the one
    real caller today is server.py's inbound personal-email webhook: an
    email arrives at <local-part>@config.TEXTMESSA_EMAIL_DOMAIN, which
    resolves straight to a user_id (db.get_user_by_messa_email_local_part),
    with no phone number involved at all. `channel` still defaults to
    "sms" rather than introducing a distinct value, since the reply for
    that flow genuinely does go out over the user's own SMS/iMessage
    number (see server.py's _process_inbound_personal_email) -- it's the
    one-off synthetic prompt text that tells Messa an email triggered this
    turn, not the channel field, which only controls output *formatting*
    (see agents/registry.py's channel_str). Returns None if user_id doesn't
    exist (should not happen in practice: it always comes from a lookup
    that already confirmed the row exists moments earlier)."""
    user_row = await db.get_user_by_id(user_id)
    if user_row is None:
        return None
    return await _context_from_row(user_row, channel)


async def _load_user_context(channel: str = "cli") -> config.UserContext:
    """CLI convenience wrapper: always the fixed local dev identity."""
    return await load_user_context(config.DEFAULT_CLI_PHONE, config.DEFAULT_CLI_NAME, channel)


async def run_turn(
    agent, messages: list[dict], on_ai_message=None, _allow_retry: bool = True
) -> list[dict]:
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

    The "empty promise" failure and its retry: LangChain's ReAct agent loop
    ends a turn the instant an AI message carries zero tool_calls -- there
    is no next turn where the model gets to actually make a call it just
    described in text (e.g. "Checking both now... give me a few"). The
    system prompt tells Messa not to do this, but that's a probabilistic
    steer, not a guarantee, and it does still happen. Rather than rely on
    the prompt alone, this also catches it structurally: if the WHOLE turn
    made zero tool calls and the final text matches `_STALL_PATTERN` (the
    exact acknowledgment phrasing the prompt asks Messa to use right before
    a delegation), that's a strong signal a call was promised and dropped
    -- so this replays the same messages plus one nudge and retries
    exactly once (`_allow_retry=False` on the recursive call prevents a
    loop). The original (broken) acknowledgment may have already reached
    the user via `on_ai_message` before this check runs, which is fine --
    it was a true statement of intent, just unfinished; the retry's job is
    making sure the actual delegation (and eventually a real answer)
    follows it instead of leaving the user's request silently dropped."""
    final_messages = list(messages)
    any_tool_call = False
    last_text = ""
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
                if tool_calls:
                    any_tool_call = True
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
                        last_text = text
                    if on_ai_message:
                        await on_ai_message(text, delegating_to)
                final_messages.append(m)

    if _allow_retry and not any_tool_call and last_text and _STALL_PATTERN.search(last_text):
        console.system(
            "Messa: this reply looked like a dropped delegation (acknowledgment with no "
            "tool call all turn) -- retrying once with a nudge."
        )
        nudge = HumanMessage(
            content=(
                "(auto-nudge, not from the user: your previous reply didn't include the "
                "tool call it described. If you intended to delegate to a subagent just "
                "now, call it in this response. If you'd already fully answered the "
                "request, ignore this.)"
            )
        )
        return await run_turn(
            agent, final_messages + [nudge], on_ai_message=on_ai_message, _allow_retry=False
        )

    return final_messages


def last_ai_text(messages: list) -> str:
    """The final assistant reply text out of a message list -- what actually
    goes back to the user (SMS/iMessage reply, or the CLI's own persisted
    transcript line), as opposed to the full intermediate agent chatter."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "ai" and getattr(m, "content", None):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


_LIVE_VIEW_URL_RE = re.compile(r"https?://[^\s/]+/live/([A-Za-z0-9_-]+)", re.IGNORECASE)


def _sanitize_live_view_urls(text: str, user: config.UserContext) -> str:
    """Ensures any live-view URL in an assistant message points to this user's
    authentic live_view_share_url, never a hallucinated/invented token
    copied from context. If the user has no live_view_share_url, strips the
    dead link entirely."""
    if not text:
        return text

    real_share_url = user.live_view_share_url
    real_token = user.live_view_token

    def _replace_match(match: re.Match) -> str:
        found_token = match.group(1)
        if real_share_url and real_token:
            if found_token == real_token:
                return match.group(0)
            return real_share_url
        return ""

    sanitized = _LIVE_VIEW_URL_RE.sub(_replace_match, text)
    if not real_share_url:
        sanitized = re.sub(r"Watch it live:\s*", "", sanitized, flags=re.IGNORECASE)
    return sanitized


async def _onboarding_complete_messages(user: config.UserContext) -> list[str]:
    """Fires exactly once per user, the turn onboarding_step first reaches
    "complete" (right after they answer the last onboarding question,
    awaiting_email, via save_profile_info) -- checked and composed here in
    code rather than left to the model for the same reason run_message
    stopped trusting the model to write the deepsearch live-view link
    itself (see that function's own docstring): this message carries
    Messa's actual email address and live-view link, and asking a model to
    faithfully reproduce both, verbatim, in the same response as an
    unrelated tool call, is exactly the "mostly works" reliability this
    project has already been burned by once. agents/registry.py's
    awaiting_email prompt tells Messa not to try writing this herself.

    Returns [] if onboarding wasn't the thing that just completed on this
    call (the common case, checked cheaply since UserContext.onboarding_complete
    -- loaded at the START of this turn -- already being True short-circuits
    before any DB call), or the message(s) to send/persist otherwise. Two
    short messages rather than one long one (see run_message's docstring on
    keeping texts scannable): the first sets expectations about what Messa
    actually is, the second is the concrete email/link payload -- each
    reads fine as its own text, and skipping the second entirely (older
    deployment, migrations 006/013 not applied) still leaves the first
    intact rather than silently dropping the whole reveal."""
    if user.onboarding_complete:
        return []
    fresh = await db.get_user_by_id(user.user_id)
    if not fresh or fresh.get("onboarding_step") != "complete":
        return []

    name = fresh.get("name")
    greeting = f"You're all set, {name}!" if name else "You're all set!"
    messages = [
        f"{greeting} Quick thing about me -- I'm not your typical chatbot. I can browse "
        "the web, click through sites, manage your calendar and reminders, and send "
        "emails for you -- basically get things done, not just answer questions. Just "
        "tell me what you need."
    ]

    reveal_parts = []
    if user.messa_email:
        reveal_parts.append(
            f"Here's my own email: {user.messa_email} -- it's mine to manage, so feel "
            "free to hand it out anywhere (signups, forms, whatever) and I'll take care "
            "of what lands there, looping you in before anything that needs your OK. I "
            "can manage your personal inbox too, just say the word."
        )
    if user.live_view_share_url:
        reveal_parts.append(f"Check that inbox, or watch me work, anytime here: {user.live_view_share_url}")
    if reveal_parts:
        messages.append("\n".join(reveal_parts))

    return messages


async def _send_limit_notice_once(
    user: config.UserContext, feature: str, status: usage.LimitResult, send, marker_context: str,
) -> str | None:
    """Shared by run_message's pre-agent gate and its mid-turn check in
    _on_ai_message below: claims the day's one "you're over your limit"
    notice (usage.claim_daily_notice) and, only if THIS call actually won
    that claim, writes a system-role backlog marker plus the user-facing
    notice itself, sends it, and returns it. Returns None when nothing
    should go out this time -- either someone already claimed the notice
    for today, or the caller decides not to send one -- in which case the
    caller drops the current message/turn silently rather than repeating
    "you're over your limit" on every subsequent message, which would read
    as spammy and add no new information (see usage_limits_proposal.md's
    "chicken-and-egg problem, resolved")."""
    if not await usage.claim_daily_notice(user, feature):
        return None
    notice = (
        f"You've hit your daily limit for {feature.replace('_', ' ')} "
        f"({status.count}/{status.limit_display}) on the {status.plan_name} plan. I'll go "
        "quiet on this until it resets -- upgrading for a higher limit is coming soon."
    )
    marker = (
        f"The user went over their daily {feature} limit {marker_context}. Messages logged "
        "from here until the limit resets were not answered in real time -- once this "
        "conversation resumes after the reset, treat any of their messages that follow this "
        "marker as backlog to briefly acknowledge, not live questions to individually answer "
        "one by one."
    )
    await db.append_message(user.user_id, "system", marker, channel=user.channel)
    await db.append_message(user.user_id, "assistant", notice, channel=user.channel)
    if send:
        await send(notice)
    return notice


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
    regardless of what the model's own text looks like.

    Two more deterministic, code-authored additions live in this same
    function, same reasoning as the link above: the short "this can take a
    few minutes, I'll text you when it's done" (plus, on a user's first-ever
    deepsearch, a one-time "I can do more than just browse" tip) appended
    right alongside it in _on_ai_message, and the onboarding-complete reveal
    (Messa's own email + live-view link, sent once right after the user
    answers the last onboarding question) via _onboarding_complete_messages
    above. Both are kept short and fixed on purpose -- an explicit ask was
    to keep outbound texts concise, and fixed strings are the only way to
    guarantee that stays true forever rather than drifting longer over time
    the way freely-generated model text tends to."""
    # 12 rather than 20: measured against the real system prompt + tool
    # schemas (~2,000 tokens fixed, every turn, regardless of history), 20
    # short SMS-length messages only added another ~600-800 tokens -- not
    # actually "bloat" in the sense of pushing near a context limit or deep
    # into "lost in the middle" territory. Trimmed anyway since it's free
    # (fewer, more recent messages can only reduce the chance of an older,
    # unrelated exchange distracting a turn) and costs nothing to try, but
    # this alone isn't expected to fix dropped-delegation replies -- that's
    # a structural model-behavior issue (see run_turn's retry logic above),
    # not a context-size one.
    # Inbound message is always logged, whether or not this turn ends up
    # producing a reply -- this table is the honest record of what
    # actually happened (see usage_limits_proposal.md's "blocked messages
    # aren't lost" section), and it's what let the pre-agent gate below
    # even exist: without this line running first, a user's texts sent
    # while over their limit would just vanish instead of showing up as
    # backlog once the limit resets.
    await db.append_message(user.user_id, "user", text, channel=user.channel)

    # Pre-agent usage gate for number_of_texts, checked BEFORE loading
    # history or building/running the agent at all -- a user already over
    # today's limit costs zero model calls and zero tool calls for a reply
    # that was never going to reach their phone anyway (see
    # usage_limits_proposal.md's "chicken-and-egg problem, resolved").
    if not user.is_admin:
        texts_status = await usage.peek_usage(user, usage.FEATURE_NUMBER_OF_TEXTS)
        if not texts_status.allowed:
            notice = await _send_limit_notice_once(
                user, usage.FEATURE_NUMBER_OF_TEXTS, texts_status, send,
                marker_context="at the start of this message",
            )
            return notice or ""

    recent = await db.get_recent_messages(user.user_id, limit=12)
    history = [{"role": r["role"], "content": r["content"]} for r in recent]
    history.append({"role": "user", "content": text})

    sent_texts: list[str] = []
    # Once per incoming message, not once per delegation: a compound ask
    # ("how far is X, also get me today's news") can legitimately produce
    # more than one deepsearch delegation in the same exchange -- and now
    # that run_turn can also retry a dropped delegation once (see its
    # docstring), the *same* logical attempt can produce a second
    # acknowledgment moments after the first. Either way, repeating the
    # exact same permanent link (or the reminder/tip lines below) twice in
    # one exchange reads as spammy rather than helpful -- it's still right
    # there in the first message. Reported directly (of the link, before
    # the reminder/tip existed): "it happened twice and was a little
    # annoying."
    deepsearch_extras_sent = False
    # Resolved at most once per call, only if a deepsearch delegation
    # actually happens -- None means "not checked yet" (distinct from
    # False, a real answer), so the has_prior_deepsearch_session query
    # never runs on a turn that doesn't need it.
    is_first_deepsearch: bool | None = None

    async def _on_ai_message(msg_text: str, delegating_to: str | None = None) -> None:
        nonlocal deepsearch_extras_sent, is_first_deepsearch
        msg_text = _sanitize_live_view_urls(msg_text, user)
        if delegating_to == "deepsearch" and not deepsearch_extras_sent:
            share_url = user.live_view_share_url
            extra_lines: list[str] = []
            if share_url and share_url not in msg_text:
                extra_lines.append(f"Watch it live: {share_url}")
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
            # These two are deterministic and code-authored for the same
            # reason the link is (see this function's own docstring): a
            # user's explicit ask was "remind users this'll take a while,
            # they can sit back, and that deepsearch can do more than just
            # browsing" -- baking that into the system prompt would mean
            # trusting the model to remember and reword it correctly on
            # every single delegation, forever. Appending fixed text here
            # instead guarantees it's always said, always this short, and
            # never balloons turn over turn the way model-authored
            # boilerplate tends to.
            step_away_notice = (
                "This can take a few minutes -- go ahead and step away, I'll text you "
                "the second it's done."
            )
            if "step away" not in msg_text.lower():
                extra_lines.append(step_away_notice)
            if is_first_deepsearch is None:
                is_first_deepsearch = not await db.has_prior_deepsearch_session(user.user_id)
            if is_first_deepsearch:
                # Shown once, on this user's very first-ever deepsearch
                # delegation only (not every single time) -- the same
                # "repeating this reads as spammy" reasoning as the link
                # above, just on a longer timescale: useful the first time
                # someone sees deepsearch in action, noise on the tenth.
                extra_lines.append(
                    "And it's not just browsing -- I can click through logins, fill out "
                    "forms, and handle more involved stuff too, so feel free to ask for that."
                )
            extra_block = "\n".join(extra_lines)
            msg_text = f"{msg_text.rstrip()} {extra_block}" if msg_text.strip() else extra_block
            deepsearch_extras_sent = True
        if not msg_text:
            return  # nothing to say and no link to attach -- e.g. a silent, non-deepsearch delegation

        if not user.is_admin:
            # The pre-agent gate in run_message only catches a user who was
            # ALREADY over their limit before this turn started -- a turn
            # that sends several messages in a row (an ack plus a
            # delegation summary, say) can still cross the daily cap
            # partway through. Same rule applies here: consume one unit
            # for the message about to go out, and if that pushes the
            # user over, either send today's one notice (if nobody's
            # claimed it yet) or drop this message silently.
            text_result = await usage.check_and_consume(user, usage.FEATURE_NUMBER_OF_TEXTS)
            if not text_result.allowed:
                notice = await _send_limit_notice_once(
                    user, usage.FEATURE_NUMBER_OF_TEXTS, text_result, send,
                    marker_context="mid-conversation",
                )
                if notice:
                    sent_texts.append(notice)
                return  # this particular message is dropped either way

        sent_texts.append(msg_text)
        await db.append_message(user.user_id, "assistant", msg_text, channel=user.channel)
        if send:
            await send(msg_text)

    final = await run_turn(agent, history, on_ai_message=_on_ai_message)

    # Deterministic, one-time onboarding-complete reveal -- see
    # _onboarding_complete_messages' own docstring for why this isn't left
    # to the model. Sent (and persisted) as its own follow-up, after
    # whatever the model itself said this turn (typically a short
    # acknowledgment of the just-answered email question).
    for onboarding_msg in await _onboarding_complete_messages(user):
        sent_texts.append(onboarding_msg)
        await db.append_message(user.user_id, "assistant", onboarding_msg, channel=user.channel)
        if send:
            await send(onboarding_msg)

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
