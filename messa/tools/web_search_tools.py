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

Six tools, all at the same "don't open a browser you don't need" tier, all
exposed directly on Messa's own toolset (see agents/registry.py), not
nested inside deepsearch -- the routing decision ("does this need a real
browser or not") has to happen BEFORE a browser session ever opens, or the
whole point is lost. Two independent PROVIDERS sit behind the search/fetch
job on purpose (not redundancy for its own sake) -- see tools/parallel_search.py's
own docstring for why a single free provider having a bad day shouldn't be
the thing that pushes a read-only question into a billed deepsearch
delegation:

  - `web_search` (DuckDuckGo, via the `ddgs` package -- no API key) for a
    quick factual lookup when you don't have a URL yet.
  - `parallel_web_search` (Parallel Search MCP -- free, no API key) -- a
    second, independent search provider, purpose-built for AI agents. Try
    this if web_search comes back empty/errors, or as a second opinion.
  - `wikipedia_lookup` (Wikipedia's own REST API -- no key) for anything
    that's likely to have its own encyclopedia article -- faster and more
    reliable than a generic search for that specific shape of question.
  - `fetch_page_text` (plain HTTP GET, no JS) for reading a page you
    already have a link to.
  - `fetch_rendered_page_text` (tools/jina_reader.py -- JS rendered on
    Jina's own infrastructure, still no browser session of ours) for a
    page `fetch_page_text` came back empty on, e.g. a modern JS-heavy
    ticket/booking site.
  - `parallel_web_fetch` (Parallel Search MCP again -- free, no API key) --
    a second, independent provider for the SAME job as fetch_rendered_page_text
    (JS-rendered reads, plus PDFs). Try this if fetch_rendered_page_text
    comes back empty/errors on a page you genuinely need to read.

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
from .jina_reader import fetch_rendered_page_text as _fetch_rendered_page_text
from .parallel_search import parallel_web_fetch as _parallel_web_fetch
from .parallel_search import parallel_web_search as _parallel_web_search
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
    async def web_search(query: str, max_results: int = 5) -> str:
        """Quick, no-browser web search for a factual lookup -- a fact, current
        news, a definition, a simple 'what is X' / 'who is X' / 'what's the
        latest on X' question. Powered by a multi-provider waterfall (Tavily,
        Brave, Serper, Parallel, DuckDuckGo) for high reliability and instant
        answers. Returns short titles/snippets/URLs and direct answers --
        follow up with fetch_page_text or fetch_rendered_page_text on a specific
        URL from the results if you need more detail. NOT for anything that
        requires clicking through a site, logging in, filling out a form, or
        adding something to a cart -- delegate those to deepsearch instead."""
        return await _unified_web_search(query, max_results=max_results)

    @tool
    async def parallel_web_search(objective: str, search_queries: Optional[list[str]] = None) -> str:
        """A second, independent web search provider (Parallel Search MCP --
        free, no API key), purpose-built for AI agents rather than a browser
        UI. Try this if web_search comes back empty, errors out, or you just
        want a second opinion on results -- it's not a replacement for
        web_search, it's a backup at the same free/no-browser tier. `objective`
        is a plain-English description of what you're trying to find out;
        `search_queries` (optional) lets you pass one or more specific search
        strings instead of leaving query formulation to Parallel. Like
        web_search, this can't interact with a page at all -- only read
        search results; delegate to deepsearch for anything needing a click,
        login, form, or cart."""
        return await _parallel_web_search(objective, search_queries)

    @tool
    async def fetch_page_text(url: str, max_chars: int = DEFAULT_MAX_FETCH_CHARS) -> str:
        """Fetch a URL and return its readable text content -- fast, free, and
        costs no Browserbase session time, for reading an article or page you
        already have a link to (e.g. from web_search's results). This does NOT
        run JavaScript and can't click/scroll/log in -- it just reads whatever
        HTML the server returns as-is. If the returned text looks empty, tiny,
        or like a bare loading shell, the page is probably JS-rendered --
        try fetch_rendered_page_text on the SAME url next, which runs the page's
        JS server-side (still no browser session of ours) before falling back
        to deepsearch. Go straight to deepsearch instead of either fetch tool
        only when the task needs real interaction: a login, a form, a cart,
        clicking through a flow -- not just reading a page's current content."""
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return (
                f"ERROR fetching {url}: {e}. Try fetch_rendered_page_text on the same url next "
                "(handles JS-rendered pages); delegate to deepsearch only if this needs real "
                "interaction (a login wall, a form, clicking through a flow)."
            )
        return extract_readable_text(resp.text, max_chars)

    @tool
    async def fetch_rendered_page_text(url: str, max_chars: int = DEFAULT_MAX_FETCH_CHARS) -> str:
        """Fetch a URL's content AFTER letting its JavaScript run, and return
        readable text -- powered by a multi-provider scrape waterfall (Jina Reader,
        Firecrawl Cloud, Parallel Fetch) with zero Browserbase session overhead.
        For pages that need JS to render (most modern sites). Still read-only:
        it can't click, type, scroll, or log in. If this ALSO comes back
        empty/unhelpful, or the task genuinely needs to interact with the page
        (buy, submit, log in), delegate to deepsearch instead."""
        return await _unified_web_read(url, max_chars=max_chars)

    @tool
    async def parallel_web_fetch(url: str) -> str:
        """A second, independent provider (Parallel Search MCP -- free, no
        API key) for the SAME job as fetch_rendered_page_text: a JS-rendered
        page read that costs no Browserbase session time. Try this if
        fetch_rendered_page_text comes back empty or errors out on a page
        you genuinely need to read -- also handles PDFs. Still read-only --
        can't click, type, scroll, or log in. If this ALSO fails, or the
        task needs real interaction, delegate to deepsearch."""
        return await _parallel_web_fetch(url)

    @tool
    async def wikipedia_lookup(topic: str) -> str:
        """Free, instant lookup of a Wikipedia article's summary -- for a
        person, place, thing, event, or concept likely to have its own
        Wikipedia page (a public figure, a company, a historical event, a
        scientific concept, a country/city). Faster and more reliable than
        web_search for this specific shape of question, since it goes
        straight to the actual article instead of guessing from search
        snippets -- try this FIRST for anything encyclopedia-shaped. NOT for
        anything current/time-sensitive (today's score, this week's news,
        live prices, today's showtimes) -- Wikipedia articles lag real-time
        events; use web_search for those instead. If the topic name is
        ambiguous (matches more than one thing), says so instead of
        guessing -- ask the user which one they meant."""
        try:
            async with httpx.AsyncClient(timeout=WIKIPEDIA_TIMEOUT_SECONDS, follow_redirects=True) as client:
                resp = await client.get(
                    f"{WIKIPEDIA_API_BASE}/api/rest_v1/page/summary/{quote(topic)}",
                    headers={"User-Agent": _USER_AGENT},
                )
                if resp.status_code == 404:
                    resolved = await _wikipedia_resolve_title(client, topic)
                    if not resolved:
                        return f"No Wikipedia article found for \"{topic}\". Try web_search instead."
                    resp = await client.get(
                        f"{WIKIPEDIA_API_BASE}/api/rest_v1/page/summary/{quote(resolved)}",
                        headers={"User-Agent": _USER_AGENT},
                    )
                resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return f"ERROR looking up \"{topic}\" on Wikipedia: {e}. Try web_search instead."
        return _format_wikipedia_summary(resp.json())

    raw_tools: list[BaseTool] = [
        web_search, parallel_web_search, fetch_page_text, fetch_rendered_page_text,
        parallel_web_fetch, wikipedia_lookup,
    ]
    return trace_all(raw_tools, LABEL)
