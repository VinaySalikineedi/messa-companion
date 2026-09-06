"""Real-browser (local Chromium, no Browserbase/network needed) tests for
the two new "Faster Than Human" JS pieces:

  - assets/deepsearch_enhancements.js (cookie/consent auto-dismiss)
  - _FORM_ERROR_CHECK_FN in tools/deepsearch_tools.py (form-error scan)

These exercise the ACTUAL script content against real DOM/CSS behavior
(computed styles, getBoundingClientRect, MutationObserver) the way
test_fth_upgrades.py's mocked-evaluate-tool tests can't -- a regex or a
mocked tool result can't tell you whether the consent-dismiss heuristic
would really click the right button and leave the wrong one alone on a
page shaped like a real site.

Uses `context.add_init_script`, the same mechanism @playwright/mcp's own
--init-script flag uses under the hood, against a tiny local HTTP server
(page.set_content() does not reliably run init scripts the same way a real
navigation does -- see /tmp/test_real_scenario_initscript.py from this same
project for the precedent of testing --init-script behavior this way).

No Browserbase account, API key, or external network access required --
this is pure local Chromium (already installed in this sandbox, same one
live_smoke_test.py and other live-browser tests in this repo use).
"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from messa.tools.deepsearch_tools import _FORM_ERROR_CHECK_FN  # noqa: E402

CONSENT_SCRIPT_PATH = REPO_ROOT / "messa" / "assets" / "deepsearch_enhancements.js"
CHROMIUM_PATH = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
PORT = 8997

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---- Synthetic pages -------------------------------------------------

REAL_BANNER_PAGE = """
<html><body>
<div id="app-content"><h1>A real site</h1><p>Some content the agent actually wants.</p></div>
<div class="cookie-banner" style="position:fixed;bottom:0;left:0;width:100%;background:#222;padding:16px;">
  <span>We use cookies. <a href="#">Learn more</a></span>
  <button id="reject-btn">Reject All</button>
  <button id="accept-btn">Accept All</button>
</div>
</body></html>
"""

# A page with a button whose text matches the consent vocabulary but that
# is NOT inside anything resembling a banner -- this must NOT be clicked.
DECOY_BUTTON_PAGE = """
<html><body>
<div id="app-content">
  <h1>A pricing page</h1>
  <p>Ready to get started?</p>
  <button id="decoy-agree-btn" onclick="window.__decoyClicked = true;">I Agree</button>
  <p>By continuing you agree to our Terms of Service.</p>
</div>
</body></html>
"""

# A banner with a custom (non-keyword) class name, relying purely on the
# fixed/sticky-near-edge heuristic rather than id/class vocabulary.
UNBRANDED_BANNER_PAGE = """
<html><body>
<div id="app-content"><h1>Another real site</h1></div>
<div class="gdpr-thingy-xyz" style="position:fixed;top:0;left:0;width:100%;background:#eee;padding:12px;">
  <button id="got-it-btn">Got it</button>
</div>
</body></html>
"""

FORM_ERROR_PAGE = """
<html><body>
<form>
  <input id="email" aria-invalid="true" />
  <div class="field-error">Email already exists</div>
  <div class="error-message">Password must include a special character</div>
  <div role="alert">Please fix the errors above</div>
  <p>Some unrelated long paragraph of page content that should never match because it's far longer than the 200-character cap this scan intentionally applies to avoid pulling in whole sections of a page instead of a real short validation message right here at the end of this sentence which pushes it well past two hundred characters total.</p>
</form>
</body></html>
"""

FORM_NO_ERROR_PAGE = """
<html><body><form><input id="email" /><p>All good, no errors here.</p></form></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    routes: dict[str, str] = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        html = self.routes.get(self.path, "<html><body>not found</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


async def _run():
    from playwright.async_api import async_playwright

    Handler.routes = {
        "/real-banner": REAL_BANNER_PAGE,
        "/decoy": DECOY_BUTTON_PAGE,
        "/unbranded-banner": UNBRANDED_BANNER_PAGE,
        "/form-errors": FORM_ERROR_PAGE,
        "/form-clean": FORM_NO_ERROR_PAGE,
    }
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROMIUM_PATH, headless=True)
        try:
            context = await browser.new_context()
            await context.add_init_script(path=str(CONSENT_SCRIPT_PATH))

            # --- Part 1: real, recognizable cookie banner ---
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{PORT}/real-banner")
            await page.wait_for_timeout(300)
            dismissed = await page.evaluate("() => window.__messaConsentDismiss && window.__messaConsentDismiss.dismissedCount")
            last_clicked = await page.evaluate("() => window.__messaConsentDismiss && window.__messaConsentDismiss.lastClicked")
            reject_still_present = await page.locator("#reject-btn").count()
            check("real cookie banner: exactly one element auto-dismissed", dismissed == 1)
            check("real cookie banner: clicked 'Accept All', never 'Reject All'",
                  last_clicked == "accept all")
            check("real cookie banner: the Reject button itself was never touched (still in DOM)",
                  reject_still_present == 1)
            await page.close()

            # --- Part 2: decoy button with matching text but no banner context ---
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{PORT}/decoy")
            await page.wait_for_timeout(300)
            decoy_clicked = await page.evaluate("() => window.__decoyClicked === true")
            dismissed_count = await page.evaluate("() => window.__messaConsentDismiss && window.__messaConsentDismiss.dismissedCount")
            check("decoy 'I Agree' button (not in a banner) is NEVER auto-clicked", decoy_clicked is not True)
            check("no dismissal counted on the decoy page", dismissed_count == 0)
            await page.close()

            # --- Part 3: unbranded banner, fixed/sticky-position heuristic only ---
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{PORT}/unbranded-banner")
            await page.wait_for_timeout(300)
            dismissed2 = await page.evaluate("() => window.__messaConsentDismiss && window.__messaConsentDismiss.dismissedCount")
            check("unbranded fixed-position banner still gets dismissed via the position heuristic",
                  dismissed2 == 1)
            await page.close()

            # --- Part 4: form-error scan JS function, against a page with real errors ---
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{PORT}/form-errors")
            raw = await page.evaluate(_FORM_ERROR_CHECK_FN)
            errors = json.loads(raw)
            check("form-error scan finds the field-error text", "Email already exists" in errors)
            check("form-error scan finds the error-message text", any("special character" in e for e in errors))
            check("form-error scan finds the role=alert toast text", any("fix the errors" in e for e in errors))
            check("form-error scan does NOT pull in the long unrelated paragraph (>200 chars filtered out)",
                  not any(len(e) > 200 for e in errors))
            check("form-error scan returns a small, deduplicated list (<=8)", len(errors) <= 8)
            await page.close()

            # --- Part 5: form-error scan on a clean form ---
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{PORT}/form-clean")
            raw_clean = await page.evaluate(_FORM_ERROR_CHECK_FN)
            errors_clean = json.loads(raw_clean)
            check("form-error scan returns an empty list on a clean form", errors_clean == [])
            await page.close()

            await context.close()
        finally:
            await browser.close()
    httpd.shutdown()


def main():
    check("deepsearch_enhancements.js exists", CONSENT_SCRIPT_PATH.exists())
    try:
        asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        check(f"live browser scenario ran without raising ({e})", False)

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
