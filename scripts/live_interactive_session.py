"""Live end-to-end conversation demonstration with the upgraded Messa.
Exercises the real orchestrator, real OpenRouter models, and real DB,
printing the complete back-and-forth dialogue and system telemetry.
"""

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import HumanMessage
from messa import cli, config, console, db
from messa.agents.registry import build_orchestrator
from messa.approval import AutoApproveGate


async def run_live_session():
    print("==================================================================")
    print("MESSA LIVE INTERACTIVE SESSION (UPGRADED AGENTIC ARCHITECTURE)")
    print("==================================================================")
    
    # Load user context
    user = await cli._load_user_context()
    print(f"[User Context] ID: {user.user_id} | Name: {user.name or 'User'} | Channel: {user.channel}")
    print(f"[Model Config] Orchestrator: {config.ORCHESTRATOR_MODEL_NAME}")
    print(f"[Model Config] Subagents: {config.SUBAGENT_MODEL_NAME}")
    print("------------------------------------------------------------------\n")

    # Use AutoApproveGate for read/safe operations during demo
    approval_gate = AutoApproveGate()
    
    # In-memory history for the session
    history = []

    dialogue_turns = [
        "Hey Messa! Good afternoon. What's your role, and how can you help me stay on top of things today?",
        "I'm prepping for an investor sync on Friday. Could you help me keep track of this task and note down that we need the deck finalized and financial metrics verified?",
        "What's the current status of that prep task in our notes?",
    ]

    for turn_idx, user_text in enumerate(dialogue_turns, 1):
        print(f"\n==================== TURN {turn_idx} ====================")
        print(f"USER: {user_text}\n")
        
        # Build orchestrator fresh each turn so system prompt reflects fresh context/scratchpad
        agent = await build_orchestrator(user, approval_gate)
        
        history.append(HumanMessage(content=user_text))
        
        # Run turn through the upgraded run_turn (with claim checks, ladder, and guardrails)
        history = await cli.run_turn(agent, history)
        
        # Get final response
        reply = cli.last_ai_text(history)
        print(f"\nMESSA: {reply}\n")
        print("--------------------------------------------------")

    await db.close_pool()
    print("\n==================================================================")
    print("LIVE SESSION COMPLETED SUCCESSFULLY")
    print("==================================================================")


if __name__ == "__main__":
    asyncio.run(run_live_session())
