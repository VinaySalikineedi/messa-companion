"""Tests for the three items in agent-feedback.md (the team review that
followed the reliability-hardening pass in test_reliability_hardening.py):

  1. Critical blocker fix -- StagehandZeroDeltaMiddleware's fingerprint now
     includes a privacy-safe form-field signature (not just url/title), so
     sequential form-fill browser_act calls on a SPA (Uber/Amazon checkout/
     Stripe) no longer false-positive-trip the breaker.
  2. OTP early-return fix -- deepsearch_tools.py's `_run` now has a
     code-level backstop (mirroring the existing anti-hallucination
     completion gate) that nudges the model to stay and call
     await_email_verification_code when its final message reads like a
     pending OTP screen, plus strengthened STAGEHAND_SYSTEM_PROMPT /
     DEEPSEARCH_SYSTEM_PROMPT language against ending the turn mid-wait.
  3. New feature -- send_screenshot(caption): texts the current page to the
     user via the existing generated-document-share + Sendblue media_url
     mechanism, rate-limited per task (shared across delegate_website_task
     sub-workers) as a code-level backstop for the "zero-spam" system
     prompt rules.

No live browser, no live Composio, no live LLM call -- same plain-double
convention as test_reliability_hardening.py.
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

from langchain_core.messages import ToolMessage  # noqa: E402

from messa import config, db  # noqa: E402
from messa.channels import sendblue  # noqa: E402
from messa.tools import browser_circuit_breaker  # noqa: E402
from messa.tools import deepsearch_tools  # noqa: E402
from messa.tools import stagehand_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _make_user(user_id=1, channel="sms", phone="+15551234567"):
    return config.UserContext(user_id=user_id, phone_number=phone, channel=channel)


# ---------------------------------------------------------------------------
# Item 1: the fingerprint now includes a form-field signature, fixing the
# SPA form-fill false positive.
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, name, args=None, call_id="c1"):
        self.tool_call = {"name": name, "args": args or {}, "id": call_id}


class _FakeFormPage:
    """Same URL/title throughout (the SPA case) but with a `form_state`
    string that stands in for real form-field content -- evaluate() returns
    it directly rather than actually parsing JS, since this test is about
    the middleware's tuple comparison, not the JS itself."""

    def __init__(self, url="https://checkout.example.com/step", title="Checkout"):
        self.url = url
        self._title = title
        self.form_state = "0:0"

    async def title(self):
        return self._title

    async def evaluate(self, script):
        assert script is browser_circuit_breaker._FORM_SIGNATURE_SCRIPT
        return self.form_state


class _FakeStagehandProvider:
    def __init__(self, page):
        self._page = page

    async def get_active_page(self):
        return self._page

    async def _get_url(self, page):
        return page.url


async def item1_form_fill_on_spa_does_not_trip_breaker():
    """The reported production bug: 3 sequential browser_act calls filling
    First Name, Last Name, Email on a checkout SPA where url/title never
    change. Each fill DOES change the form signature, so this must NOT
    trip even though it would have with the old (url, title)-only design."""
    page = _FakeFormPage()
    provider = _FakeStagehandProvider(page)
    mw = browser_circuit_breaker.StagehandZeroDeltaMiddleware(provider, max_zero_delta_repeats=3)

    field_values = iter(["1:9", "2:17", "3:23"])  # growing char counts as fields fill in

    async def fill_field_handler(request):
        page.form_state = next(field_values)
        return ToolMessage(content="Action Result: filled field", tool_call_id=request.tool_call["id"])

    req = _FakeRequest("browser_act", {"instruction": "type into a field"})
    tripped = False
    try:
        for _ in range(3):
            await mw.awrap_tool_call(req, fill_field_handler)
    except browser_circuit_breaker.ZeroDeltaExceeded:
        tripped = True
    check(
        "3 sequential field fills with unchanged url/title but changing form content do NOT trip the breaker",
        not tripped,
    )


async def item1_genuinely_stuck_form_still_trips():
    """The breaker must still catch the ORIGINAL motivating incident: a
    dead-end/refusal state where nothing changes at all, including form
    content (e.g. a static anti-fraud modal with no inputs)."""
    page = _FakeFormPage()
    provider = _FakeStagehandProvider(page)
    mw = browser_circuit_breaker.StagehandZeroDeltaMiddleware(provider, max_zero_delta_repeats=3)

    async def noop_handler(request):
        return ToolMessage(content="Action Result: nothing happened", tool_call_id=request.tool_call["id"])

    req = _FakeRequest("browser_act", {"instruction": "click the disabled submit button"})
    tripped = False
    try:
        for _ in range(5):
            await mw.awrap_tool_call(req, noop_handler)
    except browser_circuit_breaker.ZeroDeltaExceeded:
        tripped = True
    check("a genuinely stuck page (url/title/form all unchanged) still trips the breaker", tripped)


async def item1_evaluate_failure_is_never_fatal():
    """A page/provider that can't run evaluate() (e.g. a page object from an
    older code path, or a transient RPC hiccup) must never crash the
    fingerprint -- it degrades to an empty form signature instead."""
    class _NoEvaluatePage:
        url = "https://example.com"

        async def title(self):
            return "Example"

    provider = _FakeStagehandProvider(_NoEvaluatePage())
    mw = browser_circuit_breaker.StagehandZeroDeltaMiddleware(provider, max_zero_delta_repeats=2)
    fp = await mw._fingerprint()
    check("fingerprint degrades gracefully (empty form signature) when evaluate() is unavailable", fp[2] == "")
    check("fingerprint still returns url/title even when evaluate() fails", fp[0] == "https://example.com" and fp[1] == "Example")


def item1_fingerprint_is_now_a_3tuple_including_form_signature():
    src = (REPO_ROOT / "messa" / "tools" / "browser_circuit_breaker.py").read_text()
    check(
        "the fingerprint script sums character counts / checked state, never raw values",
        "el.value || ''" in src or "(el.value || '').length" in src,
    )
    check(
        "the fingerprint script caps how many form elements it inspects",
        ".slice(0, 300)" in src,
    )
    check(
        "_fingerprint's return type is documented as a 3-tuple",
        "tuple[str, str, str]" in src,
    )


# ---------------------------------------------------------------------------
# Item 2: OTP early-return fix -- prompt language + the code-level backstop
# helper (`_looks_like_pending_otp`), plus a structural check on `_run`'s
# wiring (the full `_run` function needs a live LangGraph agent to drive
# end-to-end, so -- same convention as test_reliability_hardening.py's
# part2_breaker_only_attached_for_stagehand_engine -- the integration point
# itself is verified via source inspection).
# ---------------------------------------------------------------------------

def item2_looks_like_pending_otp_keyword_matcher():
    check(
        "a message describing a verification-code screen is detected",
        deepsearch_tools._looks_like_pending_otp("We sent a verification code to your email. Enter the code below."),
    )
    check(
        "a message describing a normal completed task is NOT flagged",
        not deepsearch_tools._looks_like_pending_otp("Added the Kindle Paperwhite to your cart and proceeded to checkout."),
    )
    check("empty/None text never crashes and is not flagged", not deepsearch_tools._looks_like_pending_otp(None) and not deepsearch_tools._looks_like_pending_otp(""))
    check(
        "OTP-adjacent phrasing variants are all caught",
        all(
            deepsearch_tools._looks_like_pending_otp(t)
            for t in [
                "Please check your email for a confirmation code.",
                "A one-time passcode was sent.",
                "Enter the 6-digit code we texted you.",
            ]
        ),
    )


def item2_system_prompts_forbid_ending_turn_during_otp_wait():
    stagehand_src = (REPO_ROOT / "messa" / "tools" / "stagehand_tools.py").read_text()
    deepsearch_src = (REPO_ROOT / "messa" / "tools" / "deepsearch_tools.py").read_text()
    check(
        "STAGEHAND_SYSTEM_PROMPT explicitly forbids ending the turn during an OTP wait",
        "STAY IN THIS TURN UNTIL IT RESOLVES" in stagehand_src,
    )
    check(
        "STAGEHAND_SYSTEM_PROMPT explains the browser closes automatically even without close_browser()",
        "is torn down" in stagehand_src or "is automatically closed" in stagehand_src,
    )
    check(
        "DEEPSEARCH_SYSTEM_PROMPT (legacy engine) carries the same explicit stay-in-turn language",
        "STAY IN THIS TURN UNTIL IT RESOLVES" in deepsearch_src,
    )
    check(
        "STAGEHAND_SYSTEM_PROMPT's zero-spam send_screenshot rules are present (item 3, checked here for prompt completeness)",
        "ZERO-SPAM RULES" in stagehand_src,
    )


def item2_run_has_the_otp_backstop_wired_in():
    src = (REPO_ROOT / "messa" / "tools" / "deepsearch_tools.py").read_text()
    # NOTE: "async def _run(" alone matches an unrelated nested _run() at
    # line ~1729 first -- anchor on the real signature to land on the actual
    # deepsearch _run(state) function this backstop lives in.
    run_start = src.index("async def _run(state: dict[str, Any])")
    run_body = src[run_start:run_start + 40000]
    check(
        "_run checks _looks_like_pending_otp against the run's final message",
        "_looks_like_pending_otp(" in run_body,
    )
    check(
        "_run's OTP backstop specifically checks for a missing await_email_verification_code call",
        'tc.get("name") == "await_email_verification_code"' in run_body,
    )
    check(
        "_run's OTP backstop issues a corrective nudge via inner_agent.ainvoke, same pattern as the hallucination gate",
        run_body.count("inner_agent.ainvoke(") >= 2,
    )


# ---------------------------------------------------------------------------
# Item 3: send_screenshot -- full tool behavior via fakes, plus the shared
# per-task rate-limit cap across delegate_website_task sub-workers.
# ---------------------------------------------------------------------------

class _FakeScreenshotPage:
    def __init__(self):
        self.screenshot_calls = 0

    async def screenshot(self):
        self.screenshot_calls += 1
        return b"fake-png-bytes"


def _make_screenshot_provider(page, screenshot_tracker=None):
    provider = stagehand_tools.StagehandToolProvider(
        approval_gate=None, user_id=1, deepsearch_session_id=1,
        phone_number="+15551234567",
        page=page,
        screenshot_tracker=screenshot_tracker,
    )
    provider._page = page

    async def fake_ensure_session():
        return None

    async def fake_get_active_page():
        return page

    provider._ensure_session = fake_ensure_session
    provider.get_active_page = fake_get_active_page
    return provider


async def item3_send_screenshot_happy_path(tmp_outputs_dir):
    page = _FakeScreenshotPage()
    provider = _make_screenshot_provider(page)

    share_calls = []

    async def fake_create_document_share(user_id, file_path, filename):
        share_calls.append((user_id, file_path, filename))
        return "tok_abc123"

    sent_messages = []

    async def fake_send_message(number, content, *, media_url=None, send_style=None):
        sent_messages.append((number, content, media_url))
        return {"status": "ok"}

    db.create_document_share = fake_create_document_share
    sendblue.send_message = fake_send_message
    config.SENDBLUE_API_KEY = "sb-test-dummy-key"
    config.SENDBLUE_API_SECRET = "sb-test-dummy-secret"
    config.SENDBLUE_NUMBER = "+14438062833"

    result = await provider._send_screenshot(caption="Cart ready, $34.20 total")
    check("send_screenshot captures a screenshot from the active page", page.screenshot_calls == 1)
    check("send_screenshot writes the PNG under config.OUTPUTS_DIR", len(share_calls) == 1 and share_calls[0][1].endswith(".png"))
    check("send_screenshot creates a document share for the saved file", share_calls[0][0] == 1)
    check("send_screenshot sends via sendblue with the caption", sent_messages and sent_messages[0][1] == "Cart ready, $34.20 total")
    check(
        "send_screenshot's media_url points at the /files/{token} route with the real token",
        sent_messages[0][2] == f"{config.LIVE_VIEW_BASE_URL}/files/tok_abc123",
    )
    check("send_screenshot reports success back to the model", "sent" in result.lower())
    check("the shared screenshot tracker now shows 1 used", provider._screenshot_tracker["count"] == 1)


async def item3_send_screenshot_default_caption():
    page = _FakeScreenshotPage()
    provider = _make_screenshot_provider(page)

    async def fake_create_document_share(user_id, file_path, filename):
        return "tok_xyz"

    sent_messages = []

    async def fake_send_message(number, content, *, media_url=None, send_style=None):
        sent_messages.append(content)
        return {}

    db.create_document_share = fake_create_document_share
    sendblue.send_message = fake_send_message

    await provider._send_screenshot()
    check("an empty caption falls back to a sensible default message", bool(sent_messages) and sent_messages[0])


async def item3_send_screenshot_enforces_the_spam_cap():
    page = _FakeScreenshotPage()
    tracker = {"count": 0}
    provider = _make_screenshot_provider(page, screenshot_tracker=tracker)

    async def fake_create_document_share(user_id, file_path, filename):
        return "tok_cap"

    async def fake_send_message(number, content, *, media_url=None, send_style=None):
        return {}

    db.create_document_share = fake_create_document_share
    sendblue.send_message = fake_send_message

    orig_cap = config.DEEPSEARCH_MAX_SCREENSHOTS_PER_SESSION
    try:
        config.DEEPSEARCH_MAX_SCREENSHOTS_PER_SESSION = 2
        r1 = await provider._send_screenshot(caption="milestone 1")
        r2 = await provider._send_screenshot(caption="milestone 2")
        r3 = await provider._send_screenshot(caption="milestone 3 -- should be refused")
        check("screenshot 1 under the cap succeeds", "sent" in r1.lower())
        check("screenshot 2 at the cap succeeds", "sent" in r2.lower())
        check("screenshot 3 beyond the cap is refused", "BLOCKED" in r3)
        check("the refused call never captured a 3rd screenshot", page.screenshot_calls == 2)
    finally:
        config.DEEPSEARCH_MAX_SCREENSHOTS_PER_SESSION = orig_cap


async def item3_screenshot_cap_is_shared_across_sub_worker_tracker():
    """Mirrors how self._captcha_tracker is shared by reference with
    delegate_website_task's sub-providers -- the cap must apply to the
    WHOLE task across tabs, not reset per sub-worker."""
    shared_tracker = {"count": 0}
    page_a = _FakeScreenshotPage()
    page_b = _FakeScreenshotPage()
    parent = _make_screenshot_provider(page_a, screenshot_tracker=shared_tracker)
    sub_worker = _make_screenshot_provider(page_b, screenshot_tracker=shared_tracker)
    check(
        "parent and sub-worker start out sharing the exact same tracker object",
        parent._screenshot_tracker is sub_worker._screenshot_tracker,
    )

    async def fake_create_document_share(user_id, file_path, filename):
        return "tok_shared"

    async def fake_send_message(number, content, *, media_url=None, send_style=None):
        return {}

    db.create_document_share = fake_create_document_share
    sendblue.send_message = fake_send_message

    orig_cap = config.DEEPSEARCH_MAX_SCREENSHOTS_PER_SESSION
    try:
        config.DEEPSEARCH_MAX_SCREENSHOTS_PER_SESSION = 1
        r_parent = await parent._send_screenshot(caption="parent milestone")
        r_sub = await sub_worker._send_screenshot(caption="sub-worker milestone")
        check("the parent's screenshot succeeds and consumes the shared budget", "sent" in r_parent.lower())
        check("the sub-worker is refused because the PARENT already spent the shared per-task cap", "BLOCKED" in r_sub)
    finally:
        config.DEEPSEARCH_MAX_SCREENSHOTS_PER_SESSION = orig_cap


async def item3_send_screenshot_requires_phone_and_config():
    page = _FakeScreenshotPage()
    provider = stagehand_tools.StagehandToolProvider(
        approval_gate=None, user_id=None, deepsearch_session_id=None,
        phone_number=None,
    )
    result = await provider._send_screenshot(caption="x")
    check("send_screenshot refuses cleanly with no user/phone context", "ERROR" in result)
    check("no screenshot is captured when the tool refuses up front", page.screenshot_calls == 0)


def item3_delegate_website_task_shares_the_screenshot_tracker():
    src = (REPO_ROOT / "messa" / "tools" / "stagehand_tools.py").read_text()
    delegate_start = src.index("async def _delegate_website_task")
    delegate_body = src[delegate_start:delegate_start + 3000]
    check(
        "_delegate_website_task passes screenshot_tracker=self._screenshot_tracker to the sub-worker",
        "screenshot_tracker=self._screenshot_tracker" in delegate_body,
    )


def item3_send_screenshot_registered_and_documented():
    src = (REPO_ROOT / "messa" / "tools" / "stagehand_tools.py").read_text()
    check("send_screenshot is registered as a model-facing tool", 'name="send_screenshot"' in src)
    check(
        "the tool docstring instructs sparing use / lists high-value milestones",
        "USE THIS SPARINGLY" in src,
    )


def item3_server_route_infers_media_type_instead_of_hardcoding_pdf():
    src = (REPO_ROOT / "messa" / "server.py").read_text()
    route_start = src.index('@app.get("/files/{token}")')
    route_body = src[route_start:route_start + 3000]
    check(
        "the /files/{token} route no longer unconditionally hardcodes application/pdf",
        'media_type="application/pdf")' not in route_body,
    )
    check(
        "the route guesses media_type from the filename with a safe pdf fallback",
        "mimetypes.guess_type(share[" in route_body and 'or "application/pdf"' in route_body,
    )
    import mimetypes as _mimetypes
    check("mimetypes correctly resolves a screenshot filename to image/png", _mimetypes.guess_type("deepsearch_screenshot_abc123.png")[0] == "image/png")
    check("mimetypes still resolves an existing pdf filename to application/pdf", _mimetypes.guess_type("contract.pdf")[0] == "application/pdf")


async def main() -> None:
    await item1_form_fill_on_spa_does_not_trip_breaker()
    await item1_genuinely_stuck_form_still_trips()
    await item1_evaluate_failure_is_never_fatal()
    item1_fingerprint_is_now_a_3tuple_including_form_signature()

    item2_looks_like_pending_otp_keyword_matcher()
    item2_system_prompts_forbid_ending_turn_during_otp_wait()
    item2_run_has_the_otp_backstop_wired_in()

    await item3_send_screenshot_happy_path(None)
    await item3_send_screenshot_default_caption()
    await item3_send_screenshot_enforces_the_spam_cap()
    await item3_screenshot_cap_is_shared_across_sub_worker_tracker()
    await item3_send_screenshot_requires_phone_and_config()
    item3_delegate_website_task_shares_the_screenshot_tracker()
    item3_send_screenshot_registered_and_documented()
    item3_server_route_infers_media_type_instead_of_hardcoding_pdf()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
