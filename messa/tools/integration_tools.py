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
from typing import Any

from langchain_core.tools import BaseTool, tool

from .. import config, console, db
from ..approval import ApprovalGate
from ..channels.sendblue import SendblueError, send_message
from .common import trace_tool

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
    async def connect_integration_app(toolkit_slug: str) -> str:
        """Generate a fresh OAuth connect link for the given app (e.g.
        'todoist', 'slack', 'notion' -- use the toolkit name search_
        integration_tools showed as NOT CONNECTED) and send it to the user
        directly as its own text message right now. Do NOT try to relay
        the link yourself -- it's sent automatically; this tool's return
        value tells you what to say instead. Only call this when the user
        actually wants to connect that app, not automatically after every
        search."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

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
                f"The user's {toolkit_slug} is already connected -- no need to send another "
                "link. Just let them know it's already set up."
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

    def _is_write_action(*args: Any, **kwargs: Any) -> bool:
        slug = (kwargs.get("slug") or (args[0] if args else "")) or ""
        slug_upper = str(slug).upper()
        return any(keyword in slug_upper for keyword in config.INTEGRATION_WRITE_ACTION_KEYWORDS)

    @tool
    async def execute_integration_tool(slug: str, arguments: dict) -> str:
        """Run one specific Composio tool/action by its exact slug (from
        search_integration_tools' results -- e.g. 'TODOIST_CREATE_TASK'),
        with `arguments` as the keyword arguments that action needs (check
        the description search_integration_tools returned for what it
        expects). Only works for an app the user has already connected --
        if it's not connected, use connect_integration_app first.

        Read-only actions (list/get/fetch/search) run immediately.
        Anything that creates, updates, deletes, sends, posts, or
        otherwise changes state on the user's behalf requires their
        confirmation first, same as every other irreversible action in
        this app."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

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
            return f"'{slug}' failed: {e}. Don't retry with the exact same arguments."
        return str(result)

    raw_tools: list[BaseTool] = [
        search_integration_tools, connect_integration_app, execute_integration_tool,
    ]
    return [
        trace_tool(
            t, LABEL,
            # connect_integration_app isn't gated -- same as email_tools.py's
            # request_email_connection: generating/sending a connect link
            # changes nothing on the user's behalf, so there's nothing to
            # confirm. Only execute_integration_tool's actual write-shaped
            # calls need approval, decided dynamically per slug below.
            destructive_check=_is_write_action if t.name == "execute_integration_tool" else None,
            approval_gate=approval_gate,
        )
        for t in raw_tools
    ]


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
        "- execute_integration_tool(slug, arguments) to actually run something, using the exact "
        "slug and expected arguments search_integration_tools showed you. Read-only actions run "
        "immediately; anything that changes state will prompt the user for confirmation "
        "automatically -- you don't need to ask yourself first.\n"
        "- If search_integration_tools finds nothing, that MIGHT mean the app isn't supported -- "
        "but it can also just mean this phrasing or toolkit scope didn't hit. If you're fairly "
        "confident the app should exist, try again (a different phrasing, or without the "
        "toolkit filter) before concluding it's unsupported and reporting that back to Messa.\n"
        "Be concise in what you report back -- Messa relays your summary as a text message, not "
        "your raw tool output.\n"
    )
