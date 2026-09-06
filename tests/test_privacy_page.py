"""Regression tests for the new public Privacy Policy page (GET /privacy,
see messa/privacy_page.py + messa/assets/legal/privacy.html), added after a
real end user asked how Messa handles their data and this repo turned out
to have no privacy page at all (see README's "known gaps" section).

Checks:
  1. render_privacy_page() leaves no unsubstituted __TOKEN__ placeholders.
  2. Every fact this page states about data handling is one this repo can
     actually back up with real code (see the third-party table + the
     encryption claim) -- structural checks only, not exhaustive.
  3. No personal legal name or physical address is disclosed anywhere on
     the page (by request -- Messa is a sole proprietorship with no
     separate legal entity); email is the only contact info shown, and the
     page carries an explicit "by chatting with Messa you agree to this
     policy" consent clause.
  4. server.py actually wires GET /privacy to this renderer, and the
     landing page footer actually links to it.
"""
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import config  # noqa: E402
from messa.landing_page import render_landing_page  # noqa: E402
from messa.privacy_page import render_privacy_page  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def part1_no_leftover_placeholders():
    html = render_privacy_page()
    leftover = re.findall(r"__[A-Z_]+__", html)
    check(f"no unsubstituted __TOKEN__ placeholders remain (found: {leftover})", leftover == [])
    check("page renders a non-trivial amount of content", len(html) > 5000)


def part2_content_matches_real_implementation():
    html = render_privacy_page()
    # Every named subprocessor below corresponds to a real integration in
    # this codebase (config.py) -- not an invented/aspirational list.
    for provider in ["OpenRouter", "Composio", "Resend", "Sendblue", "Browserbase", "Neon", "Mem0", "Open-Meteo"]:
        check(f"third-party table names {provider}", provider in html)

    check("states data is not sold", "do not sell your personal information" in html)
    check("states conversations aren't used for ad targeting",
          "do not use your conversations to serve you advertising" in html
          or "not use your conversations with Messa to serve you advertising" in html)
    check("describes credential encryption accurately (encrypted, key held separately)",
          "encrypted at rest" in html and "held separately" in html)
    check("does not overclaim formal certification (no 'GDPR compliant'/'SOC 2 certified' claim)",
          "GDPR compliant" not in html and "SOC 2 certified" not in html and "SOC2 certified" not in html)
    check("honestly states retention is not yet fully automated",
          "actively building automated retention" in html)


def part3_no_personal_name_or_address_disclosed():
    # By explicit request: Messa is a sole proprietorship with no separate
    # legal entity, and the operator's personal name/home address are
    # deliberately NOT published on this page -- email is the only contact
    # info disclosed. This guards against a future edit accidentally
    # reintroducing a __LEGAL_ENTITY__/__BUSINESS_ADDRESS__-style token.
    html = render_privacy_page()
    check("PRIVACY_CONTACT_EMAIL has a sensible default derived from the real email domain",
          "@" in config.PRIVACY_CONTACT_EMAIL)
    check("no leftover entity/address placeholder tokens or config attrs exist",
          not hasattr(config, "LEGAL_ENTITY_NAME") and not hasattr(config, "BUSINESS_MAILING_ADDRESS"))
    check("page states Messa operates as a sole proprietorship",
          "sole proprietorship" in html)
    check("page carries an explicit by-chatting-you-agree consent clause",
          "you agree to" in html.lower() and ("texting" in html.lower() or "chatting" in html.lower()))


def part4_wired_into_server_and_landing_page():
    server_src = (REPO_ROOT / "messa" / "server.py").read_text()
    check("server.py imports render_privacy_page", "from .privacy_page import render_privacy_page" in server_src)
    check("server.py registers GET /privacy", '@app.get("/privacy")' in server_src)

    landing_html = render_landing_page()
    check("landing page footer links to /privacy", 'href="/privacy"' in landing_html)

    # End-to-end: the route actually dispatches, not just present in source.
    from starlette.testclient import TestClient

    from messa.server import app

    client = TestClient(app)
    resp = client.get("/privacy")
    check("GET /privacy returns 200", resp.status_code == 200)
    check("GET /privacy body contains the policy heading", "Privacy Policy" in resp.text)


def part5_no_leftover_amp_semicolon_typo():
    # Cheap sanity check on the footer edit in assets/landing/index.html --
    # a stray literal "&amp;copy;" (double-escaped) instead of "&copy;"
    # would silently print as text in a browser.
    html = render_landing_page()
    check("copyright entity renders correctly (no double-escaping)", "&amp;copy;" not in html)


def main():
    part1_no_leftover_placeholders()
    part2_content_matches_real_implementation()
    part3_no_personal_name_or_address_disclosed()
    part4_wired_into_server_and_landing_page()
    part5_no_leftover_amp_semicolon_typo()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
