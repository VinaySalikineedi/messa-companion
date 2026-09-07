"""Stagehand v4 Tool Provider for Messa Deepsearch.

Replaces the micro-step @playwright/mcp architecture (which produces 30,000-line
DOM accessibility trees on every turn) with Stagehand v4's in-browser AI engine.

Equips the Deepsearch agent with macro-action tools:
  - `browser_navigate`: Navigates to a URL.
  - `browser_act`: High-level natural-language action execution in the browser
    (e.g., "search for Gurunanda toothbrush and add to cart", "type zip code 32256 and apply").
  - `browser_extract`: Extracts structured facts or summaries from the page
    without dumping massive raw DOM trees into context.
  - `browser_observe`: Discovers interactive elements and candidate actions.
  - `request_human_help`: Preserves Messa's human-in-the-loop escalation gate.
  - `await_email_verification_code`: Automated OTP verification listener.
  - `generate_account_credential` / `get_account_credential`: Credential vault.
  - `search_web` / `read_webpage`: Fast zero-browser search & scrape waterfall.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from openai import AsyncOpenAI
import stagehand
from stagehand._generated.models import (
    LLMStructuredGenerateParams,
    LLMMessageGenerateParams,
    LLMStructuredGenerateResult,
    LLMMessageGenerateResult,
    LLMTextContent,
)

from urllib.parse import urlparse

from .. import config, console, credentials, db, live_activity
from ..approval import ApprovalGate
from ..channels import browserbase
from .search_engine import unified_web_read, unified_web_search

logger = logging.getLogger(__name__)


def _get_domain(url: str) -> str:
    if not url:
        return "default"
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = parsed.netloc or parsed.path
        if ":" in host:
            host = host.split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        return host.lower() or "default"
    except Exception:
        return "default"

_CAPTCHA_KEYWORDS = (
    "captcha",
    "robot",
    "human verification",
    "verify you are human",
    "press and hold",
    "press & hold",
    "security check",
    "security challenge",
    "bot detection",
    "turnstile",
    "cloudflare",
    "arkose",
    "recaptcha",
    "hcaptcha",
)

STAGEHAND_SUBAGENT_SYSTEM_PROMPT = (
    "You are a deepsearch sub-worker powered by Stagehand v4, delegated to work on exactly ONE website: {url}. "
    "You have your OWN browser tab, separate from other sub-workers running at the same time.\n"
    "- Perform the assigned instructions directly and efficiently using `browser_navigate`, `browser_act`, and `browser_extract`.\n"
    "- Use `browser_act` with high-level natural language instructions (e.g. 'search for Nespresso Vertuo and press Enter', 'click Add to Cart on the top result').\n"
    "- Use `browser_extract` to pull structured facts, prices, ratings, or delivery information.\n"
    "- CAPTCHA POLICY (CUT RETRIES TO 2): If you encounter a CAPTCHA or robot challenge, attempt to solve it AT MOST 2 times. "
    "If it still blocks you after 2 attempts, STOP retrying immediately. CAPTCHAs are hard to pass automatically. "
    "Report that the website is blocked by CAPTCHA and summarize whatever data was already found (or fall back to search_web).\n"
    "- When your goal is achieved, reply with a concise, clear summary of what you found or completed.\n"
)

STAGEHAND_SYSTEM_PROMPT = (
    "You are deepsearch, Messa's fast, autonomous browser automation agent powered by Stagehand v4. "
    "You perform web browsing and research tasks delegated to you by Messa. You control a real "
    "remote browser on Browserbase using high-level macro-actions.\n\n"
    "HOW TO BROWSE WITH MAXIMUM SPEED (MACRO-ACTIONS):\n"
    "- Use `browser_navigate(url)` to open a website or search URL directly (e.g. `https://www.amazon.com/s?k=Kindle+Paperwhite`).\n"
    "- Use `browser_act(instruction)` to perform natural language actions:\n"
    "  * 'click Add to Cart on the top search result'\n"
    "  * 'open delivery location modal, enter zip code 78701, and click Apply'\n"
    "  * 'fill in the form and click Submit'\n"
    "  * 'click Proceed to Checkout'\n\n"
    "MULTI-SITE DELEGATION (PARALLEL TABS):\n"
    "- If a task asks you to compare, check, or research MULTIPLE websites or stores (e.g. Target and Walmart):\n"
    "  Call `delegate_website_task(url, instructions)` for EACH website ALL IN THE SAME TURN! "
    "  Both sub-workers will run simultaneously in parallel tabs within the same browser session and return their findings.\n\n"
    "CRITICAL SPEED RULES (AVOID SPLIT TURNS & DELAYS):\n"
    "1. CHAIN FILL AND SUBMIT: When filling any form, search input, or modal, ALWAYS combine the input and the submit/apply button into ONE `browser_act` instruction!\n"
    "   * Example: `browser_act('type 78701 in the zip code field and click Apply')`\n"
    "   * Example: `browser_act('search for Kindle Paperwhite in the search bar and press Enter')`\n"
    "   NEVER split typing into one turn and clicking the button or pressing enter into a separate turn.\n"
    "2. DIRECT NAVIGATION FOR SEARCH: When searching Amazon or store sites, you can navigate directly to the search results URL "
    "(e.g. `browser_navigate('https://www.amazon.com/s?k=Kindle+Paperwhite')`) to instantly load results in 1 turn!\n"
    "3. NEVER OBSERVE BEFORE ACTING: `browser_act` finds elements by description automatically. "
    "DO NOT call `browser_observe` before clicking or adding to cart. Simply call `browser_act('click Add to Cart on the top result')` directly.\n"
    "4. TRUST SUCCESSFUL ACTIONS: When `browser_act` completes, proceed immediately to the next task step without redundant intermediate verification.\n\n"
    "EFFICIENCY & COST CONTROLS:\n"
    "- If you just need general facts, links, or background info, call `search_web` first -- it is "
    "instant (<1s) and costs ZERO browser time. Never navigate to google.com or a search engine tab.\n"
    "- Use `read_webpage(url)` to read articles or static pages without spending browser minutes.\n"
    "- CLOSE BROWSER EARLY: The moment your browsing tasks are finished, or if you hit a CAPTCHA and decide "
    "to switch to `search_web`, you MUST call `close_browser()` immediately! Never leave the remote browser open "
    "while you do web searches or write your response. Calling `close_browser()` immediately stops billing.\n\n"
    "CAPTCHA & HUMAN ESCALATION (CUT RETRIES TO 2):\n"
    "- Hit an unsolved CAPTCHA or bot verification? You are permitted AT MOST 2 attempts to solve or click it. "
    "CAPTCHAs are hard to pass automatically. If it does not clear after 2 attempts, STOP RETRYING immediately! "
    "Call `close_browser()` and switch to `search_web` to find the information, or call `request_human_help(reason)` "
    "if you need the user to solve it.\n"
    "- Hit a 2FA prompt or login wall you don't have credentials for? "
    "First check `get_account_credential(site_name)`. If none is stored, call `request_human_help(reason)` "
    "immediately so the user can assist via their live view.\n"
    "- Hit an email verification code screen after signup? Call `await_email_verification_code`.\n"
    "- When creating accounts, use `generate_account_credential(site_name)` to generate a secure password. "
    "Never invent passwords or expose them in your final summary.\n"
    "- When your goal is achieved, reply with a concise, clear summary of what you did and found.\n"
)


def _build_stagehand_openrouter_callback(model_name: str, api_key: str):
    """Bridges Stagehand v4 in-browser reasoning requests to OpenRouter via client LLM callback.

    This routes all DOM extraction, action planning (act), and observation (observe)
    through OpenRouter using the user's OPENROUTER_API_KEY, completely bypassing
    Browserbase's Model Gateway ($0.00 Browserbase model spend).
    """
    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    async def _callback(params: LLMStructuredGenerateParams | LLMMessageGenerateParams):
        is_structured = isinstance(params, LLMStructuredGenerateParams)
        openai_messages = []
        if params.system_prompt:
            openai_messages.append({"role": "system", "content": params.system_prompt})

        d = params.model_dump()
        for m in d.get("messages", []):
            role = m.get("role")
            raw_content = m.get("content")
            if isinstance(raw_content, str):
                openai_messages.append({"role": role, "content": raw_content})
            elif isinstance(raw_content, dict):
                if raw_content.get("type") == "text":
                    openai_messages.append({"role": role, "content": raw_content.get("text", "")})
                elif raw_content.get("type") == "image":
                    mime = raw_content.get("mime_type", "image/png")
                    b64 = raw_content.get("data", "")
                    openai_messages.append({
                        "role": role,
                        "content": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
                    })
            elif isinstance(raw_content, list):
                parts = []
                for part in raw_content:
                    if part.get("type") == "text":
                        parts.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image":
                        mime = part.get("mime_type", "image/png")
                        b64 = part.get("data", "")
                        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                openai_messages.append({"role": role, "content": parts})
            else:
                openai_messages.append({"role": role, "content": str(raw_content)})

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": openai_messages,
        }
        if params.temperature is not None:
            kwargs["temperature"] = params.temperature

        if is_structured:
            rf = d.get("response_format")
            if rf and rf.get("schema_"):
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": rf.get("name") or "Response",
                        "schema": rf.get("schema_"),
                        "strict": True,
                    }
                }
            else:
                kwargs["response_format"] = {"type": "json_object"}

            resp = await client.chat.completions.create(**kwargs)
            text_out = resp.choices[0].message.content or "{}"
            try:
                parsed = json.loads(text_out)
            except Exception:
                parsed = {}

            return LLMStructuredGenerateResult(
                role="assistant",
                content=[LLMTextContent(type="text", text=text_out)],
                output_format="json_schema",
                structured_content=parsed,
            )
        else:
            resp = await client.chat.completions.create(**kwargs)
            text_out = resp.choices[0].message.content or ""
            return LLMMessageGenerateResult(
                role="assistant",
                content=[LLMTextContent(type="text", text=text_out)],
                output_format="text",
            )

    return _callback


class StagehandToolProvider:
    """Owns a Stagehand v4 session connected to Browserbase. Exposes macro-action
    browser tools to LangChain agents."""

    def __init__(
        self,
        approval_gate: ApprovalGate | None = None,
        user_id: int | None = None,
        deepsearch_session_id: int | None = None,
        *,
        model: Any = None,
        messa_email: str | None = None,
        user_email: str | None = None,
        task_title: str | None = None,
        phone_number: str | None = None,
        live_view_share_url: str | None = None,
        initial_resume_url: str | None = None,
        page: Any = None,
        stagehand_instance: Any = None,
        browser_instance: Any = None,
        is_subagent: bool = False,
        captcha_tracker: dict[str, int] | None = None,
    ):
        self._approval_gate = approval_gate
        self._user_id = user_id
        self._deepsearch_session_id = deepsearch_session_id
        self._model = model
        self._messa_email = messa_email
        self._user_email = user_email
        self._task_title = task_title
        self._phone_number = phone_number
        self._live_view_share_url = live_view_share_url
        self._initial_resume_url = initial_resume_url
        self._current_url: str = initial_resume_url or ""
        self._tab_id = f"tab-{uuid.uuid4().hex[:10]}"
        self._is_subagent = is_subagent

        self.browser = browser_instance
        self.stagehand = stagehand_instance
        self._bb_session_id: str | None = None
        self.live_view_url: str | None = None
        self.tools: list[BaseTool] = []
        self._page = page
        self._captcha_tracker: dict[str, int] = captcha_tracker if captcha_tracker is not None else {}
        self._session_lock: asyncio.Lock = asyncio.Lock()

    async def __aenter__(self) -> StagehandToolProvider:
        if self._is_subagent:
            self._build_tools()
            return self

        if not config.BROWSERBASE_API_KEY:
            raise RuntimeError("BROWSERBASE_API_KEY is not set in environment.")

        # Do NOT open the Browserbase session here. Instead, build the tools
        # immediately so the agent can start (and think), and defer the actual
        # browser launch to _ensure_session(), which is called lazily by the
        # first browser tool that the LLM actually invokes. This way we don't
        # pay for idle Browserbase time while the LLM is deciding what to do.
        self._build_tools()
        return self

    async def _ensure_session(self) -> None:
        """Open the Browserbase session on the first browser tool call.

        All browser-touching tools call this before doing any real work.
        Non-browser tools (search_web, read_webpage, credentials) skip this
        entirely so they stay zero-cost even when no tab is ever needed.
        Idempotent and concurrency-safe: subsequent calls return immediately.
        """
        if self.browser is not None:
            # Session already open — fast path.
            return

        async with self._session_lock:
            if self.browser is not None:
                return

            context_id = None
            if self._user_id is not None:
                context_id = await db.get_browserbase_context_id(self._user_id)

            browser_settings: dict[str, Any] = {
                "block_ads": config.DEEPSEARCH_BLOCK_ADS,
                "solve_captchas": True,
                "viewport": {"width": 1280, "height": 800},
            }
            if context_id:
                browser_settings["context"] = {"id": context_id, "persist": True}

            console.system("Deepsearch (Stagehand v4): launching remote Browserbase browser (first browser tool call)...")
            self.browser = await stagehand.browserbase.launch(
                api_key=config.BROWSERBASE_API_KEY,
                browser_settings=browser_settings,
                timeout=float(config.BROWSERBASE_SESSION_TIMEOUT_SECONDS),
            )
            self._bb_session_id = self.browser.session_id
            console.system(f"Deepsearch (Stagehand v4): session started (ID: {self._bb_session_id}).")

            # Fetch live view debugger link
            try:
                self.live_view_url = await browserbase.get_live_view_url(self._bb_session_id)
                if self.live_view_url:
                    console.system(f"Deepsearch (Stagehand v4): Live View debugger -> {self.live_view_url}")
            except Exception as e:
                logger.warning(f"Could not fetch live view URL: {e}")
                self.live_view_url = None

            if self._user_id is not None:
                live_activity.set_session_id(self._user_id, self._bb_session_id)
                if self.live_view_url:
                    live_activity.set_url(self._user_id, self.live_view_url)
                    await db.set_live_browser_active(
                        self._user_id, self.live_view_url, self._task_title or "Deepsearch"
                    )

            # Create Stagehand instance with configured LLM
            sh_kwargs: dict[str, Any] = {"browser": self.browser}

            if config.STAGEHAND_MODEL_API_KEY:
                # Direct provider API key supplied (e.g. direct OpenAI or Anthropic key)
                sh_kwargs["model"] = config.STAGEHAND_MODEL
                sh_kwargs["model_api_key"] = config.STAGEHAND_MODEL_API_KEY
                console.system(f"Deepsearch (Stagehand v4): using direct model provider {config.STAGEHAND_MODEL}.")
            else:
                # Route through OpenRouter via client LLM callback (default, $0 Browserbase model spend)
                sh_model = config.STAGEHAND_MODEL or "google/gemini-2.5-flash"
                sh_kwargs["model"] = _build_stagehand_openrouter_callback(
                    model_name=sh_model,
                    api_key=config.OPENROUTER_API_KEY,
                )
                console.system(f"Deepsearch (Stagehand v4): routing browser reasoning through OpenRouter ({sh_model}) - $0 Browserbase model spend.")

            self.stagehand = await stagehand.Stagehand.create(**sh_kwargs)
            console.system("Deepsearch (Stagehand v4): Stagehand engine initialized.")

            # Initialize active page
            self._page = await self.get_active_page()

            if self._initial_resume_url and self._page:
                try:
                    await self._page.goto(self._initial_resume_url)
                    self._current_url = self._initial_resume_url
                except Exception as e:
                    logger.warning(f"Failed to auto-reopen initial_resume_url: {e}")

    async def _close_browser(self) -> str:
        """Immediately close and release the remote browser session to stop Browserbase billing."""
        if self._is_subagent:
            return "Sub-workers run in shared tabs and cannot close the browser session."

        if self.browser is None and self.stagehand is None:
            return "Browser session is already closed (no billing active)."

        console.system(f"Deepsearch (Stagehand v4): releasing browser session #{self._deepsearch_session_id} to stop billing...")
        if self.stagehand is not None:
            try:
                await self.stagehand.close()
            except Exception as e:
                logger.debug(f"Stagehand close exception: {e}")
            self.stagehand = None

        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception as e:
                logger.debug(f"Browser close exception: {e}")
            self.browser = None

        self._page = None

        if self._user_id is not None:
            try:
                await db.clear_live_browser_active(self._user_id)
                live_activity.clear(self._user_id)
            except Exception as e:
                logger.debug(f"Clear live activity exception: {e}")

        return "Remote browser session closed successfully. Browserbase billing stopped."

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._is_subagent:
            return
        await self._close_browser()

    async def get_active_page(self) -> stagehand.Page:
        if self._page is not None:
            return self._page
        if self.browser is None:
            raise RuntimeError("Browser is not open.")
        page = await self.browser.context.active_page()
        if page is None:
            pages = await self.browser.context.pages()
            page = pages[0] if pages else None
        if page is None:
            raise RuntimeError("No active page available in browser context.")
        self._page = page
        return page

    def _build_tools(self) -> None:
        self.tools = [
            StructuredTool.from_function(
                coroutine=self._browser_navigate,
                name="browser_navigate",
                description=(
                    "Navigate the browser to a given URL. "
                    "Example: browser_navigate(url='https://www.amazon.com')."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._browser_act,
                name="browser_act",
                description=(
                    "Execute a natural language action directly. Stagehand automatically finds and interacts with elements "
                    "by description -- you do NOT need to observe first. "
                    "Examples: "
                    "browser_act(instruction='search for Hydro Flask water bottle and press Enter'), "
                    "browser_act(instruction='click Add to Cart on the first search result'), "
                    "browser_act(instruction='click the delivery location button, enter 94105 in zip code, and click Apply')."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._browser_extract,
                name="browser_extract",
                description=(
                    "Extract structured facts, text, prices, or summaries from the current page using Stagehand v4. "
                    "Examples: "
                    "browser_extract(instruction='extract the cart subtotal, list of items, and quantities'), "
                    "browser_extract(instruction='extract the product title, price, and customer rating')."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._browser_observe,
                name="browser_observe",
                description=(
                    "Inspect the current page to discover interactive elements or candidate actions matching an intent. "
                    "Use ONLY when stuck, when an action fails, or on an unfamiliar UI. Do NOT call after standard successful actions."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._browser_screenshot,
                name="browser_screenshot",
                description="Capture a visual screenshot of the current page.",
            ),
            StructuredTool.from_function(
                coroutine=self._request_human_help,
                name="request_human_help",
                description=(
                    "Call this when you hit a CAPTCHA, 2FA screen, login wall, or payment verification "
                    "that requires the user's manual action. Pauses and waits for the user to complete it."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._await_email_verification_code,
                name="await_email_verification_code",
                description=(
                    "Wait for a 4-8 digit verification code sent to the user's Messa email address after signing up."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._generate_account_credential,
                name="generate_account_credential",
                description=(
                    "Generate and securely store a new password for signing up on a site."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._get_account_credential,
                name="get_account_credential",
                description=(
                    "Retrieve a previously generated username and password for a site."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._search_web,
                name="search_web",
                description=(
                    "Search the web instantly without opening a browser or consuming browser session time. "
                    "Use this for researching facts, comparing options, or finding URLs in <1 second."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._read_webpage,
                name="read_webpage",
                description=(
                    "Fetch and read a web page's text content instantly without using the browser session. "
                    "Costs ZERO browser time."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self._close_browser,
                name="close_browser",
                description=(
                    "Immediately close the remote browser session to STOP Browserbase billing. "
                    "Call this the MOMENT you finish your browsing tasks, when switching to web search, "
                    "or before drafting your final response. Once closed, you can still use search_web or format your output."
                ),
            ),
        ]

        if not self._is_subagent:
            self.tools.append(
                StructuredTool.from_function(
                    coroutine=self._delegate_website_task,
                    name="delegate_website_task",
                    description=(
                        "Delegate independent browsing work on ONE website to a fresh sub-worker with its "
                        "OWN browser tab in the SAME shared browser session. Call this MULTIPLE TIMES IN THE SAME TURN "
                        "for independent multi-site tasks (e.g. comparing prices on Target and Walmart simultaneously). "
                        "Example: delegate_website_task(url='https://www.target.com', instructions='search for Nespresso Vertuo and find the price')."
                    ),
                )
            )

    async def _delegate_website_task(self, url: str, instructions: str) -> str:
        """Delegate independent work on ONE website to a fresh sub-worker with its
        OWN browser tab in the SAME shared Browserbase session."""
        if self._is_subagent:
            return "ERROR: delegate_website_task cannot be called from within a sub-worker (no recursive delegation)."
        if self._model is None:
            return "ERROR: delegate_website_task isn't available in this context."

        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        # Ensure the Browserbase session is open before opening sub-tabs.
        await self._ensure_session()

        console.system(f"Deepsearch (Stagehand v4): delegating sub-worker to {url}...")
        task_start = time.monotonic()
        try:
            sub_page = await self.browser.context.new_page()
        except Exception as e:
            return f"[{url}] ERROR: failed to open tab: {e}"

        tab_id = f"tab-{uuid.uuid4().hex[:8]}"
        if self._user_id is not None:
            live_activity.set_tab(self._user_id, tab_id, url)

        try:
            sub_provider = StagehandToolProvider(
                approval_gate=self._approval_gate,
                user_id=self._user_id,
                deepsearch_session_id=self._deepsearch_session_id,
                model=self._model,
                messa_email=self._messa_email,
                user_email=self._user_email,
                task_title=self._task_title,
                phone_number=self._phone_number,
                page=sub_page,
                stagehand_instance=self.stagehand,
                browser_instance=self.browser,
                is_subagent=True,
                captcha_tracker=self._captcha_tracker,
            )
            async with sub_provider as worker:
                sub_agent = create_agent(
                    model=self._model,
                    tools=worker.tools,
                    system_prompt=STAGEHAND_SUBAGENT_SYSTEM_PROMPT.format(url=url),
                )
                try:
                    result = await asyncio.wait_for(
                        sub_agent.ainvoke(
                            {"messages": [HumanMessage(content=f"Navigate to {url}, then: {instructions}")]},
                            config={"recursion_limit": config.DEEPSEARCH_SUBAGENT_MAX_STEPS},
                        ),
                        timeout=config.DEEPSEARCH_MAX_SESSION_SECONDS,
                    )
                    messages = result.get("messages", [])
                    summary = messages[-1].content if messages else "(no output)"
                except Exception as e:
                    console.tool_error("DEEPSEARCH", "stagehand_subagent", str(e))
                    summary = f"(sub-worker error: {e})"
        finally:
            if self._user_id is not None:
                live_activity.clear_tab(self._user_id, tab_id)
            try:
                await sub_page.close()
            except Exception:
                pass
            console.system(f"Deepsearch (Stagehand v4): sub-worker for {url} completed in {time.monotonic() - task_start:.1f}s.")

        return f"[{url}] {summary}"

    async def _get_url(self, page: Any) -> str:
        try:
            url_attr = getattr(page, "url", "")
            if callable(url_attr):
                res = url_attr()
                if inspect.isawaitable(res):
                    return str(await res)
                return str(res)
            if inspect.isawaitable(url_attr):
                return str(await url_attr)
            return str(url_attr)
        except Exception:
            return ""

    async def _browser_navigate(self, url: str) -> str:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        await self._ensure_session()
        try:
            page = await self.get_active_page()
            console.system(f"Deepsearch (Stagehand v4): navigating to {url}")
            await page.goto(url)
            self._current_url = await self._get_url(page)
            title = await page.title()
            if self._user_id is not None:
                live_activity.set_url(self._user_id, self._current_url)
            return f"Navigated to {url}. Page Title: {title}"
        except Exception as e:
            return f"Navigation to {url} failed: {e}"

    async def _browser_act(self, instruction: str) -> str:
        await self._ensure_session()
        try:
            page = await self.get_active_page()
            curr_url = await self._get_url(page)
            domain = _get_domain(curr_url or self._current_url)

            # Check if this action targets a CAPTCHA or if current page is blocked by one
            inst_lower = instruction.lower()
            is_captcha_target = any(kw in inst_lower for kw in _CAPTCHA_KEYWORDS)
            if not is_captcha_target:
                try:
                    title = await page.title()
                    if any(kw in title.lower() for kw in ("robot or human", "just a moment", "captcha", "security check", "human verification", "bot verification", "security challenge")):
                        is_captcha_target = True
                except Exception:
                    pass

            if is_captcha_target:
                domain_attempts = self._captcha_tracker.get(domain, 0) + 1
                self._captcha_tracker[domain] = domain_attempts
                if domain_attempts > config.DEEPSEARCH_CAPTCHA_MAX_RETRIES:
                    console.system(
                        f"Deepsearch (Stagehand v4): CAPTCHA retry limit reached for {domain} "
                        f"({domain_attempts} > {config.DEEPSEARCH_CAPTCHA_MAX_RETRIES}) across session."
                    )
                    if not self._is_subagent:
                        return (
                            f"BLOCKED: CAPTCHA retry limit reached for {domain} ({config.DEEPSEARCH_CAPTCHA_MAX_RETRIES} attempts). "
                            "CAPTCHAs are hard to pass automatically. Do NOT retry solving this CAPTCHA again. "
                            "Call close_browser() and switch to search_web, or call request_human_help if manual user intervention is needed."
                        )
                    else:
                        return (
                            f"BLOCKED: CAPTCHA retry limit reached for {domain} ({config.DEEPSEARCH_CAPTCHA_MAX_RETRIES} attempts). "
                            "CAPTCHAs are hard to pass automatically. Do NOT retry solving this CAPTCHA. "
                            "Stop retrying: report this website as blocked by CAPTCHA and conclude your summary."
                        )

            console.system(f"Deepsearch (Stagehand v4): executing act: {instruction!r}")
            res = await self.stagehand.act(instruction, page=page)
            curr_url = await self._get_url(page)
            domain = _get_domain(curr_url or self._current_url)
            msg = getattr(res.data, "message", "Action performed successfully")
            console.system(f"Deepsearch (Stagehand v4): act completed ({msg})")

            # Check if page cleared the CAPTCHA
            try:
                title = await page.title()
            except Exception:
                title = ""

            page_looks_like_captcha = any(
                kw in f"{curr_url} {title}".lower()
                for kw in ("captcha", "robot or human", "verify you are human", "press and hold", "challenge", "just a moment")
            )
            if not page_looks_like_captcha and self._captcha_tracker.get(domain, 0) > 0:
                self._captcha_tracker[domain] = 0

            # If this was a CAPTCHA attempt and we reached the retry cap, inform the agent to stop retrying
            if is_captcha_target and self._captcha_tracker.get(domain, 0) >= config.DEEPSEARCH_CAPTCHA_MAX_RETRIES:
                msg += (
                    f"\n\n[CAPTCHA NOTICE: You have attempted this CAPTCHA for {domain} {self._captcha_tracker[domain]} time(s). "
                    f"Maximum allowed retries is {config.DEEPSEARCH_CAPTCHA_MAX_RETRIES}. "
                    "CAPTCHAs are hard to pass automatically. Do NOT attempt to solve this CAPTCHA again. "
                    + ("Call close_browser() and switch to search_web, or call request_human_help.]" if not self._is_subagent else "Report this site is blocked by CAPTCHA and finish summary.]")
                )

            return f"Action Result: {msg}\nCurrent Page: {curr_url}"
        except Exception as e:
            console.tool_error("DEEPSEARCH", "stagehand_act", str(e))
            return f"Error executing action {instruction!r}: {e}"

    async def _browser_extract(self, instruction: str) -> str:
        await self._ensure_session()
        try:
            page = await self.get_active_page()
            curr_url = await self._get_url(page)
            domain = _get_domain(curr_url or self._current_url)
            if self._captcha_tracker.get(domain, 0) >= config.DEEPSEARCH_CAPTCHA_MAX_RETRIES:
                try:
                    title = await page.title()
                except Exception:
                    title = ""
                if any(
                    kw in f"{curr_url} {title}".lower()
                    for kw in ("captcha", "robot or human", "verify you are human", "challenge", "just a moment")
                ):
                    return (
                        f"BLOCKED: The page on {domain} is currently displaying an unsolved CAPTCHA (retry limit of "
                        f"{config.DEEPSEARCH_CAPTCHA_MAX_RETRIES} reached). Do not retry. "
                        + ("Call close_browser() and switch to search_web." if not self._is_subagent else "Report site blocked by CAPTCHA.")
                    )

            console.system(f"Deepsearch (Stagehand v4): executing extract: {instruction!r}")
            res = await self.stagehand.extract(instruction, page=page)
            extracted = getattr(res.data, "extraction", str(res.data))
            console.system("Deepsearch (Stagehand v4): extraction completed")
            return f"Extracted Content:\n{extracted}"
        except Exception as e:
            console.tool_error("DEEPSEARCH", "stagehand_extract", str(e))
            return f"Error extracting {instruction!r}: {e}"

    async def _browser_observe(self, instruction: str = "") -> str:
        await self._ensure_session()
        try:
            page = await self.get_active_page()
            curr_url = await self._get_url(page)
            domain = _get_domain(curr_url or self._current_url)
            if self._captcha_tracker.get(domain, 0) >= config.DEEPSEARCH_CAPTCHA_MAX_RETRIES:
                try:
                    title = await page.title()
                except Exception:
                    title = ""
                if any(
                    kw in f"{curr_url} {title}".lower()
                    for kw in ("captcha", "robot or human", "verify you are human", "challenge", "just a moment")
                ):
                    return (
                        f"BLOCKED: The page on {domain} is displaying an unsolved CAPTCHA (retry limit of "
                        f"{config.DEEPSEARCH_CAPTCHA_MAX_RETRIES} reached). Do not retry. "
                        + ("Call close_browser() and switch to search_web." if not self._is_subagent else "Report site blocked by CAPTCHA.")
                    )

            inst = instruction.strip() or None
            console.system(f"Deepsearch (Stagehand v4): observing page (intent={inst!r})")
            res = await self.stagehand.observe(inst, page=page)
            actions = res.data or []
            if not actions:
                return "No matching interactive elements found on the page."
            formatted = "\n".join(
                f"- {getattr(a, 'description', '')} (method: {getattr(a, 'method', '')}, selector: {getattr(a, 'selector', '')})"
                for a in actions[:15]
            )
            return f"Observed Elements:\n{formatted}"
        except Exception as e:
            console.tool_error("DEEPSEARCH", "stagehand_observe", str(e))
            return f"Error observing page: {e}"

    async def _browser_screenshot(self) -> str:
        await self._ensure_session()
        try:
            page = await self.get_active_page()
            img_bytes = await page.screenshot()
            return f"Screenshot captured successfully ({len(img_bytes)} bytes)."
        except Exception as e:
            return f"Failed to capture screenshot: {e}"

    async def _request_human_help(self, reason: str) -> str:
        if self._user_id is None:
            return "ERROR: request_human_help requires an active user session."
        console.system(f"Deepsearch (Stagehand v4): requesting human help -- {reason}")
        request_row = await db.create_human_help_request(
            self._user_id, self._deepsearch_session_id, self._tab_id, reason
        )
        request_id = request_row["id"] if request_row else None
        live_activity.set_waiting_for_human(self._user_id, reason)

        try:
            await self._ensure_session()
            page = await self.get_active_page()
            start_url = await self._get_url(page)
            elapsed = 0
            while elapsed < config.DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS:
                await asyncio.sleep(config.DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS)
                elapsed += config.DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS
                curr_url = await self._get_url(page)
                if curr_url != start_url:
                    if request_id is not None:
                        await db.resolve_human_help_request(request_id)
                    console.system(f"Deepsearch (Stagehand v4): human help resolved (URL changed to {curr_url}).")
                    return f"RESOLVED: Page URL changed to {curr_url}. You are likely past the verification wall."
                if elapsed >= config.DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS:
                    break

            if request_id is not None:
                await db.timeout_human_help_request(request_id)
            return "BLOCKED: Human help timed out. Wrap up and report what help is needed."
        finally:
            live_activity.clear_waiting_for_human(self._user_id)

    async def _await_email_verification_code(self, sender_keyword: str = "") -> str:
        if self._user_id is None:
            return "ERROR: User ID missing."
        if not self._messa_email:
            return "ERROR: No Messa email provisioned for this user."
        console.system(f"Deepsearch (Stagehand v4): awaiting OTP code on {self._messa_email}")
        # Immediate lookback: code may have arrived during LLM planning
        recent = await db.find_recent_otp_in_inbox(self._user_id, sender_keyword, max_age_seconds=180)
        if recent and recent.get("code"):
            return f"VERIFICATION_CODE_RECEIVED: {recent['code']}"
        start = time.monotonic()
        while time.monotonic() - start < config.DEEPSEARCH_OTP_WAIT_MAX_SECONDS:
            await asyncio.sleep(config.DEEPSEARCH_OTP_WAIT_POLL_INTERVAL_SECONDS)
            recent = await db.find_recent_otp_in_inbox(self._user_id, sender_keyword, max_age_seconds=120)
            if recent and recent.get("code"):
                return f"VERIFICATION_CODE_RECEIVED: {recent['code']}"
        return "TIMEOUT: No verification code received within timeout window."

    async def _generate_account_credential(self, site_name: str, username: str | None = None) -> str:
        if self._user_id is None:
            return "ERROR: User ID missing."
        user_name = username or self._messa_email or f"user_{self._user_id}@messa.ai"
        password = uuid.uuid4().hex[:14] + "A1!"
        try:
            encrypted = credentials.encrypt_secret(password)
            await db.save_site_credential(self._user_id, site_name, user_name, encrypted)
            return (
                f"Generated credential for {site_name!r}: username={user_name}, password={password}. "
                "Use this to sign up now. Do NOT repeat the password in your final summary."
            )
        except Exception as e:
            return f"Error storing credential: {e}"

    async def _get_account_credential(self, site_name: str) -> str:
        if self._user_id is None:
            return "ERROR: User ID missing."
        row = await db.get_site_credential(self._user_id, site_name)
        if not row:
            return f"No credential found for site {site_name!r}."
        try:
            pwd = credentials.decrypt_secret(row["encrypted_password"])
            return f"Found credential for {site_name!r}: username={row['username']}, password={pwd}."
        except Exception as e:
            return f"Error decrypting credential: {e}"

    async def _search_web(self, query: str) -> str:
        # If the browser is open and ANY domain hit a CAPTCHA limit, shut down the browser
        # immediately so we don't pay for idle browser time while searching!
        any_blocked = any(count >= config.DEEPSEARCH_CAPTCHA_MAX_RETRIES for count in self._captcha_tracker.values())
        if not self._is_subagent and self.browser is not None and any_blocked:
            console.system("Deepsearch (Stagehand v4): auto-closing browser session on fallback to search_web to eliminate idle billing...")
            await self._close_browser()
        return await unified_web_search(query)

    async def _read_webpage(self, url: str) -> str:
        return await unified_web_read(url)
