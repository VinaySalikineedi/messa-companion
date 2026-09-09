"""The Dynamic Integration Engine: lets Messa discover, connect to, and use
any of Composio's 1,400+ app toolkits (Todoist, Slack, Notion, GitHub,
Instagram, ...) on a per-user basis, WITHOUT hardcoding a dedicated tools
module per app the way tools/email_tools.py does for Gmail specifically.
See docs/dynamic_tool_injection_spec.md for the original design doc this
implements.

Deliberately separate from tools/email_tools.py, not a replacement for it
-- explicit product decision (the doc's own "Preserve Existing Gmail
Integration" principle): Gmail keeps its own dedicated, already-tested
connection-polling loop and minimally-scoped auth config untouched. This
module covers everything else.

Three tools, not the doc's original two -- see search_integration_tools'
and connect_integration_app's docstrings for why the connect step is its
own separate tool rather than folded into search: it mirrors
tools/email_tools.py's request_email_connection exactly (generate the
link, text it directly as its own message, right now) rather than
inventing a new "deterministic link, relayed through two more model turns"
mechanism this codebase doesn't have anywhere else. One more small, cheap
tool schema (~100-150 tokens) bought a proven, already-shipped pattern
instead of new plumbing.

  - search_integration_tools(query): read-only discovery. No side effects
    -- safe for the model to call just to look around.
  - connect_integration_app(toolkit_slug): the one tool with a real side
    effect for a NOT-yet-connected app -- generates an OAuth link and
    texts it directly, exactly like request_email_connection.
  - execute_integration_tool(slug, arguments): the generic executor.
    Gating is dynamic per call (see config.INTEGRATION_WRITE_ACTION_KEYWORDS
    and trace_tool's destructive_check parameter) since ONE tool here can
    run anything from a read-only list action to an irreversible delete,
    depending on which slug it's called with -- unlike every other
    destructive tool in this app (send_email, reply_to_email, ...), whose
    destructiveness is fixed at registration time.

Verified against the actual installed `composio` SDK the same way
tools/email_tools.py was (0.21.0 / composio-client 1.43.0 at the time that
file was written) for the calls this file SHARES with it --
`Composio(api_key=...)`, `client.tools.execute(...)`,
`client.connected_accounts.link(...)`/`.get(...)`/`.list(...)`,
`client.auth_configs.list/create(...)`. `client.tools.get(user_id=...,
search=..., limit=...)` -- Composio's tool-search API -- WAS since
independently confirmed against a live account (see the Reddit
integration's first real production run, documented in README): the
actual result shape is OpenAI-style function-wrapped items (each result's
real slug/description/toolkit live under `item.function.*`, not directly
on `item`) -- `_extract_tool_fields` below unwraps that first, falling
back to the item itself for any other shape. Also confirmed live:
`connected_accounts.list` does NOT accept a `toolkit_slugs` filter kwarg
the way an earlier version of this file assumed -- `_connected_toolkits_sync`
now fetches all of this user's active connections unfiltered and matches
toolkit slugs client-side instead.

That same production run also surfaced a real search-QUALITY gap, not
just a shape bug: an unscoped, single-word search ("reddit") ranked
several unrelated toolkits' actions (whose own descriptions merely
mention "Reddit") ahead of the actual Reddit action the model needed,
costing over a dozen search_integration_tools round trips before landing
on a phrasing that happened to match. The fix isn't cleverer query
phrasing -- the transcript shows phrasing length/style wasn't actually
the deciding factor (semantic closeness to the real action's own wording
was, which is unpredictable to hand-tune for) -- it's that Composio's own
`tools.get` already supports a `toolkits=[...]` filter, confirmed against
both Composio's own docs and the installed SDK's source, that scopes a
search to one specific app's action set instead of ranking across all
1,400+ toolkits at once. search_integration_tools now exposes this as an
optional `toolkit` parameter -- pass it whenever the app is already known
(the common case: the user usually names the app), leaving an unscoped
search as the fallback for genuine discovery rather than the default
path. INTEGRATION_SEARCH_RESULT_LIMIT was also bumped 5 -> 8 as a cheap
secondary safety margin, deliberately not raised further since scoping
does the real work and a much bigger limit mostly just adds token cost
from irrelevant results on the (now less common) unscoped search.

Auth-config scope, one real difference from Gmail worth knowing: Gmail's
_get_or_create_gmail_auth_config_id scopes the OAuth consent to exactly
the four actions email_tools.py calls, via
tool_access_config.tools_for_connected_account_creation, because that
whole action set is known in advance. Here, the whole point is that we
DON'T know in advance which of a toolkit's actions a user will end up
using -- so _get_or_create_auth_config_id below omits that restriction
and lets Composio grant its own default scope for the toolkit instead.
That's a real, deliberate trade of "minimum necessary scope" for "works
generically across 1,400+ toolkits without a hand-maintained action list
per app" -- worth a second look before this goes to real users broadly,
called out again in the README.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool

from .. import config, console, db, reliability, usage
from ..approval import ApprovalGate
from ..channels.sendblue import SendblueError, send_message
from .common import last_ai_text, run_inner_agent_with_claim_check, trace_tool
from .integration_circuit_breaker import ToolFailureLadderMiddleware, integration_slug_identity
from .scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block
from .web_search_tools import build_web_search_tools
from .workspace_asset_tools import (
    apply_cached_entity_defaults,
    build_workspace_asset_tool,
    discover_and_cache_app_entities,
    record_asset_from_execution,
    toolkit_for_injection_target,
)

LABEL = "integrations_agent"

# ---------------------------------------------------------------------------
# Primary-app preference system: which connected toolkits actually COMPETE
# with one of Messa's own native tools for the same job, and what job. A
# toolkit not listed here (Reddit, Slack, Notion, GitHub, ...) never enters
# any primary/conflict logic at all -- connecting it behaves exactly as
# before this feature existed. Deliberately just data (not, say, a live
# Composio category lookup): the set of toolkits that plausibly conflict
# with Messa's native calendar/tasks/email is small, known, and rarely
# changes, so a live/dynamic classification would be real complexity (and
# a real per-turn cost) for no actual benefit over a short, hand-maintained
# map -- same "static beats dynamic when the underlying set barely moves"
# reasoning already used for _TOOLKIT_APP_CATEGORY's own callers below and
# for the auto-set/ask-on-conflict flow in server.py's connection poll
# loops. 'reminders' deliberately has no entry/category here: there's no
# mainstream Composio app that's a drop-in equivalent for Messa's own
# lightweight SMS reminders (a Todoist/Asana "task" is a genuinely
# different concept from a scheduled nudge), so reminders stay native-only
# -- nothing to route between.
TOOLKIT_APP_CATEGORY: dict[str, str] = {
    "gmail": "email",
    "outlook": "email",
    "outlookmail": "email",
    "googlecalendar": "calendar",
    "outlookcalendar": "calendar",
    "todoist": "tasks",
    "asana": "tasks",
    "clickup": "tasks",
}


def app_category_for_toolkit(toolkit_slug: str) -> str | None:
    """'email'/'calendar'/'tasks' if this toolkit slug competes with one of
    Messa's own native tools, else None (the common case -- most of
    Composio's 1,400+ toolkits have no native equivalent to conflict
    with). Case-insensitive since toolkit slugs arrive from a few
    different places (Composio's own API, this project's own DB rows) with
    inconsistent casing in practice."""
    return TOOLKIT_APP_CATEGORY.get((toolkit_slug or "").strip().lower())


# In-process memoization, one auth config id per toolkit -- same reasoning
# as email_tools.py's single _gmail_auth_config_id_cache, just keyed by
# toolkit now that there's more than one possible toolkit. Nothing needs to
# persist this to the DB for it to survive a restart: a cache miss just
# means one extra auth_configs.list call to find (or one .create call to
# make) the config Composio already reuses by name.
_auth_config_id_cache: dict[str, str] = {}


class _NotConfigured(Exception):
    pass


def _get_client():
    if not config.COMPOSIO_API_KEY:
        raise _NotConfigured(
            "The dynamic integration engine isn't configured yet -- add COMPOSIO_API_KEY to .env."
        )
    from composio import Composio  # imported lazily so the app runs without composio installed

    return Composio(api_key=config.COMPOSIO_API_KEY)


def _composio_user_id(user: config.UserContext) -> str:
    """Same identifier email_tools.py uses -- a stable internal id rather
    than the phone number, shared across every Composio integration for
    this user regardless of which toolkit, so a user's Gmail connection
    (email_tools.py) and, say, their Todoist connection (this module) both
    resolve to the same Composio-side user."""
    return str(user.user_id)


def _auth_config_name(toolkit_slug: str) -> str:
    return f"Messa Dynamic Access: {toolkit_slug}"


def _get_or_create_auth_config_id(client, toolkit_slug: str) -> str:
    """Find-or-create, memoized per toolkit for this process -- see
    email_tools.py's _get_or_create_gmail_auth_config_id for the identical
    reasoning (reused by name across restarts, so a redeploy doesn't spawn
    a fresh auth config -- and fresh OAuth scopes/consent screen -- every
    time). Blocking; always call via asyncio.to_thread."""
    if toolkit_slug in _auth_config_id_cache:
        return _auth_config_id_cache[toolkit_slug]

    name = _auth_config_name(toolkit_slug)
    existing = client.auth_configs.list(toolkit_slug=toolkit_slug)
    for item in existing.items:
        if item.name == name:
            _auth_config_id_cache[toolkit_slug] = item.id
            return item.id

    created = client.auth_configs.create(
        toolkit_slug,
        {"type": "use_composio_managed_auth", "name": name},
    )
    _auth_config_id_cache[toolkit_slug] = created.id
    console.system(
        f"Composio: created auth config {created.id!r} for toolkit {toolkit_slug!r} "
        "(Composio's own default scope for this toolkit -- see this module's own "
        "docstring for why, unlike Gmail's, this isn't scoped to a specific action list)."
    )
    return created.id


def _guess_toolkit_slug(slug: str, explicit: str | None) -> str:
    """Composio action slugs are consistently TOOLKIT_VERB_NOUN (e.g.
    TODOIST_CREATE_TASK -> toolkit 'todoist') -- see toolkits.composio.dev.
    Prefers a toolkit field the search result actually carries, if any;
    falls back to this naming-convention heuristic otherwise. Not verified
    against a live search result (see module docstring)."""
    if explicit:
        return explicit.lower()
    return slug.split("_", 1)[0].lower() if "_" in slug else slug.lower()


def _extract_tool_fields(item: Any) -> dict[str, str | None]:
    """Defensive extraction across a couple of plausible shapes for one
    tools.get(search=...) result item -- see this module's own docstring
    for why this is written defensively rather than assuming one exact
    shape. Handles OpenAI-style function dicts/objects, attributes, or plain dicts."""
    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    func = _get(item, "function")
    target = func if func is not None else item

    slug = _get(target, "slug") or _get(target, "name") or _get(target, "action") or ""
    description = _get(target, "description") or _get(target, "summary") or ""
    toolkit = _get(target, "toolkit_slug") or _get(target, "toolkit") or _get(target, "app") or _get(target, "appName")
    if isinstance(toolkit, dict):
        toolkit = toolkit.get("slug") or toolkit.get("name")
    elif toolkit is not None and not isinstance(toolkit, str):
        toolkit = getattr(toolkit, "slug", None) or getattr(toolkit, "name", None)
    return {"slug": str(slug or ""), "description": str(description or ""), "toolkit": toolkit}


async def get_connection_status(connected_account_id: str) -> str | None:
    """Composio's current status string for one connected account (e.g.
    'ACTIVE', 'INITIALIZING', 'FAILED', 'EXPIRED', 'REVOKED'), or None if
    Composio isn't configured or the lookup itself failed. Identical in
    shape to email_tools.py's own get_connection_status (this call is
    already toolkit-agnostic -- it looks up by Composio's own
    connected_account_id, not by which app it's for) -- kept as its own
    copy here rather than imported from email_tools.py so this module has
    no dependency on that one, matching the "these stay separate" product
    decision in this module's own docstring. This is the one piece of
    Composio-specific knowledge server.py's generic connection-poll loop
    needs (see _production_app_connection_poll_loop)."""
    try:
        client = _get_client()
    except _NotConfigured:
        return None

    def _get():
        return client.connected_accounts.get(connected_account_id)

    try:
        account = await asyncio.to_thread(_get)
    except Exception as e:  # noqa: BLE001 - the poll loop just retries next cycle
        console.system(f"Composio: connection status lookup failed for {connected_account_id!r}: {e}")
        return None
    return account.status


async def send_connect_link(user: config.UserContext, toolkit_slug: str) -> str:
    """Starts a fresh Composio OAuth connect flow for `toolkit_slug` and
    texts the link straight to `user` -- the SAME steps
    connect_integration_app (the model-facing tool below) performs for a
    normal, first-time, non-switch connect. Factored out as its own
    standalone, non-tool function (rather than reused by having
    connect_integration_app call it, which would touch that already-working
    tool's code path) so it can ALSO be called from two places that have no
    `@tool`-decorated function or model turn in the loop at all: server.py's
    _production_app_connection_poll_loop, sending the NEXT queued app's link
    the moment the CURRENT one finishes connecting (see migrations/032_
    app_connect_queue.sql's header for the full design), and
    queue_app_connections just below, sending the FIRST queued app's link
    immediately.

    Deliberately does NOT handle switch_account (disconnect current, then
    reconnect) -- every caller here is always a first-time connect offer,
    never a switch, so that extra branch (and its own failure modes) simply
    doesn't apply to this path. Yes, this duplicates a chunk of
    connect_integration_app's own body rather than sharing one
    implementation -- an accepted, deliberate trade-off: it keeps this
    change from touching that tool's existing, already-tested code at all,
    which mattered more here than avoiding the duplication. If the two ever
    need to change in lockstep, that's a sign it's worth revisiting."""
    try:
        client = _get_client()
    except _NotConfigured as e:
        return str(e)

    composio_user_id = _composio_user_id(user)
    toolkit_slug = (toolkit_slug or "").strip().lower()
    if not toolkit_slug:
        return "No app named -- nothing to connect."

    def _list_connected_sync() -> set[str]:
        result = client.connected_accounts.list(user_ids=[composio_user_id], statuses=["ACTIVE"])
        items = getattr(result, "items", result)
        connected: set[str] = set()
        for account in items:
            toolkit = getattr(account, "toolkit", None)
            slug = toolkit if isinstance(toolkit, str) else (
                getattr(toolkit, "slug", None) or getattr(toolkit, "name", None)
            )
            if slug:
                connected.add(str(slug).lower())
        return connected

    try:
        live_connected = await asyncio.to_thread(_list_connected_sync)
    except Exception as e:  # noqa: BLE001 - surfaced to the model/caller, not raised
        return f"Couldn't check current connections before starting {toolkit_slug}: {e}"

    cap_result = usage.check_connected_apps_cap(user, len(live_connected))
    if not cap_result.allowed:
        return cap_result.upgrade_message

    def _start_link():
        auth_config_id = _get_or_create_auth_config_id(client, toolkit_slug)
        return client.connected_accounts.link(
            user_id=composio_user_id,
            auth_config_id=auth_config_id,
            callback_url=config.COMPOSIO_CALLBACK_URL,
        )

    from composio import exceptions as composio_exceptions

    try:
        connection_request = await asyncio.to_thread(_start_link)
    except composio_exceptions.ComposioMultipleConnectedAccountsError:
        return f"The user's {toolkit_slug} is already connected."
    except Exception as e:  # noqa: BLE001 - surfaced to the model/caller, not raised
        return f"Couldn't start the {toolkit_slug} connection: {e}"

    await db.create_app_connection_request(user.user_id, toolkit_slug, connection_request.id)

    link = connection_request.redirect_url
    if not link:
        return "Composio didn't return a connect link -- something's misconfigured."

    if user.channel == "cli":
        return f"Connect link (CLI/dev mode -- would normally be texted directly): {link}"

    try:
        await send_message(user.phone_number, f"Connect your {toolkit_slug} here: {link}")
    except SendblueError as e:
        return f"Generated the connect link but couldn't text it (Sendblue error: {e})."
    return f"Sent the {toolkit_slug} connect link."


def build_integration_tools(
    user: config.UserContext, approval_gate: ApprovalGate | None = None
) -> list[BaseTool]:
    composio_user_id = _composio_user_id(user)

    def _search_sync(query: str, toolkit: str | None) -> list[Any]:
        client = _get_client()
        kwargs: dict = {"user_id": composio_user_id, "search": query, "limit": config.INTEGRATION_SEARCH_RESULT_LIMIT}
        if toolkit:
            # Scopes the search server-side to just this one app's action
            # set instead of ranking across all 1,400+ toolkits at once --
            # confirmed as a real, documented Composio SDK parameter
            # (`toolkits=[...]`, combinable with `search=`), not inferred.
            # This is the actual fix for the failure mode a real production
            # run hit: an unscoped query like "reddit" ranked unrelated
            # toolkits' actions (which merely mention "Reddit" in their own
            # descriptions) ahead of the real Reddit action -- see README.
            kwargs["toolkits"] = [toolkit.lower()]
        result = client.tools.get(**kwargs)
        # Composio's own examples show tools.get returning either a plain
        # list or a wrapper with an .items/.tools attribute depending on
        # SDK version -- handled defensively for the same reason as
        # _extract_tool_fields above.
        if isinstance(result, list):
            return result
        return getattr(result, "items", None) or getattr(result, "tools", None) or list(result)

    def _connected_toolkits_sync(toolkit_slugs: list[str]) -> set[str]:
        client = _get_client()
        result = client.connected_accounts.list(
            user_ids=[composio_user_id], statuses=["ACTIVE"],
        )
        items = getattr(result, "items", result)
        connected: set[str] = set()
        for account in items:
            toolkit = getattr(account, "toolkit", None)
            if isinstance(toolkit, str):
                connected.add(toolkit.lower())
            elif toolkit is not None:
                slug = getattr(toolkit, "slug", None) or getattr(toolkit, "name", None)
                if isinstance(slug, str):
                    connected.add(slug.lower())
            raw_slug = getattr(account, "toolkit_slug", None) or getattr(account, "appName", None)
            if isinstance(raw_slug, str):
                connected.add(raw_slug.lower())
        return connected

    @tool
    async def search_integration_tools(query: str, toolkit: str | None = None) -> str:
        """Search for a tool/action across the apps Composio supports
        (1,400+ toolkits: Todoist, Slack, Notion, GitHub, Instagram, and
        more) that could satisfy something the user asked for that none of
        Messa's OWN native subagents cover -- e.g. "add a task to my
        Todoist", "post this in our #general Slack channel". Read-only,
        no side effects -- safe to call just to check what's available or
        whether an app is already connected.

        toolkit: if you already know which app this is for (the user named
        it, e.g. "Reddit", "Slack", "Todoist" -- or an earlier search
        result already told you), PASS IT HERE (lowercase, e.g. 'reddit').
        This scopes the search to just that app's own actions instead of
        ranking across every app Composio supports, which matters more
        than how you phrase the query -- an unscoped search for a generic
        word like a bare app name can rank unrelated apps' actions (that
        merely mention that word in their own description) ahead of the
        real one. Only leave this unset when you genuinely don't know the
        app yet and are searching to find out.

        Returns a short list of candidate tool slugs with descriptions,
        each marked as either already connected (pass its exact slug to
        execute_integration_tool) or not yet connected (call
        connect_integration_app with that app's name to send the user a
        connect link -- don't do this automatically on every search, only
        when the user actually wants to use that app).

        If nothing matches at all, that MIGHT mean this isn't an app
        Composio supports -- but it can also just mean this phrasing or
        toolkit scope didn't hit. If you're confident the app should exist
        (e.g. it's already connected, or a broader unscoped search found
        it under a different toolkit name), try again before concluding
        it's unsupported. If you're still getting nothing, tell the user
        plainly and offer web browser automation instead (delegate to
        deepsearch for that)."""
        try:
            results = await asyncio.to_thread(_search_sync, query, toolkit)
        except _NotConfigured as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return f"Search failed: {e}. Try a different/simpler query, or tell the user it didn't work."

        if not results:
            await db.log_unsupported_integration_request(user.user_id, query[:128], query)
            scope_note = f" (scoped to toolkit {toolkit!r})" if toolkit else ""
            return (
                f"No candidate tool matched {query!r}{scope_note}. This might mean the app isn't "
                "supported by Composio yet, OR that this query/toolkit scope just didn't match --  "
                "if you're fairly sure the app exists (the user named it, or it showed up in an "
                "earlier search), try again with a different phrasing or without the toolkit "
                "filter before concluding it's unsupported. If you're still getting nothing after "
                "that, tell the user plainly and offer browser automation instead (delegate to "
                "deepsearch for that). This request has been logged for the product team either way."
            )

        parsed = [_extract_tool_fields(r) for r in results]
        toolkit_slugs = sorted({_guess_toolkit_slug(p["slug"], p["toolkit"]) for p in parsed if p["slug"]})
        try:
            connected = await asyncio.to_thread(_connected_toolkits_sync, toolkit_slugs)
        except Exception as e:  # noqa: BLE001 - connection status is a nice-to-have here, not fatal
            console.system(f"Composio: connected-accounts lookup failed during search: {e}")
            connected = set()

        lines = [f"{len(parsed)} candidate tool(s) for {query!r}:"]
        for p in parsed:
            toolkit_slug = _guess_toolkit_slug(p["slug"], p["toolkit"])
            status = "CONNECTED" if toolkit_slug in connected else "NOT CONNECTED"
            desc = f" -- {p['description']}" if p["description"] else ""
            lines.append(f"- {p['slug']} [{toolkit_slug}, {status}]{desc}")
        return "\n".join(lines)

    @tool
    async def connect_integration_app(toolkit_slug: str, switch_account: bool = False) -> str:
        """Generate a fresh OAuth connect link for the given app (e.g.
        'todoist', 'slack', 'notion' -- use the toolkit name search_
        integration_tools showed as NOT CONNECTED) and send it to the user
        directly as its own text message right now. Do NOT try to relay
        the link yourself -- it's sent automatically; this tool's return
        value tells you what to say instead. Only call this when the user
        actually wants to connect that app, not automatically after every
        search.

        switch_account: set True when the user explicitly wants to
        DISCONNECT the app's current connection and connect a different
        account instead (e.g. "switch my Google Calendar to a different
        Google account", "use my other Todoist"). This actually disconnects
        the old connected account first (real, immediate: Composio's own
        connected_accounts.delete, not a manual step for the user) and then
        sends a fresh connect link, so the OAuth screen lets them pick a
        different account. There is no dashboard/settings page for the user
        to do this themselves -- this tool IS the disconnect. Leave this
        False for a normal first-time connect; if that raises "already
        connected" and the user didn't ask to switch, just tell them it's
        already set up rather than calling this again."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        if switch_account:
            existing = await db.get_active_app_connection(user.user_id, toolkit_slug)
            if existing and existing.get("connected_account_id"):

                def _delete_sync():
                    client.connected_accounts.delete(
                        existing["connected_account_id"], revoke_on_delete=True,
                    )

                try:
                    await asyncio.to_thread(_delete_sync)
                except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
                    return (
                        f"Couldn't disconnect the current {toolkit_slug} connection to switch "
                        f"accounts: {e}. Tell the user it didn't work and to try again shortly -- "
                        "don't tell them to do it manually anywhere, there's no such page."
                    )
                await db.disconnect_app_connection(existing["id"])

        # max_connected_apps is a standing ceiling, not a daily rate --
        # checked against Composio's own LIVE connected-accounts count
        # (which already covers Gmail too, since it's connected through
        # this same composio_user_id), never a cached/local number, so a
        # stale row can't let someone slip past the cap or get wrongly
        # blocked. Checked here, after any switch_account disconnect above
        # already freed a slot, so a same-toolkit reconnect never counts
        # against the user twice.
        live_connected = await asyncio.to_thread(_connected_toolkits_sync, [])
        cap_result = usage.check_connected_apps_cap(user, len(live_connected))
        if not cap_result.allowed:
            return cap_result.upgrade_message

        def _start_link():
            auth_config_id = _get_or_create_auth_config_id(client, toolkit_slug)
            return client.connected_accounts.link(
                user_id=composio_user_id,
                auth_config_id=auth_config_id,
                callback_url=config.COMPOSIO_CALLBACK_URL,
            )

        from composio import exceptions as composio_exceptions

        try:
            connection_request = await asyncio.to_thread(_start_link)
        except composio_exceptions.ComposioMultipleConnectedAccountsError:
            return (
                f"The user's {toolkit_slug} is already connected. If they want to switch to a "
                f"different account, call connect_integration_app('{toolkit_slug}', "
                "switch_account=True) -- that disconnects the current one and sends a fresh link "
                "immediately, no manual step needed anywhere. Otherwise just let them know it's "
                "already set up."
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return f"Couldn't start the {toolkit_slug} connection: {e}"

        await db.create_app_connection_request(user.user_id, toolkit_slug, connection_request.id)

        link = connection_request.redirect_url
        if not link:
            return (
                "Composio didn't return a connect link -- something's misconfigured "
                "(check COMPOSIO_API_KEY). Tell the user it didn't work and to try again shortly."
            )

        if user.channel == "cli":
            # No real phone to text in local/dev use -- surface the link directly,
            # same as email_tools.py's request_email_connection does.
            return f"Connect link (CLI/dev mode -- would normally be texted directly): {link}"

        try:
            await send_message(user.phone_number, f"Connect your {toolkit_slug} here: {link}")
        except SendblueError as e:
            return (
                f"Generated the connect link but couldn't text it (Sendblue error: {e}). "
                "Tell the user to ask again in a moment."
            )
        return (
            f"Sent. The {toolkit_slug} connect link just went out to the user as its own text "
            "message -- don't repeat the URL yourself. Just tell them to check their messages, "
            "and that you'll let them know once it's connected."
        )

    @tool
    async def queue_app_connections(toolkit_slugs: list[str]) -> str:
        """The user just named SEVERAL apps they want connected at once
        (e.g. answering Messa's own 'what apps do you use day to day'
        onboarding question, or unprompted -- 'connect Slack, Notion, and
        Todoist'). Resolve each one to its Composio toolkit slug yourself
        (same resolution you'd already do for a single connect_
        integration_app call -- e.g. 'gmail', 'slack', 'notion', 'todoist',
        'googlecalendar') and pass the whole list here in ONE call, instead
        of calling connect_integration_app once per app.

        Sends the FIRST app's connect link right away, exactly like
        connect_integration_app would, and queues the rest to be sent
        automatically, one at a time, each only once the previous one
        actually finishes connecting -- so the user gets one OAuth link to
        deal with at a time instead of being handed several at once. Tell
        the user their first link just went out and that you'll send the
        next one once that's connected -- don't repeat any URL yourself.

        For a single app, just use connect_integration_app directly -- this
        tool is specifically for a LIST of more than one."""
        seen: set[str] = set()
        slugs: list[str] = []
        for raw in toolkit_slugs or []:
            slug = (raw or "").strip().lower()
            if slug and slug not in seen:
                seen.add(slug)
                slugs.append(slug)
        if not slugs:
            return "Nothing to queue -- no app names came through."

        first, rest = slugs[0], slugs[1:]
        if rest:
            await db.set_pending_app_connect_queue(user.user_id, rest)
        first_result = await send_connect_link(user, first)
        if rest:
            return (
                f"{first_result} Queued the rest ({', '.join(rest)}) to send automatically, one "
                "at a time, as each previous one finishes connecting."
            )
        return first_result

    @tool
    async def disconnect_integration_app(toolkit_slug: str) -> str:
        """Disconnect the user's currently-connected account for this app
        (e.g. 'googlecalendar', 'todoist') WITHOUT connecting a new one --
        for when the user just wants it off, not switched. (For "switch to
        a different account", prefer connect_integration_app(toolkit_slug,
        switch_account=True) instead -- one call, disconnect + fresh link
        together.) There's no dashboard/settings page for the user to do
        this themselves; this tool IS the disconnect, right now.

        Tells Composio to revoke the underlying OAuth grant too, but that
        revocation runs as Composio's own background job with no way for
        Messa to confirm it finished -- so say the disconnection is done
        (Messa's own side, and future requests, immediately stop using it),
        and that revoking the app's own access is in progress, not confirmed
        complete on the spot."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        existing = await db.get_active_app_connection(user.user_id, toolkit_slug)
        if not existing or not existing.get("connected_account_id"):
            return f"The user's {toolkit_slug} isn't currently connected -- nothing to disconnect."

        def _delete_sync():
            client.connected_accounts.delete(
                existing["connected_account_id"], revoke_on_delete=True,
            )

        try:
            await asyncio.to_thread(_delete_sync)
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return f"Couldn't disconnect {toolkit_slug}: {e}. Tell the user it didn't work and to try again shortly."

        await db.disconnect_app_connection(existing["id"])
        return (
            f"Done -- the user's {toolkit_slug} is disconnected on Messa's side; she won't use it "
            "for anything going forward. Revoking the app's own access on Google's/etc.'s side is "
            "in progress in the background (not something you can confirm finished right now) -- "
            "tell them it's disconnected, and that fully revoking access may take a short moment "
            "on the provider's end if they check there."
        )

    def _is_switch_account_call(*args: Any, **kwargs: Any) -> bool:
        """connect_integration_app is only destructive on the branch that
        actually disconnects an existing account first -- a normal
        first-time connect (switch_account left False/default) still needs
        no confirmation, same as before this feature existed."""
        if "switch_account" in kwargs:
            return bool(kwargs["switch_account"])
        return len(args) > 1 and bool(args[1])

    def _always_destructive(*args: Any, **kwargs: Any) -> bool:
        return True

    def _is_write_action(*args: Any, **kwargs: Any) -> bool:
        slug = (kwargs.get("slug") or (args[0] if args else "")) or ""
        slug_upper = str(slug).upper()
        return any(keyword in slug_upper for keyword in config.INTEGRATION_WRITE_ACTION_KEYWORDS)

    @tool
    async def describe_integration_tool(slug: str) -> str:
        """Look up the REAL parameter schema for one Composio tool/action by
        its exact slug (from search_integration_tools' results). Call this
        BEFORE execute_integration_tool if a previous call to this exact
        slug already failed with a parameter/argument-shaped error, or if
        search_integration_tools' one-line description wasn't enough to
        know what `arguments` this action actually expects -- don't guess
        the argument shape a second time when you can just look it up.
        Read-only, no side effects."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        def _describe_sync() -> Any:
            return client.tools.get_raw_composio_tool_by_slug(slug)

        try:
            tool_obj = await asyncio.to_thread(_describe_sync)
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return f"Couldn't look up the schema for {slug!r}: {e}."
        schema = getattr(tool_obj, "input_parameters", None) or {}
        desc = getattr(tool_obj, "description", "") or ""
        return f"{slug}: {desc}\nParameters (JSON schema): {json.dumps(schema, default=str)}"

    @tool
    async def execute_integration_tool(slug: str, arguments: dict) -> str:
        """Run one specific Composio tool/action by its exact slug (from
        search_integration_tools' results -- e.g. 'TODOIST_CREATE_TASK'),
        with `arguments` as the keyword arguments that action needs (check
        the description search_integration_tools returned for what it
        expects, or call describe_integration_tool for the real schema).
        Only works for an app the user has already connected -- if it's
        not connected, use connect_integration_app first.

        Read-only actions (list/get/fetch/search) run immediately.
        Anything that creates, updates, deletes, sends, posts, or
        otherwise changes state on the user's behalf requires their
        confirmation first, same as every other irreversible action in
        this app."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        # execute_integration_tool is one generic dispatcher for 1,400+
        # apps' worth of actions -- there's no separate "send email" tool
        # to tag the way email_tools.py/personal_inbox_tools.py do, so the
        # usage-limits check has to live here, keyed on the incoming slug
        # itself. Only a 3rd-party email SEND slug is metered; every other
        # action (Todoist, Slack, Notion, read-only Outlook calls, ...)
        # passes through unmetered, same as today.
        if usage.slug_is_email_send(slug):
            limit_result = await usage.check_and_consume(user, usage.FEATURE_OUTBOUND_EMAILS)
            if not limit_result.allowed:
                return limit_result.upgrade_message

        # Entity & Workspace Auto-Discovery (docs/executive_agent_
        # architecture_proposal.md 3.B, tools/workspace_asset_tools.py):
        # if this slug needs a default id (e.g. AIRTABLE_CREATE_BASE's
        # workspace_id) and this connection predates the feature (or was
        # never seen by server.py's connection-poll loop, e.g. CLI/dev
        # use), probe for it now, once -- idempotent and non-fatal either
        # way, see discover_and_cache_app_entities' own docstring. Then
        # silently fill in whatever's cached and still missing from
        # `arguments`, never overriding a value already supplied.
        toolkit_needing_entity = toolkit_for_injection_target(slug)
        if toolkit_needing_entity and config.WORKSPACE_ASSETS_ENABLED:
            await discover_and_cache_app_entities(user.user_id, toolkit_needing_entity, client=client)
        arguments = await apply_cached_entity_defaults(user, slug, arguments)

        def _execute_sync() -> Any:
            kwargs: dict = {"slug": slug, "arguments": arguments, "user_id": composio_user_id}
            if config.COMPOSIO_TOOLKIT_VERSION:
                kwargs["version"] = config.COMPOSIO_TOOLKIT_VERSION
            else:
                kwargs["dangerously_skip_version_check"] = True
            return client.tools.execute(**kwargs)

        try:
            result = await asyncio.to_thread(_execute_sync)
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            # Self-healing: before surfacing the raw error, check whether
            # this agent (or another user's, since agent_skills is GLOBAL --
            # see tools/scratchpad_tools.py) already learned a fix for this
            # toolkit. Code-triggered, not voluntary -- the very first retry
            # attempt already has any previously-learned lesson, instead of
            # depending on the model remembering to call search_skills
            # itself. Reuses _guess_toolkit_slug (the same heuristic
            # search_integration_tools already uses to label results) and
            # the exact agent_skills table/db.search_skills function
            # tools/scratchpad_tools.py's Active Task Scratchpad + Skills
            # Playbook feature already shipped -- zero new tables.
            hint = ""
            if config.SCRATCHPAD_AND_SKILLS_ENABLED:
                try:
                    toolkit_guess = _guess_toolkit_slug(slug, None)
                    hits = await db.search_skills("integrations_agent", toolkit_guess, limit=2)
                    if hits:
                        lessons = "; ".join(h["solution_recipe"] for h in hits)
                        hint = f" Known lessons for {toolkit_guess!r}: {lessons}"
                except Exception:
                    pass  # a skills lookup must never be why the real error doesn't get reported
            return (
                f"'{slug}' failed: {e}.{hint} Don't retry with the exact same arguments -- "
                f"call describe_integration_tool({slug!r}) to check the real parameter schema, "
                "or adjust your arguments."
            )
        # Persistent Workspace Asset Registry (3.A): a no-op for the vast
        # majority of slugs (classify_asset_creation returns None for
        # anything that isn't a recognizable "made a workspace asset"
        # call) -- see record_asset_from_execution's own docstring for why
        # this is safe to call unconditionally and unawaited-for-failure
        # right here rather than gating it on a hand-maintained slug list.
        await record_asset_from_execution(user, slug, arguments, result)
        return str(result)

    raw_tools: list[BaseTool] = [
        search_integration_tools, describe_integration_tool, connect_integration_app,
        queue_app_connections, disconnect_integration_app, execute_integration_tool,
        build_workspace_asset_tool(user, _get_client, composio_user_id),
    ]

    def _destructive_check_for(name: str) -> Callable[..., bool] | None:
        # connect_integration_app isn't gated on a plain first-time connect
        # -- same as email_tools.py's request_email_connection: generating/
        # sending a connect link changes nothing on the user's behalf, so
        # there's nothing to confirm. It IS gated the one time it's actually
        # destructive: switch_account=True, which disconnects a real,
        # currently-working connection before reconnecting. disconnect_
        # integration_app is always destructive -- it only ever disconnects.
        # execute_integration_tool's actual write-shaped calls need approval,
        # decided dynamically per slug.
        if name == "execute_integration_tool":
            return _is_write_action
        if name == "create_workspace_asset":
            # Always destructive -- it calls the underlying Composio client
            # directly (bypassing execute_integration_tool's own dynamic
            # _is_write_action gating), and every one of its tiers creates
            # a brand-new real resource (an Airtable base, a Google Sheet,
            # or a PDF), so it needs the exact same confirm-before-create
            # rule execute_integration_tool's write actions already get.
            return _always_destructive
        if name == "connect_integration_app":
            return _is_switch_account_call
        if name == "disconnect_integration_app":
            return _always_destructive
        return None

    # search_web/read_webpage (feature/agentic-upgrade, part of the "Rule of
    # 3" self-healing ladder -- see integration_circuit_breaker.py's module
    # docstring): tier 2/3 of that ladder tell the model to look up the real
    # parameter format before a third guess, which was previously an empty
    # suggestion -- integrations_agent had no doc-lookup tool of any kind.
    # Reuses the SAME function the orchestrator's own build_orchestrator
    # already calls (registry.py) -- already traced/labeled internally by
    # build_web_search_tools itself, so no double-wrapping here.
    return [
        trace_tool(
            t, LABEL,
            destructive_check=_destructive_check_for(t.name),
            approval_gate=approval_gate,
        )
        for t in raw_tools
    ] + build_web_search_tools()


def build_integration_system_prompt(user: config.UserContext) -> str:
    return (
        "You give Messa access to any of Composio's 1,400+ app integrations (Reddit, Todoist, "
        "Slack, Notion, GitHub, Instagram, Google Calendar, and more) that aren't one of her "
        "own native subagents. Messa delegates to you when a request needs an app she doesn't "
        "already have a dedicated tool for (NOT for email -- that's always email_agent/"
        "personal_inbox_agent, never this. Google Calendar IS handled here, though -- it's a "
        "real external calendar via Composio's 'googlecalendar' toolkit, not the same thing as "
        "executive_assistant's own internal calendar_events, which you have no access to and "
        "shouldn't try to reconcile with).\n"
        "- search_integration_tools(query, toolkit=None) first, always -- read-only, tells you "
        "what's available and whether it's already connected. Don't guess a slug without "
        "searching.\n"
        "- If you already know which app this is for (the user named it -- 'Reddit', 'Slack', "
        "'Todoist' -- or an earlier search already told you), ALWAYS pass toolkit='<app>' "
        "(lowercase). This scopes the search to just that app's own actions instead of ranking "
        "across every app Composio supports, and matters far more than how you phrase the query "
        "-- an unscoped search for a bare app name can rank OTHER apps' actions (that merely "
        "mention that word in their own description) ahead of the one you actually want. Only "
        "search unscoped when you genuinely don't know the app yet.\n"
        "- If the app isn't connected yet: tell the user, and only call "
        "connect_integration_app(toolkit_slug) if they actually want to connect it right now -- "
        "don't send a connect link unprompted just because a search turned one up.\n"
        "- If the user names SEVERAL apps to connect at once (e.g. answering 'what apps do you "
        "use' with a list), do NOT call connect_integration_app once per app -- that fires every "
        "OAuth link at the same time and buries them. Resolve each name to its Composio toolkit "
        "slug yourself (search_integration_tools if any are unclear) and call "
        "queue_app_connections(toolkit_slugs) ONCE with the whole list. It sends the first app's "
        "link immediately and queues the rest to go out automatically, one at a time, as each "
        "previous one finishes connecting.\n"
        "- If the app IS already connected and the user wants a DIFFERENT account instead "
        "('switch my Google Calendar to my other account', 'use my other Todoist'), call "
        "connect_integration_app(toolkit_slug, switch_account=True) -- it disconnects the "
        "current account and sends a fresh link in one step. There is no settings/integrations "
        "page for the user to disconnect it themselves first; never say there is or ask them to "
        "do that -- this tool call IS the disconnect.\n"
        "- If the user just wants an app disconnected with no intent to reconnect ('disconnect "
        "my Todoist', 'stop using my Slack'), call disconnect_integration_app(toolkit_slug). "
        "Either way, be accurate about what happened: the disconnect on Messa's own side is "
        "immediate and confirmed; revoking the app's own access is a background job you can't "
        "confirm finished on the spot, so say revocation is in progress, not that it's done.\n"
        "- execute_integration_tool(slug, arguments) to actually run something, using the exact "
        "slug and expected arguments search_integration_tools showed you. Read-only actions run "
        "immediately; anything that changes state will prompt the user for confirmation "
        "automatically -- you don't need to ask yourself first.\n"
        "- If search_integration_tools finds nothing, that MIGHT mean the app isn't supported -- "
        "but it can also just mean this phrasing or toolkit scope didn't hit. If you're fairly "
        "confident the app should exist, try again (a different phrasing, or without the "
        "toolkit filter) before concluding it's unsupported and reporting that back to Messa.\n"
        "- If execute_integration_tool fails, don't just guess a different argument shape and "
        "try again blindly: call describe_integration_tool(slug) for the real parameter schema, "
        "search_skills(toolkit) for a lesson someone already learned, or search_web(...)/"
        "read_webpage(...) to look up the app's real API docs -- in that order of preference. "
        "Repeated failures on the same slug get blocked after a few attempts specifically to "
        "force this instead of a fourth blind guess; when that happens, stop and tell the user "
        "plainly what's blocking this rather than trying again or claiming it worked.\n"
        "- create_workspace_asset(title, headers=None, rows=None): when the user just wants 'a "
        "list/sheet/table of X' and doesn't care which specific app it lives in, prefer this over "
        "manually chaining search_integration_tools/execute_integration_tool calls yourself -- it "
        "already tries Airtable, then Google Sheets, then falls back to a PDF table on its own, so "
        "the user always gets a real deliverable. Use execute_integration_tool directly instead "
        "when they named a specific app, or asked for something more specific than a simple table "
        "(formulas, formatting, sharing permissions, appending to an existing sheet, ...). Whatever "
        "it made, tell the user plainly what and where -- never mention which tier(s) it tried or "
        "failed first; that's plumbing, not something they asked about.\n"
        "Be concise in what you report back -- Messa relays your summary as a text message, not "
        "your raw tool output.\n"
    )


def build_integration_subagent(
    user: config.UserContext,
    model: BaseChatModel,
    description: str,
    approval_gate: ApprovalGate | None = None,
) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec (feature/agentic-upgrade
    plan) -- same shape and same reasoning as executive_tools.py's
    build_executive_subagent / email_tools.py's build_email_subagent.
    integrations_agent used to be registered as a plain declarative
    `SubAgent` dict straight in agents/registry.py, which meant its
    system_prompt (and therefore its Active Task Scratchpad `task_block`,
    see tools/scratchpad_tools.py) was built ONCE, at build_orchestrator's
    start, before the turn's own tool calls run. Concretely: if this exact
    subagent creates a spreadsheet and saves its id via
    update_task_scratchpad, then Messa delegates to it (or a different
    subagent) again LATER IN THE SAME TURN, that later delegation's frozen
    prompt predates the write -- the id is invisible to it. Wrapping this
    as a CompiledSubAgent (which builds its tools/prompt fresh inside its
    own `_run`, at actual delegation time) fixes that, matching
    deepsearch/executive_assistant/email_agent's existing behavior exactly.

    Also carries this subagent's own step budget and "Rule of 3" tool-
    failure ladder (see tools/integration_circuit_breaker.py's module
    docstring) as middleware on its OWN inner create_agent call, moved here
    from registry.py's old declarative "middleware": [...] key (deepagents'
    `middleware=` kwarg on create_agent works identically either way --
    confirmed additive, not a restructuring).

    `description` is passed in (rather than hardcoded here, unlike the
    other CompiledSubAgent builders) because it's genuinely dynamic per
    turn: registry.py's `_integrations_agent_description(connected_slugs,
    ...)` bakes in which apps are currently connected, computed once in
    build_orchestrator from a cheap local DB read -- moving that
    computation in here would mean either a DB read inside every _run
    (redundant, and this one specifically needs to stay fast per its own
    docstring) or duplicating build_orchestrator's connected_slugs fetch."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state["messages"])
        tools = build_integration_tools(user, approval_gate)
        system_prompt = build_integration_system_prompt(user) + reliability.RELIABILITY_GUARDRAIL_STR
        if config.SCRATCHPAD_AND_SKILLS_ENABLED:
            tools = tools + build_scratchpad_tools(user, "integrations_agent", approval_gate)
            system_prompt = system_prompt + await scratchpad_prompt_block(user)
        run_config = {"recursion_limit": config.RECURSION_LIMIT}
        inner_agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[
                ModelCallLimitMiddleware(
                    run_limit=config.INTEGRATIONS_AGENT_MAX_MODEL_CALLS, exit_behavior="end"
                ),
                ToolFailureLadderMiddleware(
                    "execute_integration_tool",
                    identity_fn=integration_slug_identity,
                    lookup_hint=(
                        "call describe_integration_tool(slug), search_skills(toolkit), or "
                        "search_web(...)/read_webpage(...) for the real parameter format"
                    ),
                ),
                # create_workspace_asset already runs its own internal
                # Airtable -> Google Sheets -> PDF waterfall and never
                # raises/claims false success -- this just stops the MODEL
                # from repeatedly re-calling it with cosmetic title changes
                # believing a retry might reach a different app, once it's
                # already told plainly (via its own return string) that the
                # PDF fallback is what it got.
                ToolFailureLadderMiddleware("create_workspace_asset"),
            ],
        )
        final_messages = await run_inner_agent_with_claim_check(
            inner_agent, messages, run_config, label="integrations_agent"
        )
        return {"messages": [AIMessage(content=last_ai_text(final_messages))]}

    return {
        "name": "integrations_agent",
        "description": description,
        "runnable": RunnableLambda(_run),
    }
