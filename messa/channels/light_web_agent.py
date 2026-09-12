"""Universal Dynamic Web Navigation Agent (light-web-agent v2.2).

Features:
- Single Persistent Browser Session: Cloud tabs stay alive across turns without dying.
- Sense-Reflect-Act-Verify Cognitive Loop: Self-healing navigation with reflection.
- Macro-Action Batching with Staleness Guard: Multi-action execution per turn with target_signature.
- Zero-LLM Web Convention Prior: Instant fast-path landmark navigation.
- Intent-Alignment Critic: Consequential actions (order submit, payment, address) verified against user goal.
- Human-in-the-Loop Checkpoints: SMS OTP, CAPTCHA live view link, 3D-Secure, and risk review.
- Circuit Breakers: 15-step limit, exact-repeat detection, oscillation detection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .. import config, db, live_activity
from .browser_session_manager import ManagedBrowserSession, session_manager
from .light_web_perception import (
    PerceptionEngine,
    WebConventionPrior,
    apply_network_shield,
    compute_target_signature,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models (Pydantic)
# ---------------------------------------------------------------------------

class MacroStep(BaseModel):
    action: Literal["click", "type", "press_key", "press", "scroll", "select_option", "wait_for"]
    target_id: str                      # e.g., "e3" or "f1:e2"
    target_signature: str               # sha256(role:name:tag), captured after stabilization
    value: Optional[str] = None         # text or token (e.g., "{{cred:password}}")


class MacroAction(BaseModel):
    steps: List[MacroStep] = Field(default_factory=list)
    expect: Dict[str, Any] = Field(default_factory=dict)  # e.g., {"url_contains": "/checkout"}
    is_consequential: bool = False      # Routes through IntentAlignmentCheck if True


class IntentAlignmentCheck(BaseModel):
    """Gatekeeper for consequential actions. Evaluates whether the proposed
    action strictly aligns with the user's original goal without being tricked
    by indirect prompt injections on the page."""
    user_goal: str
    proposed_action_summary: str
    verdict: Literal["aligned", "misaligned", "uncertain"] = "aligned"
    reason: str = ""


class HumanCheckpoint(BaseModel):
    kind: Literal["otp", "captcha", "payment_3ds", "risk_review"]
    prompt_to_user: str
    interactive_url: Optional[str] = None
    timeout_seconds: int = 300
    trigger_reason: Optional[str] = None
    human_input: Optional[str] = None


class AgentDecision(BaseModel):
    status: Literal[
        "OK_CACHED",
        "OK_LANDMARK",
        "OK_REASONED",
        "BLOCKED_MODAL",
        "BLOCKED_INJECTION",
        "NEEDS_HUMAN",
        "DONE",
        "FAILED",
    ]
    thought: Optional[str] = None
    action: Optional[MacroAction] = None
    checkpoint: Optional[HumanCheckpoint] = None
    result_summary: Optional[str] = None
    scratchpad_updates: Optional[Dict[str, Any]] = None


GLOBAL_WEB_SKILL_CACHE: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Core Cognitive Engine: light-web-agent
# ---------------------------------------------------------------------------

class LightWebAgent:
    """Universal Dynamic Web Navigation Agent with Scratchpad, Skills & Live Activity."""

    def __init__(
        self,
        session: ManagedBrowserSession,
        user_goal: str,
        credentials: Optional[Dict[str, str]] = None,
        max_steps: int = 15,
        model_name: Optional[str] = None,
        critic_model_name: Optional[str] = None,
        user_id: Optional[Any] = None,
    ) -> None:
        self.session = session
        self.user_goal = user_goal
        self.credentials = credentials or {}
        self.max_steps = max_steps
        self.model_name = model_name or getattr(config, "LIGHT_WEB_AGENT_MODEL", "google/gemini-2.5-flash")
        self.critic_model_name = critic_model_name or getattr(config, "LIGHT_WEB_AGENT_CRITIC_MODEL", "openai/gpt-4o-mini")
        self.user_id = user_id or getattr(session, "user_id", None)
        self.int_user_id = int(self.user_id) if isinstance(self.user_id, (int, str)) and str(self.user_id).isdigit() else None

        self.step_history: List[Dict[str, Any]] = []
        self.action_history_signatures: List[str] = []
        self.is_completed = False
        self.result_summary: Optional[str] = None

        # Scratchpad & Skills state (Postgres-backed with in-memory fallback)
        self.scratchpad_artifacts: Dict[str, Any] = {}
        self.domain_skills: List[str] = []
        self.active_task_id: Optional[str] = None

    async def _init_scratchpad_and_skills(self, domain: str) -> None:
        """Load persistent active task scratchpad and learned skills from Postgres."""
        # 1. Active Task Scratchpad lookup
        if self.int_user_id is not None and getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            try:
                task = await db.get_active_task(self.int_user_id)
                if task is None:
                    task = await db.start_active_task(self.int_user_id, task_type="light_web_agent")
                if task:
                    self.active_task_id = task.get("task_id")
                    self.scratchpad_artifacts = task.get("artifacts") or {}
                    logger.info(f"[LightWebAgent] Attached to active task {self.active_task_id} with {len(self.scratchpad_artifacts)} artifacts.")
            except Exception as e:
                logger.debug(f"[LightWebAgent] Scratchpad DB lookup fallback: {e}")

        # 2. Skills Playbook lookup
        if domain and getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            try:
                skills = await db.search_skills("light_web_agent", domain)
                if not skills:
                    skills = await db.search_skills("deepsearch", domain)
                self.domain_skills = [s["solution_recipe"] for s in skills if s.get("solution_recipe")]
                if self.domain_skills:
                    logger.info(f"[LightWebAgent] Loaded {len(self.domain_skills)} skills for domain '{domain}'.")
            except Exception as e:
                logger.debug(f"[LightWebAgent] Skills DB search fallback: {e}")

    async def _update_scratchpad(self, updates: Dict[str, Any]) -> None:
        """Merge key-value facts into the active task scratchpad."""
        if not updates:
            return
        self.scratchpad_artifacts.update(updates)
        if self.active_task_id and getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            try:
                await db.update_active_task_artifacts(self.active_task_id, updates)
                logger.info(f"[LightWebAgent] Updated task scratchpad: {list(updates.keys())}")
            except Exception as e:
                logger.debug(f"[LightWebAgent] Scratchpad update fallback: {e}")

    async def _persist_skill(self, domain: str, pattern: str, recipe: str) -> None:
        """Persist learned lesson to cross-user skills playbook."""
        GLOBAL_WEB_SKILL_CACHE[domain] = {
            "intent": self.user_goal.lower(),
            "recipe": recipe,
            "pattern": pattern,
            "saved_at": time.time(),
        }
        if getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            try:
                await db.upsert_skill(
                    agent_type="light_web_agent",
                    domain=domain,
                    problem_pattern=pattern,
                    solution_recipe=recipe,
                    source_user_id=self.int_user_id,
                    source_task_id=self.active_task_id,
                )
                logger.info(f"[LightWebAgent] Persisted skill for {domain} to DB playbook: {recipe}")
            except Exception as e:
                logger.debug(f"[LightWebAgent] db.upsert_skill fallback: {e}")

    def _lookup_skill_cache(self, domain: str, goal: str) -> Optional[MacroAction]:
        """Check if a verified macro-action sequence is cached for this domain & intent."""
        if domain in GLOBAL_WEB_SKILL_CACHE:
            entry = GLOBAL_WEB_SKILL_CACHE[domain]
            steps_data = entry.get("steps", [])
            if steps_data:
                steps = [MacroStep(**s) for s in steps_data]
                return MacroAction(steps=steps)
        return None

    def _write_skill_cache(self, domain: str, goal: str, history: List[Dict[str, Any]]) -> None:
        """Cache a successful macro-action sequence for domain reuse."""
        collected_steps = []
        for h in history:
            act = h.get("action")
            if act and "steps" in act:
                collected_steps.extend(act["steps"])
        if collected_steps:
            GLOBAL_WEB_SKILL_CACHE[domain] = {
                "intent": goal.lower(),
                "steps": collected_steps,
                "saved_at": time.time(),
            }
            logger.info(f"[SkillCache] Cached {len(collected_steps)} macro steps for domain: {domain}")

    def _detect_and_uncheck_dark_patterns(
        self, elements: List[Dict[str, Any]], elem_map: Dict[str, Any]
    ) -> Optional[MacroAction]:
        """Detect and uncheck pre-checked deceptive add-ons (e.g. $9.99 protection plan)."""
        steps = []
        for el in elements:
            if el.get("dark_pattern") and el.get("checked"):
                sig = el.get("signature", "")
                steps.append(MacroStep(action="click", target_id=el["id"], target_signature=sig))
        if steps:
            logger.info(f"[DarkPatternDefense] Unchecking {len(steps)} pre-checked deceptive add-on(s).")
            return MacroAction(steps=steps)
        return None


    async def run(self, max_turns: Optional[int] = None) -> AgentDecision:
        """Run the Sense-Reflect-Act-Verify loop until completion or human checkpoint."""
        turns = max_turns or self.max_steps
        page = self.session.active_page
        if not page:
            return AgentDecision(status="FAILED", result_summary="No active page in browser session")

        # Attach residential network shield to save bandwidth
        await apply_network_shield(page)

        # Initialize scratchpad & domain skills from Messa DB
        current_url = page.url or ""
        domain = re.sub(r"^https?://(www\.)?", "", current_url).split("/")[0] if current_url else ""
        await self._init_scratchpad_and_skills(domain)

        for step_idx in range(len(self.step_history), turns):
            self.session.touch()
            print(f"\n[LightWebAgent Step {step_idx + 1}/{turns}] SENSING DOM...", flush=True)

            # 1. Circuit Breakers: Step count & Oscillation Detection
            breaker_decision = self._check_circuit_breakers()
            if breaker_decision:
                print(f"[LightWebAgent Circuit Breaker] {breaker_decision.result_summary}", flush=True)
                return breaker_decision

            # 2. SENSE: Visual Stabilization & Perception
            await self._wait_for_stabilization(page)
            elements, a11y_text = await PerceptionEngine.extract_pruned_a11y_tree(page)
            elem_map = {el["id"]: el for el in elements}
            current_url = page.url or ""
            current_title = await page.title()
            print(f"[LightWebAgent Page] URL: {current_url} | Title: {current_title} | Elements: {len(elements)}", flush=True)

            if self.int_user_id is not None:
                try:
                    live_activity.set_url(self.int_user_id, current_url)
                except Exception:
                    pass

            # 3. REFLECT & DEFEND: Check for human checkpoints (SMS OTP, CAPTCHA) FIRST
            checkpoint = self._detect_human_checkpoint(current_url, current_title, a11y_text)
            if not checkpoint and page:
                try:
                    body_text = (await page.inner_text("body", timeout=1000)).lower()
                    if any(w in body_text for w in [
                        "robot or human", "press & hold", "press and hold", "verify you are human",
                        "px-captcha", "perimeterx", "security check"
                    ]):
                        live_view = self.session.live_view_url or ""
                        url_msg = f"\nTap here to complete verification: {live_view}" if live_view else ""
                        checkpoint = HumanCheckpoint(
                            kind="captcha",
                            prompt_to_user=f"A verification puzzle appeared.{url_msg}\nPlease complete it so Messa can continue.",
                            interactive_url=live_view,
                            trigger_reason="captcha_challenge",
                        )
                except Exception:
                    pass

            if checkpoint:
                print(f"[LightWebAgent Human Checkpoint] Triggered: {checkpoint.kind.upper()} -> {checkpoint.prompt_to_user}", flush=True)
                logger.info(f"[LightWebAgent] Human checkpoint reached: {checkpoint.kind}")
                if self.int_user_id is not None:
                    try:
                        live_activity.set_waiting_for_human(self.int_user_id, checkpoint.prompt_to_user)
                    except Exception:
                        pass
                await session_manager.suspend_session(self.session.session_id, checkpoint.dict())
                return AgentDecision(
                    status="NEEDS_HUMAN",
                    thought=f"Detected {checkpoint.kind} requirement. Suspending session for human input.",
                    checkpoint=checkpoint,
                )

            # Check for blocking modals (promo, cookie dialogs)
            modal_action = self._detect_and_clear_blocking_modals(elements)
            if modal_action:
                print("[LightWebAgent Modal Defense] Anomaly/Promo modal detected. Dismissing obstacle...", flush=True)
                logger.info("[LightWebAgent] Anomaly/Promo modal detected. Dismissing obstacle first.")
                exec_ok, _ = await self._execute_macro_action(page, modal_action, elem_map)
                self.step_history.append({"type": "modal_dismiss", "action": modal_action.dict()})
                continue

            # 3.5 SKILL CACHE: Check for existing verified playbook on this domain
            if len(self.step_history) == 0:
                domain = re.sub(r"^https?://(www\.)?", "", current_url).split("/")[0]
                cached_action = self._lookup_skill_cache(domain, self.user_goal)
                if cached_action:
                    if all(s.target_id in elem_map for s in cached_action.steps):
                        print(f"[LightWebAgent Skill Cache] Replaying verified playbook for {domain} (0 LLM cost)...", flush=True)
                        logger.info(f"[LightWebAgent] Replaying verified skill cache for {domain}")
                        exec_ok, err = await self._execute_macro_action(page, cached_action, elem_map)
                        if exec_ok:
                            self.step_history.append({"type": "skill_cache", "action": cached_action.dict()})
                            continue

            # 3.6 DARK PATTERN DEFENSE: Uncheck deceptive pre-checked add-on fees
            dark_action = self._detect_and_uncheck_dark_patterns(elements, elem_map)
            if dark_action:
                print(f"[LightWebAgent Dark Pattern Defense] Unchecking deceptive fee add-ons...", flush=True)
                exec_ok, _ = await self._execute_macro_action(page, dark_action, elem_map)
                self.step_history.append({"type": "dark_pattern_uncheck", "action": dark_action.dict()})
                continue

            # 4. FAST-PATH: Web Convention Prior (Zero-LLM Landmark Match)
            landmark_action = self._try_landmark_fastpath(elem_map)
            if landmark_action:
                print(f"[LightWebAgent FastPath] Zero-LLM Landmark match -> {landmark_action.steps[0].action} {landmark_action.steps[0].target_id}", flush=True)
                logger.info("[LightWebAgent] Executing landmark fast-path action without LLM.")
                exec_ok, err = await self._execute_macro_action(page, landmark_action, elem_map)
                if exec_ok:
                    self.step_history.append({"type": "landmark", "action": landmark_action.dict()})
                    if self.int_user_id is not None:
                        try:
                            live_activity.add_step(self.int_user_id, f"Turn {step_idx + 1}: FastPath {landmark_action.steps[0].action} {landmark_action.steps[0].target_id}")
                        except Exception:
                            pass
                    continue

            # 5. REASON: LLM Cognitive Decision (Claude 3.5 Sonnet / GPT-4o / Gemini)
            print(f"[LightWebAgent Reason] Consulting model {self.model_name}...", flush=True)
            decision = await self._query_llm_planner(current_url, current_title, a11y_text, elem_map)
            print(f"[LightWebAgent Decision] Status: {decision.status} | Thought: {decision.thought}", flush=True)

            if self.int_user_id is not None and decision.thought:
                try:
                    live_activity.set_description(self.int_user_id, decision.thought[:120])
                except Exception:
                    pass

            if decision.scratchpad_updates:
                await self._update_scratchpad(decision.scratchpad_updates)

            if decision.status == "DONE":
                self.is_completed = True
                self.result_summary = decision.result_summary
                domain = re.sub(r"^https?://(www\.)?", "", current_url).split("/")[0]
                self._write_skill_cache(domain, self.user_goal, self.step_history)
                # Persist to Messa DB skills playbook
                await self._persist_skill(
                    domain=domain,
                    pattern=f"goal:{self.user_goal[:50]}",
                    recipe=f"Completed '{self.user_goal}' on {domain} in {len(self.step_history)} steps.",
                )
                if self.active_task_id:
                    try:
                        await db.set_active_task_status(self.active_task_id, "completed")
                    except Exception:
                        pass
                if self.int_user_id is not None:
                    try:
                        live_activity.set_closing(self.int_user_id)
                    except Exception:
                        pass
                print(f"[LightWebAgent Complete] {decision.result_summary}", flush=True)
                return decision

            if decision.status in ["FAILED", "NEEDS_HUMAN"] or not decision.action:
                return decision

            # 6. INTENT-ALIGNMENT CRITIC: Validate consequential actions
            if decision.action.is_consequential:
                print(f"[LightWebAgent Critic] Evaluating consequential action against intent...", flush=True)
                is_aligned, reason = await self._run_intent_alignment_critic(decision.action)
                if not is_aligned:
                    print(f"[LightWebAgent Critic Flag] Blocked injection/misaligned action: {reason}", flush=True)
                    logger.warning(f"[LightWebAgent] Intent critic flagged action: {reason}")
                    risk_checkpoint = HumanCheckpoint(
                        kind="risk_review",
                        prompt_to_user=f"Messa flagged an action requiring confirmation: {reason}. Reply YES to approve.",
                        trigger_reason="critic_misaligned",
                    )
                    if self.int_user_id is not None:
                        try:
                            live_activity.set_waiting_for_human(self.int_user_id, risk_checkpoint.prompt_to_user)
                        except Exception:
                            pass
                    await session_manager.suspend_session(self.session.session_id, risk_checkpoint.dict())
                    return AgentDecision(
                        status="BLOCKED_INJECTION",
                        thought=f"Intent-Alignment Critic flagged consequential action: {reason}",
                        checkpoint=risk_checkpoint,
                    )

            # 7. ACT: Execute Macro-Action Batch with Staleness Check
            print(f"[LightWebAgent Act] Executing {len(decision.action.steps)} macro action steps...", flush=True)
            exec_ok, err_msg = await self._execute_macro_action(page, decision.action, elem_map)
            self.step_history.append({
                "type": "macro_action",
                "action": decision.action.dict(),
                "success": exec_ok,
                "error": err_msg,
            })
            print(f"[LightWebAgent Act Result] Success: {exec_ok} (err: {err_msg})", flush=True)

            if self.int_user_id is not None:
                try:
                    action_desc = ", ".join(f"{s.action} {s.target_id}" for s in decision.action.steps) if decision.action else "acted"
                    live_activity.add_step(self.int_user_id, f"Turn {step_idx + 1}: {action_desc}")
                except Exception:
                    pass

            # 8. VERIFY: Post-condition assertion check
            await self._wait_for_stabilization(page)
            if decision.action.expect:
                verified = await self._verify_expectations(page, decision.action.expect)
                print(f"[LightWebAgent Verify] Expectation {decision.action.expect} -> {verified}", flush=True)
                if not verified:
                    logger.warning(f"[LightWebAgent] Expectation failed for action: {decision.action.expect}")

        # If loop ended and max_turns was constrained (caller running turn by turn)
        if len(self.step_history) > 0 and len(self.step_history) < self.max_steps and max_turns is not None:
            last_step = self.step_history[-1]
            return AgentDecision(
                status="OK_REASONED" if last_step.get("type") != "landmark" else "OK_LANDMARK",
                thought=f"Completed turn {len(self.step_history)} ({last_step.get('type')})",
                action=MacroAction(**last_step["action"]) if "action" in last_step else None,
            )

        return AgentDecision(
            status="FAILED",
            result_summary=f"Reached maximum steps ({self.max_steps}) without completing goal: {self.user_goal}",
        )

    # -----------------------------------------------------------------------
    # Helper & Guard Methods
    # -----------------------------------------------------------------------

    async def _wait_for_stabilization(self, page: Any, debounce_ms: int = 400) -> None:
        """Wait briefly for network and DOM reflows to settle."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        await asyncio.sleep(debounce_ms / 1000.0)

    def _check_circuit_breakers(self) -> Optional[AgentDecision]:
        """Detect infinite loops and action oscillations."""
        # 1. Step cap
        if len(self.step_history) >= self.max_steps:
            return AgentDecision(
                status="FAILED",
                result_summary=f"Circuit Breaker: Exceeded maximum step limit ({self.max_steps}).",
            )

        # 2. Oscillation detection (e.g. A -> B -> A -> B across last 4 steps)
        if len(self.action_history_signatures) >= 4:
            recent = self.action_history_signatures[-4:]
            if recent[0] == recent[2] and recent[1] == recent[3]:
                return AgentDecision(
                    status="FAILED",
                    thought="Circuit Breaker: Detected oscillating action cycle (A-B-A-B). Halting.",
                    result_summary="Agent detected an oscillating loop on the website.",
                )

        # 3. Persistent modal loop trap (reopened >= 3 times)
        modal_dismiss_count = sum(1 for s in self.step_history if s.get("type") == "modal_dismiss")
        if modal_dismiss_count >= 3:
            return AgentDecision(
                status="FAILED",
                thought="Circuit Breaker: Detected persistent modal loop trap (modal reopened >= 3 times). Halting.",
                result_summary="Adversarial modal loop trap detected. Halting to avoid burning turns.",
            )
        return None

    def _try_landmark_fastpath(self, elem_map: Dict[str, Any]) -> Optional[MacroAction]:
        """Check for high-confidence zero-LLM landmark matches."""
        goal_lower = self.user_goal.lower()
        if len(self.step_history) == 0:
            if "search" in goal_lower and not any(k in goal_lower for k in ["cart", "sign in", "login"]):
                match = WebConventionPrior.match_canonical_landmark("search", list(elem_map.values()))
                if match:
                    sig = match["signature"]
                    # Extract search query from goal
                    query = ""
                    for prefix in ["search for", "search", "find"]:
                        if prefix in goal_lower:
                            idx = goal_lower.find(prefix) + len(prefix)
                            raw_query = self.user_goal[idx:].strip()
                            for conj in [" and ", " then ", ","]:
                                if conj in raw_query.lower():
                                    raw_query = raw_query[:raw_query.lower().find(conj)].strip()
                            query = raw_query.strip(" '\"")
                            break
                    if query:
                        return MacroAction(steps=[
                            MacroStep(action="type", target_id=match["id"], target_signature=sig, value=query),
                            MacroStep(action="press_key", target_id=match["id"], target_signature=sig, value="Enter"),
                        ])
                    return MacroAction(steps=[
                        MacroStep(action="click", target_id=match["id"], target_signature=sig)
                    ])
            elif any(k in goal_lower for k in ["sign up", "create account", "register"]):
                match = WebConventionPrior.match_canonical_landmark("sign_up", list(elem_map.values()))
                if match:
                    sig = match["signature"]
                    return MacroAction(steps=[
                        MacroStep(action="click", target_id=match["id"], target_signature=sig)
                    ])
            elif "cart" in goal_lower and any(w in goal_lower for w in ["view", "open", "show", "go to"]):
                match = WebConventionPrior.match_canonical_landmark("cart", list(elem_map.values()))
                if match:
                    sig = match["signature"]
                    return MacroAction(steps=[
                        MacroStep(action="click", target_id=match["id"], target_signature=sig)
                    ])

        return None


    def _detect_and_clear_blocking_modals(self, elements: List[Dict[str, Any]]) -> Optional[MacroAction]:
        """Detect promotional modals, cookie banners, or discount popups blocking the page."""
        # Check for cookie accept buttons
        for el in elements:
            name = (el.get("name") or "").lower()
            role = (el.get("role") or "").lower()
            if role == "button":
                if any(cb in name for cb in ["accept all cookies", "agree and proceed", "i accept cookies", "allow cookies"]):
                    return MacroAction(steps=[
                        MacroStep(action="click", target_id=el["id"], target_signature=el["signature"])
                    ])
                if any(promo in name for promo in ["claim offer", "claim $0 delivery", "dismiss", "close dialog", "no thanks", "close"]):
                    # If this appears early or overlays the screen, dismiss it
                    if len(self.step_history) < 3 and any(k in name for k in ["claim", "close", "dismiss", "no thanks"]):
                        return MacroAction(steps=[
                            MacroStep(action="click", target_id=el["id"], target_signature=el["signature"])
                        ])
        return None

    def _detect_human_checkpoint(
        self, url: str, title: str, a11y_text: str, body_text: str = ""
    ) -> Optional[HumanCheckpoint]:
        """Detect phone verification (SMS OTP), CAPTCHA, or 3D Secure challenges."""
        combined_text = f"{a11y_text} {body_text}".strip()
        text_lower = combined_text.lower()
        title_lower = title.lower()

        # 1. SMS OTP detection
        if any(w in text_lower for w in [
            "enter the 6-digit code",
            "verification code sent to",
            "we sent a code to",
            "enter security code",
            "enter verification code",
            "one-time passcode",
            "6-digit code",
        ]):
            phone_hint = ""
            phone_match = re.search(r"(\(\d{3}\)\s*\d{3}-\d{4}|\+?\d{1,2}\s*\(?\d{3}\)?\s*\d{3}-\d{4})", combined_text)
            if phone_match:
                phone_hint = f" to {phone_match.group(1)}"
            return HumanCheckpoint(
                kind="otp",
                prompt_to_user=f"Verification code sent{phone_hint}. Please reply with the 6-digit code to continue.",
                trigger_reason="sms_otp_prompt",
            )


        # 2. CAPTCHA detection
        if any(w in title_lower or w in text_lower for w in [
            "robot or human",
            "press & hold",
            "press and hold",
            "verify you are human",
            "captcha",
            "security check",
        ]):
            live_view = self.session.live_view_url or ""
            url_msg = f"\nTap here to complete verification: {live_view}" if live_view else ""
            return HumanCheckpoint(
                kind="captcha",
                prompt_to_user=f"A verification puzzle appeared.{url_msg}\nPlease complete it so Messa can continue.",
                interactive_url=live_view,
                trigger_reason="captcha_challenge",
            )

        return None

    async def _run_intent_alignment_critic(self, action: MacroAction) -> Tuple[bool, str]:
        """Isolated critic evaluating whether consequential actions strictly match user intent."""
        summary = f"Steps: {[s.dict() for s in action.steps]}, Expect: {action.expect}"
        prompt = (
            f"You are a strict security alignment critic for an AI shopping agent.\n"
            f"User's Original Goal: '{self.user_goal}'\n"
            f"Proposed Consequential Action: {summary}\n\n"
            f"Question: Does this action directly serve what the user requested?\n"
            f"Reply ONLY in JSON: {{\"verdict\": \"aligned\" | \"misaligned\" | \"uncertain\", \"reason\": \"...\"}}"
        )

        try:
            model = config.build_model(
                self.critic_model_name,
                api_key=config.api_key_for_agent("reply_guardrail"),
            )
            resp = await model.ainvoke([{"role": "user", "content": prompt}])
            content = resp.content if hasattr(resp, "content") else str(resp)
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                verdict = data.get("verdict", "aligned")
                return (verdict == "aligned", data.get("reason", ""))
        except Exception as e:
            logger.debug(f"[IntentCritic] Fallback evaluation note: {e}")
        return (True, "")

    async def _execute_macro_action(
        self,
        page: Any,
        action: MacroAction,
        elem_map: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Execute a batch of macro steps with staleness signature validation."""
        for step in action.steps:
            target_el = elem_map.get(step.target_id)
            if not target_el:
                return False, f"Target element {step.target_id} not found in perception snapshot."

            # Validate target signature (Staleness Guard)
            current_sig = target_el.get("signature")
            if step.target_signature and current_sig != step.target_signature:
                logger.warning(
                    f"[MacroExecutor] Staleness Guard: Signature mismatch on {step.target_id} "
                    f"(expected {step.target_signature}, got {current_sig}). Aborting batch."
                )
                return False, "Target signature mismatch: DOM state changed before step execution."

            # Record signature for oscillation detection
            self.action_history_signatures.append(f"{step.action}:{step.target_id}:{current_sig}")

            # Resolve credential tokens ({{cred:...}})
            val = step.value or ""
            if "{{cred:" in val:
                for cred_key, cred_val in self.credentials.items():
                    val = val.replace(f"{{{{cred:{cred_key}}}}}", cred_val)

            # Execute atomic Playwright action
            try:
                selector = target_el.get("selector")
                name_val = target_el.get("name")
                tag_val = target_el.get("tag")
                role_val = target_el.get("role")

                # Multi-Strategy Locator Resolution (resilient against React re-renders)
                locator = None
                if selector:
                    loc = page.locator(selector).first
                    try:
                        cnt = await loc.count() if asyncio.iscoroutinefunction(loc.count) else (loc.count() if callable(loc.count) else 1)
                        if cnt > 0:
                            locator = loc
                    except Exception:
                        pass

                if not locator:
                    # Strategy 2: By role + name
                    if role_val and name_val and hasattr(page, "get_by_role"):
                        try:
                            loc = page.get_by_role(role_val, name=name_val).first
                            cnt = await loc.count() if asyncio.iscoroutinefunction(loc.count) else (loc.count() if callable(loc.count) else 1)
                            if cnt > 0:
                                locator = loc
                        except Exception:
                            pass

                if not locator:
                    # Strategy 3: By label, placeholder, or text
                    if name_val:
                        for fn_name in ["get_by_label", "get_by_placeholder", "get_by_text"]:
                            fn = getattr(page, fn_name, None)
                            if fn:
                                try:
                                    loc = fn(name_val, exact=False).first
                                    cnt = await loc.count() if asyncio.iscoroutinefunction(loc.count) else (loc.count() if callable(loc.count) else 1)
                                    if cnt > 0:
                                        locator = loc
                                        break
                                except Exception:
                                    pass

                if not locator:
                    # Strategy 4: Semantic search input
                    if (tag_val == "input" or role_val == "searchbox") and any(k in (name_val or "").lower() for k in ["search", "find", "query"]):
                        try:
                            loc = page.locator('input[type="search"], input[name="q"], input#global-search-input, input[aria-label*="Search"]').first
                            cnt = await loc.count() if asyncio.iscoroutinefunction(loc.count) else (loc.count() if callable(loc.count) else 1)
                            if cnt > 0:
                                locator = loc
                        except Exception:
                            pass

                # Final fallback to selector locator
                if not locator and selector:
                    locator = page.locator(selector).first

                if not locator:
                    return False, f"Could not find locator for element {step.target_id} ({name_val})"


                if step.action == "click":
                    try:
                        try:
                            await locator.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        await locator.click(timeout=3000)
                    except Exception:
                        try:
                            await locator.click(timeout=3000, force=True)
                        except Exception:
                            try:
                                await locator.dispatch_event("click")
                            except Exception:
                                pass
                elif step.action == "type":
                    try:
                        await locator.fill(val, timeout=3000)
                    except Exception:
                        try:
                            await locator.click(timeout=2000, force=True)
                            await page.keyboard.type(val)
                        except Exception:
                            await locator.fill(val, timeout=3000)
                elif step.action in ["press_key", "press"]:
                    raw_key = val or "Enter"
                    KEY_MAP = {
                        "enter": "Enter",
                        "return": "Enter",
                        "tab": "Tab",
                        "escape": "Escape",
                        "esc": "Escape",
                        "backspace": "Backspace",
                        "arrowdown": "ArrowDown",
                        "arrowup": "ArrowUp",
                        "arrowleft": "ArrowLeft",
                        "arrowright": "ArrowRight",
                        "space": " ",
                    }
                    normalized_key = KEY_MAP.get(raw_key.lower(), raw_key)
                    try:
                        await locator.press(normalized_key, timeout=3000)
                    except Exception:
                        await page.keyboard.press(normalized_key)
                elif step.action == "scroll":
                    await page.mouse.wheel(0, 500)
                elif step.action == "wait_for":
                    await asyncio.sleep(float(val or 1.0))

                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"[MacroExecutor] Action execution failed on {step.target_id}: {e}")
                return False, str(e)

        return True, None

    async def _verify_expectations(self, page: Any, expect: Dict[str, Any]) -> bool:
        """Verify post-conditions after action batch."""
        if "url_contains" in expect:
            expected_substr = expect["url_contains"].lower()
            current_url = (page.url or "").lower()
            return expected_substr in current_url
        if "text_present" in expect:
            expected_text = expect["text_present"].lower()
            content = (await page.content()).lower()
            return expected_text in content
        return True

    async def _query_llm_planner(
        self,
        current_url: str,
        current_title: str,
        a11y_text: str,
        elem_map: Dict[str, Any],
    ) -> AgentDecision:
        """Query LLM (Claude 3.5 Sonnet / GPT-4o) for the next batched macro action."""
        history_lines = []
        for idx, h in enumerate(self.step_history[-4:], 1):
            h_type = h.get("type", "action")
            h_steps = h.get("action", {}).get("steps", [])
            steps_desc = ", ".join(f"{s.get('action')} on {s.get('target_id')}" for s in h_steps) if h_steps else h_type
            history_lines.append(f"- Step {len(self.step_history) - len(self.step_history[-4:]) + idx}: {steps_desc} (success={h.get('success', True)})")
        history_str = "\n".join(history_lines) if history_lines else "None yet."

        loop_warning = ""
        if len(self.step_history) >= 1:
            last_step = self.step_history[-1]
            last_action = last_step.get("action", {}).get("steps", [{}])[0]
            last_tid = last_action.get("target_id")
            last_act = last_action.get("action")
            if last_tid and last_act:
                loop_warning = (
                    f"\n\n[SELF-HEALING GUARD]: In your previous turn, you executed '{last_act}' on [{last_tid}]. "
                    f"If the page/URL did not transition, DO NOT execute '{last_act}' on [{last_tid}] again! "
                    "You MUST pick an alternative element (such as searching for items directly, clicking a category, or scrolling)."
                )

        skills_block = ""
        if self.domain_skills:
            skills_block = "\n\nLearned Lessons for this site (from Messa Skills Playbook):\n" + "\n".join(f"- {s}" for s in self.domain_skills)

        scratchpad_str = ""
        if self.scratchpad_artifacts:
            scratchpad_str = f"\n\n<active_task_scratchpad>\n{json.dumps(self.scratchpad_artifacts, indent=2)}\n</active_task_scratchpad>"

        system_prompt = (
            "You are light-web-agent, an autonomous high-speed web navigation agent.\n"
            "Your objective is to accomplish the user's goal with minimal steps and maximum reliability.\n\n"
            "Rules:\n"
            "1. Output ONLY valid JSON matching the AgentDecision schema.\n"
            "2. Emit batched MacroAction steps (up to 4 steps) when filling forms or sequential actions.\n"
            "3. Use exact element IDs [eX] from the perception tree in 'target_id'.\n"
            "4. For passwords or sensitive info, use tokens: '{{cred:password}}', '{{cred:email}}'.\n"
            "5. If goal is completely achieved, output status: 'DONE' and a concise result_summary.\n"
            "6. Flag is_consequential=true for order submits, address changes, payment additions, or account creations.\n"
            "7. SELF-HEALING & NO-LOOP RULE: Review the Recent Steps. If an action has already been performed and the page/URL did not change, DO NOT repeat that exact action! Switch strategies (e.g. search for items directly, scroll down, or click another interactive element toward the goal).\n"
            "8. SCRATCHPAD MEMORY: You can optionally include 'scratchpad_updates': {'cart_items': [...], 'stage': '...'} to remember key facts across turns.\n"
            f"{skills_block}\n\n"
            "Response Schema Format:\n"
            "{\n"
            '  "status": "OK_REASONED",\n'
            '  "thought": "brief explanation of what you are doing and why",\n'
            '  "scratchpad_updates": {"facts": "..."},\n'
            '  "action": {\n'
            '    "steps": [\n'
            '      {"action": "click|type|press_key|scroll|wait_for", "target_id": "e1", "value": "text or token"}\n'
            "    ],\n"
            '    "is_consequential": false\n'
            "  }\n"
            "}\n"
            "When the goal is finished, return:\n"
            '{"status": "DONE", "thought": "Goal achieved", "result_summary": "Summary of what was done"}'
        )

        user_content = (
            f"User Goal: {self.user_goal}\n"
            f"Current URL: {current_url}\n"
            f"Page Title: {current_title}\n\n"
            f"<web_content_untrusted>\n"
            f"{a11y_text}\n"
            f"</web_content_untrusted>\n\n"
            f"Recent Steps Taken:\n{history_str}"
            f"{loop_warning}"
            f"{scratchpad_str}\n\n"
            f"Total steps so far: {len(self.step_history)}\n"
            "Output your next decision in JSON."
        )

        try:
            model = config.build_model(
                self.model_name,
                api_key=config.api_key_for_agent("orchestrator"),
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            resp = await model.ainvoke(messages)
            raw = resp.content if hasattr(resp, "content") else str(resp)

            # Parse JSON
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                # Unpack common root wrapper keys
                for wrapper in ["agent_decision", "decision", "response"]:
                    if wrapper in data and isinstance(data[wrapper], dict):
                        data = data[wrapper]
                        break

                # Extract scratchpad updates if model emitted any
                if "scratchpad_updates" in data and isinstance(data["scratchpad_updates"], dict):
                    pass
                else:
                    data["scratchpad_updates"] = None

                # Normalize status
                status_raw = str(data.get("status", "OK_REASONED")).upper()
                valid_statuses = [
                    "OK_CACHED", "OK_LANDMARK", "OK_REASONED", "BLOCKED_MODAL",
                    "BLOCKED_INJECTION", "NEEDS_HUMAN", "DONE", "FAILED"
                ]
                if status_raw in valid_statuses:
                    data["status"] = status_raw
                elif status_raw in ["OK", "SUCCESS", "CONTINUE", "IN_PROGRESS", "ACT"]:
                    data["status"] = "OK_REASONED"
                else:
                    data["status"] = "OK_REASONED"

                # If steps are placed at root, wrap into action dict
                if "steps" in data and "action" not in data:
                    data["action"] = {"steps": data.pop("steps")}
                elif "action" in data and isinstance(data["action"], str):
                    act_type = data.pop("action")
                    target = data.pop("target_id", data.pop("element_id", "e1"))
                    val = data.pop("value", None)
                    data["action"] = {
                        "steps": [{"action": act_type, "target_id": target, "value": val}]
                    }

                # Normalize action dictionary if emitted
                if "action" in data and isinstance(data["action"], dict):
                    action_dict = data["action"]
                    if "action_type" in action_dict and "steps" not in action_dict:
                        action_dict["steps"] = [{
                            "action": action_dict.get("action_type", "click").lower(),
                            "target_id": action_dict.get("target_id") or action_dict.get("element_id", "e1"),
                            "target_signature": "",
                            "value": action_dict.get("value"),
                        }]
                    if "steps" in action_dict:
                        for step in action_dict["steps"]:
                            if "action_type" in step and "action" not in step:
                                step["action"] = step.pop("action_type")
                            if "action" in step:
                                step["action"] = str(step["action"]).lower()
                            if "element_id" in step and "target_id" not in step:
                                step["target_id"] = step.pop("element_id")
                            elif "id" in step and "target_id" not in step:
                                step["target_id"] = step.pop("id")
                            tid = step.get("target_id")
                            if tid and tid in elem_map and not step.get("target_signature"):
                                step["target_signature"] = elem_map[tid].get("signature", "")

                return AgentDecision(**data)
        except Exception as e:
            logger.error(f"[LightWebAgent] LLM planning query error: {e}")

        return AgentDecision(
            status="FAILED",
            result_summary="Error communicating with LLM planner.",
        )
