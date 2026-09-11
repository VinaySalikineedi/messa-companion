"""Tests for Feature 3 of the "onboarding apps / reactions / message-split /
minimal-questions" round: a long reply is split into a few shorter texts
(average bubble size) instead of arriving as one long wall of text.

Two parts:
  1. cli.py's _split_into_texts as a pure function -- short text untouched
     (zero behavior change for the overwhelmingly common case), paragraph
     splitting, sentence-boundary fallback when there's no blank-line
     structure, the _SPLIT_MAX_PARTS cap re-merging any overflow into the
     last chunk, and edge cases (empty string, whitespace-only).
  2. cli.py's _on_ai_message loop (inside run_message): a message that
     splits into several chunks sends each one as its own text, consumes
     one number_of_texts usage unit PER chunk (not one per logical AI
     message), and -- if a chunk mid-way pushes the user over today's
     limit -- sends today's one notice and drops the remaining chunks,
     exactly like the pre-existing single-message-over-limit behavior this
     replaces. The short-message case (a single-item list) is confirmed to
     behave identically to before this feature existed: one unit consumed,
     one DB row, one send() call.
"""
import asyncio
import os
import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import cli, config, db, usage  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**kwargs) -> config.UserContext:
    base = dict(user_id=1, phone_number="+15550000000", name="Jane", onboarding_step="complete")
    base.update(kwargs)
    return config.UserContext(**base)


# --- Part 1: _split_into_texts pure function ---

def part1_split_into_texts():
    check("empty string returns an empty list (nothing to send)",
          cli._split_into_texts("") == [])

    short = "Sounds good, I'll take care of it."
    check("short text (under the threshold) is returned as a single-item list, unchanged",
          cli._split_into_texts(short) == [short])

    check("text exactly at the threshold is still a single chunk",
          cli._split_into_texts("x" * cli._GUARDRAIL_LENGTH_THRESHOLD) == ["x" * cli._GUARDRAIL_LENGTH_THRESHOLD])

    # Paragraph-based splitting: four short paragraphs, clearly over the
    # threshold once joined -- should come back as more than one chunk, each
    # built only from whole paragraphs (never mid-word), each within a
    # reasonable bound of _SPLIT_TARGET_CHARS.
    paragraphs = [
        "First, I checked your calendar for tomorrow and it's wide open all morning.",
        "Second, I drafted the email to the vendor and it's ready for your review whenever you get a chance.",
        "Third, I set a reminder for the dentist appointment next Tuesday at 2pm like you asked.",
        "Last, I also noticed your flight check-in opens tonight, so I'll go ahead and check you in automatically.",
    ]
    long_text = "\n\n".join(paragraphs)
    check("long_text used for this test is actually over the guardrail threshold",
          len(long_text) > cli._GUARDRAIL_LENGTH_THRESHOLD)
    chunks = cli._split_into_texts(long_text)
    check("a long, multi-paragraph message splits into more than one chunk",
          len(chunks) > 1)
    check("splitting never drops or mangles any of the original paragraph text",
          all(p in "\n".join(chunks) for p in paragraphs))
    check(f"chunk count stays within the _SPLIT_MAX_PARTS cap ({cli._SPLIT_MAX_PARTS})",
          len(chunks) <= cli._SPLIT_MAX_PARTS)
    check("no chunk is empty", all(c.strip() for c in chunks))

    # No blank-line structure at all -- falls back to sentence-boundary
    # splitting instead of returning the whole thing as one giant chunk.
    no_paragraphs = (
        "I looked into three flights for you and here is what I found. The cheapest one leaves "
        "at 6am and costs 210 dollars with one layover in Denver. The fastest one is nonstop but "
        "costs 340 dollars and leaves at 2pm instead. There is also a middle option that leaves "
        "at 9am, has one short layover in Chicago, and costs 265 dollars total. Let me know which "
        "one you would like me to book and I will take care of the rest right away."
    )
    check("no_paragraphs test text is over the threshold too",
          len(no_paragraphs) > cli._GUARDRAIL_LENGTH_THRESHOLD)
    chunks2 = cli._split_into_texts(no_paragraphs)
    check("sentence-boundary fallback also produces more than one chunk when there's no blank-line structure",
          len(chunks2) > 1)
    check("sentence-fallback chunks are all non-empty and don't split mid-sentence "
          "(each ends in . ! or ?, aside from possibly the last)",
          all(c.strip()[-1] in ".!?" for c in chunks2[:-1]))

    # A message with no natural break points at all (one giant "sentence")
    # can't be meaningfully split -- returned as-is rather than chopped
    # mid-word.
    one_giant_sentence = "a" * 500
    check("text with no paragraph or sentence boundaries at all is left as a single chunk "
          "(never chopped mid-word)",
          cli._split_into_texts(one_giant_sentence) == [one_giant_sentence])

    # Overflow past _SPLIT_MAX_PARTS gets re-merged into the last chunk
    # rather than silently dropped.
    many_paragraphs = "\n\n".join(f"This is paragraph number {i} with some real content in it." for i in range(10))
    chunks3 = cli._split_into_texts(many_paragraphs)
    check(f"even with many short paragraphs, output never exceeds _SPLIT_MAX_PARTS ({cli._SPLIT_MAX_PARTS})",
          len(chunks3) <= cli._SPLIT_MAX_PARTS)
    check("re-merging overflow into the last chunk doesn't lose any paragraph's content",
          all(f"paragraph number {i}" in "\n".join(chunks3) for i in range(10)))

    # URL isolation for rich preview cards in iMessage / SMS
    cart_url = "https://www.instacart.com/store/recipes/23033553-messa-publix-order?retailer=publix"
    publix_msg = f"Here's your Publix cart link:\n\n{cart_url}\n\nIt's got 1 gallon whole milk and 1 dozen large eggs."
    url_chunks = cli._split_into_texts(publix_msg)
    check("message with embedded URL splits so URL is in its own standalone chunk",
          cart_url in url_chunks)
    check("URL chunk contains ONLY the URL (no surrounding text or punctuation)",
          any(c == cart_url for c in url_chunks))
    check("standalone single URL returns as single-item list",
          cli._split_into_texts(cart_url) == [cart_url])
    check("markdown link is expanded and URL is isolated",
          cart_url in cli._split_into_texts(f"Review your cart: [Publix Link]({cart_url}). Ready to order."))


# --- Part 2: _on_ai_message's per-chunk send/usage-consumption behavior ---

async def part2_on_ai_message_chunking():
    real_run_turn = cli.run_turn
    real_apply_guardrail = cli._apply_reply_guardrail
    real_append = db.append_message
    real_recent = db.get_recent_messages
    real_get_user_by_id = db.get_user_by_id
    real_peek_usage = usage.peek_usage
    real_check_and_consume = usage.check_and_consume
    real_claim_notice = usage.claim_daily_notice

    appended = []

    async def fake_append_message(user_id, role, content, channel=None):
        appended.append((role, content))
        return len(appended)

    async def fake_get_recent_messages(*a, **kw):
        return []

    async def fake_get_user_by_id(user_id):
        # onboarding_step is already 'complete' on the UserContext passed in
        # (onboarding_complete short-circuits _onboarding_complete_messages
        # before this is ever reached in these tests, but kept honest just
        # in case).
        return {"onboarding_step": "complete"}

    async def identity_guardrail(msg_text):
        # The guardrail's own compression behavior has its own dedicated
        # tests (test_persona_prompt_and_guardrail.py's part3) -- bypassed
        # here so this test is only exercising the NEW splitting/usage
        # logic, not re-testing the guardrail or making a real model call.
        return msg_text

    db.append_message = fake_append_message
    db.get_recent_messages = fake_get_recent_messages
    db.get_user_by_id = fake_get_user_by_id
    cli._apply_reply_guardrail = identity_guardrail

    async def fake_peek_usage(user, feature):
        return usage.LimitResult(allowed=True, feature=feature, count=0, limit=1000, plan_name="test")

    usage.peek_usage = fake_peek_usage

    async def fake_claim_notice(user, feature):
        return True

    usage.claim_daily_notice = fake_claim_notice

    try:
        paragraphs = [
            "First, I checked your calendar for tomorrow and it's wide open all morning.",
            "Second, I drafted the email to the vendor and it's ready for your review whenever you get a chance.",
            "Third, I set a reminder for the dentist appointment next Tuesday at 2pm like you asked.",
            "Last, I also noticed your flight check-in opens tonight, so I'll go ahead and check you in automatically.",
        ]
        long_text = "\n\n".join(paragraphs)
        expected_chunks = cli._split_into_texts(long_text)
        check("test setup: the long reply used below actually splits into multiple chunks",
              len(expected_chunks) > 1)

        # --- 2a: everyone allowed -- every chunk sent, one usage unit each ---
        async def fake_run_turn_long(agent, messages, on_ai_message=None, _allow_retry=True):
            await on_ai_message(long_text)
            return []

        cli.run_turn = fake_run_turn_long

        consume_calls = []

        async def fake_check_and_consume_allow(user, feature, amount=1):
            consume_calls.append(feature)
            return usage.LimitResult(allowed=True, feature=feature, count=len(consume_calls), limit=1000, plan_name="test")

        usage.check_and_consume = fake_check_and_consume_allow

        sent = []

        async def fake_send(text):
            sent.append(text)

        user = _user(user_id=10, is_admin=False)
        result = await cli.run_message(user, agent=object(), text="what's the status?", send=fake_send)

        check("every chunk of the split reply is sent as its own text, in order",
              sent == expected_chunks)
        check("one number_of_texts usage unit is consumed PER CHUNK, not once per AI message",
              consume_calls == [usage.FEATURE_NUMBER_OF_TEXTS] * len(expected_chunks))
        check("each chunk is persisted as its own assistant row in message_history",
              [c for r, c in appended if r == "assistant"] == expected_chunks)
        check("run_message returns the LAST chunk actually sent", result == expected_chunks[-1])

        # --- 2b: a chunk mid-way pushes the user over their daily limit --
        # remaining chunks are dropped and today's one notice goes out
        # instead, matching the existing single-message-over-limit behavior.
        appended.clear()
        sent.clear()
        consume_calls.clear()

        limit_trip_at = len(expected_chunks) - 1  # allow all but the last chunk
        call_count = {"n": 0}

        async def fake_check_and_consume_trip(user, feature, amount=1):
            call_count["n"] += 1
            consume_calls.append(feature)
            allowed = call_count["n"] <= limit_trip_at
            return usage.LimitResult(allowed=allowed, feature=feature, count=call_count["n"], limit=limit_trip_at, plan_name="test")

        usage.check_and_consume = fake_check_and_consume_trip

        user2 = _user(user_id=11, is_admin=False)
        await cli.run_message(user2, agent=object(), text="what's the status?", send=fake_send)

        check("only the chunks before the limit was hit are actually sent",
              sent[:limit_trip_at] == expected_chunks[:limit_trip_at])
        check("exactly one over-limit notice is sent, appended after the allowed chunks",
              len(sent) == limit_trip_at + 1 and "daily limit" in sent[-1])
        check("the usage check was attempted for the chunk that tripped the limit too "
              "(consumption is attempted before deciding to drop)",
              len(consume_calls) == limit_trip_at + 1)

        # --- 2c: admin users bypass per-chunk usage checks entirely (no
        # regression to the existing is_admin short-circuit). ---
        appended.clear()
        sent.clear()
        consume_calls.clear()
        usage.check_and_consume = fake_check_and_consume_allow  # would fail the test if actually called

        async def fake_check_and_consume_should_not_be_called(user, feature, amount=1):
            raise AssertionError("check_and_consume must never be called for an admin user")

        usage.check_and_consume = fake_check_and_consume_should_not_be_called

        admin_user = _user(user_id=12, is_admin=True)
        await cli.run_message(admin_user, agent=object(), text="status?", send=fake_send)
        check("an admin user still gets every chunk sent, with zero usage-limit calls",
              sent == expected_chunks)

        # --- 2d: the pre-existing short-message case is completely
        # unaffected -- a single-item list behaves exactly as it always did. ---
        appended.clear()
        sent.clear()
        consume_calls.clear()
        usage.check_and_consume = fake_check_and_consume_allow

        short_text = "All set -- I booked it for 2pm Tuesday."

        async def fake_run_turn_short(agent, messages, on_ai_message=None, _allow_retry=True):
            await on_ai_message(short_text)
            return []

        cli.run_turn = fake_run_turn_short
        user3 = _user(user_id=13, is_admin=False)
        result3 = await cli.run_message(user3, agent=object(), text="book it", send=fake_send)
        check("a short reply is still sent as exactly one message (no regression)",
              sent == [short_text])
        check("a short reply still consumes exactly one usage unit",
              consume_calls == [usage.FEATURE_NUMBER_OF_TEXTS])
        check("run_message still returns that one message for the short case", result3 == short_text)
    finally:
        cli.run_turn = real_run_turn
        cli._apply_reply_guardrail = real_apply_guardrail
        db.append_message = real_append
        db.get_recent_messages = real_recent
        db.get_user_by_id = real_get_user_by_id
        usage.peek_usage = real_peek_usage
        usage.check_and_consume = real_check_and_consume
        usage.claim_daily_notice = real_claim_notice


async def main() -> None:
    part1_split_into_texts()
    await part2_on_ai_message_chunking()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
