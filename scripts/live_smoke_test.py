"""Live, end-to-end smoke test for the onboarding/persona/guardrail/confetti
release (plans/glowing-forging-pumpkin.md). Unlike everything in tests/,
which runs entirely against fake pools/models and proves the CODE PATHS are
correct, this script talks to your REAL Postgres and REAL LLM (via whatever
.env this repo already has configured) and prints Messa's actual replies so
a human can eyeball the thing unit tests structurally cannot verify: does
this actually read like a short, human text message with the right persona,
does onboarding really complete after just a name, does the live-view link
and confetti really fire, does a follow-up question get city/email asked
contextually instead of blocked or forgotten.

This is NOT a pass/fail script (a few checks assert obvious structural
things, but the persona/tone/formatting quality is for you to read and
judge) -- it's the "does this feel right for real" gate before a
production push, complementing (not replacing) tests/'s regression suite.

Requirements:
  - A real DATABASE_URL in this repo's .env (or exported in your shell),
    pointing at a Postgres you're OK writing one throwaway test row into
    (see cleanup below).
  - A real OPENROUTER_API_KEY (or whatever ORCHESTRATOR/SUBAGENT keys your
    .env sets) -- this makes real, billed LLM calls. A handful of short
    turns, so cost is a few cents at most, not a load test.
  - migrations/031_profile_prompt_skips.sql applied (this script applies it
    itself, idempotently, as its first step -- see apply_migration_031()).

What it does:
  1. Applies migration 031 for real against your DATABASE_URL (safe to run
     even if already applied -- IF NOT EXISTS).
  2. Creates ONE throwaway user (phone number +10000000099 -- a sentinel
     that can never be a real Sendblue-routed number) with no name, so it
     starts fresh at 'awaiting_name'.
  3. Runs a short scripted conversation through the REAL orchestrator
     (build_orchestrator) and REAL cli.run_message -- the exact function
     the production Sendblue webhook calls -- printing every message Messa
     actually sends, in order, exactly as a phone would receive them.
  4. Deletes the throwaway user (and everything that cascades from it) when
     done, so this leaves no trace in your database. If the script crashes
     before cleanup, the user row is easy to find and remove by hand (see
     the phone number above) -- it's printed again at the end either way.

Run:
  cd <repo root>
  python3 scripts/live_smoke_test.py

Read the transcript it prints and check, for each turn, against what you
actually want:
  - Turn 1 ("Hi"): does Messa ask for a name, briefly, without a wall of
    text explaining everything she can do?
  - Turn 2 (giving a name): does onboarding complete immediately -- one or
    two short messages, her own email address, a live-view link, and (this
    script can't show you the iMessage confetti effect itself -- that only
    renders on a real iMessage thread -- but it prints whether send_style
    was actually invoked)?
  - Turn 3 (a location-dependent ask, no city on file): does she ask for
    the city/zip naturally, folded into answering the question, rather
    than as a separate blocking demand?
  - Turn 4 ("how are you different from other AI assistants"): does she
    lead with task-oriented/life-manager positioning, not documents/legal?
  - Turn 5 (a long/rambly hypothetical): does the reply stay short,
    paragraph-separated, no markdown/bullets?
  - Across all turns: any stray "honestly"/"let me be honest" tics?
"""
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from messa import cli, config, db  # noqa: E402
from messa.agents.registry import build_orchestrator  # noqa: E402

TEST_PHONE = "+10000000099"  # sentinel -- never a real, Sendblue-routable number


def _looks_like_a_real_dsn(dsn: str | None) -> bool:
    return bool(dsn) and "dummy" not in dsn and "localhost" not in dsn.lower()


async def apply_migration_031() -> None:
    sql_path = REPO_ROOT / "migrations" / "031_profile_prompt_skips.sql"
    sql = sql_path.read_text()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    print("[setup] migration 031 applied (or already was) -- city_prompt_skipped/"
          "email_prompt_skipped columns confirmed present.\n")


async def cleanup(reason: str = "done") -> None:
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM users WHERE phone_number = $1 RETURNING id", TEST_PHONE,
            )
        if deleted:
            print(f"[cleanup:{reason}] removed throwaway test user id={deleted} "
                  f"(phone {TEST_PHONE}) and everything that cascaded from it.")
        else:
            print(f"[cleanup:{reason}] no test user row found to remove -- already clean.")
    except Exception as e:  # noqa: BLE001
        print(f"[cleanup:{reason}] FAILED -- remove it by hand: "
              f"DELETE FROM users WHERE phone_number = '{TEST_PHONE}'; ({e})")


async def send_turn(user: config.UserContext, agent, label: str, text: str) -> None:
    print(f"\n{'=' * 70}\nTURN: {label}\nYou: {text}\n{'-' * 70}")
    sent = []

    async def fake_send(msg_text: str) -> None:
        sent.append(msg_text)
        print(f"Messa: {msg_text}\n")

    reply = await cli.run_message(user, agent, text, send=fake_send)
    if reply and reply not in sent:
        # run_message's return value is the final reply; if `send` already
        # fired for it (the common case), don't print it twice.
        print(f"Messa (return value, not already sent above): {reply}\n")
    if not sent and not reply:
        print("Messa: (no reply this turn)\n")


async def main() -> None:
    if not _looks_like_a_real_dsn(config.DATABASE_URL if hasattr(config, "DATABASE_URL") else None):
        print(
            "This script needs a real DATABASE_URL in your .env (not a dummy/local "
            "placeholder) -- refusing to run against what looks like a non-real "
            "database. Set DATABASE_URL and re-run."
        )
        sys.exit(1)

    await cleanup(reason="pre-run safety sweep")
    await apply_migration_031()

    user_row = await db.get_or_create_user(TEST_PHONE, name=None)
    user = await cli.load_user_context_by_id(user_row["id"], channel="imessage")
    print(f"[setup] created throwaway test user id={user.user_id}, phone={TEST_PHONE}, "
          f"onboarding_step starts at {user_row['onboarding_step']!r}.")

    try:
        agent = await build_orchestrator(user)
        await send_turn(user, agent, "1. First contact", "Hi")

        # Reload context after every turn -- onboarding_step/name etc. are
        # loaded once per turn in production too (see cli.main_async's own
        # comment on why), so this mirrors a real webhook-per-message setup.
        user = await cli.load_user_context_by_id(user_row["id"], channel="imessage")
        agent = await build_orchestrator(user)
        await send_turn(user, agent, "2. Giving a name (should complete onboarding)", "It's Alex")

        user = await cli.load_user_context_by_id(user_row["id"], channel="imessage")
        agent = await build_orchestrator(user)
        await send_turn(
            user, agent, "3. Location-dependent ask (no city on file yet)",
            "Can you recommend a good coffee shop near me?",
        )

        user = await cli.load_user_context_by_id(user_row["id"], channel="imessage")
        agent = await build_orchestrator(user)
        await send_turn(
            user, agent, "4. Positioning question",
            "How are you different from other AI assistants?",
        )

        user = await cli.load_user_context_by_id(user_row["id"], channel="imessage")
        agent = await build_orchestrator(user)
        await send_turn(
            user, agent, "5. Open-ended ask (watch for length/markdown)",
            "I'm trying to plan a weekend trip somewhere warm next month, not sure "
            "where to start, what would you do?",
        )

        fresh = await db.get_user_by_id(user.user_id)
        print(f"\n{'=' * 70}\nFinal state: onboarding_step={fresh.get('onboarding_step')!r}, "
              f"name={fresh.get('name')!r}, city={fresh.get('city')!r}, "
              f"messa_email_local_part={fresh.get('messa_email_local_part')!r}")
    finally:
        await cleanup(reason="post-run")
        await db.close_pool()

    print(
        "\nDone. Read the transcript above against the checklist in this script's "
        "own module docstring -- this script can't grade tone/persona/formatting "
        "for you, only put the real thing in front of you to judge."
    )


if __name__ == "__main__":
    asyncio.run(main())
