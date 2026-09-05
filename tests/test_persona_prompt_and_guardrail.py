"""Tests for this round's system-prompt/persona rewrite, the new
conversational "profile enrichment" replacement for the old rigid
location/email onboarding steps, the deterministic pre-send guardrail
(markdown/length -> LLM compression, with a safe fallback), and the
confetti-on-onboarding-complete wiring -- see plans/glowing-forging-pumpkin.md
for the full design.

Five parts:
  1. agents/registry.py's _profile_enrichment_str -- the new always-on
     (not step-gated) contextual nudge for city/email, respecting the new
     *_prompt_skipped flags.
  2. _build_system_prompt sanity: persona block present, the old redundant
     per-category routing prose is gone (replaced by the compact version),
     and the Google Docs/Word gap is closed (integrations_agent mentioned
     for non-PDF documents).
  3. cli.py's _apply_reply_guardrail: short/clean text passes through with
     zero model calls; markdown or overlong text triggers exactly one
     compression call; a failed compression call falls back to the
     original text rather than losing the reply.
  4. cli.py's onboarding-complete reveal loop: confetti (via
     sendblue.send_message(..., send_style="confetti")) fires on the FIRST
     reveal message only when user.message_handle is set (a real iMessage
     thread), falls back to the generic `send` callback otherwise or if
     the confetti send itself raises.
  5. Gmail (email_tools.py) still never imports channels.resend (persona/
     guardrail work shouldn't have changed this structural fact).
"""
import asyncio
import os
import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    # No real .env in this environment (e.g. a fresh cloud sandbox) --
    # fall back to harmless dummy credentials so imports succeed and
    # fake-pool/fake-model tests can run. A real .env (e.g. the actual
    # dev machine/server) always wins untouched, since
    # messa.config's own load_dotenv() never overrides an already-set var.
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import cli, config, db  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.channels import sendblue  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**kwargs) -> config.UserContext:
    base = dict(user_id=1, phone_number="+15550000000", name="Jane")
    base.update(kwargs)
    return config.UserContext(**base)


def part1_profile_enrichment_str():
    u = _user(city=None, email=None, city_prompt_skipped=False, email_prompt_skipped=False)
    s = registry._profile_enrichment_str(u)
    check("both missing: mentions city/zip", "city" in s.lower() or "zip" in s.lower())
    check("both missing: mentions email", "email" in s.lower())
    check("never blocks the task (says so explicitly)", "never block" in s.lower())

    u2 = _user(city="Austin, TX", email="jane@example.com")
    check("both known: empty string", registry._profile_enrichment_str(u2) == "")

    u3 = _user(city=None, email=None, city_prompt_skipped=True, email_prompt_skipped=True)
    check("both explicitly skipped: empty string (never asked again)",
          registry._profile_enrichment_str(u3) == "")

    u4 = _user(city=None, email="jane@example.com", city_prompt_skipped=False)
    s4 = registry._profile_enrichment_str(u4)
    check("only city missing: mentions city, not email",
          ("city" in s4.lower() or "zip" in s4.lower()) and "email" not in s4.lower())


def part2_system_prompt_sanity():
    u = _user(city=None, email=None)
    prompt = registry._build_system_prompt(u)

    check("persona block present (task-oriented framing)",
          "task-oriented" in prompt.lower())
    check("persona bans the 'honestly' crutch phrase explicitly",
          "honestly" in prompt.lower())
    check("old redundant 'Calendar routing -- same shape as email routing' prose is gone",
          "same shape as email routing" not in prompt)
    check("old redundant 'This matters for real money' aside is gone",
          "matters for real money" not in prompt)
    check("old duplicated calendar cross-reference paragraph is gone",
          "this note is just a" not in prompt.lower())
    check("Google Docs/Word gap closed: integrations_agent offered for non-PDF documents",
          "integrations_agent" in prompt and ("Word" in prompt or "Google Docs" in prompt))
    check("document_agent's PDF-only scope still stated",
          "PDF" in prompt and "document_agent" in prompt)
    check("onboarding_str no longer references the removed awaiting_location/awaiting_email prompts",
          "awaiting_location" not in prompt and "awaiting_email" not in prompt)

    u2 = _user(name=None, onboarding_step="awaiting_name")
    prompt2 = registry._build_system_prompt(u2)
    check("a brand-new user (no name) still gets asked for it",
          "what's your name" in prompt2.lower())


class _FakeContent:
    def __init__(self, content):
        self.content = content


async def part3_reply_guardrail():
    real_build_model = config.build_model

    # 3a: short, clean text -- zero model calls.
    def _explode(*a, **kw):
        raise AssertionError("build_model should never be called for a clean short reply")
    config.build_model = _explode
    try:
        out = await cli._apply_reply_guardrail("Sure, I'll send that over. What's the address?")
        check("short/clean reply passes through unchanged with zero model calls",
              out == "Sure, I'll send that over. What's the address?")
    finally:
        config.build_model = real_build_model

    # 3b: markdown artifacts trigger exactly one compression call.
    calls = []

    class _FakeModel:
        async def ainvoke(self, messages):
            calls.append(messages)
            return _FakeContent("Rewritten short version.")

    config.build_model = lambda *a, **kw: _FakeModel()
    try:
        markdown_text = "Here you go:\n- item one\n- item two\n- item three"
        out = await cli._apply_reply_guardrail(markdown_text)
        check("markdown-laden reply triggers exactly one compression call", len(calls) == 1)
        check("compression result is used as the final text", out == "Rewritten short version.")
    finally:
        config.build_model = real_build_model
        calls.clear()

    # 3c: overlong plain text (no markdown) also triggers compression.
    config.build_model = lambda *a, **kw: _FakeModel()
    try:
        long_text = "This is a very long reply. " * 20
        out = await cli._apply_reply_guardrail(long_text)
        check("overlong plain-text reply also triggers compression", len(calls) == 1)
        check("overlong reply's compression result is used", out == "Rewritten short version.")
    finally:
        config.build_model = real_build_model
        calls.clear()

    # 3d: compression call fails -- original text still goes out.
    class _BrokenModel:
        async def ainvoke(self, messages):
            raise RuntimeError("upstream blew up")

    config.build_model = lambda *a, **kw: _BrokenModel()
    try:
        markdown_text = "# Heading\n- one\n- two"
        out = await cli._apply_reply_guardrail(markdown_text)
        check("a failed compression call falls back to the original text (never lost)",
              out == markdown_text)
    finally:
        config.build_model = real_build_model


async def part4_confetti_reveal():
    real_run_turn = cli.run_turn
    real_reveal = cli._onboarding_complete_messages
    real_append = db.append_message
    real_recent = db.get_recent_messages
    real_send = sendblue.send_message

    async def fake_run_turn(agent, messages, on_ai_message=None, _allow_retry=True):
        return []  # the model said nothing else worth sending this turn

    async def fake_append_message(*a, **kw):
        return 1

    async def fake_get_recent_messages(*a, **kw):
        return []

    sent_calls = []

    async def fake_send_message(phone_number, content, send_style=None):
        sent_calls.append({"phone_number": phone_number, "content": content, "send_style": send_style})

    cli.run_turn = fake_run_turn
    db.append_message = fake_append_message
    db.get_recent_messages = fake_get_recent_messages
    sendblue.send_message = fake_send_message

    plain_sent = []

    async def plain_send(text):
        plain_sent.append(text)

    try:
        # 4a: a real iMessage thread (message_handle set) -- confetti on the
        # first reveal message, plain send for anything after it.
        async def fake_reveal_two_messages(user):
            return ["You're all set, Jane!", "Here's my email + link."]
        cli._onboarding_complete_messages = fake_reveal_two_messages

        user = _user(is_admin=True, message_handle="imessage;-;+15550000000", channel="sms")
        sent_calls.clear()
        plain_sent.clear()
        await cli.run_message(user, agent=object(), text="hi", send=plain_send)
        check("confetti fires on the first reveal message for a real iMessage thread",
              len(sent_calls) == 1 and sent_calls[0]["send_style"] == "confetti"
              and sent_calls[0]["content"] == "You're all set, Jane!")
        check("the SECOND reveal message goes through the plain send callback, not confetti",
              plain_sent == ["Here's my email + link."])

        # 4b: no message_handle (plain SMS/RCS, or CLI/email) -- no confetti
        # at all, everything through the plain send callback.
        user2 = _user(is_admin=True, message_handle=None, channel="sms")
        sent_calls.clear()
        plain_sent.clear()
        await cli.run_message(user2, agent=object(), text="hi", send=plain_send)
        check("no message_handle: confetti never attempted", sent_calls == [])
        check("no message_handle: both reveal messages go through the plain send callback",
              plain_sent == ["You're all set, Jane!", "Here's my email + link."])

        # 4c: message_handle set, but the confetti send itself raises --
        # falls back to the plain send callback for that message instead of
        # losing it.
        async def failing_send_message(phone_number, content, send_style=None):
            raise sendblue.SendblueError("boom")
        sendblue.send_message = failing_send_message

        user3 = _user(is_admin=True, message_handle="imessage;-;+15550000000", channel="sms")
        sent_calls.clear()
        plain_sent.clear()
        await cli.run_message(user3, agent=object(), text="hi", send=plain_send)
        check("a failed confetti send falls back to the plain send callback (message not lost)",
              plain_sent == ["You're all set, Jane!", "Here's my email + link."])
    finally:
        cli.run_turn = real_run_turn
        cli._onboarding_complete_messages = real_reveal
        db.append_message = real_append
        db.get_recent_messages = real_recent
        sendblue.send_message = real_send


def part5_gmail_structurally_unaffected():
    from messa.tools import email_tools
    import inspect
    source = inspect.getsource(email_tools)
    check("email_tools.py (Gmail/Composio) still never imports channels.resend",
          "channels.resend" not in source and "from ..channels import resend" not in source
          and "from .resend" not in source)


async def main() -> None:
    part1_profile_enrichment_str()
    part2_system_prompt_sanity()
    await part3_reply_guardrail()
    await part4_confetti_reveal()
    part5_gmail_structurally_unaffected()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
