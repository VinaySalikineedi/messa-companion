"""Assembles Messa (the orchestrator) and her five subagents.

Uses deepagents' native `subagents=` support on `create_deep_agent`, so
delegation, the `task` tool, and per-subagent tool/prompt isolation are all
provided by the framework rather than hand-rolled. Four subagents are plain
declarative `SubAgent` dicts: name + description (what makes Messa pick it)
+ system_prompt + its own tool list. deepsearch (the browsing/research
subagent, formerly called "browser_agent") is a `CompiledSubAgent` instead
-- see tools/deepsearch_tools.py for why (it needs to open/close its own
Playwright process per delegation, and persists/resumes progress across
runs that hit their step limit).

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
from ..tools.deepsearch_tools import build_deepsearch_subagent
from ..tools.common import trace_all
from ..tools.document_tools import DOCUMENT_SYSTEM_PROMPT, build_document_tools
from ..tools.email_tools import EMAIL_SYSTEM_PROMPT, build_email_tools
from ..tools.executive_tools import build_executive_subagent
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
        """Save one onboarding field the user just told you: field is 'name', 'email',
        or 'city'. For the optional email step, pass value='skip' if the user doesn't
        want to share one. This also advances onboarding to the next question. Also use
        this any time the user corrects their location later (not just during onboarding)
        -- it re-resolves their timezone from whatever they give you."""
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

    raw_tools: list[BaseTool] = [
        confirm_pending_action, reject_pending_action, list_pending_actions,
        track_project, list_active_projects, save_profile_info, list_deepsearch_sessions,
        *build_web_search_tools(),
    ]
    return trace_all(raw_tools, ORCHESTRATOR_LABEL)


_ONBOARDING_PROMPTS = {
    "awaiting_name": (
        "This is a brand-new user and you don't know their name yet. Before diving into "
        "their first request (or right after helping with it if they jumped straight to a "
        "task), casually ask what you should call them, then call save_profile_info('name', ...)."
    ),
    "awaiting_email": (
        "You know the user's name but not their email. Casually ask for it once (mention "
        "it's optional / skippable), then call save_profile_info('email', ...) with what "
        "they give you, or save_profile_info('email', 'skip') if they'd rather not share it."
    ),
    "awaiting_location": (
        "You still need the user's location so results like weather/local search -- and, "
        "critically, timezone-correct scheduling/reminders -- are accurate. Ask for their "
        "city AND state/country, or a zip code if they're in the US, not just a bare city "
        "name: a name alone can be genuinely ambiguous (there's a Jacksonville in Florida, "
        "one in North Carolina, one in Illinois -- all different timezones). Then call "
        "save_profile_info('city', ...) with whatever they give you; if the tool result "
        "says the timezone couldn't be confidently resolved, ask a follow-up for a zip "
        "code or a more specific city+state before treating it as settled."
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
        "reminders, notes, contacts, calendar), email_agent (the user's own inbox), "
        "document_agent (generates PDFs), routines_agent (recurring automations).\n\n"
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
        {
            "name": "email_agent",
            "description": (
                "Reads, searches, and sends the user's own email (Gmail/Outlook via "
                "Composio). Use for anything about the user's inbox."
            ),
            "system_prompt": EMAIL_SYSTEM_PROMPT,
            "tools": build_email_tools(user, approval_gate),
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
