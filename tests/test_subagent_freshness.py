"""Tests for the feature/agentic-upgrade "assets vanishing mid-task" fix:
converting personal_inbox_agent, document_agent, routines_agent,
integrations_agent, and admin_agent from plain declarative
{"tools": ..., "system_prompt": ...} SubAgent dicts (whose prompt was
frozen ONCE, at build_orchestrator's start, before the turn's own tool
calls run) into CompiledSubAgents that build their tools/prompt fresh
inside their own `_run` closure, at actual delegation time -- the same
shape email_agent/executive_assistant/deepsearch already used.

The concrete bug this closes: if integrations_agent creates a spreadsheet
and saves its id via update_task_scratchpad mid-turn, and Messa then
delegates to document_agent (or any of the other four) LATER IN THE SAME
TURN, that second delegation's frozen prompt used to predate the write --
the id was invisible to it. See
tools/integration_tools.py's build_integration_subagent docstring for the
full "why."

This file proves the ACTUAL regression the fix targets, not just that the
dict shape changed: `db.get_active_task` is monkeypatched to return a
DIFFERENT active task on each successive call (simulating "an earlier
delegation in this same turn just wrote something new"), and each
subagent's own `_run` is invoked twice in a row -- if it only fetched the
scratchpad block once (closed over a stale value), both invocations would
see the SAME artifacts; the fix means the second invocation sees the NEW
ones.

No live Postgres, no live LLM call, no live Composio call -- `create_agent`
is monkeypatched per module (same technique
test_scratchpad_and_skills.py's own part10_compiled_subagents_live uses)
to capture its kwargs instead of running a real model.
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
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from messa import config, db  # noqa: E402
from messa.tools import admin_tools, document_tools, integration_tools, personal_inbox_tools, routines_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


USER = config.UserContext(
    user_id=1, phone_number="+15551234567", name="Test User", is_admin=True,
    timezone="America/New_York", onboarding_step="complete",
)


class FakeInnerAgent:
    async def ainvoke(self, *a, **kw):
        return {"messages": [AIMessage(content="ok")]}


async def _assert_returns_runnable_not_dict(build_fn, *extra_args, label):
    sub = build_fn(USER, object(), *extra_args)
    check(f"{label}: has a 'runnable' key (CompiledSubAgent shape)", "runnable" in sub)
    check(f"{label}: has NO bare 'tools' key (would mean a frozen dict shape)", "tools" not in sub)
    check(f"{label}: has NO bare 'system_prompt' key (would mean a frozen dict shape)", "system_prompt" not in sub)


async def _assert_fetches_scratchpad_fresh(mod, build_fn_name, *extra_args, label, needle_a="ARTIFACT_A", needle_b="ARTIFACT_B"):
    """The actual regression test: two separate _run invocations of the SAME
    subagent-builder call must each see whatever get_active_task returns at
    THAT moment, not a value captured once and reused."""
    calls = {"n": 0}

    async def fake_get_active_task(uid):
        calls["n"] += 1
        marker = needle_a if calls["n"] == 1 else needle_b
        return {"task_id": f"t{calls['n']}", "task_type": "test_task", "artifacts": {"marker": marker}}

    db.get_active_task = fake_get_active_task

    captured: list[dict] = []
    orig_create_agent = mod.create_agent

    def fake_create_agent(**kwargs):
        captured.append(kwargs)
        return FakeInnerAgent()

    mod.create_agent = fake_create_agent
    try:
        build_fn = getattr(mod, build_fn_name)
        sub = build_fn(USER, object(), *extra_args)
        await sub["runnable"].ainvoke({"messages": [HumanMessage(content="first delegation")]})
        await sub["runnable"].ainvoke({"messages": [HumanMessage(content="second delegation, same turn")]})

        check(f"{label}: two delegations produced two separate inner create_agent calls", len(captured) == 2)
        if len(captured) == 2:
            check(
                f"{label}: 1st delegation's prompt reflects the artifacts AT THAT TIME ({needle_a})",
                needle_a in captured[0]["system_prompt"],
            )
            check(
                f"{label}: 2nd delegation's prompt reflects the NEW artifacts written since "
                f"the 1st delegation ({needle_b}) -- this is the actual mid-task staleness bug fixed",
                needle_b in captured[1]["system_prompt"],
            )
            check(
                f"{label}: 2nd delegation's prompt does NOT still show the 1st delegation's stale artifact",
                needle_a not in captured[1]["system_prompt"],
            )
    finally:
        mod.create_agent = orig_create_agent


async def main() -> None:
    await _assert_returns_runnable_not_dict(
        integration_tools.build_integration_subagent, "some description", None, label="integrations_agent"
    )
    await _assert_returns_runnable_not_dict(
        personal_inbox_tools.build_personal_inbox_subagent, None, label="personal_inbox_agent"
    )
    await _assert_returns_runnable_not_dict(document_tools.build_document_subagent, label="document_agent")
    await _assert_returns_runnable_not_dict(routines_tools.build_routines_subagent, label="routines_agent")
    await _assert_returns_runnable_not_dict(admin_tools.build_admin_subagent, label="admin_agent")

    await _assert_fetches_scratchpad_fresh(
        integration_tools, "build_integration_subagent", "some description", None, label="integrations_agent"
    )
    await _assert_fetches_scratchpad_fresh(
        personal_inbox_tools, "build_personal_inbox_subagent", None, label="personal_inbox_agent"
    )
    await _assert_fetches_scratchpad_fresh(document_tools, "build_document_subagent", label="document_agent")
    await _assert_fetches_scratchpad_fresh(routines_tools, "build_routines_subagent", label="routines_agent")
    await _assert_fetches_scratchpad_fresh(admin_tools, "build_admin_subagent", label="admin_agent")

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
