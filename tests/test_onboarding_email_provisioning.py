"""Live-DB regression test: onboarding + <username>@textmessa.com
provisioning. Unlike tests/test_onboarding_and_email_db.py (fake pool,
structural only), this one hits a REAL Postgres (whatever DATABASE_URL this
repo's .env points at) end to end -- creates two throwaway users, exercises
the real onboarding/provisioning code paths against real rows, and cleans
up after itself. Good to run before a production push precisely because it
can't be fooled by a mocked connection silently accepting the wrong SQL.

Post-redesign (plans/glowing-forging-pumpkin.md Part 1): onboarding
completes as soon as 'name' is saved -- there's no more awaiting_location/
awaiting_email step to walk through first. City/email are now always-
answerable "profile enrichment" fields (see db.py's ONBOARDING_STEPS
comment and agents/registry.py's _profile_enrichment_str) -- Step E below
reflects that: the onboarding-complete reveal is checked right after the
name is saved, and city/email are then exercised as an independent,
non-blocking follow-up rather than the last two steps of a wizard.
"""
import asyncio
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import db, config, cli  # noqa: E402
from messa.agents.registry import _ONBOARDING_PROMPTS  # noqa: E402


def test_slugify():
    print("=== 1. Testing _slugify_local_part ===")
    assert db._slugify_local_part(None, 1) is None
    assert db._slugify_local_part("", 1) is None
    assert db._slugify_local_part("   ", 1) is None
    assert db._slugify_local_part("🎉😀❤️", 1) is None
    assert db._slugify_local_part("Jane Doe", 1) == "janedoe"
    assert db._slugify_local_part("Vinay", 10) == "vinay"
    assert db._slugify_local_part("O'Connor-Smith", 5) == "oconnorsmith"
    # Length capped at 24 chars
    assert len(db._slugify_local_part("supercalifragilisticexpialidocious", 2)) <= 24
    print("[PASS] _slugify_local_part correctly handles names and empty/special values")


def test_onboarding_prompt():
    print("\n=== 2. Testing Onboarding Prompt ===")
    prompt = _ONBOARDING_PROMPTS["awaiting_name"]
    assert "What's your name?" in prompt, f"Expected \"What's your name?\" in prompt, got: {prompt}"
    assert "call you" not in prompt.lower(), f"Did not expect 'call you' in prompt, got: {prompt}"
    print(f"[PASS] Prompt verified:\n{prompt}")


async def test_email_provisioning_and_collision():
    print("\n=== 3. Testing DB Email Provisioning & Collision ===")
    pool = await db.get_pool()
    test_phone_1 = "+19990001111"
    test_phone_2 = "+19990002222"

    async with pool.acquire() as conn:
        # Cleanup any previous test runs
        await conn.execute("DELETE FROM users WHERE phone_number IN ($1, $2)", test_phone_1, test_phone_2)

    # Step A: User 1 signs up without a name (brand-new inbound SMS)
    user_row_1 = await db.get_or_create_user(test_phone_1, name=None, timezone_name="UTC")
    uid_1 = user_row_1["id"]

    # At context load time, load_user_context calls get_or_create_messa_email_local_part
    local_part_1 = await db.get_or_create_messa_email_local_part(uid_1, None)
    assert local_part_1 is None, f"Expected None for nameless user, got: {local_part_1}"

    # Check database: messa_email_local_part MUST be NULL (not user9, user10, etc.)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT messa_email_local_part FROM users WHERE id = $1", uid_1)
        assert row["messa_email_local_part"] is None, f"Expected NULL in DB, got {row['messa_email_local_part']}"
    print(f"[PASS] Brand-new user {uid_1} has no messa_email_local_part (no placeholder user{uid_1})")

    # Capture the "start of turn" context BEFORE the name is saved -- this
    # is what a real inbound-text turn would have loaded at the top of the
    # turn, and it's what Step E below passes to _onboarding_complete_
    # messages to detect the awaiting_name -> complete transition.
    turn_start_ctx = await cli.load_user_context(test_phone_1)
    assert not turn_start_ctx.onboarding_complete, "User should still be awaiting a name"

    # Step B: User 1 answers "What's your name?" with "Taylor Swift" --
    # onboarding completes immediately (name is the only real gate now).
    updated_1 = await db.save_profile_field(uid_1, "name", "Taylor Swift")
    assert updated_1["messa_email_local_part"] == "taylorswift", f"Expected taylorswift, got {updated_1.get('messa_email_local_part')}"
    assert updated_1["onboarding_step"] == "complete", (
        f"Expected onboarding to complete right after name, got {updated_1['onboarding_step']!r}"
    )
    print(f"[PASS] User {uid_1} provisioned <username>@textmessa.com: {updated_1['messa_email_local_part']}@textmessa.com, "
          "onboarding complete")

    # Step C: User 2 signs up, also named "Taylor Swift" (collision test)
    user_row_2 = await db.get_or_create_user(test_phone_2, name=None, timezone_name="UTC")
    uid_2 = user_row_2["id"]

    updated_2 = await db.save_profile_field(uid_2, "name", "Taylor Swift")
    expected_collided = f"taylorswift{uid_2}"
    assert updated_2["messa_email_local_part"] == expected_collided, (
        f"Expected collision fallback {expected_collided}, got {updated_2.get('messa_email_local_part')}"
    )
    print(f"[PASS] Collision handling succeeded: User {uid_2} received {updated_2['messa_email_local_part']}@textmessa.com")

    # Step D: Test legacy placeholder upgrade
    async with pool.acquire() as conn:
        # Simulate an existing user stuck with legacy 'user<id>'
        await conn.execute(f"UPDATE users SET messa_email_local_part = 'user{uid_1}' WHERE id = $1", uid_1)

    # Calling with real name should upgrade the legacy placeholder
    upgraded = await db.get_or_create_messa_email_local_part(uid_1, "Taylor Swift")
    assert upgraded == "taylorswift", f"Expected upgraded local part 'taylorswift', got {upgraded}"
    print(f"[PASS] Legacy placeholder user{uid_1} successfully upgraded to {upgraded}")

    # Step E: Test _onboarding_complete_messages reveal format. This uses
    # the turn_start_ctx captured BEFORE Step B's name save (onboarding_
    # complete was False then) -- exactly like a real turn: the context is
    # loaded once at the top of the turn, the name gets saved mid-turn, and
    # the reveal check runs at the end using that same pre-turn snapshot.
    messages = await cli._onboarding_complete_messages(turn_start_ctx)
    print(f"Reveal messages: {messages}")
    assert any("taylorswift@textmessa.com" in m for m in messages), (
        f"Expected taylorswift@textmessa.com in reveal messages, got: {messages}"
    )
    assert not any(f"user{uid_1}@textmessa.com" in m for m in messages), (
        f"Found placeholder user{uid_1} in reveal messages!"
    )
    print("[PASS] _onboarding_complete_messages correctly reveals taylorswift@textmessa.com")

    # Step F: city/email are now independent, non-blocking "profile
    # enrichment" fields -- answerable any time post-onboarding, and never
    # touch onboarding_step themselves (see db.py's save_profile_field
    # docstring). 'skip' on either sets the matching *_prompt_skipped flag
    # instead of writing a literal value.
    with_city = await db.save_profile_field(uid_1, "city", "New York")
    assert with_city["city"] == "New York" and with_city["onboarding_step"] == "complete", (
        f"Expected city written and onboarding_step untouched, got {with_city}"
    )
    skipped_email = await db.save_profile_field(uid_1, "email", "skip")
    assert skipped_email.get("email_prompt_skipped") is True and not skipped_email.get("email"), (
        f"Expected email_prompt_skipped=True and no email written, got {skipped_email}"
    )
    print("[PASS] city/email are answerable post-onboarding without touching onboarding_step, "
          "and 'skip' sets the matching flag instead of writing a literal value")

    # Cleanup test rows
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE phone_number IN ($1, $2)", test_phone_1, test_phone_2)
    print("\n=== ALL ONBOARDING & EMAIL PROVISIONING TESTS PASSED! ===")


async def main():
    test_slugify()
    test_onboarding_prompt()
    await test_email_provisioning_and_collision()


if __name__ == "__main__":
    asyncio.run(main())
