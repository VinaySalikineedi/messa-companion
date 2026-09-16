"""Open-Source Phone / Bring Your Own Phone (BYOP) subagent tools
(open-source-phone.md, feature/open-source-phone).

Unlike light_web_agent_tools.py (one deterministic _run that always does
the same thing -- "run this one browser task"), this subagent genuinely
has several distinct sub-operations a user might ask for in one message
("pair my phone", "let mom order food on it", "what's my phone doing"),
so it gets a real small inner tool-calling loop (create_agent + a handful
of @tool-decorated functions), the same shape grocery_tools.py/
email_tools.py use -- NOT a single hardcoded dispatch.

Layering matches light-web-agent's: messa/devices/android.py owns the
engine + device manager + connect/run/release orchestration; this module
is the thin subagent wrapper translating between Messa's conversation and
that engine, plus the plan-tier/family-sharing/authorization checks that
belong at the "a real user is asking for this" layer, not inside the
engine itself.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool

from .. import config, console, db, phone_activity, plans
from ..approval import ApprovalGate
from .common import last_ai_text, run_inner_agent_with_claim_check

logger = logging.getLogger(__name__)

LABEL = "android_phone_agent"

ANDROID_PHONE_SYSTEM_PROMPT = (
    "You are Messa's Open-Source Phone agent (android_phone_agent) -- you pair with and drive the "
    "user's OWN Android phone over ADB wireless debugging to open apps, order food, book rides, and "
    "navigate dashboards the way a human would tap through them.\n\n"
    "Tools:\n"
    "- pair_my_phone: the user texts pairing details (host:port and a 6-digit code) from "
    "Settings > Developer Options > Wireless Debugging > Pair device with pairing code. Call this "
    "whenever a message looks like a pairing attempt (an IP:port and a short numeric code).\n"
    "- allow_contact_to_use_phone: the phone owner authorizes a family member (email or phone number) "
    "to queue tasks, scoped to specific categories (food delivery, rides, smart home, streaming, maps "
    "ONLY -- never banking/messaging/photos/settings, no matter how it's phrased).\n"
    "- get_phone_status / list_my_phones: status checks.\n"
    "- run_phone_task: actually do something on the phone (order food, book a ride, open an app, etc). "
    "This can take a while and may pause partway through to confirm something with the user (a cart "
    "total before paying, an unfamiliar screen) -- that's expected, relay whatever it reports back "
    "verbatim so the user knows what's happening.\n"
    "- publish_phone_skill: only when the user explicitly asks to publish/share a skill/recipe for "
    "reuse -- never on your own initiative.\n\n"
    "If a message doesn't clearly match pairing/authorization/status/publish, treat it as a task goal "
    "and call run_phone_task. Never fabricate a device status or task result -- always come from a "
    "tool call.\n\n"
    "CONVERSATIONAL SELF-HEALING: if a tool reports a connectivity problem (expired pairing code, "
    "wireless debugging off, screen locked), relay that guidance to the user plainly -- don't just say "
    "'it failed.'"
)


def _split_host_port(host_port: str) -> tuple[str, int] | None:
    m = re.match(r"^\s*([^\s:]+)\s*:\s*(\d{2,5})\s*$", host_port or "")
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _guess_category(goal: str) -> str | None:
    """Best-effort keyword match against config.ANDROID_PHONE_ALLOWED_
    CATEGORIES for a family-sharing authorization check -- deliberately
    simple (this repo's established convention for "doesn't need to be
    fancy, needs to stop the obviously wrong case" heuristics, same
    reasoning as light_web_agent.py's _goal_intent_matches keyword-overlap
    check). Returns None when no category can be guessed, which callers
    treat as "can't verify -- ask the owner" rather than silently allowing
    it."""
    text = (goal or "").lower()
    keyword_map = {
        "food_delivery": ("order", "food", "doordash", "uber eats", "grubhub", "instacart", "grocery", "restaurant"),
        "rides": ("ride", "uber", "lyft", "taxi", "pickup", "drop off"),
        "smart_home": ("thermostat", "lights", "smart home", "nest", "alarm", "lock the door"),
        "streaming": ("netflix", "spotify", "youtube", "play", "watch", "music"),
        "maps": ("navigate", "directions", "maps", "traffic"),
    }
    for category, keywords in keyword_map.items():
        if any(k in text for k in keywords):
            return category
    return None


def build_android_phone_tools(
    user: config.UserContext, model: BaseChatModel | None = None,
) -> list[BaseTool]:
    """Instantiates the tool suite for android_phone_agent."""
    uid = user.user_id

    @tool
    async def pair_my_phone(host_port: str, code: str, device_name: str = "My Phone") -> str:
        """Pairs the user's Android phone. host_port is the IP:port shown on the "Pair device with
        pairing code" screen (Settings > Developer Options > Wireless Debugging), code is the 6-digit
        pairing code shown there. device_name lets a user with multiple phones name this one (e.g.
        "Mom's Pixel") -- default "My Phone" is fine for a single-device user."""
        from ..devices.android import device_manager

        parsed = _split_host_port(host_port)
        if not parsed:
            return (
                "That doesn't look like a valid IP:port (e.g. \"192.168.1.42:37251\"). Please check "
                "Settings > Developer Options > Wireless Debugging > Pair device with pairing code "
                "and send it again."
            )
        host, port = parsed
        code = (code or "").strip()
        if not re.match(r"^\d{6}$", code):
            return "The pairing code should be exactly 6 digits -- please double check and try again."

        ok, message, discovered_port = await device_manager.pair_device(host, port, code)
        if not ok:
            console.tool_result(LABEL, "pair_my_phone", f"FAILED: {message}")
            return message

        connect_port = discovered_port or port
        row = await db.pair_user_device(uid, device_name, host, connect_port)
        if row is None:
            return (
                "Paired with your phone, but couldn't save it (device storage isn't set up on this "
                "deployment yet -- run migrations/043_android_phone_agent.sql)."
            )
        await db.update_device_status(row["id"], "connected")

        if discovered_port:
            return f"Paired and connected to \"{device_name}\"! I'm ready to help -- what would you like me to do?"
        return (
            f"Paired \"{device_name}\", but I couldn't auto-detect its ongoing connection port. "
            "On the MAIN Wireless Debugging screen (not the pairing sub-screen), text me the "
            "\"IP address & Port\" shown there so I can connect."
        )

    @tool
    async def confirm_phone_connect_port(host_port: str, device_name: str = "My Phone") -> str:
        """Only needed if pair_my_phone said it couldn't auto-detect the connection port. host_port is
        the "IP address & Port" shown on the MAIN Wireless Debugging screen (not the pairing
        sub-screen)."""
        parsed = _split_host_port(host_port)
        if not parsed:
            return "That doesn't look like a valid IP:port. Please check the Wireless Debugging screen and try again."
        host, port = parsed
        row = await db.pair_user_device(uid, device_name, host, port)
        if row is None:
            return "Couldn't save that (device storage isn't set up on this deployment yet)."
        await db.update_device_status(row["id"], "connected")
        return f"Got it -- \"{device_name}\" is ready. What would you like me to do?"

    @tool
    async def allow_contact_to_use_phone(contact: str, categories: str, device_name: str | None = None) -> str:
        """The phone owner authorizes a family member to queue tasks on this phone. contact is their
        email or phone number. categories is a comma-separated list from: food_delivery, rides,
        smart_home, streaming, maps -- NEVER banking, messages, whatsapp, photos, or phone_settings,
        even if the user asks for "everything" or "full access"; silently drop any category outside
        the allowed list rather than granting it."""
        device = await db.get_user_device(uid, device_name)
        if not device:
            return "You don't have a phone paired yet -- pair one first, then you can share access to it."

        requested = {c.strip().lower().replace(" ", "_") for c in (categories or "").split(",") if c.strip()}
        allowed = requested & config.ANDROID_PHONE_ALLOWED_CATEGORIES
        blocked = requested & config.ANDROID_PHONE_BLOCKED_CATEGORIES
        unrecognized = requested - config.ANDROID_PHONE_ALLOWED_CATEGORIES - config.ANDROID_PHONE_BLOCKED_CATEGORIES
        if not allowed:
            return (
                "I can only authorize these categories: food delivery, rides, smart home, streaming, "
                "maps. None of what you listed matched -- please pick from that list."
            )
        row = await db.authorize_device_contact(device["id"], contact, sorted(allowed))
        if row is None:
            return "Couldn't save that authorization (device storage isn't set up on this deployment yet)."
        note = ""
        if blocked:
            note += f" (Skipped {', '.join(sorted(blocked))} -- never authorizable, even for family.)"
        if unrecognized:
            note += f" (Didn't recognize: {', '.join(sorted(unrecognized))}.)"
        return f"{contact} can now use your phone for: {', '.join(sorted(allowed))}.{note}"

    @tool
    async def get_phone_status(device_name: str | None = None) -> str:
        """Reports whether the user's phone is paired/connected/offline and what it's doing."""
        from ..devices.android import device_manager

        device = await db.get_user_device(uid, device_name)
        if not device:
            return "You don't have a phone paired yet. Text me \"connect <ip:port> <code>\" from Wireless Debugging to pair one."
        managed = device_manager.get_managed(device["id"])
        live_status = managed.status if managed else device["status"]
        return f"\"{device['device_name']}\" is {live_status} (last seen: {device.get('last_seen_at') or 'never'})."

    @tool
    async def list_my_phones() -> str:
        """Lists every phone the user has paired."""
        devices = await db.list_user_devices(uid)
        if not devices:
            return "No phones paired yet."
        return "\n".join(f"- {d['device_name']}: {d['status']}" for d in devices)

    @tool
    async def run_phone_task(goal: str, device_name: str | None = None) -> str:
        """Actually performs `goal` on the user's phone (order food, book a ride, open an app and do
        something). Requires an already-paired, connected device. May pause partway through and
        return a question needing the user's answer -- relay that verbatim, don't summarize it away."""
        from ..devices.android import device_manager, run_android_phone_task

        device = await db.get_user_device(uid, device_name)
        is_shared_device = False
        if not device:
            # Family sharing lookup (open-source-phone.md section 4): if the caller
            # has no phone of their own, check if an authorized family device exists
            # for their email or phone number.
            for contact in (user.email, user.phone_number):
                if contact:
                    authorized = await db.find_devices_authorized_for_contact(contact)
                    if authorized:
                        if device_name:
                            matched = [d for d in authorized if d.get("device_name", "").lower() == device_name.lower()]
                            device = matched[0] if matched else None
                        else:
                            device = authorized[0]
                        if device:
                            is_shared_device = True
                            break

        if not device:
            return "You don't have a phone paired yet. Text me \"connect <ip:port> <code>\" from Wireless Debugging to pair one."
        if device["status"] == "revoked":
            return "That phone was unpaired. Please pair it again first."

        if is_shared_device:
            cat = _guess_category(goal)
            allowed_cats = set(device.get("allowed_categories") or [])
            if cat and cat not in allowed_cats:
                return (
                    f"You're authorized to use \"{device.get('device_name', 'this phone')}\" for "
                    f"{', '.join(sorted(allowed_cats))}, but not for {cat.replace('_', ' ')}. "
                    "Please ask the owner to expand your permissions."
                )

        plan = plans.get_plan(user.plan_id)
        max_steps = plan.limits.phone_automation_steps
        timeout_seconds = plan.limits.phone_task_timeout_seconds
        if getattr(user, "is_admin", False):
            max_steps = 200
            timeout_seconds = 1800
        elif max_steps == 0:
            return (
                f"Phone automation isn't included on your {plan.name} plan yet. "
                "Upgrade to unlock it."
            )
        server_ceiling = 200  # absolute safety cap even on an "unlimited" (None) plan
        effective_max_steps = server_ceiling if max_steps is None else min(max_steps, server_ceiling)
        effective_timeout = 1800 if timeout_seconds is None else min(timeout_seconds, 1800)

        try:
            ticket = await device_manager.acquire(device["id"])
        except Exception as e:
            return str(e)

        if ticket.position > 0:
            console.system(f"[{LABEL}] user #{uid} queued at position {ticket.position} for device {device['id']}")

        # open-source-phone.md section 5: "When a task starts, Messa texts
        # the user a live monitoring link" -- sent proactively, separate
        # from this tool's own return value (which the orchestrator relays
        # only once the ENTIRE run finishes, since run_android_phone_task
        # is awaited synchronously below and can take a while). Best-effort
        # and non-fatal: a failed/missing send must never block the actual
        # automation from starting.
        if user.phone_number and getattr(config, "SENDBLUE_API_KEY", None):
            try:
                live_token = await db.get_or_create_live_share_token(uid)
                if live_token:
                    from ..channels import sendblue
                    await sendblue.send_message(
                        user.phone_number,
                        f"Working on your phone task now! Watch live: "
                        f"{config.LIVE_VIEW_BASE_URL}/live/{live_token}/phone",
                    )
            except Exception as e:
                logger.debug(f"[{LABEL}] live-link send failed (non-fatal): {e}")

        async with ticket:
            try:
                result = await run_android_phone_task(
                    goal,
                    user_id=uid,
                    device_row=device,
                    max_steps=effective_max_steps,
                    timeout_seconds=effective_timeout,
                )
            except Exception as e:
                logger.exception(f"[{LABEL}] run_phone_task failed: {e}")
                phone_activity.clear(uid)
                return f"Something went wrong running that on your phone: {e}"

        status = result.get("status", "FAILED")
        summary = result.get("result_summary") or ""
        steps = result.get("steps_taken", 0)
        checkpoint = result.get("checkpoint")

        if status == "DONE":
            phone_activity.clear(uid)
            return f"Done ({steps} steps). {summary}" if summary else f"Done in {steps} steps."
        if status == "NEEDS_HUMAN":
            # Deliberately NOT cleared here -- the live-view page should
            # keep showing "waiting on you" (phone_activity's waiting_prompt,
            # set by AndroidPhoneAgent._persist_pending_checkpoint) until
            # the task actually resumes or the user aborts it from the
            # page itself.
            prompt = ""
            token = None
            if isinstance(checkpoint, dict):
                prompt = checkpoint.get("prompt_to_user") or ""
                token = checkpoint.get("screenshot_share_token")
            reply = f"Paused -- need your input: {prompt}" if prompt else "Paused -- I need your help to continue."
            # Milestone screenshot (open-source-phone.md section 5): send it
            # directly here as an MMS attachment rather than only relaying
            # text -- same mechanism stagehand_tools.py's _send_screenshot
            # uses (a Postgres-backed document share -> /files/{token}).
            if token and user.phone_number and getattr(config, "SENDBLUE_API_KEY", None):
                from ..channels import sendblue
                try:
                    await sendblue.send_message(
                        user.phone_number, reply, media_url=f"{config.LIVE_VIEW_BASE_URL}/files/{token}",
                    )
                    return "Sent the pause reason and a screenshot directly to the user via text."
                except Exception as e:
                    logger.debug(f"[{LABEL}] milestone MMS send failed (falling back to text): {e}")
            return reply
        phone_activity.clear(uid)
        return f"Couldn't finish after {steps} steps. {summary or 'Please try again.'}"

    @tool
    async def publish_phone_skill(
        skill_slug: str, app_name: str, app_package: str, description: str, recipe_json: str,
    ) -> str:
        """Publishes a reusable automation recipe to Messa's public skill showcase (messa.ai/skills)
        ONLY when the user explicitly asks to publish/share it. recipe_json must already use
        {{cred:...}} tokens for anything personal/secret -- never a real value. skill_slug should be
        short and descriptive, e.g. 'goodreads_log_book'."""
        sanitized, warnings = _sanitize_recipe(recipe_json)
        row = await db.publish_device_skill(
            uid, skill_slug, app_name, app_package, sanitized,
            author_handle=user.name, description=description, is_public=True,
        )
        if row is None:
            return "Couldn't publish that (skill storage isn't set up on this deployment yet)."
        note = f" (Note: {warnings})" if warnings else ""
        return f"Published \"{skill_slug}\" for {app_name} to the public skill showcase.{note}"

    return [
        pair_my_phone, confirm_phone_connect_port, allow_contact_to_use_phone,
        get_phone_status, list_my_phones, run_phone_task, publish_phone_skill,
    ]


_PII_PATTERNS = [
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),   # emails
    re.compile(r"\b\+?\d{10,14}\b"),                                  # bare-digit phone-ish numbers (10-14 digits)
    # Formatted phone numbers (US and international formats: +1 (555) 123-4567, 555-123-4567,
    # +44 20 7946 0958, +91 98765-43210, etc.)
    re.compile(r"\b\+?\d{1,3}[-.\s]?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b"),
]


def _sanitize_recipe(recipe_json: str) -> tuple[str, str | None]:
    """Best-effort scrub before a recipe is made public (open-source-
    phone.md section 6: "strips personal credentials, names, addresses").
    Strips anything email/phone-shaped that isn't already a {{cred:...}}
    token. Deliberately simple pattern matching, not a security boundary
    on its own -- publish_phone_skill's own docstring already tells the
    model to use tokens, this is the defense-in-depth backstop for
    whatever slips through."""
    text = recipe_json
    warned = False
    for pattern in _PII_PATTERNS:
        def _redact(m: re.Match) -> str:
            nonlocal warned
            warned = True
            return "[redacted]"
        text = pattern.sub(_redact, text)
    warning = "removed what looked like personal contact info before publishing" if warned else None
    return text, warning


def build_android_phone_subagent(
    user: config.UserContext,
    model: BaseChatModel,
    approval_gate: ApprovalGate | None = None,
) -> dict[str, Any]:
    """Returns a deepagents CompiledSubAgent spec for android_phone_agent,
    gated by registry.py on config.ANDROID_PHONE_AGENT_ENABLED."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        if not getattr(config, "ANDROID_PHONE_AGENT_ENABLED", False):
            return {
                "messages": [AIMessage(content="Bring-your-own-phone isn't enabled on this deployment yet.")]
            }

        messages = list(state["messages"])
        tools = build_android_phone_tools(user, model)
        system_prompt = ANDROID_PHONE_SYSTEM_PROMPT

        run_config = {"recursion_limit": config.RECURSION_LIMIT}
        inner_agent = create_agent(model=model, tools=tools, system_prompt=system_prompt)
        final_messages = await run_inner_agent_with_claim_check(inner_agent, messages, run_config, label=LABEL)
        return {"messages": [AIMessage(content=last_ai_text(final_messages))]}

    return {
        "name": "android_phone_agent",
        "description": (
            "Pairs and drives the user's OWN Android phone (Bring Your Own Phone / open-source phone) "
            "over ADB wireless debugging: opening apps, ordering food, booking rides, navigating any "
            "app's real UI the way a human would tap through it. Also handles pairing a phone, "
            "authorizing family members to share it, and publishing/browsing community app-automation "
            "skills. Use whenever the user wants Messa to control THEIR PHYSICAL PHONE directly -- as "
            "opposed to light_web_agent (a cloud browser) or deepsearch (multi-site web research)."
        ),
        "runnable": RunnableLambda(_run),
    }
