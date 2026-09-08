# Future Ideas & Architecture Backlog

This document captures high-value features, architectural improvements, and smart agent capabilities identified during development to be implemented in future milestones.

---

## 1. In-Flight Instruction Queue (Mid-Way Task Modification)

### The Problem
Currently, each inbound SMS triggers an independent `cli.run_message()` that invokes an isolated `agent.ainvoke()` loop. While a long-running task is executing (such as a 2-3 minute Stagehand browser navigation, deepsearch research, or complex document drafting), any follow-up text sent by the user runs in a parallel turn. The second turn has read-only awareness that a turn is running, but cannot inject new instructions into the running agent loop.

### The Solution: Mid-Flight Interrupt Queue
Enable the running agent loop to receive dynamic user instructions between tool executions without restarting the task from scratch.

### Architectural Blueprint
1. **Task Execution Registry & Queue**:
   - Each running turn or sub-agent task registers an async instruction queue in memory/Redis: `task_instruction_queue[task_id] = asyncio.Queue()`.
2. **Middleware / Tool-Step Hook**:
   - In the agent loop's middleware (or before each tool dispatch in `StagehandZeroDeltaMiddleware` / `ModelCallLimitMiddleware`), the executor checks:
     ```python
     while not queue.empty():
         update = queue.get_nowait()
         messages.append(SystemMessage(content=f"[Mid-flight update from user: {update}]"))
     ```
3. **Turn 2 Dispatcher**:
   - When a user texts mid-flight (e.g., *"Also make sure the flights are non-stop"* or *"Wait, change the date to Saturday"*), the inbound handler routes the instruction:
     ```python
     await inject_task_instruction(active_task_id, user_message)
     ```
   - Turn 2 confirms to the user with a quick tapback or short text (*"Got it, updating the search for non-stop only"*) and exits cleanly.
4. **Agent Pivot**:
   - On its very next step, the running agent reads the injected user directive and adjusts its plan in-place (e.g., clicking the "Non-stop" checkbox on the existing browser page) rather than canceling or restarting.

### Benefits
- **Zero Wasted Work**: Preserves expensive browser session state, avoiding duplicate page loads and LLM token burn.
- **Human-Like Responsiveness**: Delivers a truly proactive personal assistant experience where users can chime in and steer work mid-task.

---
