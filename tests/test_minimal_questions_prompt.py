"""Test for Feature 4 of the "onboarding apps / reactions / message-split /
minimal-questions" round: "messa can ask user questions to better help them
do the task, mostly do not assume unless necessary... just ask necessary
questions and go to the task quickly."

This is a prompt-only change (agents/registry.py's new "Questions before
acting" paragraph, right after the persona/tone block and before the
subagent roster) -- there is no code path to exercise, so unlike Features
1-3 this can only confirm the instruction text is actually present and says
the right things, not that a live model will always follow it. That's an
honest limitation of a prompt-only change, not something a unit test can
close -- a real eval/manual read-through of live conversations is the only
way to confirm the model actually behaves this way in practice.
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

from messa import config  # noqa: E402
from messa.agents import registry  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def part1_minimal_questions_paragraph_present():
    user = config.UserContext(user_id=1, phone_number="+15551234567", channel="sms")
    prompt = registry._build_system_prompt(user)

    check("the orchestrator prompt has the new 'Questions before acting' paragraph",
          "Questions before acting" in prompt)
    para = prompt.split("Questions before acting")[1].split("\n\n")[0]

    check("it tells Messa to ask only what it genuinely can't proceed without",
          "genuinely can't proceed without" in para)
    check("it explicitly forbids a checklist / asking more than one or two things at once",
          "never a" in para and "checklist" in para)
    check("it tells Messa to bundle multiple real questions into ONE message rather than "
          "drip-feeding them one at a time",
          "single message" in para and "one at a time" in para)
    check("it tells Messa to default to a sensible guess and state the assumption, rather "
          "than asking, when a wrong guess is cheap to fix",
          "use the default" in para or "sensible default" in para)
    check("it names concrete cases where asking first IS warranted -- costly/hard-to-undo "
          "actions or genuine task ambiguity",
          "expensive or hard to undo" in para and "ambiguous" in para)


def main() -> None:
    part1_minimal_questions_paragraph_present()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
