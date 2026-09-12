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
        resume_checkpoint: Optional["HumanCheckpoint"] = None,
    ) -> None:
        self.session = session
        self.user_goal = user_goal
        self.credentials = credentials or {}
        self.max_steps = max_steps

        # When resuming a suspended session, the human's answer to the
        # checkpoint that suspended it (OTP digits, a CAPTCHA confirmation,
        # a risk-review "YES", etc). Without this, nothing ever reads
        # `HumanCheckpoint.human_input` -- resuming would just re-run
        # `_detect_human_checkpoint`, see the exact same prompt still on
        # the page, and re-suspend forever instead of ever submitting the
        # answer. `_resume_checkpoint_pending` gates BOTH the one-time
        # skip of that re-detection AND the one-time planner directive
        # below; it's cleared the first time the planner is actually
        # queried so normal checkpoint detection resumes for anything
        # that comes after (e.g. a second, unrelated checkpoint later).
        self.resume_checkpoint = resume_checkpoint
        self._resume_checkpoint_pending = resume_checkpoint is not None
        if resume_checkpoint is not None and resume_checkpoint.human_input:
            self.credentials.setdefault("checkpoint_answer", resume_checkpoint.human_input)
        self.model_name = model_name or getattr(config, "LIGHT_WEB_AGENT_MODEL", "google/gemini-2.5-flash")
        self.critic_model_name = critic_model_name or getattr(config, "LIGHT_WEB_AGENT_CRITIC_MODEL", "openai/gpt-4o-mini")
        self.user_id = user_id or getattr(session, "user_id", None)
        self.int_user_id = int(self.user_id) if isinstance(self.user_id, (int, str)) and str(self.user_id).isdigit() else None

        self.step_history: List[Dict[str, Any]] = []
        self.action_history_signatures: List[str] = []
        self.is_completed = False
        self.result_summary: Optional[str] = None

        # Which canonical landmark intents (search/sign_up/cart/...) have
        # already been fast-pathed once. The fast-path used to only ever
        # run on step 0 -- safe, but meant it only ever fired a single
        # time per task, which isn't how a human skims a page: they look
        # for the next obvious control on EVERY page, not just the first.
        # Tracking "already used" per-intent (rather than gating on
        # step_history length globally) is what lets it run every turn
        # without blindly repeating a completed intent (e.g. re-searching
        # or re-clicking "sign up" again once that step is already done).
        self._used_landmark_intents: set = set()
        self._pending_landmark_intent: Optional[str] = None

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

    async def _persist_pending_checkpoint(self, checkpoint: "HumanCheckpoint") -> None:
        """Persist enough into the Active Task Scratchpad to resume this
        exact suspended task across a turn boundary -- reuses the existing
        Active Task Scratchpad mechanism (no new DB table). Without this,
        a suspended session has nowhere durable to record WHAT it's
        waiting for or HOW to resume it once server.py/browser.py see the
        human's answer arrive on a later, unrelated turn.

        Order matters: the artifacts write must happen BEFORE the status
        flip to 'waiting_user_input' -- db.update_active_task_artifacts
        itself flips a 'waiting_user_input' task back to 'in_progress' on
        any write (by design, for the normal case), which would silently
        undo the status this call is trying to set if done in the other
        order.
        """
        if not self.active_task_id or not getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            return
        try:
            await self._update_scratchpad({
                "pending_checkpoint": {
                    "session_id": self.session.session_id,
                    "kind": checkpoint.kind,
                    "prompt_to_user": checkpoint.prompt_to_user,
                    "asked_at": time.time(),
                },
                "_call_context": {
                    "goal": self.user_goal,
                    "credentials": self.credentials,
                    "max_steps": self.max_steps,
                },
            })
            await db.set_active_task_status(self.active_task_id, "waiting_user_input")
        except Exception as e:
            logger.debug(f"[LightWebAgent] _persist_pending_checkpoint fallback: {e}")

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

    def _cache_key(self, domain: str) -> str:
        """Scope the zero-LLM skill cache per-user (or per-session when no
        user_id is known) instead of sharing it globally by domain alone.
        A bare domain-only key meant one user's completed steps --
        including literally-typed values like a search query -- could get
        replayed onto a DIFFERENT user's different goal on the same
        domain, gated only by whether the old target_ids still happened to
        exist on the page. This closes the sharpest edge of that: cross-
        session cache entries can no longer collide at all."""
        scope = self.int_user_id if self.int_user_id is not None else f"session:{getattr(self.session, 'session_id', 'unknown')}"
        return f"{scope}:{domain}"

    @staticmethod
    def _goal_intent_matches(current_goal: str, cached_intent: str) -> bool:
        """Deliberately simple keyword-overlap check -- this doesn't need
        to be a semantic match, it needs to stop blind replay of one
        goal's literal steps onto an unrelated goal. A verified macro-
        action sequence is only safe to auto-replay when the NEW goal
        looks like the same task, not just the same website/session."""
        def _words(text: str) -> set:
            return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3}
        current_words = _words(current_goal)
        cached_words = _words(cached_intent)
        if not current_words or not cached_words:
            return False
        overlap = current_words & cached_words
        shorter = min(len(current_words), len(cached_words))
        return shorter > 0 and (len(overlap) / shorter) >= 0.5

    def _lookup_skill_cache(self, domain: str, goal: str) -> Optional[MacroAction]:
        """Check if a verified macro-action sequence is cached for this
        user/session, domain, AND intent -- all three, not just domain
        (see _cache_key/_goal_intent_matches docstrings for why)."""
        key = self._cache_key(domain)
        entry = GLOBAL_WEB_SKILL_CACHE.get(key)
        if not entry:
            return None
        cached_intent = entry.get("intent", "")
        if not self._goal_intent_matches(goal, cached_intent):
            logger.info(
                f"[SkillCache] Entry exists for {key} but intent doesn't match closely enough "
                f"(cached='{cached_intent}', current='{goal.lower()}') -- refusing to replay."
            )
            return None
        steps_data = entry.get("steps", [])
        if steps_data:
            steps = [MacroStep(**s) for s in steps_data]
            return MacroAction(steps=steps)
        return None

    def _write_skill_cache(self, domain: str, goal: str, history: List[Dict[str, Any]]) -> None:
        """Cache a successful macro-action sequence for reuse by the SAME
        user/session on this domain and a matching intent (see
        _cache_key/_lookup_skill_cache)."""
        collected_steps = []
        for h in history:
            act = h.get("action")
            if act and "steps" in act:
                collected_steps.extend(act["steps"])
        if collected_steps:
            key = self._cache_key(domain)
            GLOBAL_WEB_SKILL_CACHE[key] = {
                "intent": goal.lower(),
                "steps": collected_steps,
                "saved_at": time.time(),
            }
            logger.info(f"[SkillCache] Cached {len(collected_steps)} macro steps for {key}")

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
            # Skipped for as long as a just-resumed checkpoint answer is still
            # pending delivery to the planner -- otherwise this immediately
            # re-detects the very same OTP/CAPTCHA prompt still visible on the
            # page and re-suspends the session before the human's answer ever
            # gets submitted.
            if self._resume_checkpoint_pending:
                checkpoint = None
            else:
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
                await self._persist_pending_checkpoint(checkpoint)
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
                    if self._pending_landmark_intent:
                        self._used_landmark_intents.add(self._pending_landmark_intent)
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
                    await self._persist_pending_checkpoint(risk_checkpoint)
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

        # 2. Oscillation detection across the last 6 actions (architecture
        # doc Pillar 7 / handover brief: "short cycle detection across 6
        # actions"). The original check only ever looked at a hardcoded
        # 4-length window for a strict period-2 (A-B-A-B) pattern, which
        # misses a 3-step trap (A->B->C->A->B->C) entirely, and misses a
        # flat exact-repeat (A->A->A) too -- both real "the agent is stuck"
        # shapes, not just alternating between exactly two actions. Scan
        # whatever's available in the last 6 signatures for any repeating
        # cycle of period 1, 2, or 3 -- period 2 still fires at the same
        # minimum 4 signatures as before (this is a strict superset of the
        # original check, not a replacement with different timing).
        recent = self.action_history_signatures[-6:]
        for period in (1, 2, 3):
            need = period * 2
            if len(recent) < need:
                continue
            window = recent[-need:]
            if window[:period] == window[period:]:
                return AgentDecision(
                    status="FAILED",
                    thought=(
                        f"Circuit Breaker: Detected oscillating action cycle (period {period}) "
                        "across the last actions. Halting."
                    ),
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
        """Check for high-confidence zero-LLM landmark matches.

        Gated per-intent (via `self._used_landmark_intents`), not by overall
        step_history length -- a human doesn't stop skimming for the next
        obvious control after the first page, they do it on every page. So
        this now runs every turn, but each canonical intent (search /
        sign_up / cart) can only fast-path once per task: once "search" has
        actually been executed via this path, it's marked used and won't
        fire again even though the gate now opens on every turn, so a later
        turn can't blindly re-type the same query or re-click "sign up"
        a second time. `WebConventionPrior.match_canonical_landmark` also
        independently skips any element flagged `occluded` (e.g. sitting
        behind a modal), so a fast-path match is never fired at a target a
        real user couldn't actually reach right now.
        """
        goal_lower = self.user_goal.lower()
        self._pending_landmark_intent = None

        if "search" in goal_lower and not any(k in goal_lower for k in ["cart", "sign in", "login"]) \
                and "search" not in self._used_landmark_intents:
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
                self._pending_landmark_intent = "search"
                if query:
                    return MacroAction(steps=[
                        MacroStep(action="type", target_id=match["id"], target_signature=sig, value=query),
                        MacroStep(action="press_key", target_id=match["id"], target_signature=sig, value="Enter"),
                    ])
                return MacroAction(steps=[
                    MacroStep(action="click", target_id=match["id"], target_signature=sig)
                ])

        if any(k in goal_lower for k in ["sign up", "create account", "register"]) \
                and "sign_up" not in self._used_landmark_intents:
            match = WebConventionPrior.match_canonical_landmark("sign_up", list(elem_map.values()))
            if match:
                sig = match["signature"]
                self._pending_landmark_intent = "sign_up"
                return MacroAction(steps=[
                    MacroStep(action="click", target_id=match["id"], target_signature=sig)
                ])

        if "cart" in goal_lower and any(w in goal_lower for w in ["view", "open", "show", "go to"]) \
                and "cart" not in self._used_landmark_intents:
            match = WebConventionPrior.match_canonical_landmark("cart", list(elem_map.values()))
            if match:
                sig = match["signature"]
                self._pending_landmark_intent = "cart"
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
        """Isolated critic evaluating whether consequential actions strictly match user intent.

        FAILS CLOSED, deliberately: this is the one gate standing between an
        injected/misaligned consequential action (order submit, payment,
        address change) and actually executing it. A prior version of this
        function returned (True, "") -- i.e. "aligned" -- whenever the model
        call errored or its response didn't parse, which meant the critic's
        entire protection silently vanished exactly when it was least
        available (a network hiccup, a rate limit, or a page crafted to
        break its own JSON parsing). Any failure to get a clear "aligned"
        verdict now returns misaligned=False, routing to the same
        risk_review HumanCheckpoint a genuine misalignment would."""
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
                # Default to "uncertain" (not "aligned") when the model's
                # JSON is well-formed but simply omits verdict -- same
                # fail-closed principle as the exception path below.
                verdict = data.get("verdict", "uncertain")
                reason = data.get("reason", "")
                if verdict == "aligned":
                    return (True, reason)
                return (False, reason or f"Critic verdict: {verdict}")
            logger.warning("[IntentCritic] No parseable verdict in critic response -- failing closed (uncertain).")
        except Exception as e:
            logger.warning(f"[IntentCritic] Critic evaluation failed -- failing closed (uncertain): {e}")
        return (False, "Critic evaluation failed or was inconclusive -- treating as uncertain, not aligned.")

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

            # Occlusion Guard: refuse to click/type into a target the
            # Perception Engine flagged as covered by something else
            # (almost always a modal/overlay). This is the actual fix for
            # the "clicked signup behind the popup" failure -- the
            # force=True/dispatch_event fallback below exists to route
            # around stale locators and mid-re-render timing misses, NOT to
            # punch through a genuinely, correctly blocked element. Without
            # this guard, a real Playwright actionability failure (the
            # SAFE, correct outcome for an occluded element) was being
            # treated as "try harder" instead of "this is actually blocked."
            if target_el.get("occluded") and step.action in ("click", "type"):
                logger.warning(
                    f"[MacroExecutor] Occlusion Guard: target {step.target_id} is covered by "
                    "another element (likely a modal/overlay). Refusing to force through it."
                )
                return False, (
                    f"Target {step.target_id} is occluded by another element (likely a "
                    "modal/overlay) -- resolve or engage with the overlay first instead of "
                    "forcing this action through it."
                )

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
                elif step.action == "select_option":
                    # Declared as a valid MacroStep action but previously
                    # had no execution branch at all -- the LLM could emit
                    # it for a shipping-speed/state/quantity dropdown
                    # (common on real checkout flows) and it would silently
                    # no-op while the batch still reported success. Try
                    # matching by visible label first (what an LLM is most
                    # likely to have read off the page), then by the
                    # underlying option value.
                    try:
                        await locator.select_option(label=val, timeout=3000)
                    except Exception:
                        try:
                            await locator.select_option(value=val, timeout=3000)
                        except Exception:
                            await locator.select_option(val, timeout=3000)
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

        # One-time steering directive after resuming from a suspended human
        # checkpoint: tell the planner exactly what just happened and where
        # the answer lives, instead of letting it re-discover (or worse,
        # never notice) the still-pending OTP/CAPTCHA/confirmation field.
        resume_directive = ""
        if self.resume_checkpoint is not None and self._resume_checkpoint_pending:
            ck = self.resume_checkpoint
            resume_directive = (
                f"\n\n[RESUMED FROM HUMAN CHECKPOINT]: A human was just asked to provide a "
                f"'{ck.kind}' (they were told: \"{ck.prompt_to_user}\") and has now answered. "
                "Their answer is available as the credential token '{{cred:checkpoint_answer}}' -- "
                "do NOT ask for it again and do NOT re-evaluate whether a checkpoint is needed. "
                "Find the field on THIS page that corresponds to that checkpoint (the OTP/verification "
                "code input, the CAPTCHA confirmation control, or the payment/risk confirmation control) "
                "and submit '{{cred:checkpoint_answer}}' into it now as your next action."
            )
            # Consumed: only steer the very next planner call this way. If a
            # different checkpoint shows up later in the same run, normal
            # detection (re-enabled the moment this flips off) will catch it.
            self._resume_checkpoint_pending = False

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
            f"{scratchpad_str}"
            f"{resume_directive}\n\n"
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
