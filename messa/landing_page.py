"""Renders the public marketing landing page at GET / (see server.py) --
the customer-facing "what is Messa, here's the number to text her" page,
built to actually convert (the explicit ask that drove this file).

Static file + placeholder substitution, not an f-string constant like
live_view_page.py's pages: that file's own docstring documents the real
cost of the f-string approach (every literal `{`/`}` in its CSS/JS has to
be escaped as `{{`/`}}`), which is fine for that page's smaller, more
dynamic template but would be genuinely painful across a page this size
with this much CSS. This page is almost entirely static -- the only
per-deployment values are the phone number, the domain, and the current
year -- so a plain `.replace()` pass over a handful of distinctive
`__TOKEN__` placeholders (chosen specifically so they can never collide
with real CSS/JS, unlike `{}`) does the whole job with none of that
overhead. The actual HTML/CSS/JS lives in messa/assets/landing/index.html,
exactly where the user's own plan for this page said to put it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .phone import format_phone_display

_TEMPLATE_PATH = Path(__file__).parent / "assets" / "landing" / "index.html"

# Cached after the first render -- this is a static file on disk that only
# changes via a code deploy (a new container build), never at runtime, so
# there's no staleness risk in holding it in memory for the process's whole
# life the way there would be for anything DB-backed.
_cached_template: str | None = None


def _load_template() -> str:
    global _cached_template
    if _cached_template is None:
        _cached_template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return _cached_template


def _sms_href(raw: str | None) -> str:
    """sms: link target for whatever's in config.SENDBLUE_NUMBER -- always
    uses the raw digits/plus-sign form Sendblue gave us, regardless of how
    it's displayed (see phone.format_phone_display), since that's what
    actually has to work when a phone dials it. "sms:" (no number) if
    unconfigured -- the page still renders, just without a working number
    to show (see render_landing_page's own fallback for the display half)."""
    if not raw or not raw.strip():
        return "sms:"
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    sms_target = raw if raw.startswith("+") else (f"+{digits}" if digits else raw)
    return f"sms:{sms_target}"


def _format_phone(raw: str | None) -> tuple[str, str]:
    """(display, sms_href) for whatever's in config.SENDBLUE_NUMBER.
    Thin compatibility wrapper -- the actual display formatting now lives
    in messa/phone.py's format_phone_display (shared with the Messa-email
    signature in channels/resend.py), this just pairs it back up with the
    sms: href this page still needs."""
    return format_phone_display(raw), _sms_href(raw)


def render_landing_page() -> str:
    """Returns the full landing page HTML, ready to hand to HTMLResponse."""
    html = _load_template()
    display, sms_href = _format_phone(config.SENDBLUE_NUMBER)
    if not display:
        # Defensive fallback copy for the no-number-configured case -- see
        # _format_phone's docstring. Deliberately doesn't crash or 500;
        # a customer-facing page staying up (even imperfectly) beats a
        # broken deploy over one missing secret.
        display = "our number"
    primary_domain = config.TEXTMESSA_EMAIL_DOMAIN or "textmessa.com"
    year = str(datetime.now(timezone.utc).year)

    html = html.replace("__PHONE_DISPLAY__", display)
    html = html.replace("__SMS_HREF__", sms_href)
    html = html.replace("__PRIMARY_DOMAIN__", primary_domain)
    html = html.replace("__YEAR__", year)
    return html
