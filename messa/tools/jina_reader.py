"""Shared helper for reading a URL's rendered text via Jina AI's free Reader
API (r.jina.ai) -- a JS-capable page read that costs no Browserbase session
time and needs no API key for light use (see config.py's "Jina Reader"
section).

Why this exists, separate from web_search_tools.py's plain `fetch_page_text`:
that tool does a raw HTTP GET with no JS engine at all, so any JS-rendered
page (most modern ticket/booking sites -- Fandango, AMC, Ticketmaster, ...)
comes back empty or as a bare loading shell. Jina's Reader runs the page in
a real headless browser on THEIR infrastructure and hands back clean,
readable text -- so Messa gets JS-rendered content without ever opening a
Browserbase session herself. This is a genuine middle tier:

  1. fetch_page_text        -- plain HTTP GET, no JS, fastest, free, ours.
  2. fetch_rendered_page_text (this module) -- JS rendered on Jina's own
     infrastructure, still just one HTTP call from our side, free/keyless
     by default, costs ZERO Browserbase session time.
  3. A real deepsearch delegation -- the only tier that can actually click,
     type, log in, or fill a form; the only one billed by Browserbase
     session time (see config.py's Browserbase section).

See registry.py's "Speed matters" paragraph and deepsearch_tools.py's
DEEPSEARCH_SYSTEM_PROMPT/`_SUBAGENT_SYSTEM_PROMPT` for how the routing
between all three tiers is worded to the model.

Two independent call sites share this ONE implementation rather than each
rolling their own HTTP call: web_search_tools.py exposes it directly on
Messa's own toolset (so a routine's periodic read-only check -- "has this
ticket gone on sale" -- or a plain "does this page say X" request never
needs a browser at all), and deepsearch_tools.py's BrowserToolProvider also
hands it to a live delegation (so even a task that DOES end up needing the
real browser for one interactive step can still do any reconnaissance or
comparison reading across candidate pages first, without burning paid
session time on it). Each call site wraps this same function in its own
`@tool`-decorated closure with a docstring pitched at that toolset's own
model/context -- see build_web_search_tools() and BrowserToolProvider.__aenter__.

Keyless by default (`config.JINA_API_KEY` is None): Jina's own published
free tier is 20 requests/minute with no key at all. Fails closed and cheap,
matching fetch_page_text's own contract -- a network error, rate limit, or
empty result returns a short, clearly-labeled message suggesting the next
tier up, rather than raising and losing the turn.

Not live-verified against Jina's real API in this sandbox: this container's
network egress is allowlisted to a fixed set of domains (package registries,
the Anthropic API) and r.jina.ai isn't on it -- the same restriction already
documented for `web_search_tools.py`'s DuckDuckGo calls (see
/tmp/test_web_search_tools.py's own "Confirmed sandbox network restriction"
check). The request shape below (GET https://r.jina.ai/<target-url>, target
URL appended raw after the base, `Authorization: Bearer <key>` only when a
key is configured) matches Jina's own long-stable, publicly documented
Reader contract, but a real deployed run against a live key/free tier is
the first genuine end-to-end confirmation -- see README's "Not verified"
note on this feature.
"""
from __future__ import annotations

import httpx

from .. import config

DEFAULT_MAX_CHARS = 8000
_USER_AGENT = "Mozilla/5.0 (compatible; MessaBot/1.0; +https://textmessa.com)"


async def fetch_rendered_page_text(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Fetch `url` through Jina's Reader API and return its rendered,
    readable text -- JS executed server-side on Jina's own infrastructure,
    never ours, so this costs no Browserbase session time. Pure function
    with no LangChain @tool wrapper of its own, so each call site (Messa's
    own toolset, and deepsearch's BrowserToolProvider) can wrap it with a
    docstring suited to that context while sharing this one real
    implementation -- see the module docstring above.

    Fails closed: any error (network, timeout, rate limit, empty body)
    returns a short ERROR-prefixed string rather than raising, so a tool
    built on top of this never crashes the agent loop -- it just tells the
    caller to try the next tier up."""
    target = url.strip()
    reader_url = f"{config.JINA_READER_BASE_URL}/{target}"
    headers = {"User-Agent": _USER_AGENT}
    if config.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=config.JINA_READER_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = await client.get(reader_url, headers=headers)
    except Exception as e:  # noqa: BLE001 - a tool must never crash the agent loop
        return (
            f"ERROR reading {url} (JS-rendered fetch): {e}. If this page needs real "
            "interaction (clicking, logging in, filling a form), delegate to deepsearch instead."
        )
    if resp.status_code == 429:
        return (
            "ERROR: the JS-rendered page reader is rate-limited right now (its free tier "
            "allows 20 requests/minute with no API key). Wait a moment and try again, or "
            "fall back to fetch_page_text / deepsearch if this can't wait."
        )
    if resp.status_code >= 400:
        return (
            f"ERROR reading {url} (JS-rendered fetch): HTTP {resp.status_code}. If this page "
            "needs real interaction, delegate to deepsearch instead."
        )
    text = resp.text.strip()
    if not text:
        return "(no readable text content found on this page, even with JS rendering)"
    if len(text) > max_chars:
        text = text[:max_chars] + " ... (truncated)"
    return text
