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

from . import background, config, console, db, reliability, session_store, usage
from .agents.registry import build_orchestrator
from .approval import CLIApprovalGate
from .channels import sendblue
from .channels.sendblue import SendblueError
from .intent_router import IntentType, triage_incoming_message

# Keep-alive set for run_message's optional on_turn_complete fire-and-
# forget task (see run_message's own docstring) -- same "asyncio.create_task
# alone can silently get garbage-collected mid-run" bug server.py's own
# _spawn_background/_fire_and_forget_tasks pair already guards against.
# Not reusing that exact helper (server.py is the production webhook path
# and imports cli.py already; the reverse import would be circular) -- a
# second, tiny copy of the same three lines here, scoped to this module's
# own one use of the pattern.
_fire_and_forget_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _fire_and_forget_tasks.add(task)
    task.add_done_callback(_fire_and_forget_tasks.discard)
    return task


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

# Pre-send guardrail (see agents/registry.py's persona block, which asks for
# this same shape in the prompt -- this is the deterministic backstop for
# when prompting alone isn't enough): flags leftover markdown/bullet-list
# formatting, or a reply that's grown too long for a text message.
_MARKDOWN_ARTIFACT_PATTERN = re.compile(
    r"(\*\*[^*\n]+\*\*|^\s*#{1,6}\s|^\s*[-*]\s|^\s*\d+\.\s|```)",
    re.MULTILINE,
)
_GUARDRAIL_LENGTH_THRESHOLD = 320  # ~2 SMS segments -- tunable.

# Long-reply splitter (separate from the guardrail above, and applied AFTER
# it): the guardrail's own job is to try to make a reply SHORTER without
# losing meaning; this handles what's left once that's genuinely done --
# content that's still long because shortening it further would lose real
# information (e.g. several distinct facts the user asked for at once).
# Rather than send that as one long wall-of-text bubble, break it into a
# few separate texts in a row, the way a person actually texts several
# short messages back to back instead of one giant paragraph. Target/max
# are deliberately different constants from the guardrail's threshold above
# -- this is about how big each individual OUTGOING bubble should read, not
# the trigger for the compression rewrite.
_SPLIT_TARGET_CHARS = 600  # comfortable text-message bubble (avoid micro-splitting 2-3 sentences)
_SPLIT_MAX_PARTS = 4  # a message needing more pieces than this should have been shortened upstream instead
_SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?])\s+")
_URL_PATTERN = re.compile(r"(https?://[^\s)\]]+)", re.IGNORECASE)
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _extract_url_chunks(msg_text: str) -> list[str]:
    """Isolates URLs from surrounding text into their own standalone chunks
    so Apple iMessage unfurls rich preview cards instead of treating them
    as inline plain hyperlinks."""
    if not msg_text:
        return []

    # Unpack markdown links like [Review Cart ➔](https://...) -> Review Cart ➔ \n\n https://...
    cleaned = _MARKDOWN_LINK_PATTERN.sub(r"\1\n\n\2", msg_text)

    matches = list(_URL_PATTERN.finditer(cleaned))
    if not matches:
        return []

    # If the message is strictly just a single URL (e.g. "https://example.com"), keep as-is
    if len(matches) == 1 and matches[0].group(0).strip() == cleaned.strip():
        return [cleaned.strip()]

    chunks: list[str] = []
    last_idx = 0
    for match in matches:
        raw_url = match.group(0)
        url = raw_url.rstrip(".,;:!?)>]}\"'")
        trailing_len = len(raw_url) - len(url)
        pre = cleaned[last_idx : match.start()].strip()
        if pre:
            chunks.append(pre)
        chunks.append(url)
        last_idx = match.end() - trailing_len

    post = cleaned[last_idx:].strip().lstrip(".,;:!?)>]}\"'").strip()
    if post:
        chunks.append(post)

    return chunks


def _split_text_block(msg_text: str) -> list[str]:
    """Splits a text block (with no URLs) into a handful of shorter texts of
    roughly even size, or returns it as a single-item list unchanged when
    it's already short."""
    if not msg_text or len(msg_text) <= _GUARDRAIL_LENGTH_THRESHOLD:
        return [msg_text] if msg_text else []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", msg_text) if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [s.strip() for s in _SENTENCE_BOUNDARY_PATTERN.split(msg_text) if s.strip()]
    if len(paragraphs) <= 1:
        return [msg_text]  # nothing to sensibly break on -- one sentence/word, send as-is

    chunks: list[str] = []
    current = ""
    for part in paragraphs:
        candidate = f"{current} {part}".strip() if current else part
        if current and len(candidate) > _SPLIT_TARGET_CHARS:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)

    if len(chunks) > _SPLIT_MAX_PARTS:
        head, tail = chunks[: _SPLIT_MAX_PARTS - 1], chunks[_SPLIT_MAX_PARTS - 1 :]
        chunks = head + [" ".join(tail)]
    return chunks or [msg_text]


def _split_into_texts(msg_text: str) -> list[str]:
    """Splits a reply into a handful of shorter texts of roughly even size,
    or returns it as a single-item list unchanged when it's already short.

    If the reply contains one or more HTTP/HTTPS URLs, extracts every URL
    into its own standalone text bubble so Apple iMessage (and modern messaging
    clients) can reliably unfurl rich link cards (with photo, title, and domain)
    instead of falling back to inline plain-text hyperlinks.

    Splits text blocks on blank-line paragraph breaks first, falling back to
    sentence boundaries only when there are no such breaks. Caps non-URL chunks
    at _SPLIT_MAX_PARTS pieces."""
    if not msg_text:
        return []

    url_chunks = _extract_url_chunks(msg_text)
    if url_chunks:
        final_chunks: list[str] = []
        for chunk in url_chunks:
            if _URL_PATTERN.fullmatch(chunk):
                final_chunks.append(chunk)
            else:
                final_chunks.extend(_split_text_block(chunk))
        return final_chunks

    return _split_text_block(msg_text)


async def _apply_reply_guardrail(msg_text: str) -> str:
    """Deterministic, mechanical pass over a model-drafted reply right
    before it's persisted/sent. Both checks below are cheap/synchronous and
    the common case (a short, clean reply) costs nothing extra -- only a
    reply that actually trips one of them pays for a follow-up model call.

    That follow-up call asks the cheaper SUBAGENT_MODEL_NAME to rewrite the
    reply -- not truncate it -- into a short, human, plain-paragraph text
    that keeps every fact that matters, only staying as long as the
    original if shortening it would genuinely lose necessary meaning. If
    that call itself fails for any reason, the original msg_text is
    returned unchanged: a guardrail must never be the reason a real reply
    never reaches the user."""
    is_long = len(msg_text) > _GUARDRAIL_LENGTH_THRESHOLD
    has_markdown = bool(_MARKDOWN_ARTIFACT_PATTERN.search(msg_text))
    if not is_long and not has_markdown:
        return msg_text
    try:
        model = config.build_model(
            config.SUBAGENT_MODEL_NAME, api_key=config.api_key_for_agent("reply_guardrail"),
        )
        result = await model.ainvoke([
            {
                "role": "system",
                "content": (
                    "Rewrite the following text-message reply to be short and human: plain "
                    "paragraphs separated by a blank line, no bullet points, no numbered "
                    "lists, no markdown (**bold**, #headers, code fences) at all. Keep every "
                    "fact, name, and number that matters -- don't drop real information. Only "
                    "stay as long as the original if shortening it would genuinely lose "
                    "necessary meaning; otherwise make it meaningfully shorter. Reply with "
                    "ONLY the rewritten text, nothing else -- no preamble, no explanation."
                ),
            },
            {"role": "user", "content": msg_text},
        ])
        rewritten = (getattr(result, "content", None) or "").strip()
        return rewritten or msg_text
    except Exception as e:  # noqa: BLE001 -- guardrail failure must never suppress a real reply
        console.system(f"[reply guardrail] compression call failed, sending original: {e}")
        return msg_text


async def _context_from_row(user_row: dict, channel: str, message_handle: str | None = None) -> config.UserContext:
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
        city_prompt_skipped=bool(user_row.get("city_prompt_skipped", False)),
        email_prompt_skipped=bool(user_row.get("email_prompt_skipped", False)),
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
        deepsearch_beta_access=bool(user_row.get("deepsearch_beta_access", False)),
        call_beta_access=bool(user_row.get("call_beta_access", False)),
        memory_profile=user_row.get("memory_profile"),
        message_handle=message_handle,
    )


async def load_user_context(
    phone_number: str,
    name: str | None = None,
    channel: str = "cli",
    message_handle: str | None = None,
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
    return await _context_from_row(user_row, channel, message_handle=message_handle)


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
    the prompt alone, this also catches it structurally, in two distinct
    shapes (feature/agentic-upgrade generalized this from the original,
    narrower version -- see messa/reliability.py's own module docstring):

      1. Dropped delegation: the WHOLE turn made zero tool calls and the
         final text matches `_STALL_PATTERN` (the exact acknowledgment
         phrasing the prompt asks Messa to use right before a delegation)
         -- a strong signal a call was promised and dropped.
      2. Unverified completion claim: the final text reads like "I've sent
         it"/"all set"/"booked it" (reliability.unverified_claim_reason)
         but either no tool call happened this turn, or the most recent
         tool result reads like a failure -- a strong signal Messa is
         about to tell the user something succeeded when it didn't.

    Either case replays the same messages plus one nudge and retries
    exactly once (`_allow_retry=False` on the recursive call prevents a
    loop -- the user's own "empty-promise LOOPS" framing is specifically
    about never letting this recur/compound, so this is a single bounded
    correction, not a retry-until-it-looks-right cycle). The original
    (broken) reply may have already reached the user via `on_ai_message`
    before this check runs, which is fine for case 1 (it was a true
    statement of intent, just unfinished) and still the least-bad option
    for case 2 (the false claim may already be visible, but a follow-up
    that actually completes the action -- or honestly reports the failure
    -- is far better than leaving it uncorrected for the rest of the
    conversation)."""
    final_messages = list(messages)
    any_tool_call = False
    last_text = ""
    last_tool_content: object = None
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
                if getattr(m, "type", None) == "tool":
                    last_tool_content = content
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
                    # Only stream to user if this is a final answer (no tool calls pending)
                    # OR if delegating to a long-running subagent like deepsearch where an upfront notice is expected.
                    # Intermediate thoughts before calling a tool are kept silent on SMS.
                    if on_ai_message and (not tool_calls or delegating_to == "deepsearch"):
                        await on_ai_message(text, delegating_to)
                final_messages.append(m)

    nudge_text = None
    if _allow_retry and not any_tool_call and last_text and _STALL_PATTERN.search(last_text):
        console.system(
            "Messa: this reply looked like a dropped delegation (acknowledgment with no "
            "tool call all turn) -- retrying once with a nudge."
        )
        nudge_text = (
            "(auto-nudge, not from the user: your previous reply didn't include the "
            "tool call it described. If you intended to delegate to a subagent just "
            "now, call it in this response. If you'd already fully answered the "
            "request, ignore this.)"
        )
    elif _allow_retry and last_text:
        claim_reason = reliability.unverified_claim_reason(last_text, any_tool_call, last_tool_content)
        if claim_reason:
            console.system(f"Messa: {claim_reason} -- retrying once with a nudge.")
            nudge_text = (
                "(auto-check, not from the user: your previous reply claims something is "
                "done/sent/scheduled, but the tool trace doesn't back that up this turn -- "
                "either no tool call actually did it, or the most recent one failed. Check "
                "the real result: if it actually failed, tell the user plainly what went "
                "wrong instead of claiming success; if it should still happen, actually call "
                "the tool now.)"
            )

    if nudge_text:
        return await run_turn(
            agent, final_messages + [HumanMessage(content=nudge_text)],
            on_ai_message=on_ai_message, _allow_retry=False,
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
    local_part = fresh.get("messa_email_local_part")
    if not local_part and name:
        local_part = await db.get_or_create_messa_email_local_part(user.user_id, name)
    messa_email = f"{local_part}@{config.TEXTMESSA_EMAIL_DOMAIN}" if local_part else user.messa_email
    if messa_email:
        reveal_parts.append(
            f"Here's my own email: {messa_email} -- it's mine to manage, so feel "
            "free to hand it out anywhere (signups, forms, whatever) and I'll take care "
            "of what lands there, looping you in before anything that needs your OK. I "
            "can manage your personal inbox too, just say the word."
        )
    if user.live_view_share_url:
        reveal_parts.append(f"Check that inbox, or watch me work, anytime here: {user.live_view_share_url}")
    if reveal_parts:
        messages.append("\n".join(reveal_parts))

    # New, deterministic third reveal message (migrations/032_app_connect_
    # queue.sql): asks once what apps the user uses day to day, so
    # tools/integration_tools.py's queue_app_connections has something to
    # act on if they answer with a list. Code-authored (not left to the
    # model to remember to ask) for the exact same reason the email/
    # live-view reveal above is: this whole function already only ever
    # fires once per user (gated by onboarding_step above), so
    # apps_onboarding_asked is a belt-and-suspenders idempotency guard, not
    # the thing actually preventing repeats -- but checking it here means
    # this can never double-ask even if that guarantee is ever loosened
    # later, and it's what agents/registry.py's profile-enrichment prompt
    # could check too, if a future change wants the model aware of it.
    if not fresh.get("apps_onboarding_asked", False):
        messages.append(
            "One more thing -- what apps do you use day to day that I could help with? "
            "Think Gmail, Slack, Google Calendar, Notion, Todoist, that kind of thing -- "
            "just name whichever ones you actually use and I'll get them connected for "
            "you, one at a time."
        )
        await db.mark_apps_onboarding_asked(user.user_id)

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


async def run_message(
    user: config.UserContext,
    agent,
    text: str,
    send=None,
    log_texts: list[str] | None = None,
    on_turn_complete=None,
) -> str:
    """One full, stateless turn for `user`: loads recent DB history for
    context, runs it, persists both sides, returns the final reply text.

    on_turn_complete: optional async callable(user, final_messages) -- see
    asset_consolidation.consolidate_after_turn, the one real caller today
    (server.py's _run_turn passes it in). Fired as its own background task
    (via this module's own _spawn_background) the moment this turn's
    messages are known, WITHOUT being awaited -- so it adds ZERO latency
    to this function's own return, and a slow or failing consolidation
    pass can never delay or break the reply the user is actually waiting
    on. Exceptions inside it are the callable's own responsibility to
    swallow (consolidate_after_turn does); nothing here catches them.

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

    The model is never asked to write the live-view link itself (see
    agents/registry.py): an earlier version had the system prompt tell
    Messa to write the exact URL into her own acknowledgment sentence,
    which mostly worked but failed exactly the way free-text LLM output
    eventually always does -- a real run trailed off mid-sentence
    ("checking a") right as the model fired its tool call, before ever
    reaching the link. stream_mode "updates" only ever hands back complete
    messages, never partial tokens, so that wasn't a streaming/truncation
    bug in this code -- it was genuinely the full, final `content` the
    model chose to generate for that turn.

    The link is NOT attached here in _on_ai_message either, though -- an
    earlier version of this fix did exactly that (appended deterministically
    to the pre-delegation acknowledgment, the same place the "this can take
    a few minutes" notice below still lives), which fixed the reliability
    problem above but created a timing one: the ack (and its link) goes out
    the instant the model DECIDES to delegate, which is well before a real
    Browserbase session exists -- deepsearch's own planning LLM calls,
    session creation, and the CDP connect all still have to happen after
    that text is already sent. Real runs showed the link arriving a minute
    or two before there was anything to actually watch. It's now sent
    separately, straight from tools/deepsearch_tools.py's
    BrowserToolProvider._ensure_live_session, the moment the browser
    session is genuinely live (right after db.set_live_browser_active
    confirms it) -- guaranteed correct and complete regardless of what the
    model's own text looks like, same as before, just arriving when it's
    actually useful instead of arriving first.

    Two more deterministic, code-authored additions live in this same
    function, same "don't trust free-text model output for something that
    must always be exactly right" reasoning as the link above: the short
    "this can take a few minutes, I'll text you when it's done" (plus, on a
    user's first-ever deepsearch, a one-time "I can do more than just
    browse" tip) appended in _on_ai_message, and the onboarding-complete
    reveal (Messa's own email + live-view link, sent once right after the
    user answers the last onboarding question) via
    _onboarding_complete_messages above. Both are kept short and fixed on
    purpose -- an explicit ask was to keep outbound texts concise, and
    fixed strings are the only way to guarantee that stays true forever
    rather than drifting longer over time the way freely-generated model
    text tends to.

    log_texts: optional list of raw inbound texts to log INDIVIDUALLY,
    instead of logging `text` as a single row -- for server.py's double-
    text batching (see plans/glowing-forging-pumpkin.md's Feature B1),
    where several raw messages arriving in quick succession get combined
    into one `text` string (with bracketed framing) for the model to read,
    but the message_history transcript should still show exactly what the
    user actually sent, as separate rows, not the combined/annotated
    version. When given, each entry is logged via db.append_message in
    order, those rows are excluded from the get_recent_messages read-back
    (so the combined `text` appended below isn't duplicated against its
    own already-logged raw pieces), and the read limit is raised by the
    batch size so the merge doesn't push real prior context out of the
    window. With log_texts=None (every caller today), this function's
    logging/history-building behavior is byte-for-byte identical to
    before this parameter existed."""
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
    # Logged INDIVIDUALLY when log_texts is given (double-text batching --
    # see this function's own docstring above), one row per raw text the
    # user actually sent; otherwise (every caller today) `text` alone is
    # logged as a single row, exactly as before this parameter existed.
    logged_ids: set[int] = set()
    if log_texts:
        for raw_text in log_texts:
            logged_ids.add(await db.append_message(user.user_id, "user", raw_text, channel=user.channel))
    else:
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

    # Load conversational context by turns rather than raw fragmented bubbles,
    # preventing multi-part SMS replies from evicting context within 1-2 turns.
    recent_turns = await db.get_recent_turns(user.user_id, limit_turns=15)
    recent_turns = [t for t in recent_turns if not any(mid in logged_ids for mid in t.get("ids", []))]
    history = [{"role": t["role"], "content": t["content"]} for t in recent_turns]

    # Front-Door Intent Router triage
    intent_decision = await triage_incoming_message(user, text)
    if intent_decision.intent in (IntentType.ACTIVE_TASK_INPUT, IntentType.STATUS_QUERY):
        history.append({"role": "system", "content": f"[Intent Triage]: {intent_decision.context_hint}"})

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
        if not msg_text:
            return  # nothing to say -- e.g. a silent delegation

        msg_text = await _apply_reply_guardrail(msg_text)

        # A message still long after the guardrail's own compression attempt
        # goes out as a few separate texts instead of one long bubble (see
        # _split_into_texts' own docstring) -- the common, short-reply case
        # returns a single-item list here and behaves EXACTLY as before this
        # existed: one usage unit consumed, one DB row, one send() call.
        for chunk in _split_into_texts(msg_text):
            if not user.is_admin:
                # The pre-agent gate in run_message only catches a user who was
                # ALREADY over their limit before this turn started -- a turn
                # that sends several messages in a row (an ack plus a
                # delegation summary, say -- or now, several split pieces of
                # the same reply) can still cross the daily cap partway
                # through. Same rule applies here: consume one unit per real
                # outgoing text, and if that pushes the user over, either
                # send today's one notice (if nobody's claimed it yet) or
                # drop the rest silently -- a partially-delivered split reply
                # (the first chunk or two, then a stop) is an acceptable
                # trade-off for the same reason dropping a single message
                # over-limit already was: the alternative is silently
                # billing/serving past the user's own plan limit.
                text_result = await usage.check_and_consume(user, usage.FEATURE_NUMBER_OF_TEXTS)
                if not text_result.allowed:
                    notice = await _send_limit_notice_once(
                        user, usage.FEATURE_NUMBER_OF_TEXTS, text_result, send,
                        marker_context="mid-conversation",
                    )
                    if notice:
                        sent_texts.append(notice)
                    return  # this message (remaining chunks included) is dropped either way

            sent_texts.append(chunk)
            await db.append_message(user.user_id, "assistant", chunk, channel=user.channel)
            if send:
                await send(chunk)

    final = await run_turn(agent, history, on_ai_message=_on_ai_message)


    # "Sleep & dream" post-turn consolidation (docs/executive_agent_
    # architecture_proposal.md 3.C) -- fired here, the earliest point
    # `final` (this turn's full message list, tool calls included) is
    # known, and spawned rather than awaited so it truly adds zero latency
    # to everything below (the onboarding reveal, sent_texts, this
    # function's own return) that the user is actually waiting on.
    if on_turn_complete is not None:
        _spawn_background(on_turn_complete(user, final))

    # Deterministic, one-time onboarding-complete reveal -- see
    # _onboarding_complete_messages' own docstring for why this isn't left
    # to the model. Sent (and persisted) as its own follow-up, after
    # whatever the model itself said this turn (typically a short
    # acknowledgment of the just-answered email question).
    reveal_messages = await _onboarding_complete_messages(user)
    for i, onboarding_msg in enumerate(reveal_messages):
        sent_texts.append(onboarding_msg)
        await db.append_message(user.user_id, "assistant", onboarding_msg, channel=user.channel)
        # The completion moment gets Apple's 'confetti' effect on a real
        # iMessage thread (same user.message_handle gate react_to_message
        # already uses -- SMS/RCS has no Apple effects support) -- fired
        # deterministically here, on the FIRST reveal message, rather than
        # left to the model, same "don't trust free-text output for
        # something that must always happen exactly right" reasoning as the
        # rest of this reveal (see _onboarding_complete_messages' own
        # docstring). Sent directly via sendblue.send_message rather than
        # the generic `send` callback since `send_style` is Sendblue-
        # specific and this reveal also fires over the personal-email/CLI
        # paths, which have no such concept.
        if i == 0 and user.message_handle:
            try:
                await sendblue.send_message(user.phone_number, onboarding_msg, send_style="confetti")
                continue
            except SendblueError as e:
                console.system(f"[onboarding confetti] send failed, falling back to plain send: {e}")
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
