"""Unified multi-provider web search & scrape engine.

Chains legitimate free tiers of top search & scraper APIs into a resilient,
sub-second waterfall with automatic failover on rate limits (HTTP 429), timeouts,
or missing keys:

Search Waterfall:
  1. Tavily Search API (agentic search + clean markdown answers)
  2. Brave Search API (fast, independent search index)
  3. Serper.dev (Google Search API)
  4. Parallel Search MCP (hosted agent-search MCP)
  5. DuckDuckGo (fallback keyless web search)

Scrape/Read Waterfall:
  1. Jina Reader (r.jina.ai JS-rendered markdown)
  2. Firecrawl Cloud (api.firecrawl.dev/v1/scrape)
  3. Parallel Web Fetch (parallel.ai MCP web_fetch)
  4. Plain HTTP GET + BeautifulSoup text extraction

Allows both Messa orchestrator and the deepsearch subagent to get instant
answers and inspect web pages in <1-2 seconds without holding open or paying
for a Browserbase cloud browser session.
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

from .. import config, console
from .jina_reader import fetch_rendered_page_text as _jina_fetch_rendered_page_text
from .parallel_search import parallel_web_fetch as _parallel_web_fetch
from .parallel_search import parallel_web_search as _parallel_web_search

_USER_AGENT = "Mozilla/5.0 (compatible; MessaBot/1.0; +https://textmessa.com)"
DEFAULT_MAX_SEARCH_RESULTS = 5
DEFAULT_MAX_READ_CHARS = 8000


def _format_search_markdown(
    query: str,
    results: list[dict[str, Any]],
    answer: str | None = None,
    provider: str = "",
) -> str:
    lines = []
    if answer:
        lines.append(f"**Direct Answer**:\n{answer.strip()}\n")
    lines.append(f"Search results for \"{query}\" (via {provider}):")
    for r in results:
        title = (r.get("title") or "(no title)").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        entry = f"- **{title}**\n  {url}"
        if snippet:
            entry += f"\n  {snippet}"
        lines.append(entry)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Individual Search Providers
# ---------------------------------------------------------------------------

async def _search_tavily(query: str, max_results: int) -> tuple[list[dict[str, Any]], str | None] | None:
    """Tavily Search API (https://api.tavily.com/search)."""
    if not config.TAVILY_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=config.SEARCH_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": config.TAVILY_API_KEY,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": True,
                },
                headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
            )
        if resp.status_code == 429:
            console.system("[search:tavily] Rate limit (429) hit, falling back to next provider.")
            return None
        if resp.status_code >= 400:
            console.system(f"[search:tavily] HTTP {resp.status_code}, falling back.")
            return None
        data = resp.json()
        raw_results = data.get("results") or []
        answer = data.get("answer")
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in raw_results
            if item.get("url")
        ]
        return (results, answer) if results else None
    except Exception as e:
        console.system(f"[search:tavily] Failed: {e}, falling back.")
        return None


async def _search_brave(query: str, max_results: int) -> tuple[list[dict[str, Any]], str | None] | None:
    """Brave Search API (https://api.search.brave.com/res/v1/web/search)."""
    if not config.BRAVE_SEARCH_API_KEY:
        return None
    try:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": config.BRAVE_SEARCH_API_KEY,
            "User-Agent": _USER_AGENT,
        }
        params = {"q": query, "count": max_results}
        async with httpx.AsyncClient(timeout=config.SEARCH_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params=params,
            )
        if resp.status_code == 429:
            console.system("[search:brave] Rate limit (429) hit, falling back to next provider.")
            return None
        if resp.status_code >= 400:
            console.system(f"[search:brave] HTTP {resp.status_code}, falling back.")
            return None
        data = resp.json()
        raw_results = data.get("web", {}).get("results", [])
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            }
            for item in raw_results
            if item.get("url")
        ]
        return (results, None) if results else None
    except Exception as e:
        console.system(f"[search:brave] Failed: {e}, falling back.")
        return None


async def _search_serper(query: str, max_results: int) -> tuple[list[dict[str, Any]], str | None] | None:
    """Serper Google Search API (https://google.serper.dev/search)."""
    if not config.SERPER_API_KEY:
        return None
    try:
        headers = {
            "X-API-KEY": config.SERPER_API_KEY,
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=config.SEARCH_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers=headers,
                json={"q": query, "num": max_results},
            )
        if resp.status_code == 429:
            console.system("[search:serper] Rate limit (429) hit, falling back to next provider.")
            return None
        if resp.status_code >= 400:
            console.system(f"[search:serper] HTTP {resp.status_code}, falling back.")
            return None
        data = resp.json()
        raw_results = data.get("organic", [])
        answer_box = data.get("answerBox", {})
        answer = answer_box.get("answer") or answer_box.get("snippet")
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
            for item in raw_results
            if item.get("link")
        ]
        return (results, answer) if results else None
    except Exception as e:
        console.system(f"[search:serper] Failed: {e}, falling back.")
        return None


async def _search_parallel(query: str) -> str | None:
    """Parallel Search MCP."""
    try:
        res = await _parallel_web_search(objective=query)
        if res and not res.startswith("ERROR"):
            return res
    except Exception as e:
        console.system(f"[search:parallel] Failed: {e}, falling back.")
    return None


async def _search_duckduckgo(query: str, max_results: int) -> str | None:
    """DuckDuckGo fallback search via ddgs."""
    if DDGS is None:
        return None
    try:
        results = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=max_results))
        )
        if not results:
            return None
        formatted_list = [
            {
                "title": r.get("title") or "(no title)",
                "url": r.get("href") or "",
                "snippet": (r.get("body") or "")[:220],
            }
            for r in results
            if r.get("href")
        ]
        return _format_search_markdown(query, formatted_list, provider="DuckDuckGo")
    except Exception as e:
        console.system(f"[search:duckduckgo] Failed: {e}.")
        return None


# ---------------------------------------------------------------------------
# Unified Search Function
# ---------------------------------------------------------------------------

async def unified_web_search(
    query: str, max_results: int = DEFAULT_MAX_SEARCH_RESULTS
) -> str:
    """Performs a web search across the waterfall:
    Tavily -> Brave -> Serper -> Parallel -> DuckDuckGo.
    """
    clean_query = query.strip()
    if not clean_query:
        return "ERROR: empty search query."

    # 1. Tavily
    tavily_res = await _search_tavily(clean_query, max_results)
    if tavily_res:
        items, answer = tavily_res
        return _format_search_markdown(clean_query, items, answer=answer, provider="Tavily")

    # 2. Brave Search
    brave_res = await _search_brave(clean_query, max_results)
    if brave_res:
        items, answer = brave_res
        return _format_search_markdown(clean_query, items, answer=answer, provider="Brave")

    # 3. Serper (Google)
    serper_res = await _search_serper(clean_query, max_results)
    if serper_res:
        items, answer = serper_res
        return _format_search_markdown(clean_query, items, answer=answer, provider="Google/Serper")

    # 4. Parallel Search MCP
    parallel_res = await _search_parallel(clean_query)
    if parallel_res:
        return parallel_res

    # 5. DuckDuckGo
    ddg_res = await _search_duckduckgo(clean_query, max_results)
    if ddg_res:
        return ddg_res

    return (
        f"No results found for \"{clean_query}\" across all available search providers. "
        "Try rephrasing the query or delegate to deepsearch if an interactive site is required."
    )


# ---------------------------------------------------------------------------
# Individual Scrape / Page Read Providers
# ---------------------------------------------------------------------------

def extract_readable_text(html: str, max_chars: int = DEFAULT_MAX_READ_CHARS) -> str:
    """Extract readable text from HTML by removing boilerplate."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    if not text:
        return "(no readable text content found on this page)"
    if len(text) > max_chars:
        text = text[:max_chars] + " ... (truncated)"
    return text


async def _read_jina(url: str, max_chars: int) -> str | None:
    """Jina Reader API (r.jina.ai)."""
    try:
        content = await _jina_fetch_rendered_page_text(url, max_chars=max_chars)
        if content and not content.startswith("ERROR:"):
            return content
        console.system(f"[read:jina] {content[:100]}... falling back.")
    except Exception as e:
        console.system(f"[read:jina] Failed: {e}, falling back.")
    return None


async def _read_firecrawl(url: str, max_chars: int) -> str | None:
    """Firecrawl Cloud Scrape API (https://api.firecrawl.dev/v1/scrape)."""
    if not config.FIRECRAWL_API_KEY:
        return None
    try:
        headers = {
            "Authorization": f"Bearer {config.FIRECRAWL_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=config.SEARCH_HTTP_TIMEOUT_SECONDS * 2) as client:
            resp = await client.post(
                f"{config.FIRECRAWL_BASE_URL}/v1/scrape",
                headers=headers,
                json={"url": url, "formats": ["markdown"]},
            )
        if resp.status_code == 429:
            console.system("[read:firecrawl] Rate limit (429) hit, falling back.")
            return None
        if resp.status_code >= 400:
            console.system(f"[read:firecrawl] HTTP {resp.status_code}, falling back.")
            return None
        data = resp.json()
        markdown = data.get("data", {}).get("markdown")
        if markdown:
            if len(markdown) > max_chars:
                markdown = markdown[:max_chars] + " ... (truncated)"
            return markdown
    except Exception as e:
        console.system(f"[read:firecrawl] Failed: {e}, falling back.")
    return None


async def _read_parallel(url: str) -> str | None:
    """Parallel Web Fetch MCP."""
    try:
        content = await _parallel_web_fetch(url)
        if content and not content.startswith("ERROR:"):
            return content
    except Exception as e:
        console.system(f"[read:parallel] Failed: {e}, falling back.")
    return None


async def _read_http(url: str, max_chars: int) -> str | None:
    """Direct HTTP GET + HTML parser fallback."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
        if resp.status_code < 400 and resp.text:
            return extract_readable_text(resp.text, max_chars=max_chars)
    except Exception as e:
        console.system(f"[read:http] Failed: {e}.")
    return None


# ---------------------------------------------------------------------------
# Unified Read Function
# ---------------------------------------------------------------------------

async def unified_web_read(url: str, max_chars: int = DEFAULT_MAX_READ_CHARS) -> str:
    """Reads a web page across the waterfall:
    Jina Reader -> Firecrawl Cloud -> Parallel Web Fetch -> Direct HTTP.
    """
    clean_url = url.strip()
    if not clean_url:
        return "ERROR: empty URL."

    # 1. Jina Reader
    jina_text = await _read_jina(clean_url, max_chars)
    if jina_text:
        return jina_text

    # 2. Firecrawl Cloud
    firecrawl_text = await _read_firecrawl(clean_url, max_chars)
    if firecrawl_text:
        return firecrawl_text

    # 3. Parallel Web Fetch
    parallel_text = await _read_parallel(clean_url)
    if parallel_text:
        return parallel_text

    # 4. Direct HTTP
    http_text = await _read_http(clean_url, max_chars)
    if http_text:
        return http_text

    return (
        f"ERROR reading {clean_url}: all page reader providers failed or returned empty content. "
        "If this page requires login, complex cookies, or human interaction, delegate to deepsearch."
    )
