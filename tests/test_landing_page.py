"""Tests for landing page phone number formatting, replacement, and asset serving."""
from starlette.testclient import TestClient
from messa import config
from messa.channels.resend import _signature_block
from messa.landing_page import _format_phone, render_landing_page
from messa.phone import format_phone_display
from messa.server import app

failures = []


def check(label: str, cond: bool):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def test_phone_number_and_landing_page():
    # 1. Verify default/active phone configuration
    check("config.SENDBLUE_NUMBER is set to +14438062833", config.SENDBLUE_NUMBER == "+14438062833")

    # 2. Verify display formatting
    display = format_phone_display("+14438062833")
    check("format_phone_display formats +14438062833 as +1 (443) 806 2833", display == "+1 (443) 806 2833")

    # 3. Verify _format_phone pair
    disp, href = _format_phone("+14438062833")
    check("_format_phone returns expected display", disp == "+1 (443) 806 2833")
    check("_format_phone returns expected sms href", href == "sms:+14438062833")

    # 4. Verify landing page HTML rendering
    html = render_landing_page()
    check("__PHONE_DISPLAY__ placeholder is replaced", "__PHONE_DISPLAY__" not in html)
    check("__SMS_HREF__ placeholder is replaced", "__SMS_HREF__" not in html)
    check("Formatted phone number appears in HTML", "+1 (443) 806 2833" in html)
    check("sms:+14438062833 href appears in HTML", "sms:+14438062833" in html)

    # 5. Verify email signature
    sig = _signature_block()
    check("Email signature includes +1 (443) 806 2833", "+1 (443) 806 2833" in sig)

    # 6. Verify FastAPI server endpoints
    client = TestClient(app)
    res_root = client.get("/")
    check("GET / returns 200", res_root.status_code == 200)
    check("GET / contains formatted phone", "+1 (443) 806 2833" in res_root.text)

    res_og = client.get("/og-image.png")
    check("GET /og-image.png returns 200", res_og.status_code == 200)
    check("GET /og-image.png is image/png", res_og.headers.get("content-type") == "image/png")
    check("GET /og-image.png has content", len(res_og.content) > 1000)


if __name__ == "__main__":
    test_phone_number_and_landing_page()
    if failures:
        print(f"\n{len(failures)} test(s) failed: {failures}")
        exit(1)
    print("\nALL LANDING PAGE & PHONE TESTS PASSED!")
