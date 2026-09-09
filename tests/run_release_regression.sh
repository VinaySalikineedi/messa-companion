#!/usr/bin/env bash
# Runs the regression suite most relevant to a production push: everything
# touched or exercised by the onboarding/persona/guardrail/confetti release
# (see plans/glowing-forging-pumpkin.md) plus the email-persona tests it
# borders. Every file here is self-contained (portable sys.path, falls back
# to dummy credentials only when this repo has no real .env) -- run this
# from the repo root with whatever Python has this repo's requirements.txt
# installed (a real .env with DATABASE_URL/OPENROUTER_API_KEY is picked up
# automatically if present; nothing here requires one, except the live-LLM
# section of test_email_persona_rigorous.py, which just skips itself
# without a real key).
#
# Usage:
#   cd <repo root>
#   bash tests/run_release_regression.sh
#
# This is the FAKE-POOL/FAKE-MODEL regression suite -- fast, free, and safe
# to run as often as you like, but it can't tell you whether the real
# persona/formatting/onboarding flow actually feels right against a real
# LLM and a real database. For that, see scripts/live_smoke_test.py
# (requires a real DATABASE_URL + OPENROUTER_API_KEY, makes a handful of
# real billed LLM calls, and cleans up its own throwaway test user).
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

TESTS=(
  tests/test_privacy_page.py
  tests/test_onboarding_and_email_db.py
  tests/test_onboarding_and_deepsearch_msg.py
  tests/test_onboarding_email_provisioning.py
  tests/test_persona_prompt_and_guardrail.py
  tests/test_assistant_email_persona.py
  tests/test_email_persona_rigorous.py
  tests/test_app_preferences.py
  tests/test_routing_boundaries.py
  tests/test_disconnect_switch.py
  tests/test_default_email_provider.py
  tests/test_app_connect_queue.py
  tests/test_message_splitting.py
  tests/test_inbound_reactions.py
  tests/test_progressive_tapbacks.py
  tests/test_turn_control.py
  tests/test_pure_acknowledgment.py
  tests/test_double_texting.py
  tests/test_region_gate.py
  tests/test_minimal_questions_prompt.py
  tests/test_contact_sharing.py
  tests/test_fth_upgrades.py
  tests/test_fth_consent_and_form_js_live.py
  tests/test_scratchpad_and_skills.py
  tests/test_call_plans_usage.py
  tests/test_vapi_channel.py
  tests/test_call_control_and_activity.py
  tests/test_call_tools_security.py
  tests/test_call_tools_flow.py
  tests/test_call_webhook.py
)

PASSED=()
FAILED=()

for t in "${TESTS[@]}"; do
  echo ""
  echo "=================================================================="
  echo "RUNNING: $t"
  echo "=================================================================="
  if python3 "$t"; then
    PASSED+=("$t")
  else
    FAILED+=("$t")
  fi
done

echo ""
echo "=================================================================="
echo "SUMMARY: ${#PASSED[@]} passed, ${#FAILED[@]} failed"
for t in "${PASSED[@]}"; do echo "  [PASS] $t"; done
for t in "${FAILED[@]}"; do echo "  [FAIL] $t"; done
echo "=================================================================="

if [ "${#FAILED[@]}" -gt 0 ]; then
  exit 1
fi
