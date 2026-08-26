import os
import asyncio
import traceback
from urllib.parse import urlparse
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool, StructuredTool
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from deepagents import create_deep_agent
from langgraph.errors import GraphRecursionError

load_dotenv()

# ---- CONFIG ----
STRICT_FINISH = True
MAX_STRICT_RETRIES = 3
RECURSION_LIMIT = 100

# Empty list = allow all domains. Add entries to restrict, e.g. ["news.ycombinator.com", "example.com"]
ALLOWED_DOMAINS = []

# Tools that require manual y/n confirmation before running
DESTRUCTIVE_TOOLS = {
    "browser_fill_form", "browser_press_key",
    "browser_drag", "browser_file_upload", "browser_evaluate",
    "browser_run_code_unsafe", "browser_handle_dialog",
}
# ----------------"browser_click", "browser_type", 

model = ChatOpenAI(
    model="~deepseek/deepseek-v4-flash-latest",
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)

TASK_DONE = {"done": False, "summary": None}

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
    """Wrap an MCP tool with domain-allowlist, destructive-action confirmation,
    and error containment so tool failures never crash the CLI."""
    name = original_tool.name

    async def guarded(*args, **kwargs):
        try:
            # Domain allowlist check (navigate tool)
            if name == "browser_navigate":
                url = kwargs.get("url") or (args[0] if args else None)
                if url and not domain_allowed(url):
                    return f"BLOCKED: '{url}' is not in the allowed domain list. Do not attempt this navigation again."

            # Destructive action confirmation
            if name in DESTRUCTIVE_TOOLS:
                print(f"\n[CONFIRM] Agent wants to run '{name}' with args: {kwargs or args}")
                resp = input("Allow this action? (y/n): ").strip().lower()
                if resp != "y":
                    return f"BLOCKED: user declined to run '{name}'. Do not retry this exact action; try another approach or ask the user."

            return await original_tool.coroutine(*args, **kwargs)

        except Exception as e:
            # Never let a tool failure crash the session — feed it back to the model instead
            print(f"[tool error] {name} failed: {e}")
            return (
                f"ERROR running '{name}': {str(e)}. "
                f"This action failed — do not retry with the exact same arguments. "
                f"Take a fresh browser_snapshot to get valid element references, then try a different approach."
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
    return final_messages


async def agentic_turn(agent, messages):
    """Runs the agent until finish_task fires (strict) or it stops naturally (loose)."""
    TASK_DONE["done"] = False
    TASK_DONE["summary"] = None
    attempt = 0
    while True:
        attempt += 1
        try:
            messages = await run_stream(agent, messages)
        except GraphRecursionError:
            print("\n[warn] Hit recursion limit before finishing this task. "
                  "The browser state is preserved — you can ask it to continue.\n")
            return messages, None

        if TASK_DONE["done"]:
            return messages, TASK_DONE["summary"]

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
            "- Only use element refs (ref=...) from the MOST RECENT snapshot. Refs go stale "
            "after any navigation or click — take a new snapshot before interacting again.\n"
            "- You must base every factual claim strictly on text that literally appears "
            "in the browser_snapshot output. Never infer, guess, or use prior knowledge.\n"
            "- If a tool result starts with 'BLOCKED:' or 'ERROR', do not retry the exact same "
            "action; take a fresh snapshot or try a different approach.\n"
        )
        if STRICT_FINISH:
            base_prompt += "- You MUST call finish_task with a summary as your final action.\n"

        agent = create_deep_agent(
            model=model,
            tools=guarded_tools + [finish_task],
            system_prompt=base_prompt,
        )

        # In-memory session history only — never written to disk, gone on exit.
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
                session_messages, summary = await agentic_turn(agent, session_messages)
        except Exception:
            print("!!! EXCEPTION !!!")
            traceback.print_exc()
        finally:
            session_messages.clear()
            print("\nSession memory cleared. Goodbye.")

if __name__ == "__main__":
    asyncio.run(main())