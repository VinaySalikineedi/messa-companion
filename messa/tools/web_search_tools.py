"""Lightweight, no-browser web lookup tools for Messa's OWN direct use --
not deepsearch's.

Why this exists: users reported deepsearch feeling slow, and a lot of what
gets delegated to it is actually a plain lookup ("what's the score",
"who's the mayor of X", "what does this article say") that doesn't need a
browser at all. Every deepsearch delegation pays a fixed, real cost before
a single useful thing happens: opening a Browserbase session, launching
`@playwright/mcp` over CDP, and running a full LangChain ReAct loop with
the whole Playwright tool schema in context -- see deepsearch_tools.py's
BrowserToolProvider. For a pure "look this up" request, all of that is
pure overhead with zero benefit; a plain HTTP search + a plain HTTP page
fetch answers the same question in a fraction of the time and tokens.

`web_search` (DuckDuckGo, via the `ddgs` package -- no API key, actively
maintained) and `fetch_page_text` (a direct HTTP GET + HTML-to-text) are
exposed directly on Messa's own toolset (see agents/registry.py), not
nested inside deepsearch -- the routing decision ("does this need a real
browser or not") has to happen BEFORE a browser session ever opens, or the
whole point is lost. Messa's system prompt tells her to prefer these for a
plain factual lookup and reserve deepsearch for anything that actually
requires clicking through a site, logging in, or filling out a form.

Both tools fail closed and cheap: a network error or empty result returns
a short message suggesting deepsearch as a fallback, rather than raising
and losing the turn -- a plain HTTP fetch has no way to run the JS some
pages need to render their content at all, and that's a real, expected
limitation, not a bug.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup
try:
    from duckduckgo_search import DDGS
    from duckduckgo_search.exceptions import DuckDuckGoSearchException as DDGSException
except ImportError:
    try:
        from ddgs import DDGS
        DDGSException = Exception
    except ImportError:
        DDGS = None
        DDGSException = Exception
from langchain_core.tools import BaseTool, tool

from .common import trace_all

LABEL = "web_search"

DEFAULT_MAX_FETCH_CHARS = 6000
FETCH_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "Mozilla/5.0 (compatible; MessaBot/1.0; +https://textmessa.com)"


def _format_results(results: list[dict[str, Any]]) -> str:
    """Pure formatting, split out so it's unit-testable without a network
    call (see /tmp/test_web_search_tools.py)."""
    if not results:
        return "No results found."
    lines = []
    for r in results:
        title = (r.get("title") or "(no title)").strip()
        href = (r.get("href") or "").strip()
        body = (r.get("body") or "").strip()
        line = f"- {title}\n  {href}"
        if body:
            line += f"\n  {body[:220]}"
        lines.append(line)
    return "\n".join(lines)


def extract_readable_text(html: str, max_chars: int = DEFAULT_MAX_FETCH_CHARS) -> str:
    """Strip a raw HTML document down to its readable text, dropping
    script/style/nav/footer/header noise. Pure function (no network) so
    it's directly unit-testable against a fixed HTML string."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    if not text:
        return "(no readable text content found on this page)"
    if len(text) > max_chars:
        text = text[:max_chars] + " ... (truncated)"
    return text


def build_web_search_tools() -> list[BaseTool]:
    @tool
    async def web_search(query: str, max_results: int = 5) -> str:
        """Quick, no-browser web search for a factual lookup -- a fact, current
        news, a definition, a simple 'what is X' / 'who is X' / 'what's the
        latest on X' question. Returns short titles/snippets/URLs, not full
        page content -- follow up with fetch_page_text on a specific URL from
        the results if you need more detail. NOT for anything that requires
        clicking through a site, logging in, filling out a form, or adding
        something to a cart -- delegate those to deepsearch instead, since
        this tool can't interact with a page at all, only read search results."""
        try:
            results = await asyncio.to_thread(
                lambda: list(DDGS().text(query, max_results=max_results))
            )
        except DDGSException as e:
            return (
                f"ERROR: web search failed ({e}). If this needs an actual browser "
                "(e.g. the search itself requires JS or login), delegate to deepsearch instead."
            )
        except Exception as e:  # noqa: BLE001 - a tool must never crash the agent loop
            return f"ERROR: web search failed ({e})."
        return _format_results(results)

    @tool
    async def fetch_page_text(url: str, max_chars: int = DEFAULT_MAX_FETCH_CHARS) -> str:
        """Fetch a URL and return its readable text content -- fast, for reading
        an article or page you already have a link to (e.g. from web_search's
        results). This does NOT run JavaScript and can't click/scroll/log in --
        it just reads whatever HTML the server returns. If the page needs JS to
        render its actual content, is behind a login, or you need to interact
        with it, use deepsearch instead."""
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return (
                f"ERROR fetching {url}: {e}. If this page needs a real browser (JS-rendered "
                "content, a login wall, etc), delegate to deepsearch instead."
            )
        return extract_readable_text(resp.text, max_chars)

    raw_tools: list[BaseTool] = [web_search, fetch_page_text]
    return trace_all(raw_tools, LABEL)
