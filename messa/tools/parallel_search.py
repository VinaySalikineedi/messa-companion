"""Parallel Search MCP -- a second, backup provider at the same free/fast
tier as `jina_reader.py`, not a replacement for it or for DuckDuckGo. A
hosted, remote MCP server (`https://search.parallel.ai/mcp` by default --
no local process to spawn, unlike `@playwright/mcp`) exposing two tools
purpose-built for AI agents: `web_search` (an agent-tuned search API,
Parallel's own published benchmarks claim better ranking than a generic
search engine) and `web_fetch` (JS-rendered and PDF-capable page reads --
the same job `jina_reader.py` does, from a different provider). Free and
keyless by default, same story as Jina: `config.PARALLEL_API_KEY` stays
unset until real usage outgrows the free tier.

Why a SECOND provider at this tier is worth having, not just Jina alone:
this whole tier exists so Messa (and deepsearch) can answer a read-only
question WITHOUT opening a Browserbase session -- if the one free provider
serving that tier has a bad day (rate-limited, briefly down), the fallback
today is jumping straight to a real, billed deepsearch delegation. A second
independent provider means that fallback is another free call first, not
an automatic escalation to something that costs real money. Same reasoning
DuckDuckGo/web_search and Parallel's own `web_search` now sit side by side
for the same reason -- see registry.py's "Speed AND cost matter" paragraph
for exactly how Messa is told to move between them.

Connection design: unlike `@playwright/mcp` (a subprocess deepsearch spawns
and keeps alive for a whole browsing delegation, since a live browser
session has to persist across many tool calls), Parallel's server is
remote, stateless, and shared across every user -- there's no per-user
browser tab to keep alive, so there's nothing to gain from holding a
persistent connection open across turns, and real complexity/risk in trying
to (a long-lived shared MCP session surviving across concurrent users,
reconnecting after a drop, etc.). So this opens a fresh, short-lived MCP
session for each individual tool call and closes it immediately after --
the same `langchain_mcp_adapters.MultiServerMCPClient` + `load_mcp_tools`
mechanism deepsearch_tools.py already uses for Playwright, just scoped to
one call instead of one whole delegation. A streamable-HTTP session's own
init handshake is one extra request-response round trip on top of the real
search/fetch call -- real, but nowhere near deepsearch's actual cost driver
(opening a Browserbase browser session, which is seconds, not the tens of
milliseconds a plain HTTP round trip adds); see config.PARALLEL_MCP_TIMEOUT_SECONDS
for the explicit ceiling on how long this is ever allowed to take before it
fails fast rather than silently holding Messa's reply up.

Tool schemas are loaded live from the MCP server on every call
(`load_mcp_tools`), not hand-copied here -- if Parallel changes their tool's
parameters, this keeps working without a code change on our side, the same
self-describing-schema property that makes MCP useful for Playwright's own
much-larger tool surface. The two `@tool`-decorated wrappers below
(`parallel_web_search`/`parallel_web_fetch`) still declare their OWN fixed
parameter names, since Messa's model-facing toolset needs a concrete,
static schema to call -- `objective`/`search_queries` and `url` are taken
directly from Parallel's own documented request shape (their REST Search
API's example request body, and the MCP docs' own description of the
`web_search` tool's arguments) -- see README's "Not verified" note: this
sandbox's network is allowlisted and can't reach search.parallel.ai to
confirm these parameter names against the LIVE server before a real
deployed run does.
"""
from __future__ import annotations

from typing import Any, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .. import config


async def _call_parallel_tool(tool_name: str, **kwargs: Any) -> str:
    """Open a fresh, short-lived MCP session against Parallel's remote
    server, call ONE named tool, and close -- see module docstring for why
    a per-call session (not a persistent one) is the right shape here.
    Fails closed and cheap, same contract as jina_reader.fetch_rendered_page_text:
    any error (connection, timeout, missing tool, a bad call) returns a
    short ERROR-prefixed string rather than raising, so a tool built on top
    of this never crashes the agent loop."""
    headers = {"x-api-key": config.PARALLEL_API_KEY} if config.PARALLEL_API_KEY else None
    client = MultiServerMCPClient({
        "parallel": {
            "url": config.PARALLEL_MCP_URL,
            "transport": "streamable_http",
            "headers": headers,
            "timeout": config.PARALLEL_MCP_TIMEOUT_SECONDS,
        }
    })
    try:
        async with client.session("parallel") as session:
            tools = await load_mcp_tools(session)
            tool = next((t for t in tools if t.name == tool_name), None)
            if tool is None:
                available = ", ".join(t.name for t in tools) or "(none)"
                return (
                    f"ERROR: Parallel Search MCP didn't expose a '{tool_name}' tool right now "
                    f"(it currently offers: {available}) -- its interface may have changed. "
                    "Try web_search/fetch_rendered_page_text instead."
                )
            result = await tool.coroutine(**kwargs)
            return str(result)
    except Exception as e:  # noqa: BLE001 - a tool must never crash the agent loop
        return (
            f"ERROR calling Parallel Search MCP ({tool_name}): {e}. Try web_search / "
            "fetch_rendered_page_text instead, or delegate to deepsearch if this can't wait."
        )


async def parallel_web_search(objective: str, search_queries: Optional[list[str]] = None) -> str:
    """Shared implementation behind the model-facing `parallel_web_search`
    tool (see web_search_tools.build_web_search_tools) -- pulled out as its
    own function purely so it's directly callable/testable without going
    through the LangChain @tool wrapper."""
    return await _call_parallel_tool("web_search", objective=objective, search_queries=search_queries or [])


async def parallel_web_fetch(url: str) -> str:
    """Shared implementation behind the model-facing `parallel_web_fetch`
    tool -- see parallel_web_search's docstring above for why this is a
    separate, directly-testable function."""
    return await _call_parallel_tool("web_fetch", url=url)
