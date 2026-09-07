"""Tests for channels/sendblue.py's new Contact Sharing (name/photo) wrapper
functions -- set_contact_profile / get_contact_profile_state /
share_contact_profile / delete_contact_profile -- and the /contact-photo.png
static-file route in server.py.

This project's outbound network access in the environment these were
written in can't actually reach api.sendblue.com (see scripts/publish_
contact_profile.py's own docstring), so these tests can't be a real
end-to-end check the way, say, a live smoke test would be. What they DO
verify, against a fake httpx.AsyncClient that records every call instead of
making one: each function hits the right HTTP method, the right
api.sendblue.com/api/v2/... URL (a different domain/version prefix than
every other Sendblue endpoint in this file -- see sendblue.py's own
comment), the right auth headers, and builds the exact payload shape
documented at https://docs.sendblue.com/api-v2/contact-sharing/ -- plus the
documented edge cases (photo_url + clear_photo together is rejected before
any network call at all, a non-2xx response raises SendblueError).
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
    # sendblue.py's _require_credentials checks these at call time (not just
    # import time), but config.py reads them from the environment once at
    # import -- so a dummy value must be set before `from messa import
    # config` below, same as every other dummy fallback in this block.
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

import httpx  # noqa: E402

from messa import config  # noqa: E402
from messa.channels import sendblue  # noqa: E402

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
    """Records every .request(...) call instead of making one -- shared by
    every test below via install_fake_client()."""
    instances = []

    def __init__(self, *a, **kw):
        self.calls = []
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, *, headers=None, params=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json})
        return FakeAsyncClient.response_to_return

    async def post(self, url, *, headers=None, json=None):
        # send_message/send_reaction/etc. use client.post directly, not
        # client.request -- captured the same way so existing endpoints
        # aren't accidentally broken by anything shared with the new code.
        self.calls.append({"method": "POST", "url": url, "headers": headers, "params": None, "json": json})
        return FakeAsyncClient.response_to_return


def install_fake_client(response: FakeResponse):
    FakeAsyncClient.instances = []
    FakeAsyncClient.response_to_return = response
    httpx.AsyncClient = FakeAsyncClient


async def part1_set_contact_profile():
    real_client = httpx.AsyncClient
    try:
        install_fake_client(FakeResponse(200, {
            "status": "OK",
            "data": {"hasProfile": True, "sharingEnabled": True, "firstName": "Messa",
                      "lastName": "AI", "displayName": "Messa AI", "hasPhoto": True},
        }))
        result = await sendblue.set_contact_profile(
            first_name="Messa", last_name="AI", photo_url="https://textmessa.com/contact-photo.png",
        )
        call = FakeAsyncClient.instances[0].calls[0]
        check("set_contact_profile hits POST", call["method"] == "POST")
        check("set_contact_profile hits the v2 contact-sharing/profile URL on api.sendblue.com",
              call["url"] == "https://api.sendblue.com/api/v2/contact-sharing/profile")
        check("payload includes fromNumber defaulting to config.SENDBLUE_NUMBER",
              call["json"]["fromNumber"] == config.SENDBLUE_NUMBER)
        check("payload includes firstName/lastName/photoUrl exactly as passed",
              call["json"]["firstName"] == "Messa" and call["json"]["lastName"] == "AI"
              and call["json"]["photoUrl"] == "https://textmessa.com/contact-photo.png")
        check("clearPhoto is NOT sent when a photo_url is given", "clearPhoto" not in call["json"])
        check("auth headers are the same sb-api-key-id/sb-api-secret-key pair every other endpoint uses",
              call["headers"]["sb-api-key-id"] == config.SENDBLUE_API_KEY
              and call["headers"]["sb-api-secret-key"] == config.SENDBLUE_API_SECRET)
        check("the real Sendblue response is returned back to the caller unchanged",
              result["data"]["displayName"] == "Messa AI")

        # Empty-string name clears that field -- must still be SENT (not
        # dropped), since Sendblue's docs say an empty string is how you
        # clear a name, and `if first_name is not None` must not treat ""
        # as falsy-and-skip.
        install_fake_client(FakeResponse(200, {"status": "OK", "data": {}}))
        await sendblue.set_contact_profile(first_name="", last_name=None)
        call2 = FakeAsyncClient.instances[0].calls[0]
        check("an empty-string first_name IS sent (clears it), not silently dropped",
              call2["json"].get("firstName") == "")
        check("a None last_name is NOT sent at all (vs an explicit clear)", "lastName" not in call2["json"])

        # clear_photo=True
        install_fake_client(FakeResponse(200, {"status": "OK", "data": {}}))
        await sendblue.set_contact_profile(clear_photo=True)
        call3 = FakeAsyncClient.instances[0].calls[0]
        check("clear_photo=True sends clearPhoto: true and no photoUrl",
              call3["json"].get("clearPhoto") is True and "photoUrl" not in call3["json"])

        # photo_url + clear_photo together -- rejected before any network call.
        try:
            await sendblue.set_contact_profile(photo_url="https://x.example/p.png", clear_photo=True)
            check("photo_url + clear_photo together raises ValueError", False)
        except ValueError:
            check("photo_url + clear_photo together raises ValueError", True)

        # Non-2xx response raises SendblueError.
        install_fake_client(FakeResponse(422, {}, text="unprocessable"))
        try:
            await sendblue.set_contact_profile(first_name="Messa")
            check("a non-2xx response raises SendblueError", False)
        except sendblue.SendblueError as e:
            check("a non-2xx response raises SendblueError", "422" in str(e))
    finally:
        httpx.AsyncClient = real_client


async def part2_get_contact_profile_state():
    real_client = httpx.AsyncClient
    try:
        install_fake_client(FakeResponse(200, {"status": "OK", "data": {"hasProfile": False}}))
        result = await sendblue.get_contact_profile_state()
        call = FakeAsyncClient.instances[0].calls[0]
        check("get_contact_profile_state hits GET", call["method"] == "GET")
        check("get_contact_profile_state hits the state URL", call["url"] == "https://api.sendblue.com/api/v2/contact-sharing/state")
        check("fromNumber is passed as a query param, not a JSON body (this is a GET)",
              call["params"] == {"fromNumber": config.SENDBLUE_NUMBER} and call["json"] is None)
        check("the real response comes back unchanged", result["data"]["hasProfile"] is False)
    finally:
        httpx.AsyncClient = real_client


async def part3_share_contact_profile():
    real_client = httpx.AsyncClient
    try:
        install_fake_client(FakeResponse(200, {"status": "OK", "requested": True, "deduplicated": False}))
        result = await sendblue.share_contact_profile("+15551234567")
        call = FakeAsyncClient.instances[0].calls[0]
        check("share_contact_profile hits POST", call["method"] == "POST")
        check("share_contact_profile hits the share URL", call["url"] == "https://api.sendblue.com/api/v2/contact-sharing/share")
        check("payload has both fromNumber (default) and the given toNumber",
              call["json"] == {"fromNumber": config.SENDBLUE_NUMBER, "toNumber": "+15551234567"})
        check("a dedup/requested response comes back to the caller as-is", result["requested"] is True)
    finally:
        httpx.AsyncClient = real_client


async def part4_delete_contact_profile():
    real_client = httpx.AsyncClient
    try:
        install_fake_client(FakeResponse(200, {"status": "OK"}))
        await sendblue.delete_contact_profile()
        call = FakeAsyncClient.instances[0].calls[0]
        check("delete_contact_profile hits DELETE", call["method"] == "DELETE")
        check("delete_contact_profile hits the profile URL (same URL as set, different verb)",
              call["url"] == "https://api.sendblue.com/api/v2/contact-sharing/profile")
        check("delete_contact_profile still sends fromNumber", call["json"] == {"fromNumber": config.SENDBLUE_NUMBER})
    finally:
        httpx.AsyncClient = real_client


async def part5_missing_credentials():
    real_key = config.SENDBLUE_API_KEY
    try:
        config.SENDBLUE_API_KEY = ""
        try:
            await sendblue.set_contact_profile(first_name="Messa")
            check("a missing SENDBLUE_API_KEY raises SendblueError before any network call", False)
        except sendblue.SendblueError as e:
            check("a missing SENDBLUE_API_KEY raises SendblueError before any network call",
                  "not configured" in str(e).lower())
    finally:
        config.SENDBLUE_API_KEY = real_key


def part6_contact_photo_asset_and_route_exist():
    photo_path = REPO_ROOT / "messa" / "assets" / "landing" / "contact-photo.png"
    check("messa/assets/landing/contact-photo.png exists in the repo", photo_path.is_file())
    server_src = (REPO_ROOT / "messa" / "server.py").read_text()
    check("server.py defines a GET /contact-photo.png route", '@app.get("/contact-photo.png")' in server_src)
    check("that route serves the contact-photo.png file specifically (not og-image.png by mistake)",
          "contact-photo.png" in server_src.split('@app.get("/contact-photo.png")')[1][:600])


async def main() -> None:
    await part1_set_contact_profile()
    await part2_get_contact_profile_state()
    await part3_share_contact_profile()
    await part4_delete_contact_profile()
    await part5_missing_credentials()
    part6_contact_photo_asset_and_route_exist()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
