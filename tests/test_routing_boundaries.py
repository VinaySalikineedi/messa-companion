"""Regression checks for docs/orchestrator_routing_spec.md's fix: the
Google-Calendar-misrouted-to-reminders and Reddit-scraped-instead-of-
Composio problems it described.

Deliberately NOT solved the way the spec's Tier 1 proposed (bolting new
GOOGLECALENDAR_* tools directly onto executive_assistant, "matching how
email_agent handles both internal Messa email and Gmail") -- that
justification doesn't hold: email_agent and personal_inbox_agent are two
SEPARATE subagents for two separate inboxes (see registry.py's own module
docstring), not one agent handling both, so there's no existing precedent
to match. More importantly, executive_assistant's calendar
(calendar_events) is a purely internal, DB-backed table with no OAuth/sync
of any kind -- bolting Composio's real GOOGLECALENDAR_* actions onto it
would create two unconnected, overlapping "calendars" with no
reconciliation between them, which the spec didn't design for.

Fixed instead with prompt/routing-only changes, since integrations_agent
(with today's toolkit-scoped search fix) ALREADY handles any Composio
toolkit generically, Google Calendar included (confirmed as a real,
well-supported Composio toolkit, slug 'googlecalendar') -- no new tools,
no new migration, no duplicate calendar system. This file checks the
actual routing guidance text landed in the right places, since there's no
live-LLM way to test "the model routes correctly" without a real account.
"""
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

from messa import config  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import executive_tools, integration_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**overrides):
    defaults = dict(user_id=1, phone_number="+15551234567", channel="sms")
    defaults.update(overrides)
    return config.UserContext(**defaults)


def part1_orchestrator_prompt():
    prompt = registry._build_system_prompt(_user())

    check("subagent intro names Reddit as an integrations_agent example",
          "Reddit" in prompt.split("Email routing")[0])
    check("subagent intro names Google Calendar as an integrations_agent example",
          "Google Calendar" in prompt.split("Email routing")[0])
    check("subagent intro marks executive_assistant's calendar as internal, not a real connected one",
          "INTERNAL calendar" in prompt)

    check("routing paragraph tells Messa to delegate named apps/platforms to integrations_agent FIRST",
          "delegate to integrations_agent FIRST" in prompt)
    check("routing paragraph explicitly gives 'check Reddit' as a named-app example",
          "check Reddit" in prompt)

    # Post-compression (see plans/glowing-forging-pumpkin.md Part 3): email/
    # calendar/tasks routing collapsed from three separately-headed
    # paragraphs into one shared "Routing --" paragraph plus a compact
    # per-category bullet -- same rules, terser text, so this now checks
    # the calendar BULLET rather than a standalone "Calendar routing --"
    # header. The old separate cross-reference paragraph pointing back at
    # "Calendar routing" is gone entirely too -- it only existed to manage
    # duplication between two separate calendar-routing locations, and
    # there's only one now, so there's nothing left to cross-reference.
    check("the calendar routing bullet exists",
          "calendar: executive_assistant" in prompt)
    gcal_para = prompt.split("calendar: executive_assistant")[1].split("\n")[0]
    check("that bullet sends CONNECT/manage requests to integrations_agent, never executive_assistant",
          "goes to integrations_agent, never executive_assistant" in gcal_para)
    check("that bullet names the real Composio toolkit slug",
          "'googlecalendar'" in gcal_para)


def part2_executive_assistant_boundary():
    prompt = executive_tools._build_system_prompt(_user())
    check("executive_assistant's own prompt disclaims being a real connected calendar",
          "NOT a real, " in prompt and "connected Google/Apple/Outlook calendar" in prompt)
    check("it explicitly has no tool for connecting one",
          "no tool that connects to one" in prompt)
    check("it's told to say so plainly rather than faking it with an internal event",
          "say so plainly rather than creating an internal event" in prompt)
    check("it's told Messa routes that elsewhere (to integrations_agent)",
          "Messa routes that kind of request to integrations_agent" in prompt)

    subagent = executive_tools.build_executive_subagent(_user(), model=None)
    desc = subagent["description"]
    check("the subagent's own description (what Messa sees when choosing where to delegate) "
          "also carries the same internal-calendar disclaimer",
          "INTERNAL calendar" in desc and "integrations_agent" in desc)


def part3_integrations_agent_prompt():
    prompt = integration_tools.build_integration_system_prompt(_user())
    check("integrations_agent's own prompt names Reddit as a covered example",
          "Reddit" in prompt)
    check("integrations_agent's own prompt names Google Calendar as a covered example",
          "Google Calendar" in prompt)
    check("it clarifies Google Calendar IS handled here (unlike email, which never is)",
          "Google Calendar IS handled here" in prompt)
    check("it clarifies this is NOT the same thing as executive_assistant's internal calendar",
          "not the same thing as executive_assistant's own internal calendar_events" in prompt)


def main():
    part1_orchestrator_prompt()
    part2_executive_assistant_boundary()
    part3_integrations_agent_prompt()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    else:
        print("\nALL PASS")


if __name__ == "__main__":
    main()
