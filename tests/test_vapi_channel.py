"""Tests for PR2 of the voice-calling feature (plans/glowing-forging-pumpkin.md):
messa/channels/vapi.py, the Vapi AI provider client, plus its new
config.py env vars. Nothing wires this into a real tool/route yet (that's
PR3-5) -- this is a pure client-layer test.

Same approach as tests/test_contact_sharing.py: real outbound network
access to api.vapi.ai doesn't exist in this environment (no Vapi account
exists at all yet, per the plan), so these verify request-shaping against
a fake httpx.AsyncClient that records every call instead of making one --
right HTTP method, right URL, right Bearer auth header, and the documented
payload shape -- plus the credential-missing and non-2xx error paths.

Five parts:
  1. _require_credentials -- names exactly which env var(s) are missing.
  2. create_call -- POST /call with phoneNumberId/customer.number/
     assistant/metadata, and that maxDurationSeconds gets folded into the
     assistant config (not left off, not overriding a value the caller
     already set as anything other than what's passed in).
  3. get_call / end_call -- GET/PATCH the right per-call URL.
  4. verify_webhook_request -- unset secret = unverified pass-through
     (matches Sendblue's own posture); a set secret requires an exact
     header match, case-insensitively on the header name.
  5. Non-2xx response raises VapiError with the endpoint/status visible in
     the message (same as sendblue.py's own SendblueError shape).
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
    # vapi.py's _require_credentials checks config.VAPI_* at call time, but
    # config.py itself reads plain os.environ.get with no dummy default
    # (VAPI is meant to be genuinely absent until a real account exists) --
    # so tests that need "configured" behavior set these explicitly per
    # test via monkeypatching config.VAPI_*, not via env vars here.

import httpx  # noqa: E402

from messa import config  # noqa: E402
from messa.channels import vapi  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text or ""

    def json(self):
        return self._json_body


class FakeAsyncClient:
    instances = []

    def __init__(self, *a, **kw):
        self.calls = []
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return FakeAsyncClient.response_to_return

    async def get(self, url, *, headers=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "json": None})
        return FakeAsyncClient.response_to_return

    async def patch(self, url, *, headers=None, json=None):
        self.calls.append({"method": "PATCH", "url": url, "headers": headers, "json": json})
        return FakeAsyncClient.response_to_return


def install_fake_client(response: FakeResponse):
    FakeAsyncClient.instances = []
    FakeAsyncClient.response_to_return = response
    httpx.AsyncClient = FakeAsyncClient


def set_credentials(api_key="vapi-test-key", phone_number_id="phone-123"):
    config.VAPI_API_KEY = api_key
    config.VAPI_PHONE_NUMBER_ID = phone_number_id


def clear_credentials():
    config.VAPI_API_KEY = None
    config.VAPI_PHONE_NUMBER_ID = None


# ---------------------------------------------------------------------------
# Part 1: _require_credentials
# ---------------------------------------------------------------------------

async def part1_require_credentials():
    real_key, real_phone = config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID
    try:
        clear_credentials()
        try:
            await vapi.create_call(
                "+15551234567", assistant={}, metadata={}, max_duration_seconds=600,
            )
            check("create_call with no credentials raises VapiError", False)
        except vapi.VapiError as e:
            check("create_call with no credentials raises VapiError", True)
            check("the error names VAPI_API_KEY", "VAPI_API_KEY" in str(e))
            check("the error names VAPI_PHONE_NUMBER_ID", "VAPI_PHONE_NUMBER_ID" in str(e))

        config.VAPI_API_KEY = "only-the-key"
        config.VAPI_PHONE_NUMBER_ID = None
        try:
            await vapi.get_call("call-1")
            check("get_call with only VAPI_API_KEY set raises VapiError", False)
        except vapi.VapiError as e:
            check("get_call with only VAPI_API_KEY set raises VapiError", True)
            missing_clause = str(e).split("missing", 1)[1].split(" in .env", 1)[0]
            check("the error names the missing var (VAPI_PHONE_NUMBER_ID)",
                  "VAPI_PHONE_NUMBER_ID" in missing_clause)
            check("the error does not also name the var that IS set (VAPI_API_KEY)",
                  "VAPI_API_KEY" not in missing_clause)
    finally:
        config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID = real_key, real_phone


# ---------------------------------------------------------------------------
# Part 2: create_call
# ---------------------------------------------------------------------------

async def part2_create_call():
    real_client = httpx.AsyncClient
    real_key, real_phone = config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID
    try:
        set_credentials(api_key="vapi-secret", phone_number_id="phone-abc")
        install_fake_client(FakeResponse(201, {"id": "vapi-call-1", "status": "queued"}))

        assistant_cfg = {"model": {"provider": "openai"}, "voice": {"provider": "11labs"}}
        result = await vapi.create_call(
            "+15551234567",
            assistant=assistant_cfg,
            metadata={"call_session_id": "c1"},
            max_duration_seconds=300,
        )
        check("create_call returns the parsed response", result.get("id") == "vapi-call-1")

        call = FakeAsyncClient.instances[0].calls[0]
        check("create_call hits POST", call["method"] == "POST")
        check("create_call hits /call", call["url"] == f"{vapi.BASE_URL}/call")
        check("create_call sends a Bearer auth header with the configured key",
              call["headers"]["Authorization"] == "Bearer vapi-secret")
        body = call["json"]
        check("create_call's body uses the configured phoneNumberId", body["phoneNumberId"] == "phone-abc")
        check("create_call's body nests the destination under customer.number",
              body["customer"]["number"] == "+15551234567")
        check("create_call's body carries the metadata through untouched",
              body["metadata"] == {"call_session_id": "c1"})
        check("create_call folds maxDurationSeconds into the assistant config",
              body["assistant"]["maxDurationSeconds"] == 300)
        check("create_call does not drop the rest of the assistant config",
              body["assistant"]["model"] == {"provider": "openai"} and
              body["assistant"]["voice"] == {"provider": "11labs"})
        # The original dict passed in must not be mutated by folding in
        # maxDurationSeconds -- a caller reusing the same base config for
        # multiple calls (different remaining-minutes each time) would
        # otherwise leak state across calls.
        check("create_call does not mutate the caller's original assistant dict",
              "maxDurationSeconds" not in assistant_cfg)
    finally:
        httpx.AsyncClient = real_client
        config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID = real_key, real_phone


# ---------------------------------------------------------------------------
# Part 3: get_call / end_call
# ---------------------------------------------------------------------------

async def part3_get_and_end_call():
    real_client = httpx.AsyncClient
    real_key, real_phone = config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID
    try:
        set_credentials()
        install_fake_client(FakeResponse(200, {"id": "vapi-call-1", "status": "in-progress"}))
        result = await vapi.get_call("vapi-call-1")
        check("get_call returns the parsed response", result.get("status") == "in-progress")
        call = FakeAsyncClient.instances[0].calls[0]
        check("get_call hits GET", call["method"] == "GET")
        check("get_call hits /call/{id}", call["url"] == f"{vapi.BASE_URL}/call/vapi-call-1")

        install_fake_client(FakeResponse(200, {"id": "vapi-call-1", "status": "ended"}))
        result2 = await vapi.end_call("vapi-call-1")
        check("end_call returns the parsed response", result2.get("status") == "ended")
        call2 = FakeAsyncClient.instances[0].calls[0]
        check("end_call hits PATCH", call2["method"] == "PATCH")
        check("end_call hits /call/{id}", call2["url"] == f"{vapi.BASE_URL}/call/vapi-call-1")
    finally:
        httpx.AsyncClient = real_client
        config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID = real_key, real_phone


# ---------------------------------------------------------------------------
# Part 4: verify_webhook_request
# ---------------------------------------------------------------------------

def part4_verify_webhook_request():
    real_secret = config.VAPI_WEBHOOK_SECRET
    try:
        config.VAPI_WEBHOOK_SECRET = None
        check("no secret configured: any request is accepted (unverified)",
              vapi.verify_webhook_request({}, b"{}") is True)

        config.VAPI_WEBHOOK_SECRET = "shh-secret"
        check("secret configured, header missing: rejected",
              vapi.verify_webhook_request({}, b"{}") is False)
        check("secret configured, header present but wrong: rejected",
              vapi.verify_webhook_request({"x-vapi-secret": "wrong"}, b"{}") is False)
        check("secret configured, header present and correct (lowercase key): accepted",
              vapi.verify_webhook_request({"x-vapi-secret": "shh-secret"}, b"{}") is True)
        check("secret configured, header present and correct (capitalized key): accepted",
              vapi.verify_webhook_request({"X-Vapi-Secret": "shh-secret"}, b"{}") is True)
    finally:
        config.VAPI_WEBHOOK_SECRET = real_secret


# ---------------------------------------------------------------------------
# Part 5: non-2xx raises VapiError
# ---------------------------------------------------------------------------

async def part5_non_2xx_raises():
    real_client = httpx.AsyncClient
    real_key, real_phone = config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID
    try:
        set_credentials()
        install_fake_client(FakeResponse(500, {}, text="internal error"))
        try:
            await vapi.get_call("vapi-call-1")
            check("a 500 response raises VapiError", False)
        except vapi.VapiError as e:
            check("a 500 response raises VapiError", True)
            check("the error message includes the status code", "500" in str(e))
            check("the error message includes the endpoint", "call/vapi-call-1" in str(e))
    finally:
        httpx.AsyncClient = real_client
        config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID = real_key, real_phone


async def main() -> None:
    await part1_require_credentials()
    await part2_create_call()
    await part3_get_and_end_call()
    part4_verify_webhook_request()
    await part5_non_2xx_raises()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
