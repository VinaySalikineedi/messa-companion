import os
import asyncio
import traceback
from urllib.parse import urlparse
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool, StructuredTool
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.errors import GraphRecursionError
from deepagents import create_deep_agent

load_dotenv()

# ---- CONFIG ----
STRICT_FINISH = True
MAX_STRICT_RETRIES = 3
RECURSION_LIMIT = 100
ALLOWED_DOMAINS = []
DESTRUCTIVE_TOOLS = {
    "browser_press_key",
    "browser_drag", "browser_file_upload", "browser_evaluate",
    "browser_run_code_unsafe", "browser_handle_dialog",
} # Extra: "browser_click", "browser_type", "browser_fill_form", 
MAX_CONSECUTIVE_ERRORS = 3   # triggers forced replan
SNAPSHOT_DEPENDENT_TOOLS = {"browser_click", "browser_type", "browser_hover", "browser_drag", "browser_select_option"}
# ----------------

model = ChatOpenAI(
    model="~deepseek/deepseek-v4-flash-latest",
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)

# Cheap/fast model for self-critique — swap to whatever's cheapest on OpenRouter
critic_model = ChatOpenAI(
    model="~deepseek/deepseek-v4-flash-latest",
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)

TASK_DONE = {"done": False, "summary": None}

# ---- Runtime state for autonomy features ----
STATE = {
    "consecutive_errors": 0,
    "snapshot_fresh": False,   # True only right after a browser_snapshot, until next navigate/click
}


@tool
def finish_task(summary: str) -> str:
    """Call this ONLY when the entire user goal has been fully completed and verified.
    Provide a clear summary of what was found/done. This ends the run."""
    TASK_DONE["done"] = True
    TASK_DONE["summary"] = summary
    return "Task marked complete."


def domain_allowed(url: str) -> bool:
    if not ALLOWED_DOMAINS:
        return True
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def guard_tool(original_tool):
    name = original_tool.name

    async def guarded(*args, **kwargs):
        try:
            if name == "browser_navigate":
                url = kwargs.get("url") or (args[0] if args else None)
                if url and not domain_allowed(url):
                    return f"BLOCKED: '{url}' is not in the allowed domain list."
                STATE["snapshot_fresh"] = False  # page changed, old refs invalid

            if name == "browser_snapshot":
                STATE["snapshot_fresh"] = True

            if name in SNAPSHOT_DEPENDENT_TOOLS and not STATE["snapshot_fresh"]:
                return (
                    "ERROR: You must call browser_snapshot before using element refs. "
                    "The page may have changed since your last snapshot. Take a fresh "
                    "snapshot now, then retry this action with a valid ref."
                )

            if name in DESTRUCTIVE_TOOLS:
                print(f"\n[CONFIRM] Agent wants to run '{name}' with args: {kwargs or args}")
                resp = input("Allow this action? (y/n): ").strip().lower()
                if resp != "y":
                    return f"BLOCKED: user declined to run '{name}'."
                STATE["snapshot_fresh"] = False  # clicking/typing likely changes page state

            result = await original_tool.coroutine(*args, **kwargs)
            STATE["consecutive_errors"] = 0
            return result

        except Exception as e:
            STATE["consecutive_errors"] += 1
            print(f"[tool error] {name} failed: {e}")
            return (
                f"ERROR running '{name}': {str(e)}. Do not retry with the exact same "
                f"arguments. Take a fresh browser_snapshot, then try a different approach."
            )

    return StructuredTool.from_function(
        name=original_tool.name,
        description=original_tool.description,
        args_schema=original_tool.args_schema,
        coroutine=guarded,
    )


async def run_stream(agent, messages):
    final_messages = list(messages)
    async for chunk in agent.astream(
        {"messages": messages},
        config={"recursion_limit": RECURSION_LIMIT},
        stream_mode="updates",
    ):
        for node, update in chunk.items():
            if not update:
                continue
            msgs = update.get("messages", [])
            for m in msgs:
                content = getattr(m, "content", "")
                tool_calls = getattr(m, "tool_calls", None)
                if tool_calls:
                    for tc in tool_calls:
                        print(f"  -> calling {tc['name']}({tc['args']})")
                if content and m.type == "ai":
                    print(f"\nAgent: {content}\n")
                final_messages.append(m)

        # Failure-triggered replanning: interrupt the stream logic by flagging state;
        # actual nudge is injected in agentic_turn between runs.
        if STATE["consecutive_errors"] >= MAX_CONSECUTIVE_ERRORS:
            break

    return final_messages


async def self_critique(original_goal: str, summary: str) -> tuple[bool, str]:
    """Independent check: does the summary actually satisfy the original goal?"""
    critique_prompt = (
        "You are a strict QA reviewer. Given a user's original goal and an agent's claimed "
        "summary of completing it, decide if the goal was ACTUALLY fully satisfied.\n\n"
        f"Original goal: {original_goal}\n\n"
        f"Agent's summary: {summary}\n\n"
        "Respond in exactly this format:\n"
        "VERDICT: PASS or FAIL\n"
        "REASON: one sentence"
    )
    resp = await critic_model.ainvoke([HumanMessage(content=critique_prompt)])
    text = resp.content
    passed = "VERDICT: PASS" in text.upper() or "VERDICT:PASS" in text.upper()
    return passed, text


async def agentic_turn(agent, messages, original_goal):
    TASK_DONE["done"] = False
    TASK_DONE["summary"] = None
    STATE["consecutive_errors"] = 0
    STATE["snapshot_fresh"] = False
    attempt = 0

    while True:
        attempt += 1
        try:
            messages = await run_stream(agent, messages)
        except GraphRecursionError:
            print("\n[warn] Hit recursion limit before finishing. Browser state preserved.\n")
            return messages, None

        # Failure loop detected -> force replan instead of blind retry
        if STATE["consecutive_errors"] >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n[autonomy] {STATE['consecutive_errors']} consecutive tool errors — forcing replan.\n")
            STATE["consecutive_errors"] = 0
            messages.append(HumanMessage(
                content=(
                    "You've had repeated tool failures in a row. Stop retrying the same "
                    "approach. Update your plan/todo list with a genuinely different strategy "
                    "for this step, then continue."
                )
            ))
            if attempt >= MAX_STRICT_RETRIES + 2:
                print("[warn] too many replan cycles, giving up this turn.")
                return messages, None
            continue

        if TASK_DONE["done"]:
            # Self-critique gate before accepting completion
            passed, critique_text = await self_critique(original_goal, TASK_DONE["summary"])
            print(f"\n[self-critique] {critique_text}\n")
            if passed:
                return messages, TASK_DONE["summary"]
            else:
                TASK_DONE["done"] = False
                messages.append(HumanMessage(
                    content=(
                        f"Your completion was reviewed and REJECTED: {critique_text}\n"
                        "Do not call finish_task again until this gap is actually addressed."
                    )
                ))
                if attempt >= MAX_STRICT_RETRIES + 2:
                    print("[warn] self-critique kept failing, giving up this turn.")
                    return messages, TASK_DONE["summary"]
                continue

        if not STRICT_FINISH:
            return messages, None

        if attempt >= MAX_STRICT_RETRIES:
            print("[warn] max retries reached without finish_task")
            return messages, None

        messages.append(HumanMessage(
            content="You did not call finish_task. If the goal is fully done, "
                    "call finish_task now with your summary. If not, continue working."
        ))


async def main():
    client = MultiServerMCPClient({
        "playwright": {
            "command": "npx",
            "args": ["@playwright/mcp@latest"],
            "transport": "stdio",
        }
    })

    print("Starting browser session...")
    async with client.session("playwright") as session:
        raw_tools = await load_mcp_tools(session)
        guarded_tools = [guard_tool(t) for t in raw_tools]
        print(f"Loaded {len(guarded_tools)} guarded browser tools")

        base_prompt = (
            "You are a goal-oriented browser automation agent.\n"
            "- Break the goal into steps using your planning/todo tools.\n"
            "- After navigating, always call browser_snapshot to read actual page content.\n"
            "- Only use element refs from the MOST RECENT snapshot.\n"
            "- Base every factual claim strictly on text that literally appears in snapshots.\n"
            "- If a tool result starts with 'BLOCKED:' or 'ERROR', do not retry the exact same "
            "action; take a fresh snapshot or try a different approach.\n"
            "- Your finish_task summary will be independently reviewed against the original "
            "goal — be honest and complete, don't claim success prematurely.\n"
        )
        if STRICT_FINISH:
            base_prompt += "- You MUST call finish_task with a summary as your final action.\n"

        agent = create_deep_agent(
            model=model,
            tools=guarded_tools + [finish_task],
            system_prompt=base_prompt,
        )

        session_messages = []
        print("\n=== Browser Agent CLI ===")
        print("Type your goal, or 'exit' to quit.\n")

        try:
            while True:
                user_input = input("You: ").strip()
                if user_input.lower() in ("exit", "quit"):
                    break
                if not user_input:
                    continue

                session_messages.append({"role": "user", "content": user_input})
                session_messages, summary = await agentic_turn(agent, session_messages, user_input)
        except Exception:
            print("!!! EXCEPTION !!!")
            traceback.print_exc()
        finally:
            session_messages.clear()
            print("\nSession memory cleared. Goodbye.")

if __name__ == "__main__":
    asyncio.run(main())