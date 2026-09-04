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
fetch answers the same question in a fraction of the time and tokens --
and, unlike deepsearch, Browserbase never bills a cent for any of it, since
no Browserbase session is ever involved.

Two tools, both at the same "don't open a browser you don't need" tier, both
exposed directly on Messa's own toolset (see agents/registry.py), not
nested inside deepsearch -- the routing decision ("does this need a real
browser or not") has to happen BEFORE a browser session ever opens, or the
whole point is lost. Each is itself backed by a multi-provider waterfall
(see search_engine.py) so a single free provider having a bad day doesn't
push a read-only question into a billed deepsearch delegation:

  - `search_web` (Tavily -> Brave -> Serper -> Parallel -> DuckDuckGo) for
    a quick factual lookup when you don't have a URL yet.
  - `read_webpage` (Jina Reader -> Firecrawl Cloud -> Parallel Fetch ->
    plain HTTP) for reading a page's rendered text content, JS included,
    without opening a browser session.

Messa's system prompt tells her to prefer this whole tier for a plain
factual lookup or a read-only check (including inside an autonomous
routine's periodic firing -- see server.py's `_fire_autonomous_routine`,
which reuses this same toolset), and reserve deepsearch for anything that
actually requires clicking through a site, logging in, or filling out a
form -- the one tier genuinely billed by Browserbase session time.

Every tool here fails closed and cheap: a network error or empty result
returns a short message suggesting the next thing to try (the other
provider at the same tier, then deepsearch as the last resort) rather than
raising and losing the turn -- a plain HTTP fetch has no way to run the JS
some pages need to render their content at all, and that's a real, expected
limitation, not a bug.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import quote

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
from .search_engine import unified_web_read as _unified_web_read
from .search_engine import unified_web_search as _unified_web_search

LABEL = "web_search"

DEFAULT_MAX_FETCH_CHARS = 6000
FETCH_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "Mozilla/5.0 (compatible; MessaBot/1.0; +https://textmessa.com)"

WIKIPEDIA_API_BASE = "https://en.wikipedia.org"
WIKIPEDIA_TIMEOUT_SECONDS = 10.0


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


def _format_wikipedia_summary(data: dict[str, Any], max_chars: int = DEFAULT_MAX_FETCH_CHARS) -> str:
    """Pure formatting for a Wikipedia REST API page-summary response --
    split out so it's unit-testable without a network call, same pattern as
    _format_results/extract_readable_text above. Handles the disambiguation
    case explicitly (Wikipedia's own `type` field) rather than returning a
    thin, unhelpful summary for a page that doesn't actually answer
    anything on its own."""
    title = (data.get("title") or "").strip()
    extract = (data.get("extract") or "").strip()
    url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    if data.get("type") == "disambiguation":
        return (
            f"\"{title}\" is a disambiguation page on Wikipedia -- it doesn't point to one "
            f"specific article. {extract or 'Multiple different things share this name.'} Ask "
            f"the user which one they mean, or try wikipedia_lookup again with a more specific "
            f"title.\n{url}"
        ).strip()
    if not extract:
        return "(Wikipedia article found, but it has no summary text.)"
    if len(extract) > max_chars:
        extract = extract[:max_chars] + " ... (truncated)"
    return f"{title}: {extract}\n{url}".strip()


async def _wikipedia_resolve_title(client: httpx.AsyncClient, query: str) -> str | None:
    """Wikipedia's summary endpoint only matches an exact (or lightly
    redirect-normalized) title -- 'einstein' won't find 'Albert Einstein'.
    Falls back to the MediaWiki opensearch API (same free, keyless
    Wikimedia service, a fuzzy title search) to resolve a loose query to
    its real article title, returning the top match's title or None if
    opensearch itself found nothing."""
    resp = await client.get(
        f"{WIKIPEDIA_API_BASE}/w/api.php",
        params={"action": "opensearch", "format": "json", "limit": 1, "search": query},
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    titles = data[1] if isinstance(data, list) and len(data) > 1 else []
    return titles[0] if titles else None


def build_web_search_tools() -> list[BaseTool]:
    @tool
    async def search_web(query: str, max_results: int = 5) -> str:
        """Search the web instantly without opening a browser or using session time.
        Powered by a fast multi-provider search engine (Tavily, Brave, Serper, Parallel, DuckDuckGo).
        Use this for looking up facts, finding URLs, researching options, or getting background info.
        Returns titles, URLs, snippets, and direct answers in <1 second."""
        return await _unified_web_search(query, max_results=max_results)

    @tool
    async def read_webpage(url: str, max_chars: int = DEFAULT_MAX_FETCH_CHARS) -> str:
        """Read a web page's rendered text content instantly WITHOUT opening a browser.
        Powered by a remote reader waterfall (Jina Reader, Firecrawl Cloud, Parallel Fetch, HTTP)
        that executes JavaScript server-side. Costs ZERO browser session time. Use this to read
        articles, compare options, or inspect content on candidate URLs."""
        return await _unified_web_read(url, max_chars=max_chars)

    raw_tools: list[BaseTool] = [search_web, read_webpage]
    return trace_all(raw_tools, LABEL)

