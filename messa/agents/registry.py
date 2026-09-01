"""Assembles Messa (the orchestrator) and her six subagents.

Uses deepagents' native `subagents=` support on `create_deep_agent`, so
delegation, the `task` tool, and per-subagent tool/prompt isolation are all
provided by the framework rather than hand-rolled. Three subagents are
plain declarative `SubAgent` dicts: name + description (what makes Messa
pick it) + system_prompt + its own tool list -- personal_inbox_agent,
document_agent, routines_agent. The other three are `CompiledSubAgent`s,
each for its own reason: deepsearch (the browsing/research subagent,
formerly called "browser_agent") needs to open/close its own Playwright
process per delegation and persists/resumes progress across runs that hit
their step limit (see tools/deepsearch_tools.py); executive_assistant and
email_agent both need their OWN small step budget, independent of whatever
recursion_limit happens to be ambient on the call -- a plain SubAgent dict
has no field for that, it just inherits the ambient one (see
tools/executive_tools.py's/tools/email_tools.py's build_*_subagent
docstrings).

Email routing: there are two separate subagents for two separate inboxes
(personal_inbox_agent -- the user's own address on Messa's own domain --
and email_agent -- their connected Gmail), and which one handles a GENERIC
"send/check my email" request is a per-user preference
(users.default_email_provider, migrations/015_default_email_provider.sql)
that Messa's own system prompt reads and routes on -- see
`_build_system_prompt`'s "Email routing" paragraph below, and
`set_default_email_provider` above, the only thing that ever changes it
(always at the user's explicit request; connecting Gmail does not).

Confirming or rejecting a *proposed* action (see db.py's pending_actions
gate) is deliberately kept OFF every subagent and given to Messa directly --
subagents can suggest a mutation, only the orchestrator holding the actual
conversation with the user can commit it.

Latency: `create_deep_agent`'s default harness gives every agent a virtual
filesystem (ls/read_file/write_file/edit_file/delete/glob/grep), a shell
`execute` tool, and an auto-added "general-purpose" subagent -- none of
which this project uses. Every one of those is extra tool-schema tokens on
every single model call, which adds up across 6 agents. `register_harness_profile`
below strips them once, globally, for every agent built on our model.
"""
from __future__ import annotations

from typing import Any

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
from langchain_core.tools import BaseTool, tool

from .. import config, db, timeutil
from ..approval import ApprovalGate, CLIApprovalGate
from ..channels import sendblue
from ..channels.sendblue import SendblueError
from ..tools.deepsearch_tools import build_deepsearch_subagent
from ..tools.common import trace_all
from ..tools.document_tools import DOCUMENT_SYSTEM_PROMPT, build_document_tools
from ..tools.email_tools import build_email_subagent
from ..tools.executive_tools import _format_contact_line, build_executive_subagent
from ..tools.personal_inbox_tools import build_personal_inbox_system_prompt, build_personal_inbox_tools
from ..tools.routines_tools import ROUTINES_SYSTEM_PROMPT, build_routines_tools
from ..tools.web_search_tools import build_web_search_tools

ORCHESTRATOR_LABEL = "messa"

# Registered once at import time. All our agents use the same ChatOpenAI
# instance pointed at OpenRouter, which deepagents' model-introspection
# resolves to provider "openai" regardless of the actual model string (see
# deepagents._models.get_model_provider) -- so a provider-wide registration
# here covers Messa and every subagent built via create_deep_agent.
register_harness_profile(
    "openai",
    HarnessProfile(
        excluded_tools=frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)


def build_orchestrator_tools(user: config.UserContext) -> list[BaseTool]:
    """Messa's own direct tools: approvals, lightweight project tracking, onboarding."""
    uid = user.user_id

    @tool
    async def confirm_pending_action(pending_action_id: int) -> str:
        """Apply a previously-proposed action now that the user has confirmed it."""
        result = await db.confirm_pending_action(uid, pending_action_id)
        if not result.get("ok"):
            return f"Could not confirm #{pending_action_id}: {result.get('error')}"
        return f"Confirmed and applied action #{pending_action_id} ({result['action_type']})."

    @tool
    async def reject_pending_action(pending_action_id: int) -> str:
        """Discard a previously-proposed action the user declined."""
        result = await db.reject_pending_action(uid, pending_action_id)
        if not result.get("ok"):
            return f"Could not reject #{pending_action_id}: {result.get('error')}"
        return f"Rejected action #{pending_action_id}. Nothing was changed."

    @tool
    async def list_pending_actions() -> str:
        """List actions awaiting the user's confirmation."""
        rows = await db.list_pending_actions(uid)
        if not rows:
            return "Nothing pending confirmation."
        return "\n".join(f"#{r['id']} {r['action_type']}: {r['payload']}" for r in rows)

    @tool
    async def track_project(title: str) -> str:
        """Get-or-create a lightweight project thread for a multi-step user request,
        so related tasks/messages can be grouped together. Safe to call even if the
        projects table/migration hasn't been applied -- it will just no-op."""
        row = await db.get_or_create_project(uid, title)
        if row is None:
            return "Project tracking isn't enabled yet (run migrations/002_projects_and_channel.sql)."
        return f"Tracking project '{row['title']}' (#{row['id']})."

    @tool
    async def list_active_projects() -> str:
        """List the user's active tracked projects."""
        rows = await db.list_projects(uid)
        if not rows:
            return "No active projects."
        return "\n".join(f"#{r['id']} {r['title']}" for r in rows)

    @tool
    async def save_profile_info(field: str, value: str) -> str:
        """Save one onboarding field the user just told you: field is 'name', 'city', or
        'email' -- asked in that order. For the optional email step, pass value='skip' if
        the user doesn't want to share one (they'll just use your own Messa address for
        anything that needs one, e.g. signups). Also use this any time the user corrects
        their location later (not just during onboarding) -- it re-resolves their
        timezone from whatever they give you.

        Connecting Gmail is separate and NOT part of onboarding -- if the user brings it
        up (now or any time later), delegate to email_agent with request_email_connection
        directly; don't call this tool for that."""
        row = await db.save_profile_field(uid, field, value)
        msg = f"Saved {field}. Onboarding is now at: {row['onboarding_step']}."
        if field == "city" and value and value.strip().lower() != "skip":
            if row.get("timezone_confirmed"):
                msg += f" Resolved timezone: {row['timezone']}."
            else:
                msg += (
                    f" Could NOT confidently determine a single timezone from '{value}' alone "
                    "(it matches places in more than one timezone, or couldn't be resolved at "
                    "all) -- ask the user for their zip code, or their city AND state/country, "
                    "so scheduling, reminders, and anything else time-sensitive land at the "
                    "correct local time. Do not schedule anything time-sensitive off a guess."
                )
        return msg

    @tool
    async def set_default_email_provider(provider: str) -> str:
        """Set which inbox you use by default for a GENERIC 'send/check my email'
        request that doesn't name one -- 'messa' (the user's own address on your
        domain, personal_inbox_agent) or 'gmail' (their connected Gmail, email_agent).
        Call this only when the user explicitly asks to change it (e.g. 'use my gmail
        as default from now on', 'switch back to my messa email', 'make gmail my main
        email') -- never on your own judgment, and connecting Gmail does NOT change
        this by itself. Switching to 'gmail' requires it to already be connected: if
        it isn't, this tells you so instead of changing anything -- offer to connect
        it first (delegate to email_agent with request_email_connection), then call
        this again once they confirm it's active."""
        normalized = (provider or "").strip().lower()
        if normalized not in ("messa", "gmail"):
            return "provider must be exactly 'messa' or 'gmail'."
        if normalized == "gmail" and not user.email_connected:
            return (
                "Can't switch the default to Gmail yet -- it isn't connected. Offer to connect "
                "it (delegate to email_agent with request_email_connection), then call this "
                "again once the user confirms it's active."
            )
        row = await db.set_default_email_provider(uid, normalized)
        if row is None:
            return (
                "Default-email preference isn't set up on this deployment yet (run "
                "migrations/015_default_email_provider.sql) -- for now, the user's own Messa "
                "address is used for any unnamed 'send/check my email' request."
            )
        label = "their own Messa address" if normalized == "messa" else "their connected Gmail"
        return f"Default email is now {label}. Use it for any generic send/check-email request that doesn't name an inbox."

    @tool
    async def list_deepsearch_sessions(status: str | None = None) -> str:
        """List the user's deepsearch (browsing/research) sessions, most recent first.
        status: 'active' (unfinished/resumable), 'completed', or omit for all. Use this
        to check whether an earlier research task is still in progress before starting a
        new one, or when the user asks what you were looking into."""
        rows = await db.list_deepsearch_sessions(uid, status)
        if not rows:
            return "No deepsearch sessions found."
        return "\n".join(
            f"#{r['id']} [{r['status']}] {r['title']} (updated {r['updated_at']}): "
            f"{(r['summary'] or '')[:140]}"
            for r in rows
        )

    @tool
    async def find_contact(name: str) -> str:
        """Look up a saved contact by name (partial/case-insensitive match,
        e.g. "sam" matches a saved "Samantha Lee") to get their phone
        number/email -- call this FIRST whenever the user names someone by
        name for an email or text ("email Sam about the invoice", "text
        John I'm running late") WITHOUT also giving you their actual
        address/number, so you can resolve it yourself instead of asking
        the user to repeat something they already told Messa once. A
        direct tool (not a delegation) since this is a cheap lookup, not a
        multi-step task. If it returns more than one match, ask the user
        which one they meant before sending anything. If no saved contact
        has the info you need (or none exists at all), ask the user for it
        directly -- don't guess an address/number."""
        rows = await db.find_person_by_name(uid, name)
        if not rows:
            return f"No saved contact matching '{name}'."
        return "\n".join(_format_contact_line(r) for r in rows)

    @tool
    async def send_pdf_over_text(file_path: str, caption: str | None = None) -> str:
        """Text a PDF to the user as an MMS/iMessage attachment on their
        own thread with you -- the SMS/iMessage counterpart to
        personal_inbox_agent's attachment_path (that one emails a PDF,
        this one texts it). Pass EXACTLY the file path document_agent's
        generate_pdf returned, never a path you invented yourself. This is
        a direct tool (not a delegation) and goes straight to the user's
        own number -- it does NOT need confirmation, same as any other
        message you send them directly.

        caption: optional short message to send alongside the attachment
        (defaults to a plain line saying here's the PDF). file_path must
        be under the outbound-text size limit -- if it's too large, the
        error tells you so; offer to email it instead (personal_inbox_agent
        has a much higher attachment limit)."""
        if not (config.SENDBLUE_API_KEY and config.SENDBLUE_API_SECRET and config.SENDBLUE_NUMBER):
            return "Texting isn't configured on this deployment yet."
        try:
            resolved = config.resolve_output_file(file_path, max_bytes=config.MAX_SMS_ATTACHMENT_BYTES)
        except ValueError as e:
            return f"Couldn't send that file over text: {e}"
        token = await db.create_document_share(uid, str(resolved), resolved.name)
        if not token:
            return (
                "Texting a file isn't set up on this deployment yet "
                "(run migrations/018_generated_document_shares.sql)."
            )
        media_url = f"{config.LIVE_VIEW_BASE_URL}/files/{token}"
        try:
            await sendblue.send_message(
                user.phone_number, caption or "Here's the PDF you asked for.", media_url=media_url,
            )
        except SendblueError as e:
            return f"Couldn't send that over text: {e}"
        return f"Sent {resolved.name} to {user.phone_number} as a text attachment."

    raw_tools: list[BaseTool] = [
        confirm_pending_action, reject_pending_action, list_pending_actions,
        track_project, list_active_projects, save_profile_info, set_default_email_provider,
        list_deepsearch_sessions, find_contact, send_pdf_over_text,
        *build_web_search_tools(),
    ]
    return trace_all(raw_tools, ORCHESTRATOR_LABEL)


_ONBOARDING_PROMPTS = {
    "awaiting_name": (
        "This is a brand-new user and you don't know their name yet. Before diving into "
        "their first request (or right after helping with it if they jumped straight to a "
        "task), casually ask what you should call them, then call save_profile_info('name', ...)."
    ),
    "awaiting_location": (
        "You know the user's name but not their location. Ask for their city AND "
        "state/country, or a zip code if they're in the US, not just a bare city name: a "
        "name alone can be genuinely ambiguous (there's a Jacksonville in Florida, one in "
        "North Carolina, one in Illinois -- all different timezones), and this is what "
        "makes scheduling, reminders, and morning/evening briefings land at the correct "
        "local time. Then call save_profile_info('city', ...) with whatever they give "
        "you; if the tool result says the timezone couldn't be confidently resolved, ask "
        "a follow-up for a zip code or a more specific city+state before treating it as "
        "settled."
    ),
    "awaiting_email": (
        "Last onboarding question: ask if there's a particular email they'd want on file "
        "for creating accounts on their behalf -- make clear it's optional, and that if "
        "they skip it you'll just use your own Messa address for that instead. Then call "
        "save_profile_info('email', ...) with what they give you, or "
        "save_profile_info('email', 'skip') if they'd rather not share one. Keep your own "
        "reply here short either way -- a quick acknowledgment is enough, you don't need "
        "to introduce yourself or your own email address here. (That introduction is "
        "handled automatically right after this: the moment onboarding is complete, the "
        "system sends its own follow-up message telling them what you can actually do "
        "and what your own email address is -- don't try to write that yourself or "
        "duplicate it, it's guaranteed to go out on its own.)"
    ),
}


def _build_system_prompt(user: config.UserContext) -> str:
    known = []
    if user.name:
        known.append(f"name: {user.name}")
    if user.email:
        known.append(f"email: {user.email}")
    if user.city:
        known.append(f"city: {user.city}")
    known.append(
        "Gmail is connected (can read/send email via email_agent)"
        if user.email_connected
        else "Gmail is NOT connected yet (email_agent can send them a connect link on request)"
    )
    if user.messa_email:
        known.append(f"their own Messa email address is {user.messa_email}")
    if user.default_email_provider == "gmail" and not user.email_connected:
        known.append(
            "default email is set to Gmail, but Gmail isn't connected right now -- treat "
            "personal_inbox_agent as the effective default for any unnamed email request "
            "until Gmail is reconnected, and mention that mismatch if it's relevant"
        )
    elif user.default_email_provider == "gmail":
        known.append("default email for an unnamed 'send/check my email' request: their connected Gmail (email_agent)")
    else:
        known.append("default email for an unnamed 'send/check my email' request: their own Messa address (personal_inbox_agent)")
    known_str = ("Known about this user so far -- " + ", ".join(known) + ".\n\n") if known else ""

    # Neither Messa's nor any subagent's system prompt used to state the
    # actual current date/time or the user's timezone at all -- meaning the
    # model had zero deterministic anchor for "what time is it right now"
    # when interpreting anything relative ("tomorrow", "in an hour"). This
    # is that anchor. See timeutil.current_context_str's docstring for why
    # it also calls out an unconfirmed default timezone explicitly.
    time_str = timeutil.current_context_str(user.timezone, user.timezone_confirmed) + "\n\n"

    onboarding_str = ""
    if not user.onboarding_complete:
        instruction = _ONBOARDING_PROMPTS.get(user.onboarding_step)
        if instruction:
            onboarding_str = instruction + "\n\n"

    channel_str = ""
    if user.channel != "cli":
        channel_str = (
            f"You're replying over {user.channel} (a real text conversation, not a chat "
            "UI). Write plain text only -- no markdown (no **bold**, no bullet/numbered "
            "lists, no headers, no code fences). Use line breaks and casual punctuation "
            "the way a person texting would. Keep it as short as the answer allows.\n\n"
        )

    live_view_str = ""
    if user.live_view_share_url:
        live_view_str = (
            " Specifically for deepsearch: your acknowledgment before delegating to it can "
            "just be a short, natural line about what you're checking -- don't try to write "
            "out a URL yourself. The system automatically appends the user's live-view link "
            "to that same message right before it's sent, every single time, so you never "
            "need to type, remember, or repeat the link -- just say what you're about to do."
        )

    return (
        "You are Messa, a personal assistant reachable by text, email, and (soon) WhatsApp. "
        "You talk to the user directly and delegate specialized work to subagents via the "
        "task tool: deepsearch (web browsing/research), executive_assistant (tasks, "
        "reminders, notes, contacts, calendar), email_agent (the user's own Gmail), "
        "personal_inbox_agent (the user's own Messa-owned email address -- a different "
        "inbox from their Gmail), document_agent (generates PDFs), routines_agent "
        "(recurring automations).\n\n"
        "Email routing -- there are two separate inboxes, and you decide which one handles "
        "each request: personal_inbox_agent (their own address on your domain) and email_agent "
        "(their connected Gmail, once set up). For a GENERIC request that doesn't name an inbox "
        "('send an email to X', 'check my email', 'any new messages?'), delegate to whichever "
        "one is their current default (see 'Known about this user' above) -- don't ask which "
        "inbox every time, that default exists so you don't have to. If they explicitly name "
        "one ('my gmail', 'my real/personal email' -> email_agent; 'my messa email', 'the "
        "address you gave me', 'the textmessa one' -> personal_inbox_agent), use that one "
        "regardless of the default. If they ask to change the default ('use my gmail from now "
        "on', 'switch back to messa email'), call set_default_email_provider -- it'll tell you "
        "if Gmail needs to be connected first, in which case offer to connect it (email_agent's "
        "request_email_connection) before trying again. Connecting Gmail does NOT change the "
        "default on its own -- only this explicit request does.\n\n"
        "Documents, email, and text: when the user wants something generated AND sent/attached "
        "(\"make me a PDF of this and email it to me\", \"...and text it to me\"), delegate to "
        "document_agent first, get the exact file path back from its reply, then either "
        "delegate to personal_inbox_agent with that same path relayed verbatim in the "
        "description (e.g. \"attach /outputs/quote.pdf and send it to jane@example.com\") so "
        "it can pass it as attachment_path -- or, if they want it TEXTED instead of emailed, "
        "call your own send_pdf_over_text(file_path) directly with that exact path (no "
        "delegation needed, it goes straight to their own number) -- never invent or paraphrase "
        "the path yourself either way. Email attachments only work for personal_inbox_agent "
        "(Messa's own email) -- email_agent (Gmail) doesn't support attachments in this build, "
        "so if the user's default/named inbox is Gmail and they want a document emailed with an "
        "attachment, tell them plainly that attachments only work from their Messa address "
        "right now (or offer to text it instead) rather than sending it without the file.\n\n"
        "Reading PDFs the user sends YOU: if the user texts you a PDF, or a PDF arrives "
        "attached to an email in your own inbox, its extracted text is already included right "
        f"in the message that told you about it (capped at {config.MAX_PDF_READ_PAGES} pages "
        "-- a note says so if it was cut off) -- you don't need to fetch or open anything "
        "yourself, just read and use it like any other text in that message. If it says the PDF "
        "couldn't be read (too large, corrupted, scanned/image-only, or password-protected), "
        "tell the user plainly rather than guessing at what it might have said.\n\n"
        "Contacts: when the user names someone by name for an email or text ('email Sam about "
        "the invoice', 'text John I'm running late') without also giving you their actual "
        "address/number, call find_contact(name) FIRST to resolve it before delegating -- don't "
        "ask the user to repeat something they may have already told you. If it finds more than "
        "one match, ask which one before sending anything; if it finds none (or finds the "
        "contact but not the info you actually need), ask the user directly rather than "
        "guessing or inventing an address/number.\n\n"
        "Speed matters: you also have web_search and fetch_page_text as your OWN direct tools "
        "(no delegation, no browser, answers in a second or two) -- use them for a plain "
        "factual lookup (a fact, news, a definition, 'who is X') instead of delegating to "
        "deepsearch, which opens a real browser session and is overhead when nothing needs to "
        "be clicked. Reserve deepsearch for anything that needs interacting with a page: a "
        "flow, a form, a login, a cart, or JS-rendered content a plain fetch won't show. If "
        "web_search/fetch_page_text come back empty or error out, delegate to deepsearch "
        "instead of giving up.\n\n"
        f"{known_str}"
        f"{time_str}"
        f"{onboarding_str}"
        f"{channel_str}"
        "Responsiveness: delegating to a subagent can take a little while. Before calling "
        "the task tool, send one short line acknowledging what you're about to do (e.g. "
        "\"Checking flights now...\") so the user isn't staring at silence -- don't just go "
        "straight to a silent tool call."
        f"{live_view_str}"
        "\n\n"
        "Critical: that acknowledgment and the task tool call must be in the SAME response "
        "-- there is no next turn where you get to actually make the call. If your reply "
        "says something like \"sending this to deepsearch\" or \"doing both now\" but "
        "doesn't also include the task tool call(s) right then, in that exact response, "
        "nothing happens: the conversation just ends there. Not delayed, not queued -- "
        "silently dropped, with no error shown to you or the user, and no later chance to "
        "catch up on it. If one message needs more than one subagent action (two different "
        "subagents, or the same one twice), include all of those task tool calls together "
        "in that single response rather than describing one now and coming back for the "
        "rest -- you only get one shot per response to actually act on what you just said "
        "you'd do.\n\n"
        "Live data means a fresh check every time: if the user asks again for anything that "
        "can change between messages (a price, availability, a live status, today's weather) "
        "-- even just \"try again\" or \"is it back yet\" -- delegate to deepsearch again. "
        "Never answer from an earlier turn for this kind of request, even if that attempt "
        "failed and you're confident it's still true: conditions on your end can change "
        "silently between messages, and 'it hasn't changed since I last checked' without "
        "actually checking is a worse failure than a slow reply.\n\n"
        "Deepsearch sessions: a deepsearch reply always starts with "
        "'[deepsearch session #<id> -- completed]' or '...-- not finished, hit its step "
        "limit'. Reference that same 'session #<id>' in your next delegation's description "
        "any time the new ask is a continuation of that same task -- not only when it hit "
        "its step limit, but just as much when the user wants to adjust, correct, or add to "
        "something deepsearch just finished (e.g. \"session #12: change the drink to a "
        "fountain Coke and add a bag of chips\" after it built a cart, or \"session #12: also "
        "check the return flights\" after it hit its limit). Referencing the session id "
        "replays everything deepsearch already did and found straight back into its context, "
        "so it can build on the actual cart/page/result it left off at instead of guessing "
        "from scratch or -- worse -- redoing the entire task over again. Only skip the "
        "session id when the new ask is genuinely a fresh, unrelated task. Use "
        "list_deepsearch_sessions if you need to check on or remind yourself of past "
        "research before starting something that might duplicate it.\n\n"
        "Confirmation flow: only SCHEDULING needs your confirmation -- executive_assistant's "
        "calendar tools and routines_agent's new-job tool only PROPOSE the change and return a "
        "pending id. Relay that proposal in plain language and get an explicit yes before "
        "calling confirm_pending_action; call reject_pending_action if they decline or details "
        "need to change -- never confirm without the user having just said yes to that "
        "specific thing. Everything else executive_assistant does (create/update/delete a task "
        "or reminder, save a note, add/remove a contact) happens immediately with no proposal "
        "step -- when it says one's done, it's done, just relay it.\n\n"
        "Use track_project when a request looks like it'll span multiple turns or tasks "
        "(e.g. planning a trip, redesigning something), so related work stays grouped.\n\n"
        "Be concise -- responses may be read as a text message. Don't restate a subagent's "
        "full output verbatim; summarize what matters to the user.\n"
    )


async def build_orchestrator(
    user: config.UserContext,
    approval_gate: ApprovalGate | None = None,
    model: Any = None,
    subagent_model: Any = None,
) -> Any:
    """`model` is Messa's own orchestrator model (config.ORCHESTRATOR_MODEL_NAME
    by default); `subagent_model` is what every subagent below uses instead
    (config.SUBAGENT_MODEL_NAME by default) -- see config.py's comment for why
    these are deliberately two different models now rather than one shared
    instance. Both params exist mainly so tests can inject fakes for either
    or both independently; real callers (cli.py, server.py) just omit them
    and get the configured defaults."""
    approval_gate = approval_gate or CLIApprovalGate()
    # effective_context_tokens: see config.py's big comment above
    # SUBAGENT_EFFECTIVE_CONTEXT_TOKENS/ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS
    # -- without this, every agent's auto-compaction silently falls back to
    # deepagents' generic 170k-token trigger, since our OpenRouter model
    # strings don't match anything in LangChain's model-profile lookup.
    model = model or config.build_model(
        config.ORCHESTRATOR_MODEL_NAME,
        effective_context_tokens=config.ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS,
    )
    subagent_model = subagent_model or config.build_model(
        config.SUBAGENT_MODEL_NAME,
        effective_context_tokens=config.SUBAGENT_EFFECTIVE_CONTEXT_TOKENS,
    )

    subagents = [
        build_deepsearch_subagent(user, subagent_model, approval_gate),
        build_executive_subagent(user, subagent_model),
        build_email_subagent(user, subagent_model, approval_gate),
        {
            "name": "personal_inbox_agent",
            "description": (
                "Manages the user's own Messa email address (a real inbox on Messa's own "
                "domain, separate from their personal Gmail) -- what it is, sending new "
                "emails from it, and replying to anything that lands in it."
            ),
            "system_prompt": build_personal_inbox_system_prompt(user),
            "tools": build_personal_inbox_tools(user, approval_gate),
            "model": subagent_model,
        },
        {
            "name": "document_agent",
            "description": "Generates polished PDF documents from structured content on request.",
            "system_prompt": DOCUMENT_SYSTEM_PROMPT,
            "tools": build_document_tools(),
            "model": subagent_model,
        },
        {
            "name": "routines_agent",
            "description": (
                "Sets up, lists, pauses, resumes, and cancels recurring automations "
                "(cron-scheduled reminders/tasks). Use for anything recurring/scheduled."
            ),
            "system_prompt": ROUTINES_SYSTEM_PROMPT,
            "tools": build_routines_tools(user),
            "model": subagent_model,
        },
    ]

    agent = create_deep_agent(
        model=model,
        tools=build_orchestrator_tools(user),
        system_prompt=_build_system_prompt(user),
        subagents=subagents,
    )
    return agent
