"""Shared helpers behind three related roadmap items
(docs/executive_agent_architecture_proposal.md sections 3.A/3.B/4.A),
all built on top of feature/agentic-upgrade's reliability work and
migration 037's user_assets/user_app_entities tables:

  - Persistent Workspace Asset Registry (3.A): record_asset_from_execution
    is called from tools/integration_tools.py's execute_integration_tool
    right after a successful Composio call, and from
    tools/document_tools.py right after a successful PDF generation, so a
    created/touched asset survives past the active_tasks row that
    created it (see migration 037's own header for why that's a real gap
    active_tasks/agent_skills don't cover).
  - Entity & Workspace Auto-Discovery (3.B): a small, explicit,
    per-toolkit ENTITY_PROBES registry plus discover_and_cache_app_entities
    (probe once, cache in user_app_entities) and
    apply_cached_entity_defaults (silently fill in a missing parameter
    from that cache) -- so Messa never has to ask the user for something
    like "what's your Airtable workspace ID".
  - Autonomous Fallback Waterfall (4.A): create_workspace_asset, a single
    tool that tries Airtable, then Google Sheets, then falls back to a
    locally-generated PDF table -- so "make me a list of X" always ends
    in a real, shareable deliverable instead of a dead end when one app
    isn't connected/configured, translated into one plain sentence per
    the "Clean Air Interface" rule (never raw tool/API error text -- see
    reliability.RELIABILITY_GUARDRAIL_STR, which this rule is a specific
    instance of).

Honesty note on Composio slugs: unlike the rest of this module (and
db.py), the actual third-party action/probe slugs named in
ENTITY_PROBES/_WATERFALL_TIERS below (e.g. 'AIRTABLE_LIST_WORKSPACES')
are BEST-EFFORT GUESSES following Composio's documented TOOLKIT_VERB_NOUN
naming convention (see tools/integration_tools.py's own
_guess_toolkit_slug docstring for the same, already-accepted caveat in
this codebase) -- this file has no live Composio credential to verify
them against. Every call site here is wrapped to degrade gracefully
(cache miss / try-the-next-tier / plain "couldn't create it" message)
if a guessed slug turns out to be wrong, so a bad guess costs one extra
non-fatal API error, never a crash or a fabricated success claim. Correct
these against Composio's real catalog before relying on them in
production; the surrounding plumbing (probe -> cache -> inject,
tier -> tier -> local-PDF-fallback) is fully real and unit-tested
independently of whether these exact strings are right.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import BaseTool, tool

from .. import config, console, db

LABEL = "integrations_agent"


# ---------------------------------------------------------------------------
# 3.A -- classifying a Composio slug as "this created/touched a workspace
# asset worth remembering" and pulling an id/url back out of its result.
# ---------------------------------------------------------------------------

# Toolkit-agnostic on purpose (matches this codebase's "no hardcoded
# per-app branching" dynamic-integration philosophy -- see
# integration_tools.py's own module docstring): classifies by the NOUN in
# a CREATE/GENERATE-shaped slug, not by a hand-maintained list of specific
# toolkits, so a brand-new toolkit that fits the same shape (TOOLKIT_
# CREATE_SPREADSHEET, say) is recognized automatically.
_ASSET_NOUNS: dict[str, str] = {
    "BASE": "base",
    "SPREADSHEET": "spreadsheet",
    "SHEET": "spreadsheet",
    "WORKBOOK": "spreadsheet",
    "DOCUMENT": "document",
    "DOC": "document",
    "DATABASE": "database",
    "PAGE": "page",
    "FILE": "file",
    "FOLDER": "folder",
    "REPORT": "report",
    "TABLE": "table",
    "BOARD": "board",
}
_ASSET_VERBS = ("CREATE", "GENERATE", "ADD_BASE", "NEW_")


def classify_asset_creation(slug: str) -> str | None:
    """None for anything that isn't a recognizable "made a workspace
    asset" call (the overwhelming majority of the 1,400+ toolkits'
    actions -- e.g. TODOIST_CREATE_TASK is a create-verb slug too, but
    'task' isn't in _ASSET_NOUNS, so it's correctly ignored here; a task
    is active_tasks/reminders territory, not a workspace asset). Returns
    a toolkit-prefixed type string like 'airtable_base' or
    'googlesheets_spreadsheet' otherwise -- stored as-is in
    user_assets.asset_type."""
    if not slug:
        return None
    slug_upper = slug.upper()
    if not any(verb in slug_upper for verb in _ASSET_VERBS):
        return None
    toolkit = slug.split("_", 1)[0].lower() if "_" in slug else slug.lower()
    for noun, label in _ASSET_NOUNS.items():
        if noun in slug_upper:
            return f"{toolkit}_{label}"
    return None


def _get(obj: Any, key: str) -> Any:
    """Same defensive dict-or-attribute accessor integration_tools.py's
    own _extract_tool_fields already uses -- Composio SDK response shapes
    aren't guaranteed to be plain dicts."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


_ID_KEYS = (
    "id", "base_id", "baseId", "spreadsheet_id", "spreadsheetId",
    "record_id", "recordId", "page_id", "pageId", "file_id", "fileId",
    "database_id", "databaseId", "document_id", "documentId",
)
_URL_KEYS = (
    "url", "web_view_link", "webViewLink", "spreadsheet_url", "spreadsheetUrl",
    "permalink", "html_url", "htmlUrl", "link", "shareable_link", "view_url",
)


def extract_asset_identity(result: Any) -> tuple[str | None, str | None]:
    """Best-effort (external_id, url) pulled out of a raw Composio
    execute() result -- checked at the top level AND one level into a
    nested 'data' field (a common Composio response wrapper), since the
    exact shape varies per toolkit/SDK version (see this module's own
    docstring on why nothing here assumes one fixed shape). Either or
    both can come back None -- callers must treat that as "still worth
    recording, just without that field" rather than a failure."""
    candidates = [result]
    nested = _get(result, "data")
    if nested is not None:
        candidates.append(nested)

    ext_id: str | None = None
    url: str | None = None
    for candidate in candidates:
        for key in _ID_KEYS:
            value = _get(candidate, key)
            if isinstance(value, str) and value and ext_id is None:
                ext_id = value
        for key in _URL_KEYS:
            value = _get(candidate, key)
            if isinstance(value, str) and value and url is None:
                url = value
    return ext_id, url


def _looks_like_failed_result(result: Any) -> bool:
    """Defensive check for the common Composio ExecuteToolResponse shape
    (a 'successful: bool' / 'error: str | None' pair) -- degrades to
    "not failed" (proceed) when neither field is present, same
    fail-open-to-today's-behavior reasoning as every other defensive
    extraction in this file. A raised exception is handled separately by
    the caller's own try/except; this only covers the "call returned
    normally but reports its own failure" case."""
    successful = _get(result, "successful")
    if successful is False:
        return True
    error = _get(result, "error")
    return isinstance(error, str) and bool(error.strip())


async def record_asset_from_execution(
    user: "config.UserContext", slug: str, arguments: dict[str, Any], result: Any,
) -> None:
    """Called from execute_integration_tool right after a successful
    Composio call. Best-effort and completely non-fatal -- persisting the
    asset must never be why a real, already-successful tool call looks
    like it failed to the model/user."""
    if not config.WORKSPACE_ASSETS_ENABLED:
        return
    asset_type = classify_asset_creation(slug)
    if not asset_type:
        return
    try:
        ext_id, url = extract_asset_identity(result)
        title = (
            arguments.get("title") or arguments.get("name") or arguments.get("base_name")
            or arguments.get("file_name") or asset_type.replace("_", " ").title()
        )
        await db.record_user_asset(
            user.user_id, asset_type, str(title)[:200], ext_id, url,
            summary=f"Created via {slug}",
        )
    except Exception as e:  # noqa: BLE001 - a memory aid, never load-bearing
        console.system(f"record_asset_from_execution: failed to persist asset for {slug!r} (non-fatal): {e}")


# ---------------------------------------------------------------------------
# 3.B -- per-toolkit entity auto-discovery/caching.
# ---------------------------------------------------------------------------

# Declarative, one entry per toolkit that has a "default X the user
# shouldn't have to know/paste a technical id for" concept. Adding a new
# toolkit here is the ENTIRE integration -- no other code path needs to
# change. See this module's docstring for the "these exact slugs are
# unverified guesses" caveat.
ENTITY_PROBES: dict[str, dict[str, Any]] = {
    "airtable": {
        "entity_type": "workspace_id",
        "probe_slug": "AIRTABLE_LIST_WORKSPACES",
        "probe_arguments": {},
        "id_keys": ("id", "workspace_id", "workspaceId"),
        # slug -> the argument name that slug needs filled from this cache
        "inject": {"AIRTABLE_CREATE_BASE": "workspace_id"},
    },
    "googledrive": {
        "entity_type": "folder_id",
        "probe_slug": "GOOGLEDRIVE_LIST_FILES",
        "probe_arguments": {"q": "name = 'Messa' and mimeType = 'application/vnd.google-apps.folder'"},
        "id_keys": ("id", "folder_id", "folderId"),
        "inject": {"GOOGLEDRIVE_CREATE_FOLDER": "parent_id", "GOOGLEDRIVE_UPLOAD_FILE": "parent_id"},
    },
}

# Reverse index built once at import time: slug -> (toolkit, entity_type, param_name).
_INJECT_TARGETS: dict[str, tuple[str, str, str]] = {}
for _toolkit, _spec in ENTITY_PROBES.items():
    for _slug, _param in _spec["inject"].items():
        _INJECT_TARGETS[_slug] = (_toolkit, _spec["entity_type"], _param)


def toolkit_for_injection_target(slug: str) -> str | None:
    """The toolkit slug (e.g. 'airtable') this Composio action slug would
    have an entity auto-injected for, or None if it isn't one -- lets
    execute_integration_tool trigger a just-in-time discover_and_cache_
    app_entities call for a connection that predates this feature (or was
    never seen by server.py's connection-poll loop) without reaching into
    this module's own internal _INJECT_TARGETS index directly."""
    target = _INJECT_TARGETS.get(slug)
    return target[0] if target else None


def _extract_first_id(probe_result: Any, id_keys: tuple[str, ...]) -> str | None:
    """A probe (a 'list workspaces'/'list files' style action) typically
    returns a list of items somewhere -- checked under a few plausible
    wrapper keys/attrs before falling back to treating the result itself
    as the list, same defensive-shape reasoning as
    integration_tools.py's own _search_sync."""
    items = _get(probe_result, "data")
    if items is None:
        items = _get(probe_result, "items")
    if items is None:
        items = probe_result
    if isinstance(items, dict):
        # A single-object result rather than a list -- check it directly.
        for key in id_keys:
            value = items.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    try:
        first = next(iter(items))
    except (TypeError, StopIteration):
        return None
    for key in id_keys:
        value = _get(first, key)
        if isinstance(value, str) and value:
            return value
    return None


async def discover_and_cache_app_entities(user_id: int, toolkit_slug: str, client=None) -> None:
    """Best-effort, idempotent probe-and-cache for one toolkit. Takes a
    bare `user_id` (not a full config.UserContext) deliberately -- its two
    call sites are execute_integration_tool (which has a real UserContext
    handy, and just passes its .user_id) and server.py's app-connection
    poll loop (which only ever has req["user_id"], a bare int, from the
    pending_app_connection_requests row -- loading a full UserContext
    there just for this would be a wasted extra DB round trip on a hot
    poll loop). Called from that poll loop right after a connection goes
    ACTIVE (the faithful "when an app connects, auto-probe" trigger), and
    opportunistically again from execute_integration_tool for a
    connection that predates this feature or was never seen by that loop
    (e.g. local/CLI use). A no-op if this toolkit has no ENTITY_PROBES
    entry, if a value is already cached (never re-probes needlessly), or
    if the probe call itself fails for any reason -- entity discovery is
    strictly a convenience, never load-bearing for execute_integration_tool
    actually working."""
    if not config.WORKSPACE_ASSETS_ENABLED:
        return
    toolkit_slug = (toolkit_slug or "").strip().lower()
    spec = ENTITY_PROBES.get(toolkit_slug)
    if not spec:
        return
    try:
        existing = await db.get_cached_app_entity(user_id, toolkit_slug, spec["entity_type"])
        if existing:
            return
        if client is None:
            from .integration_tools import _get_client  # local import: avoids a circular import at module load

            client = _get_client()
        composio_user_id = str(user_id)

        def _probe_sync() -> Any:
            return client.tools.execute(
                slug=spec["probe_slug"],
                arguments=dict(spec.get("probe_arguments") or {}),
                user_id=composio_user_id,
            )

        result = await asyncio.to_thread(_probe_sync)
        if _looks_like_failed_result(result):
            return
        entity_id = _extract_first_id(result, spec["id_keys"])
        if not entity_id:
            return
        await db.set_cached_app_entity(
            user_id, toolkit_slug, spec["entity_type"], entity_id,
            label=f"auto-discovered via {spec['probe_slug']}",
        )
    except Exception as e:  # noqa: BLE001 - convenience only, never load-bearing
        console.system(f"discover_and_cache_app_entities: probe failed for {toolkit_slug!r} (non-fatal): {e}")


async def apply_cached_entity_defaults(
    user: "config.UserContext", slug: str, arguments: dict[str, Any],
) -> dict[str, Any]:
    """Returns a NEW arguments dict with a missing required id (e.g.
    'workspace_id' for AIRTABLE_CREATE_BASE) silently filled in from the
    per-user entity cache -- never overrides a value the model/user
    already supplied, and never mutates the caller's own dict. Falls back
    to returning `arguments` completely unchanged on any error, on a
    cache miss, or when this slug isn't a known injection target -- this
    must never be why a call that would have worked on its own fails
    instead."""
    target = _INJECT_TARGETS.get(slug)
    if not target:
        return arguments
    toolkit, entity_type, param = target
    if arguments.get(param):
        return arguments  # already supplied -- never override
    try:
        cached = await db.get_cached_app_entity(user.user_id, toolkit, entity_type)
    except Exception as e:  # noqa: BLE001 - degrade to "no default available", never fail the call
        console.system(f"apply_cached_entity_defaults: cache lookup failed for {slug!r} (non-fatal): {e}")
        return arguments
    if not cached:
        return arguments
    filled = dict(arguments)
    filled[param] = cached
    return filled


# ---------------------------------------------------------------------------
# 4.A -- Autonomous Fallback Waterfall.
# ---------------------------------------------------------------------------

_WATERFALL_TIERS: list[dict[str, Any]] = [
    {
        "toolkit": "airtable",
        "label": "Airtable base",
        "create_slug": "AIRTABLE_CREATE_BASE",
        "build_arguments": lambda title, headers, rows: {"name": title},
    },
    {
        "toolkit": "googlesheets",
        "label": "Google Sheet",
        "create_slug": "GOOGLESHEETS_CREATE_SPREADSHEET",
        "build_arguments": lambda title, headers, rows: {"title": title},
    },
]


def _generate_fallback_pdf(title: str, headers: list[str] | None, rows: list[list[str]] | None) -> Path:
    """Last-resort tier: no guessed third-party slug involved at all --
    reuses document_tools.py's own already-shipped, already-tested PDF
    builder (_build_generic_pdf/_slugify), so this tier is exactly as
    reliable as document_agent's own generate_pdf tool. Row 0 of `table`
    is rendered as the header row by _build_generic_pdf itself."""
    from .document_tools import _build_generic_pdf, _slugify  # local import: avoids a circular import at module load

    table = ([headers] if headers else []) + (rows or [])
    sections = [{"heading": title, "table": table}] if table else [{"heading": title, "body": "(no rows provided)"}]
    out_dir = Path(config.OUTPUTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slugify(title)}.pdf"
    _build_generic_pdf(title, sections, out_path)
    return out_path


def build_workspace_asset_tool(
    user: "config.UserContext", get_client: Callable[[], Any], composio_user_id: str,
) -> BaseTool:
    """Factory (not a bare @tool function) so the tool closes over the
    SAME _get_client/composio_user_id build_integration_tools' other tools
    already use, instead of re-deriving them -- keeps this one client
    config path, matching every other tool in that module."""

    @tool
    async def create_workspace_asset(
        title: str, headers: list[str] | None = None, rows: list[list[str]] | None = None,
    ) -> str:
        """Create a shareable workspace asset -- a table/list the user can
        open and edit -- for `title`. Tries Airtable first, then Google
        Sheets, then falls back to a plain PDF table if neither app is
        connected or working, so the user always gets a real deliverable
        instead of a dead end (the 'Clean Air Interface' rule: whichever
        tier actually succeeds, tell the user plainly what you made and
        where, never leak which ones failed or why).

        Prefer this over a raw execute_integration_tool call when the user
        just wants "a list/sheet/table of X" and doesn't care which
        specific app it lives in. Use execute_integration_tool directly
        instead when they named a specific app, or asked for something
        more specific than a simple table (formulas, formatting, sharing
        permissions, ...).

        headers: optional column headers, e.g. ['Name', 'Email', 'Stage'].
        rows: optional row values, each a list matching headers' length.
        """
        for tier in _WATERFALL_TIERS:
            try:
                client = get_client()
            except Exception:
                continue
            arguments = tier["build_arguments"](title, headers, rows)
            arguments = await apply_cached_entity_defaults(user, tier["create_slug"], arguments)

            def _execute_sync(_client=client, _slug=tier["create_slug"], _arguments=arguments) -> Any:
                kwargs: dict = {"slug": _slug, "arguments": _arguments, "user_id": composio_user_id}
                if config.COMPOSIO_TOOLKIT_VERSION:
                    kwargs["version"] = config.COMPOSIO_TOOLKIT_VERSION
                else:
                    kwargs["dangerously_skip_version_check"] = True
                return _client.tools.execute(**kwargs)

            try:
                result = await asyncio.to_thread(_execute_sync)
            except Exception as e:  # noqa: BLE001 - try the next tier, never surface this raw
                console.system(
                    f"create_workspace_asset: {tier['label']} tier failed (non-fatal, trying next): {e}"
                )
                continue
            if _looks_like_failed_result(result):
                console.system(f"create_workspace_asset: {tier['label']} tier reported failure, trying next")
                continue

            await record_asset_from_execution(user, tier["create_slug"], arguments, result)
            _, url = extract_asset_identity(result)
            noun = "sheet" if "sheet" in tier["label"].lower() else "base"
            location = f": {url}" if url else " (check your account -- no direct link came back)."
            return f"Created '{title}' as a new {tier['label']} {noun}{location}"

        # Every remote tier failed or is unavailable -- last resort, no
        # guessed third-party slug involved, so this tier essentially
        # cannot fail the same way the ones above can.
        try:
            pdf_path = _generate_fallback_pdf(title, headers, rows)
            if config.WORKSPACE_ASSETS_ENABLED:
                token = await db.create_document_share(user.user_id, str(pdf_path), pdf_path.name)
            else:
                token = None
            url = f"{config.LIVE_VIEW_BASE_URL}/files/{token}" if token else None
            if config.WORKSPACE_ASSETS_ENABLED:
                await db.record_user_asset(
                    user.user_id, "pdf_table", title, None, url,
                    summary="Fallback PDF (Airtable/Google Sheets unavailable)",
                )
            where = f": {url}" if url else " (saved, but couldn't mint a link to text/email it just now)."
            return f"Airtable and Google Sheets aren't set up yet, so I made '{title}' as a PDF table instead{where}"
        except Exception as e:  # noqa: BLE001 - the one message this tool is allowed to end on
            console.system(f"create_workspace_asset: PDF fallback failed too: {e}")
            return (
                f"I wasn't able to create '{title}' in Airtable, Google Sheets, or as a PDF right "
                "now -- there's a real problem reaching those. Let the user know plainly rather "
                "than guessing again."
            )

    return create_workspace_asset
