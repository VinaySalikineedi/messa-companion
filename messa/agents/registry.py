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

Email/calendar/tasks routing -- the primary-app preference system: each of
these three categories has both a native Messa tool and one or more
connectable real apps that can conflict with it (personal_inbox_agent vs.
email_agent/integrations_agent for email; executive_assistant vs.
integrations_agent for calendar and for tasks). Which one handles a
GENERIC request that doesn't name an app is a per-user, per-category
preference (`user_app_preferences`, migrations/020_dynamic_integrations.sql
-- email's own dedicated `users.default_email_provider` column from the
earlier migrations/015_default_email_provider.sql still exists and is kept
in sync as a legacy mirror, see db.get_app_preference/set_app_preference's
own docstrings for exactly how) that Messa's own system prompt reads and
routes on -- see `_build_system_prompt`'s "Email routing"/"Calendar
routing"/"Tasks routing" paragraphs and its "Known about this user" block
below, and `set_app_preference` above, the only thing that ever changes it
at the user's own explicit request. Connecting an app does NOT change it
by itself UNLESS nothing was already set for that category, in which case
server.py's connection-confirmation flow auto-promotes it once (see
`_connection_confirmation_message`) -- never overriding an existing choice,
only ever filling an unset one.

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

from datetime import datetime, timezone
from typing import Any

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
from langchain_core.tools import BaseTool, tool

from .. import config, console, db, deepsearch_control, live_activity, memory, reliability, timeutil, turn_control

from ..approval import ApprovalGate, CLIApprovalGate
from ..channels import sendblue
from ..channels.sendblue import SendblueError
from ..tools.admin_tools import build_admin_subagent
from ..tools.deepsearch_tools import build_deepsearch_subagent, count_warm_sessions_for_user, purge_warm_sessions_for_user
from ..tools.common import trace_all
from ..tools.document_tools import build_document_subagent
from ..tools import call_tools
from ..tools.call_tools import build_call_subagent
from ..tools.email_tools import build_email_subagent
from ..tools.executive_tools import _format_contact_line, build_executive_subagent
from ..tools.integration_tools import app_category_for_toolkit, build_integration_subagent
from ..tools.personal_inbox_tools import build_personal_inbox_subagent
from ..tools.routines_tools import build_routines_subagent
from ..tools.scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block
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
        base = f"Confirmed and applied action #{pending_action_id} ({result['action_type']})."
        if result["action_type"] == "place_call":
            # The 'place_call' applier (db._insert_call_session_confirmed)
            # only ever staged a call_sessions row with status='confirmed'
            # -- the actual dial (a real, slow, failable HTTP call to
            # Vapi) happens HERE, as a separate step outside that
            # transaction, same reasoning broadcast_message's own fan-out
            # send being a separate background loop gives. See
            # call_tools.dial_confirmed_call's own docstring.
            outcome = await call_tools.dial_confirmed_call(result["result"])
            return f"{base} {outcome}"
        if result["action_type"] == "create_routine":
            # V3-autonomous.md Pillar 1: conflict auto-supersede. See
            # db._auto_supersede_conflicting_routine's own docstring --
            # this key is only ever present when it actually cancelled
            # something, never set to None otherwise.
            superseded = (result.get("result") or {}).get("_superseded_job")
            if superseded:
                base += (
                    f" (I also cancelled routine #{superseded['id']} "
                    f"('{superseded['prompt_or_task']}') since this one replaces it -- "
                    "same recipient.)"
                )
        return base

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
        if field == "name" and row.get("messa_email_local_part"):
            msg += f" Assigned Messa email: {row['messa_email_local_part']}@{config.TEXTMESSA_EMAIL_DOMAIN}."
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

    async def _is_app_connected(app: str) -> bool:
        """'messa' (native) is always considered connected -- it's the
        built-in option, never something the user connects. Gmail is
        checked via user.email_connected specifically, NOT
        get_active_connected_toolkits: Gmail's connection state lives in
        its own dedicated users.email_connected column/email_connection_requests
        table (email_tools.py's own OAuth flow, predates the generic
        Dynamic Integration Engine), not app_connection_requests -- the
        same distinction set_default_email_provider always had to make,
        just generalized here rather than dropped."""
        if app == "messa":
            return True
        if app == "gmail":
            return bool(user.email_connected)
        connected = await db.get_active_connected_toolkits(uid)
        return app in connected

    @tool
    async def set_app_preference(category: str, app: str) -> str:
        """Set which app is PRIMARY for a category, for any generic request
        that doesn't name one specifically. category: 'email', 'calendar',
        or 'tasks'. app: 'messa' (Messa's own native tool for that
        category -- executive_assistant for calendar/tasks,
        personal_inbox_agent for email) or a connected toolkit slug (e.g.
        'gmail', 'googlecalendar', 'todoist'). See 'Known about this user'
        above for what's currently primary in each category before calling
        this.

        Call this ONLY at the user's own explicit request ('use my gmail as
        my main email', 'make Google Calendar my primary calendar', 'switch
        tasks back to your own list', 'use Todoist by default') -- never on
        your own judgment, and connecting an app does NOT change this by
        itself (see server.py's connection-confirmation flow -- it only
        auto-promotes an app to primary when NOTHING was already set for
        that category; once something IS set, only this tool changes it).

        Switching to anything other than 'messa' requires that app to
        already be connected: if it isn't, this tells you so instead of
        changing anything -- offer to connect it first (delegate to
        integrations_agent, or email_agent's request_email_connection for
        Gmail specifically), then call this again once the user confirms
        it's active."""
        normalized_category = (category or "").strip().lower()
        if normalized_category not in ("email", "calendar", "tasks"):
            return "category must be exactly 'email', 'calendar', or 'tasks'."
        normalized_app = (app or "").strip().lower()
        if not normalized_app:
            return "app must be 'messa' or a connected toolkit slug (e.g. 'gmail', 'todoist')."
        if not await _is_app_connected(normalized_app):
            return (
                f"Can't switch {normalized_category}'s primary to {app!r} yet -- it isn't "
                "connected. Offer to connect it first (integrations_agent, or email_agent's "
                "request_email_connection for Gmail specifically), then call this again once "
                "the user confirms it's active."
            )
        row = await db.set_app_preference(uid, normalized_category, normalized_app)
        if row is None:
            return (
                "App-preference tracking isn't set up on this deployment yet (run "
                "migrations/020_dynamic_integrations.sql) -- for now, Messa's own native tool "
                f"is used for any unnamed {normalized_category} request."
            )
        label = "Messa's own native tool" if normalized_app == "messa" else normalized_app
        return (
            f"Primary {normalized_category} is now {label}. Use it for any generic "
            f"{normalized_category} request that doesn't name an app."
        )

    @tool
    async def list_my_connected_apps() -> str:
        """List every third-party app connected for this user, grouped by
        whether it competes with one of Messa's own native tools
        (email/calendar/tasks) or not, and which one is currently PRIMARY
        in each of those three categories. Call this when the user asks
        what's connected, or what their default/primary app is for
        something, instead of guessing or re-deriving it from scattered
        earlier context -- this is the authoritative, current answer."""
        connected = set(await db.get_active_connected_toolkits(uid))
        if user.email_connected:
            connected.add("gmail")
        lines = []
        for cat in ("email", "calendar", "tasks"):
            apps_in_cat = sorted(
                slug for slug in connected if app_category_for_toolkit(slug) == cat
            )
            primary = (await db.get_app_preference(uid, cat)) or "messa"
            if apps_in_cat:
                lines.append(f"{cat}: connected -- {', '.join(apps_in_cat)}. Primary: {primary}.")
            else:
                lines.append(f"{cat}: nothing connected yet. Primary: messa (Messa's own native tool).")
        other = sorted(slug for slug in connected if app_category_for_toolkit(slug) is None)
        if other:
            lines.append(
                "Also connected (no native Messa equivalent, so no primary concept applies): "
                + ", ".join(other) + "."
            )
        return "\n".join(lines)

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

    @tool
    async def recall_past_conversation(query: str) -> str:
        """Search what you and this user have talked about on OTHER days --
        NOT the current conversation (you already have that in context) and
        NOT the 'Known about this user' profile digest above (that's
        already given to you every turn, for free). Use this ONLY when the
        user references something from more than a few days back that you
        don't already have -- e.g. "what was that restaurant I mentioned
        last month", "did I ever tell you my sister's birthday". Don't call
        this reflexively on every message; it's a real lookup, not a cheap
        one, so use it like the other lookup tools (search_web): only when you
        actually need it.

        This is separate from the profile digest by design -- see README's
        "Memory" section: the digest is small, always-on, and free every
        turn; this is the larger day-by-day archive, searched only on
        demand so it never bloats your system prompt."""
        result = await memory.search_memory(user, query)
        if isinstance(result, dict) and result.get("error"):
            return f"Couldn't search memory: {result['error']}"
        results = (result or {}).get("results") or []
        if not results:
            return "Nothing found in past conversations matching that."
        lines = [r.get("memory", "") for r in results if r.get("memory")]
        return "From past conversations:\n" + "\n".join(f"- {line}" for line in lines)

    @tool
    async def cancel_active_search(reason: str | None = None) -> str:
        """Stop a deepsearch (browsing/research) task that's currently
        running in the background or warm for this user RIGHT NOW -- ends it
        immediately, closes and releases its browser session, and cancels
        any active browsing sessions in the database so it will not resume.

        Call this ONLY when the user's message is a clear, explicit signal
        to stop or abandon what's currently running -- either directly
        ("stop", "cancel that", "never mind", "forget it") or by clearly
        implying they want to do something else INSTEAD of waiting for it
        ("actually, let's look at hotels instead", "different idea -- check
        X for me"). Do NOT call this for an ambiguous message, a question
        about progress ("how's it going", "any update?" -- that's
        list_deepsearch_sessions instead), or an unrelated request that
        could just as easily run after this one finishes -- when in doubt,
        don't cancel; let it keep running and handle the new request
        separately.

        A no-op (not an error) if nothing is currently running -- safe to
        call even if you're not fully sure a search is still active."""
        tasks_cancelled = deepsearch_control.cancel(uid)
        warm_sessions_purged = await purge_warm_sessions_for_user(uid)
        db_cancelled_count = await db.cancel_all_active_deepsearch_sessions(uid)
        live_activity.clear(uid)

        if not tasks_cancelled and not warm_sessions_purged and db_cancelled_count == 0:
            return "Nothing was currently running or active to cancel."

        details = []
        if tasks_cancelled:
            details.append("stopped in-flight active background task")
        if warm_sessions_purged:
            details.append(f"closed {len(warm_sessions_purged)} warm browser session(s)")
        if db_cancelled_count:
            details.append(f"marked {db_cancelled_count} inactive historical session record(s) as closed in database")

        summary = "Stopped active task." if (tasks_cancelled or warm_sessions_purged) else "No tasks were running in memory."
        return f"{summary} ({', '.join(details)}). The browser is fully closed and no background actions are running."

    @tool
    async def get_system_status() -> str:
        """Get authoritative, real-time ground truth on the system state for this user:
        how many background tasks are running in memory, how many warm browser sessions
        exist, what the last browsing session was and when it ended, and any pending OTPs.
        CALL THIS FIRST whenever the user asks 'is something running', 'who is doing this',
        'why am I getting codes', or tells you to 'stop'.
        CRITICAL: If active background tasks is 0, NEVER claim, speculate, or apologize
        that a task is running or looping. State the ground truth clearly."""
        in_flight = deepsearch_control.active_count(uid)
        warm_count = count_warm_sessions_for_user(uid)
        last_session = await db.get_latest_deepsearch_session(uid)
        pending_otps = await db.list_active_otp_expectations(uid)

        lines = [
            f"Active background tasks in memory: {in_flight}",
            f"Warm browser sessions in memory: {warm_count}",
        ]
        if last_session:
            updated_str = str(last_session.get("updated_at") or "")[:19]
            lines.append(
                f"Most recent browsing session: #{last_session['id']} "
                f"[status: {last_session['status']}, steps: {last_session['steps_used']}, last updated: {updated_str} UTC]"
            )
            if last_session.get("summary"):
                lines.append(f"  Summary: {last_session['summary'][:160]}")
        else:
            lines.append("Most recent browsing session: None")

        if pending_otps:
            otp_descs = [f"#{o['id']} (filter: {o.get('sender_filter') or 'any'})" for o in pending_otps]
            lines.append(f"Pending verification code expectations: {', '.join(otp_descs)}")
        else:
            lines.append("Pending verification code expectations: None")

        state = "ACTIVE (tasks currently running)" if (in_flight or warm_count) else "IDLE (zero tasks running, browser closed)"
        lines.append(f"Authoritative State: {state}")
        return "\n".join(lines)

    @tool
    async def get_session_details(session_id: int) -> str:
        """Inspect the detailed history of a specific past browsing session by its ID #.
        Returns the session's title, final status, total steps used, creation and update
        timestamps, live view URL, and final outcome summary so you can explain what
        actually happened without guessing or hallucinating."""
        row = await db.get_deepsearch_session(uid, session_id)
        if not row:
            return f"No browsing session #{session_id} found for this user."
        created_str = str(row.get("created_at") or "")[:19]
        updated_str = str(row.get("updated_at") or "")[:19]
        return (
            f"Browsing Session #{row['id']} Details:\n"
            f"- Status: {row['status']}\n"
            f"- Title: {row['title']}\n"
            f"- Steps executed: {row['steps_used']}\n"
            f"- Created: {created_str} UTC\n"
            f"- Last Updated: {updated_str} UTC\n"
            f"- Live View: {row.get('live_view_url') or 'None'}\n"
            f"- Outcome Summary: {row.get('summary') or 'None'}"
        )

    @tool
    async def get_recent_message_activity() -> str:
        """Check when you (Messa) last sent an outgoing text to this user and what you said.
        Use this before sending autonomous or scheduled routine remarks, or secondary alerts,
        to ensure you are not spamming or repeating messages sent just moments ago."""
        msgs = await db.get_recent_assistant_messages(uid, limit=3)
        if not msgs:
            return "No recent outgoing assistant messages found."
        now = datetime.now(timezone.utc)
        lines = ["Recent outgoing messages to user:"]
        for i, m in enumerate(msgs):
            ts = m["timestamp"]
            diff_sec = int((now - ts).total_seconds()) if ts else 0
            time_desc = f"{diff_sec}s ago" if diff_sec < 120 else f"{diff_sec // 60}m ago"
            channel = m.get("channel") or "sms"
            preview = (m.get("content") or "").replace("\n", " ")[:100]
            lines.append(f"  {i+1}. [{time_desc} via {channel}]: \"{preview}...\"")
        last_diff = int((now - msgs[0]["timestamp"]).total_seconds()) if msgs[0]["timestamp"] else 999
        if last_diff < 120:
            lines.append(f"NOTICE: You sent a message {last_diff} seconds ago. If this current turn is a background check or non-urgent routine update, do NOT send another message unless the user explicitly asked for one.")
        return "\n".join(lines)

    @tool
    async def react_to_message(reaction: str) -> str:
        """Send an Apple iMessage tapback reaction to the user's latest incoming text.
        `reaction` can be: 'like' (thumbs up), 'love' (heart), 'dislike' (thumbs down),
        'laugh' (ha ha), 'emphasize' (!!), 'question' (?), or a single emoji like '👍' or '❤️'.
        Use this for quick, natural acknowledgments ('thumbs up') instead of sending a full text bubble."""
        if not user.message_handle:
            return "Tapback reactions are only supported on incoming iMessage threads, not SMS or CLI."
        try:
            await sendblue.send_reaction(user.phone_number, user.message_handle, reaction)
            return f"Reacted with '{reaction}' to user's message."
        except SendblueError as e:
            return f"Could not send reaction: {e}"

    @tool
    async def send_styled_message(content: str, effect: str) -> str:
        """Send a message to the user with an Apple iMessage visual effect.
        `effect` can be: 'confetti', 'celebration', 'fireworks', 'shooting_star',
        'lasers', 'love', 'balloons', 'spotlight', 'echo', 'gentle', 'loud', 'slam'.
        Use 'confetti' or 'celebration' when completing a big goal or congratulating the user,
        or 'gentle' for quiet late-evening check-ins."""
        try:
            await sendblue.send_message(user.phone_number, content, send_style=effect)
            return f"Delivered message with Apple effect '{effect}'."
        except SendblueError as e:
            return f"Could not deliver styled message: {e}"

    raw_tools: list[BaseTool] = [
        confirm_pending_action, reject_pending_action, list_pending_actions,
        track_project, list_active_projects, save_profile_info,
        set_app_preference, list_my_connected_apps,
        get_system_status, get_session_details, get_recent_message_activity,
        react_to_message, send_styled_message,
        list_deepsearch_sessions, find_contact, send_pdf_over_text,
        recall_past_conversation, cancel_active_search,
        *build_web_search_tools(),
    ]
    return trace_all(raw_tools, ORCHESTRATOR_LABEL)


def _integrations_agent_description(connected_slugs: list[str], email_primary: str = "messa") -> str:
    """Builds integrations_agent's subagent `description` -- what Messa's
    OWN context sees on every turn when deciding whether to delegate here,
    per docs/dynamic_connected_apps_spec.md. `connected_slugs` comes from
    db.get_active_connected_toolkits (a cheap LOCAL Postgres read, not a
    live Composio call -- see that function's own docstring for why: this
    runs on every single turn via build_orchestrator, so anything here
    that hit Composio's API directly would add real latency to every
    message Messa handles, not just ones about an app). Deliberately kept
    a pure function, separate from build_orchestrator, so it's testable
    without spinning up a real agent.

    `email_primary` (added for the primary-app preference system, default
    'messa' so every existing caller/test keeps its old behavior unless it
    explicitly passes something else): the old hardcoded "NOT for email"
    line was only ever true while personal_inbox_agent/email_agent were
    the sole possible email handlers -- now that a generic email request
    can ALSO resolve to integrations_agent (Gmail/Outlook as primary, see
    _build_system_prompt's "Email routing" paragraph), a flatly hardcoded
    exclusion here would directly contradict that routing instead of just
    being a redundant reminder of it."""
    connected_str = (
        f" Currently connected for this user: {', '.join(connected_slugs)}."
        if connected_slugs else ""
    )
    email_note = (
        "NOT for email (that's always email_agent/personal_inbox_agent)"
        if email_primary == "messa"
        else "including email when it's the user's current primary (see 'Known about this "
        "user' above) -- otherwise still email_agent/personal_inbox_agent"
    )
    return (
        "Reaches any of Composio's 1,400+ other app integrations (Reddit, Todoist, Slack, "
        "Notion, GitHub, Instagram, Google Calendar, and more) that none of Messa's other "
        f"subagents already cover -- {email_note}." + connected_str + " Use this whenever the "
        "user names an app/service outside Messa's native capabilities and wants Messa to do "
        "something in it, OR asks to connect/link/authorize/sync one -- ALWAYS check here "
        "first, before deepsearch, for anything app-shaped."
    )


_ONBOARDING_PROMPTS = {
    "awaiting_name": (
        "This is a brand-new user and you don't know their name yet. Before diving into "
        "their first request (or right after helping with it if they jumped straight to a "
        "task), naturally ask \"What's your name?\" in a friendly, conversational way, "
        "then call save_profile_info('name', ...). That's the only thing onboarding needs "
        "-- the moment you save it, the system sends its own follow-up messages with your "
        "email address, dashboard link, and a question about which apps they use day to day "
        "-- so don't try to introduce yourself, write any of that, or ask about apps yourself "
        "right now; wait for their answer to that last question instead (see the integrations "
        "routing guidance above for what to do once they name some)."
    ),
}


def _profile_enrichment_str(user: config.UserContext) -> str:
    """City and email are no longer onboarding steps (see db.py's
    ONBOARDING_STEPS comment) -- they're ongoing, low-priority profile gaps
    to close conversationally, tied to a real moment they're actually
    useful for, never a standalone interrogation. Returns "" once both are
    known or explicitly skipped -- most turns, for most users, this costs
    nothing."""
    gaps = []
    if not user.city and not user.city_prompt_skipped:
        gaps.append(
            "their city/zip -- ask for it when something you're doing actually depends on "
            "location (a recommendation, a local time, scheduling, weather), woven into "
            "your answer ('what's your zip?'), not as a standalone question out of nowhere"
        )
    if not user.email and not user.email_prompt_skipped:
        gaps.append(
            "a personal email on file -- ask when a task actually needs one (sending them "
            "something), e.g. 'sure -- what email should I send it to?'; if they'd rather "
            "not share one, save_profile_info('email', 'skip') so you never ask again"
        )
    if not gaps:
        return ""
    return (
        "Still missing, worth closing opportunistically (call save_profile_info once they "
        "answer): " + "; and ".join(gaps) + ". Never block or delay the task waiting for "
        "either -- answer what they actually asked first, and only ask if it's genuinely "
        "relevant right now. If it's been a long real conversation and neither has come up "
        "naturally, asking once in passing is fine too.\n\n"
    )


def _category_known_line(
    category: str, primary: str, connected_slugs: list[str], user: config.UserContext,
) -> str:
    """One 'Known about this user' line for a single primary-app category
    (email/calendar/tasks) -- generalizes what used to be email-only
    inline logic (see this function's callers) so all three categories
    render their current state the same way: which app is primary, and --
    the one case that actually prevents Messa from confidently getting
    this wrong -- a clear callout when the stored primary points at
    something that isn't connected anymore, with an explicit instruction
    to fall back to the native tool rather than silently failing or
    guessing. `is_connected` special-cases 'gmail': its connection state
    lives in user.email_connected (email_tools.py's own dedicated OAuth
    flow predates the generic Dynamic Integration Engine), not in
    connected_slugs (db.get_active_connected_toolkits, which only reads
    app_connection_requests)."""
    native_labels = {
        "email": "their own Messa address (personal_inbox_agent)",
        "calendar": "Messa's own internal calendar (executive_assistant)",
        "tasks": "Messa's own internal task list (executive_assistant)",
    }
    subagent_labels = {"email": "email_agent" if primary == "gmail" else "integrations_agent", "calendar": "integrations_agent", "tasks": "integrations_agent"}
    native_label = native_labels[category]

    if primary == "messa":
        return f"primary {category}: {native_label}"

    is_connected = (primary == "gmail" and user.email_connected) or (primary in connected_slugs)
    if is_connected:
        return f"primary {category}: their connected {primary} ({subagent_labels[category]})"
    return (
        f"primary {category} is set to {primary}, but it isn't connected right now -- treat "
        f"{native_label} as the effective default for any unnamed {category} request until "
        f"{primary} is reconnected, and mention that mismatch if it's relevant"
    )


def _build_system_prompt(
    user: config.UserContext,
    connected_slugs: list[str] | None = None,
    app_preferences: dict[str, str] | None = None,
    recent_assets: list[dict[str, Any]] | None = None,
) -> str:
    connected_slugs = connected_slugs or []
    app_preferences = app_preferences or {}
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
    if user.memory_profile:
        # The cheap, always-on profile digest (users.memory_profile,
        # migrations/027_memory.sql) -- a plain column read, NOT a live
        # Mem0 call (see messa/memory.py's module docstring for the full
        # two-tier design and why this never touches Mem0 on this hot
        # path). Absent entirely for a brand-new user or before their
        # first daily batch run, same "omit rather than placeholder"
        # pattern as every other optional field here.
        known.append(f"what you've learned about them over time: {user.memory_profile}")
    for category in ("email", "calendar", "tasks"):
        # 'email' has a free, always-already-loaded fallback
        # (UserContext.default_email_provider, populated once per turn by
        # cli.py regardless of this call) for a caller that hasn't fetched
        # app_preferences at all -- same "degrade to the cheap legacy
        # source rather than silently pretend 'messa'" shape as
        # db.get_app_preference's own column read-through. Calendar/tasks
        # have no such legacy source, so they genuinely have nothing
        # better than 'messa' to fall back to when the caller passes
        # nothing here -- correct, since app_preferences omitted really
        # does mean "preference state wasn't fetched this call."
        default = user.default_email_provider if category == "email" else "messa"
        primary = app_preferences.get(category) or default
        known.append(_category_known_line(category, primary, connected_slugs, user))
    known_str = ("Known about this user so far -- " + ", ".join(known) + ".\n\n") if known else ""

    # Persistent Workspace Asset Registry (docs/executive_agent_
    # architecture_proposal.md 3.A, migration 037) -- the user's most
    # recently touched workspace assets (a Google Sheet, an Airtable
    # base, a generated PDF, ...), so Messa never has amnesia about
    # something she already made them and doesn't send them off to dig
    # through their own Google Drive to find it. Small and fixed
    # (config.USER_ASSETS_MAX_INJECTED, default 5) -- a memory aid, not a
    # full asset browser; execute_integration_tool's own list actions
    # cover genuine browsing. Omitted entirely when there's nothing to
    # show, same "omit rather than placeholder" convention as every other
    # optional fragment in this function.
    recent_assets = recent_assets or []
    if recent_assets:
        asset_lines = []
        for a in recent_assets:
            line = f"- {a.get('title')} ({a.get('asset_type')})"
            if a.get("url"):
                line += f": {a['url']}"
            asset_lines.append(line)
        assets_str = (
            "The user's most recently touched workspace assets (reference these instead of "
            "asking the user to find something you already made them, or re-creating it from "
            "scratch):\n" + "\n".join(asset_lines) + "\n\n"
        )
    else:
        assets_str = ""

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

    # City/email are no longer onboarding steps -- see db.py's ONBOARDING_STEPS
    # comment and _profile_enrichment_str's own docstring above. Computed
    # regardless of onboarding_complete (these are ongoing profile gaps, not
    # a gate), and "" the moment both are known or explicitly skipped.
    profile_str = _profile_enrichment_str(user)

    channel_str = ""
    if user.channel != "cli":
        channel_str = (
            f"You're replying over {user.channel} -- a real text thread, not a chat UI. No "
            "markdown at all (no **bold**, no bullet/numbered lists, no headers, no code "
            "fences) -- write it the way a person actually texting would.\n\n"
        )

    # Only ever non-empty for an admin account (user.is_admin) -- a regular
    # user's system prompt never even mentions admin_agent exists, matching
    # how it's also simply absent from their subagents list below (see
    # build_orchestrator). Two independent layers, neither relying on the
    # other: even if this string were somehow left in by mistake, a
    # non-admin's orchestrator still has no admin_agent to delegate to.
    admin_agent_mention = (
        ", admin_agent (admin-only: broadcast a message to all users, manage the "
        "new-user waitlist)"
    ) if user.is_admin else ""

    # Only ever non-empty for a user WITHOUT deepsearch access (the common
    # case at v1 launch -- see UserContext.has_deepsearch_access's own
    # docstring). Told up front, before the delegation-routing paragraphs
    # below even mention deepsearch, specifically so Messa never opens with
    # "sending this to deepsearch now" / "checking on that" and THEN has to
    # backpedal once the tool call comes back declined -- it can just
    # explain plainly in one go instead. The task tool itself would also
    # decline gracefully even without this (build_deepsearch_subagent's own
    # _run checks the same flag first thing), so this is purely about
    # smoother phrasing, not a second enforcement layer.
    deepsearch_access_str = "" if user.has_deepsearch_access else (
        "\n\nLive web browsing/research (deepsearch) is NOT available on this account yet -- "
        "it's in a limited beta right now. If the user asks for anything that would need real "
        "browsing (checking a live price, filling out a form on a website, signing up for "
        "something, buying/ordering online, etc.), don't delegate to deepsearch or claim you're "
        "checking -- tell them plainly that live browsing isn't turned on for their account yet, "
        "and offer to help another way if one exists (e.g. search_web for general info that "
        "doesn't need a live page)."
    )

    live_view_str = ""
    if user.live_view_share_url:
        live_view_str = (
            " Specifically for deepsearch: your acknowledgment before delegating to it must "
            "just be a short, natural line about what you're checking -- NEVER type or generate "
            "a live-view link or URL yourself. The system texts the user the real live-view link "
            "separately, once the browser session is actually up and there's something to watch. "
            "Any URL you write will be invalid."
        )

    # Only ever non-empty while a deepsearch task is genuinely still running
    # for this user (deepsearch_control.describe returns None otherwise) --
    # this is what lets cancel_active_search's own "only on a clear stop/
    # pivot signal" instruction actually be judged against something real,
    # instead of the model having to guess whether anything is even active.
    # Computed fresh here (not carried on UserContext) since it's live
    # in-process state, not a DB-backed field like everything else in
    # 'Known about this user' above.
    active_search_title = deepsearch_control.describe(user.user_id)
    active_search_str = ""
    if active_search_title:
        active_search_str = (
            f"\n\nA background search is currently running for this user right now: "
            f"\"{active_search_title}\". If THIS message clearly asks to stop/cancel it, or "
            "clearly implies wanting to do something else INSTEAD of waiting for it, call "
            "cancel_active_search immediately, then respond warmly confirming you stopped it "
            "before moving on to whatever they asked for instead. If the message is ambiguous, "
            "just a progress check, or genuinely unrelated (could just as well run after this "
            "one finishes), leave it running -- don't cancel on a guess."
        )
    else:
        active_search_str = (
            "\n\nSystem Ground Truth: There is NO background task currently running for this user right now. "
            "If the user asks 'is something running', 'why did you do X', 'why am I getting codes', or says 'stop', "
            "call get_system_status or get_session_details to inspect the real facts. "
            "NEVER claim, hallucinate, or apologize that a task is currently running, looping, or resending codes when none is active. "
            "If cancel_active_search was called and closed historical sessions, tell the user that no task was actively running and the browser is closed."
        )

    # Read-only awareness that ANOTHER message from this SAME user is still
    # being processed right now (see turn_control.py's own module docstring
    # for exactly why this can only ever be read-only, never a "go cancel
    # it" instruction the way active_search_str above is) -- for a
    # follow-up that arrives well into an already-running turn, e.g. a
    # check-in or correction sent minutes into a deepsearch delegation.
    # Gated on its own flag so this is a true no-op with it off.
    in_flight_str = ""
    if config.IN_FLIGHT_TURN_AWARENESS_ENABLED:
        in_flight_description = turn_control.describe(user.user_id)
        if in_flight_description:
            in_flight_str = (
                f"\n\nAnother message from this user is still being processed right now: "
                f"{in_flight_description}. If THIS message is a short acknowledgment, a brief "
                "reply is enough. If it reads like a correction or update to that other "
                "message, you CANNOT reach into that other turn and change it -- say so "
                "plainly rather than claiming you've updated or cancelled it. If it's a new, "
                "unrelated request, just handle it normally."
            )

    return (
        "You are Messa -- a task-oriented personal life manager reachable by text, email, and "
        "(soon) WhatsApp, not a chatbot. Your job is taking real things off the user's plate "
        "(email, schedules, tasks, research, documents) by actually doing them. Asked how "
        "you're different from other AI assistants, lead with that -- you act instead of just "
        "answering, and you reach 1,400+ connected apps -- documents/contracts are one "
        "capability among several, never the headline.\n\n"
        "How you talk: confident, warm, plainspoken. Never hedge with filler like 'honestly', "
        "'let me be honest', or 'I'll be straight with you' -- just say the true thing. State "
        "what you're sure of directly; check a tool rather than guess when you're not; decline "
        "a real limit in one short line, not an apology paragraph. Default short -- a good "
        "short answer that actually resolves the ask beats a long one -- but run as long as a "
        "genuinely complex question needs. Separate distinct thoughts into short paragraphs "
        "with a blank line between them.\n\n"
        "Questions before acting: ask only what you genuinely can't proceed without -- never a "
        "checklist, never more than one or two at once, and bundle every question you do have "
        "into a single message rather than drip-feeding them one at a time. If a reasonable "
        "default gets the task done and a wrong guess just costs a quick follow-up fix, use the "
        "default and say what you assumed instead of asking first. Actually ask, before acting, "
        "only when a wrong guess would be expensive or hard to undo (sending something to the "
        "wrong person, spending money, canceling/deleting something) or when the request is "
        "genuinely ambiguous between two different tasks -- not for details you could pick a "
        "sensible default for.\n\n"
        "You talk to the user directly and delegate specialized work to subagents via the "
        "task tool: deepsearch (web browsing/research), executive_assistant (tasks, "
        "reminders, notes, contacts, and Messa's own INTERNAL calendar -- not a real "
        "connected calendar), email_agent (the user's own Gmail), "
        "personal_inbox_agent (the user's own Messa-owned email address -- a different "
        "inbox from their Gmail), document_agent (contracts, executive reports, and PDFs), routines_agent "
        "(recurring or one-time task routines -- both plain reminders where the USER does "
        "something, and background tasks where YOU do something yourself and report back, "
        "e.g. watchers, deadline-aware follow-ups), integrations_agent (any other app -- "
        f"Reddit, Todoist, Slack, Notion, GitHub, Google Calendar, and 1,400+ more)"
        f"{admin_agent_mention}.\n\n"
        "Routing -- email, calendar, and tasks each have a NATIVE Messa tool and, once "
        "connected, a REAL app that can compete with it. Same pattern all three: a GENERIC "
        "request naming neither ('check my email', 'what's on my schedule', 'add a task') "
        "goes to whichever is the current PRIMARY for that category (see 'Known about this "
        "user' below) -- don't ask every time, that's the point of a primary. An "
        "explicitly-named app, or 'your own'/'native' phrasing, always overrides the primary "
        "regardless. To change the primary: set_app_preference(category, value) -- it says if "
        "the target needs connecting first. Connecting an app does NOT change the primary by "
        "itself, except a one-time auto-promotion the first time anything's connected with "
        "nothing else set yet (see 'Known about this user' for the live answer either way).\n"
        "  - email: personal_inbox_agent (their Messa address) vs email_agent (connected "
        "Gmail). 'my gmail'/'my real email' -> email_agent; 'my messa email'/'the address you "
        "gave me' -> personal_inbox_agent. Whenever you draft or send from their Messa "
        "address, always write third-person on their behalf ('<Name> asked me to confirm...') "
        "-- never first-person as the user, never sign off with their name.\n"
        "  - calendar: executive_assistant (Messa's own internal calendar) vs "
        "integrations_agent (a real connected calendar, e.g. Google Calendar, Composio "
        "toolkit slug 'googlecalendar'). CONNECT/sync/manage a real calendar always goes to "
        "integrations_agent, never executive_assistant -- executive_assistant has "
        "no connection to a real calendar at all. Give a heads-up before disconnecting one if "
        "you know of upcoming events that live only there.\n"
        "  - tasks: executive_assistant (Messa's own task list) vs integrations_agent (e.g. "
        "Todoist/Asana). Reminders are NOT part of this -- always executive_assistant, no "
        "connected-app equivalent exists.\n\n"
        "Switching or disconnecting a connected app: there's NO settings/integrations page for "
        "the user to do this themselves -- never say there is. Do it directly: to switch accounts, call "
        "request_email_connection/connect_integration_app with switch_account=True (disconnects "
        "then sends a fresh connect link); to disconnect with no reconnect, call "
        "disconnect_email/disconnect_integration_app. Either way the disconnect is immediate on "
        "Messa's side, but the app's own access revocation runs in the background after -- say "
        "it's in progress, not confirmed complete.\n\n"
        "Connecting SEVERAL apps at once (e.g. the user lists several apps in answer to Messa's "
        "own 'what apps do you use day to day' onboarding question, or just says 'connect Slack, "
        "Notion, and Todoist' unprompted): delegate to integrations_agent and use "
        "queue_app_connections(toolkit_slugs) with every app resolved to its Composio slug, NOT "
        "repeated connect_integration_app calls -- it sends the first link right away and queues "
        "the rest to go out automatically, one at a time, only as each previous one actually "
        "finishes connecting, instead of handing the user a wall of OAuth links all at once. A "
        "single named app still just uses connect_integration_app/request_email_connection as "
        "usual.\n\n"
        "Documents: document_agent produces PDFs specifically -- contracts (NDAs, MSAs, SOWs, "
        "consulting agreements), executive reports, and contract-risk audits. For an EDITABLE "
        "or collaborative doc, or a genuine Word-compatible file ('make me a Word doc', 'put "
        "this in Google Docs'), delegate to integrations_agent instead (Google Docs/Microsoft "
        "Word via Composio, if connected) rather than saying it's not supported -- "
        "document_agent's PDF output is a different, complementary thing, not the only option.\n"
        "When something generated needs to be sent/attached (\"make me a PDF and email it\"), "
        "delegate to document_agent first, get the exact file path back, then either delegate "
        "to personal_inbox_agent with that path relayed verbatim (only it can attach files -- "
        "email_agent/Gmail can't attach in this build, say so plainly rather than sending "
        "without the file) or call your own send_pdf_over_text(file_path) directly if they "
        "want it texted -- never invent or paraphrase the path. For legal agreements/reports, "
        "delegate to document_agent with all known details; if a deal term like counterparty "
        "name or rate is missing, ask once with standard defaults offered (Net 30, 30-day "
        "termination, Delaware law, mutual liability cap) rather than multiple follow-ups.\n\n"
        "Contracts sent to you: a texted/emailed PDF's text is already included in the message "
        f"that told you about it (capped at {config.MAX_PDF_READ_PAGES} pages). For a Gmail "
        "attachment, delegate to email_agent (read_email_attachment). To review/audit an "
        "agreement for risk, delegate to document_agent's audit_contract (liability, "
        "indemnification, IP, payment, termination) -- generate_audit_report_pdf=True for a "
        "formal PDF version. If a document couldn't be read at all (corrupted, scanned, "
        "password-protected), say so plainly rather than guessing.\n\n"
        "Contacts: when the user names someone by name for an email or text ('email Sam about "
        "the invoice', 'text John I'm running late') without also giving you their actual "
        "address/number, call find_contact(name) FIRST to resolve it before delegating -- don't "
        "ask the user to repeat something they may have already told you. If it finds more than "
        "one match, ask which one before sending anything; if it finds none (or finds the "
        "contact but not the info you actually need), ask the user directly rather than "
        "guessing or inventing an address/number.\n\n"
        "search_web and read_webpage are your OWN direct tools (no delegation, no browser "
        "session) -- use them for a factual lookup or a read-only check on a specific page "
        "instead of delegating to deepsearch, which opens a real, billed-by-time browser "
        "session regardless of how simple the question is. Only delegate to deepsearch for "
        "actual browser interaction (a multi-step flow, filling a form, logging in, clicking "
        "through a cart) -- for a mixed task, research with search_web/read_webpage first, "
        "then delegate to deepsearch with the exact target page.\n\n"
        "Apple iMessage features:\n"
        "  - react_to_message(reaction): a quick tapback ('like', 'love', 'laugh', etc.) to "
        "acknowledge a message without sending a full text bubble.\n"
        "  - send_styled_message(content, effect): send with an Apple effect ('confetti', "
        "'celebration', 'fireworks', 'balloons', or bubble effects like 'gentle', 'loud', "
        "'slam'). Use 'confetti'/'celebration' for a real milestone, 'gentle' for late check-ins.\n\n"
        "System state and anti-spam:\n"
        "  - get_system_status()/get_session_details(id): authoritative real-time facts about "
        "active tasks and browsing sessions -- always trust this over memory or assumptions.\n"
        "  - get_recent_message_activity(): check when you last texted the user before an "
        "autonomous routine or secondary check sends another one within 2 minutes.\n\n"
        "Other apps and platforms (Reddit, Todoist, Slack, Notion, GitHub, Google Calendar, "
        "and everything outside your other subagents -- never email, that always goes to "
        "email_agent/personal_inbox_agent): delegate to integrations_agent FIRST, before "
        "deepsearch or search_web, for anything naming or clearly implying one of these -- "
        "'check Reddit', 'post to Slack', 'add a Todoist task'. Composio's authenticated API "
        "access is faster and more reliable than browsing by hand. Only offer deepsearch once "
        "integrations_agent reports nothing matches.\n\n"
        "Universal app-connection rule: ANY request to connect, link, authorize, or sync ANY "
        "app or 3rd-party service -- named above or not -- goes to integrations_agent "
        "immediately, the same as a request to USE one. Let its own search tell you whether "
        "Composio supports it; only fall back to deepsearch if it reports nothing matches.\n\n"
        f"{known_str}"
        f"{assets_str}"
        f"{time_str}"
        f"{onboarding_str}"
        f"{profile_str}"
        f"{channel_str}"
        f"{deepsearch_access_str}"
        "Responsiveness: delegating to a subagent can take a little while. Before calling "
        "the task tool, send one short line acknowledging what you're about to do (e.g. "
        "\"Checking flights now...\") so the user isn't staring at silence -- don't just go "
        "straight to a silent tool call."
        f"{live_view_str}"
        f"{active_search_str}"
        f"{in_flight_str}"
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
    by default, config.ORCHESTRATOR_API_KEY); `subagent_model`, when
    explicitly passed, is what EVERY subagent below uses instead (kept for
    tests that want to inject one fake model everywhere) -- real callers
    (cli.py, server.py) always omit it, in which case each subagent gets
    its OWN model instance instead, same config.SUBAGENT_MODEL_NAME but
    bound to whichever OpenRouter key config.api_key_for_agent resolves
    for that specific subagent's name (see _subagent_model_for below and
    config.py's "Per-agent OpenRouter API keys" section for the full
    three-tier-plus-overrides design -- this is what lets Messa's own
    reply, deepsearch, and the rest of the subagents run on separate keys
    so a busy one's rate/concurrency ceiling never queues up another)."""
    approval_gate = approval_gate or CLIApprovalGate()
    # effective_context_tokens: see config.py's big comment above
    # SUBAGENT_EFFECTIVE_CONTEXT_TOKENS/ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS
    # -- without this, every agent's auto-compaction silently falls back to
    # deepagents' generic 170k-token trigger, since our OpenRouter model
    # strings don't match anything in LangChain's model-profile lookup.
    #
    # api_key=config.ORCHESTRATOR_API_KEY: Messa's own dedicated key (see
    # config.py's "Per-agent OpenRouter API keys" section) -- separate from
    # every subagent below by default, so a rate/concurrency ceiling on a
    # busy subagent's key can never queue up Messa's own reply to the user.
    model = model or config.build_model(
        config.ORCHESTRATOR_MODEL_NAME,
        effective_context_tokens=config.ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS,
        api_key=config.ORCHESTRATOR_API_KEY,
    )

    def _subagent_model_for(agent_name: str) -> Any:
        """Builds a per-agent model bound to config.api_key_for_agent's
        resolution for that specific agent name -- deepsearch, or the
        shared "everything else" key, or an individual override, per
        config.py's own docstring. `subagent_model` (this function's own
        closed-over param), when explicitly passed in by a caller (real
        callers never do; tests inject a fake here to cover every subagent
        with one object), takes priority over all of that -- same
        backward-compatible escape hatch this param already was before
        per-agent keys existed. Building N small ChatOpenAI clients here
        instead of reusing one shared instance is cheap: construction does
        no network call (confirmed by deepsearch_tools.py's own
        _pick_subagent_model, which already relies on exactly that fact for
        its per-call key rotation)."""
        if subagent_model is not None:
            return subagent_model
        return config.build_model(
            config.SUBAGENT_MODEL_NAME,
            effective_context_tokens=config.SUBAGENT_EFFECTIVE_CONTEXT_TOKENS,
            api_key=config.api_key_for_agent(agent_name),
        )
    # A cheap LOCAL DB read (not a Composio API call -- see
    # db.get_active_connected_toolkits' own docstring for why that
    # distinction matters here specifically: this runs on every turn).
    # Never fatal to a turn if it fails -- worst case, the description
    # just omits the connected-apps hint this once.
    try:
        connected_slugs = await db.get_active_connected_toolkits(user.user_id)
    except Exception as e:  # noqa: BLE001 - a hint, not load-bearing
        console.system(f"get_active_connected_toolkits failed (non-fatal): {e}")
        connected_slugs = []

    # Primary-app preferences (email/calendar/tasks) -- same "cheap local
    # read, never fatal to a turn" shape as connected_slugs just above.
    # 'email' is included even though UserContext.default_email_provider
    # already gives a free zero-query fallback -- db.get_app_preference's
    # own read-through-to-that-column logic (see its docstring) means this
    # always resolves correctly even before any user_app_preferences row
    # exists, and routing every category through the same call here keeps
    # _build_system_prompt's own logic uniform across all three instead of
    # special-casing email.
    app_preferences: dict[str, str] = {}
    for _category in ("email", "calendar", "tasks"):
        try:
            app_preferences[_category] = (await db.get_app_preference(user.user_id, _category)) or "messa"
        except Exception as e:  # noqa: BLE001 - a hint, not load-bearing, same as connected_slugs above
            console.system(f"get_app_preference({_category!r}) failed (non-fatal): {e}")
            app_preferences[_category] = "messa"

    # Persistent Workspace Asset Registry (docs/executive_agent_
    # architecture_proposal.md 3.A) -- same "cheap local read, never
    # fatal to a turn" shape as connected_slugs/app_preferences just
    # above. Gated on WORKSPACE_ASSETS_ENABLED (not just the table-exists
    # check inside db.get_recent_user_assets itself) so this is a true
    # kill switch, consistent with how every other flag in this function
    # behaves.
    recent_assets: list[dict[str, Any]] = []
    if config.WORKSPACE_ASSETS_ENABLED:
        try:
            recent_assets = await db.get_recent_user_assets(user.user_id)
        except Exception as e:  # noqa: BLE001 - a hint, not load-bearing
            console.system(f"get_recent_user_assets failed (non-fatal): {e}")
            recent_assets = []

    # Every subagent below is a deepagents CompiledSubAgent (a "runnable"
    # closure that builds its OWN tools/system_prompt fresh at actual
    # delegation time) rather than a plain declarative {"tools":
    # ..., "system_prompt": ...} dict built once here. This used to be a
    # split: deepsearch/executive_assistant/email_agent were already
    # CompiledSubAgents, while personal_inbox_agent/document_agent/
    # routines_agent/integrations_agent/admin_agent were plain dicts whose
    # system_prompt (and therefore Active Task Scratchpad `task_block`
    # below) was frozen the INSTANT this function started -- before any of
    # THIS turn's own tool calls had run. Concretely: integrations_agent
    # creates a spreadsheet and saves its id via update_task_scratchpad
    # mid-turn, Messa then delegates to document_agent or
    # personal_inbox_agent later in that SAME turn -- that second
    # delegation's frozen prompt predated the sheet, so the id was
    # invisible to it (feature/agentic-upgrade plan: "assets vanishing
    # mid-task"). Converting all five closes that gap by construction --
    # every subagent now fetches scratchpad_prompt_block(user) fresh
    # inside its own _run, exactly like deepsearch/executive_assistant/
    # email_agent always did.
    subagents = [
        build_deepsearch_subagent(user, _subagent_model_for("deepsearch"), approval_gate),
        build_executive_subagent(user, _subagent_model_for("executive_assistant")),
        build_email_subagent(user, _subagent_model_for("email_agent"), approval_gate),
        build_call_subagent(user, _subagent_model_for("call_agent"), approval_gate),
        build_personal_inbox_subagent(user, _subagent_model_for("personal_inbox_agent"), approval_gate),
        build_document_subagent(user, _subagent_model_for("document_agent")),
        build_routines_subagent(user, _subagent_model_for("routines_agent")),
        # integrations_agent's `description` stays computed HERE (not inside
        # build_integration_subagent) because it's genuinely per-turn dynamic
        # -- _integrations_agent_description bakes in which apps are
        # currently connected, from the cheap connected_slugs read just
        # above; see build_integration_subagent's own docstring for why that
        # computation can't just move into its _run without either a
        # redundant DB read there or duplicating this fetch.
        build_integration_subagent(
            user,
            _subagent_model_for("integrations_agent"),
            _integrations_agent_description(connected_slugs, app_preferences.get("email", "messa")),
            approval_gate,
        ),
    ]

    # Admin-only, and only ever added here -- a non-admin's subagents list
    # simply never contains this entry, so there's no tool/delegation path
    # to it at all (not just a runtime check inside it). Same quiet,
    # unadvertised user.is_admin flag usage.py already gates the metering
    # bypass on.
    if user.is_admin:
        subagents.append(build_admin_subagent(user, _subagent_model_for("admin_agent")))

    # Active Task Scratchpad + Skills Playbook (docs/autonomous_
    # integrations_and_task_memory_spec.md) for the ORCHESTRATOR's own
    # prompt/tools only -- every subagent above now attaches this same
    # helper itself, fresh, inside its own _run closure (see each
    # build_X_subagent's own module for its call site), so there is no
    # generic post-hoc injection loop over `subagents` here anymore.
    orchestrator_tools = build_orchestrator_tools(user)
    # RELIABILITY_GUARDRAIL_STR (messa/reliability.py, feature/agentic-upgrade
    # plan): "never claim an unconfirmed action succeeded, never relay raw
    # tool/API error text" -- the persona-level half of the empty-promise/
    # plumbing-leak guardrail (see cli.py's run_turn and every subagent's
    # own _run for the structural, code-enforced half). Unconditional, not
    # gated on SCRATCHPAD_AND_SKILLS_ENABLED -- this is an independent rule,
    # not part of the Active Task Scratchpad feature.
    orchestrator_system_prompt = (
        _build_system_prompt(user, connected_slugs, app_preferences, recent_assets)
        + reliability.RELIABILITY_GUARDRAIL_STR
    )
    if config.SCRATCHPAD_AND_SKILLS_ENABLED:
        orchestrator_tools = orchestrator_tools + build_scratchpad_tools(user, "messa_orchestrator", approval_gate)
        orchestrator_system_prompt = orchestrator_system_prompt + await scratchpad_prompt_block(user)

    agent = create_deep_agent(
        model=model,
        tools=orchestrator_tools,
        system_prompt=orchestrator_system_prompt,
        subagents=subagents,
    )
    return agent
