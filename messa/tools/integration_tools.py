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
`client.auth_configs.list/create(...)`. The one call this file uses that
email_tools.py does NOT -- `client.tools.get(user_id=..., search=...,
limit=...)`, Composio's own tool-search API -- is NOT independently
verified against a live account from this sandbox (no COMPOSIO_API_KEY,
no network to Composio here); Composio's own docs describe it, but mark it
"(experimental)", and the exact shape of what it returns per result
(attribute names for slug/toolkit/description) is inferred from those
docs, not confirmed against a real response. _extract_tool_fields below is
written defensively (tries several plausible shapes) for exactly that
reason -- flagged here and in the README as the one piece of this feature
worth a real smoke-test against a live Composio account before trusting
it fully in production.

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
    shape. Handles both an object with attributes and a plain dict."""
    def _get(key: str) -> str | None:
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    slug = _get("slug") or _get("name") or _get("action") or ""
    description = _get("description") or _get("summary") or ""
    toolkit = _get("toolkit_slug") or _get("toolkit") or _get("app") or _get("appName")
    if isinstance(toolkit, dict):
        toolkit = toolkit.get("slug") or toolkit.get("name")
    elif toolkit is not None and not isinstance(toolkit, str):
        toolkit = getattr(toolkit, "slug", None) or getattr(toolkit, "name", None)
    return {"slug": str(slug), "description": str(description), "toolkit": toolkit}


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

    def _search_sync(query: str) -> list[Any]:
        client = _get_client()
        kwargs: dict = {"user_id": composio_user_id, "search": query, "limit": config.INTEGRATION_SEARCH_RESULT_LIMIT}
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
            user_ids=[composio_user_id], toolkit_slugs=toolkit_slugs, statuses=["ACTIVE"],
        )
        items = getattr(result, "items", result)
        connected: set[str] = set()
        for account in items:
            slug = getattr(account, "toolkit_slug", None) or getattr(account, "toolkit", None)
            if isinstance(slug, str):
                connected.add(slug.lower())
        return connected

    @tool
    async def search_integration_tools(query: str) -> str:
        """Search for a tool/action across every app Composio supports
        (1,400+ toolkits: Todoist, Slack, Notion, GitHub, Instagram, and
        more) that could satisfy something the user asked for that none of
        Messa's OWN native subagents cover -- e.g. "add a task to my
        Todoist", "post this in our #general Slack channel". Read-only,
        no side effects -- safe to call just to check what's available or
        whether an app is already connected.

        Returns a short list of candidate tool slugs with descriptions,
        each marked as either already connected (pass its exact slug to
        execute_integration_tool) or not yet connected (call
        connect_integration_app with that app's name to send the user a
        connect link -- don't do this automatically on every search, only
        when the user actually wants to use that app).

        If nothing matches at all, this almost certainly isn't an app
        Composio supports -- tell the user plainly, mention you can try to
        do it via web browser automation instead (delegate to deepsearch
        for that), and this gets logged so the team knows there's demand
        for it."""
        try:
            results = await asyncio.to_thread(_search_sync, query)
        except _NotConfigured as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return f"Search failed: {e}. Try a different/simpler query, or tell the user it didn't work."

        if not results:
            await db.log_unsupported_integration_request(user.user_id, query[:128], query)
            return (
                f"No Composio-supported app/tool matched {query!r}. This most likely isn't an "
                "app Composio supports yet -- tell the user plainly, and offer to try it via "
                "browser automation instead (delegate to deepsearch for that). This request has "
                "been logged for the product team."
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
        "You give Messa access to any of Composio's 1,400+ app integrations (Todoist, Slack, "
        "Notion, GitHub, Instagram, and more) that aren't one of her own native subagents. "
        "Messa delegates to you when a request needs an app she doesn't already have a "
        "dedicated tool for (NOT for email -- that's always email_agent/personal_inbox_agent, "
        "never this).\n"
        "- search_integration_tools(query) first, always -- read-only, tells you what's "
        "available and whether it's already connected. Don't guess a slug without searching.\n"
        "- If the app isn't connected yet: tell the user, and only call "
        "connect_integration_app(toolkit_slug) if they actually want to connect it right now -- "
        "don't send a connect link unprompted just because a search turned one up.\n"
        "- execute_integration_tool(slug, arguments) to actually run something, using the exact "
        "slug and expected arguments search_integration_tools showed you. Read-only actions run "
        "immediately; anything that changes state will prompt the user for confirmation "
        "automatically -- you don't need to ask yourself first.\n"
        "- If search_integration_tools finds nothing at all, that app almost certainly isn't "
        "supported -- report that plainly back to Messa (she can offer browser automation via "
        "deepsearch instead) rather than pretending it worked.\n"
        "Be concise in what you report back -- Messa relays your summary as a text message, not "
        "your raw tool output.\n"
    )
