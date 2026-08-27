"""Pretty CLI tracing for agent thoughts, delegations, and tool calls.

Design note: instead of trying to parse LangGraph's nested subgraph stream
events to see *inside* a subagent's execution (fragile -- the exact
namespace format for `task`-tool-invoked subgraphs isn't part of any public
contract), every tool in this project is wrapped with `trace_tool()` at
construction time and stamped with the name of the subagent that owns it.
That wrapper prints synchronously as each tool actually runs, so nested
tool calls show up in real time regardless of how deep they are in the
agent graph. The top-level stream is only responsible for printing the
orchestrator's own thoughts and its `task(...)` delegation calls.
"""
from __future__ import annotations

import json
from typing import Any

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
COLORS = {
    "messa": "\033[95m",       # magenta
    "browser_agent": "\033[94m",       # blue
    "executive_assistant": "\033[92m", # green
    "email_agent": "\033[93m",         # yellow
    "document_agent": "\033[96m",      # cyan
    "routines_agent": "\033[91m",      # red
    "tool": "\033[90m",                # grey
    "system": "\033[90m",
}


def _color(label: str) -> str:
    return COLORS.get(label, "\033[37m")


def _short(value: Any, limit: int = 220) -> str:
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, default=str)
        except Exception:
            value = str(value)
    text = str(value)
    text = " ".join(text.split())  # collapse newlines/whitespace
    return text if len(text) <= limit else text[: limit - 1] + "…"


def agent_say(label: str, text: str) -> None:
    """An agent's own message content (thoughts / final answer)."""
    if not text:
        return
    c = _color(label)
    print(f"\n{c}{BOLD}{label}{RESET}{c}:{RESET} {text}\n")


def delegation(from_label: str, subagent_type: str, description: str) -> None:
    c = _color(from_label)
    print(
        f"{c}{from_label} -> delegating to {BOLD}{subagent_type}{RESET}{c}:{RESET} "
        f"{DIM}{_short(description, 160)}{RESET}"
    )


def tool_call(label: str, name: str, args: dict[str, Any]) -> None:
    c = _color(label)
    print(f"  {c}[{label}]{RESET} {DIM}calling{RESET} {BOLD}{name}{RESET}({_short(args)})")


def tool_result(label: str, name: str, result: Any) -> None:
    c = _color(label)
    print(f"  {c}[{label}]{RESET} {DIM}<- {name} returned:{RESET} {_short(result)}")


def tool_error(label: str, name: str, error: str) -> None:
    print(f"  \033[91m[{label}] {name} failed:\033[0m {_short(error)}")


def proactive(text: str) -> None:
    print(f"\n\033[95m{BOLD}Messa (proactive){RESET}\033[95m:{RESET} {text}\n")


def system(text: str) -> None:
    print(f"{DIM}[system] {text}{RESET}")
