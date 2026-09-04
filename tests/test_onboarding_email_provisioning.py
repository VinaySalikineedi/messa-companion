import asyncio
import re
from messa import db, config, cli
from messa.agents.registry import _ONBOARDING_PROMPTS


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

    # Step B: User 1 answers "What's your name?" with "Taylor Swift"
    updated_1 = await db.save_profile_field(uid_1, "name", "Taylor Swift")
    assert updated_1["messa_email_local_part"] == "taylorswift", f"Expected taylorswift, got {updated_1.get('messa_email_local_part')}"
    print(f"[PASS] User {uid_1} provisioned <username>@textmessa.com: {updated_1['messa_email_local_part']}@textmessa.com")

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

    # Step E: Test _onboarding_complete_messages reveal format
    # Move to awaiting_location, then awaiting_email
    await db.save_profile_field(uid_1, "city", "New York")
    # Load user context at start of the final onboarding turn (onboarding_step is 'awaiting_email')
    turn_start_ctx = await cli.load_user_context(test_phone_1)
    assert not turn_start_ctx.onboarding_complete, "User should still be in onboarding"

    # User answers the email step, completing onboarding in DB
    await db.save_profile_field(uid_1, "email", "skip")

    # Now _onboarding_complete_messages runs at the end of this turn
    messages = await cli._onboarding_complete_messages(turn_start_ctx)
    print(f"Reveal messages: {messages}")
    assert any("taylorswift@textmessa.com" in m for m in messages), (
        f"Expected taylorswift@textmessa.com in reveal messages, got: {messages}"
    )
    assert not any(f"user{uid_1}@textmessa.com" in m for m in messages), (
        f"Found placeholder user{uid_1} in reveal messages!"
    )
    print("[PASS] _onboarding_complete_messages correctly reveals taylorswift@textmessa.com")

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
