"""Renders the public Privacy Policy page at GET /privacy (see server.py).

Same static-file + placeholder-substitution approach as landing_page.py,
for the same reason (see that module's own docstring) -- this page is
almost entirely static prose, so a `.replace()` pass over a handful of
`__TOKEN__` placeholders does the job with none of an f-string template's
escaping overhead. The actual HTML/CSS lives in
messa/assets/legal/privacy.html.

Messa currently operates as a sole proprietorship, not a registered
company -- by request, this page deliberately publishes no personal legal
name and no physical address, only config.PRIVACY_CONTACT_EMAIL as a
contact point. This is NOT a substitute for review by an actual lawyer
before the page goes live.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

_TEMPLATE_PATH = Path(__file__).parent / "assets" / "legal" / "privacy.html"

# Cached after first render -- same reasoning as landing_page.py's
# _cached_template: static on disk, only changes via a code deploy.
_cached_template: str | None = None


def _load_template() -> str:
    global _cached_template
    if _cached_template is None:
        _cached_template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return _cached_template


def _sms_href(raw: str | None) -> str:
    """Same logic as landing_page.py's own _sms_href, duplicated (not
    imported) so this page stays independently renderable even if that
    module's internals change -- returns a full "sms:..." href, or bare
    "sms:" if unconfigured, matching that module's own contract."""
    if not raw or not raw.strip():
        return "sms:"
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    sms_target = raw if raw.startswith("+") else (f"+{digits}" if digits else raw)
    return f"sms:{sms_target}"


def render_privacy_page() -> str:
    """Returns the full Privacy Policy page HTML, ready to hand to
    HTMLResponse. `effective_date` is today's date in UTC, formatted for a
    human reader -- there's no "policy version" concept anywhere else in
    this codebase to draw from instead, so this always reflects the day
    this route last rendered, not a deliberately-set publish date. Update
    to a fixed date once this policy is actually finalized and published,
    so it stops moving on every server restart."""
    html = _load_template()
    sms_href = _sms_href(config.SENDBLUE_NUMBER)
    primary_domain = config.TEXTMESSA_EMAIL_DOMAIN or "textmessa.com"
    year = str(datetime.now(timezone.utc).year)
    effective_date = datetime.now(timezone.utc).strftime("%B %-d, %Y")

    html = html.replace("__PRIMARY_DOMAIN__", primary_domain)
    html = html.replace("__SMS_HREF__", sms_href)
    html = html.replace("__YEAR__", year)
    html = html.replace("__EFFECTIVE_DATE__", effective_date)
    html = html.replace("__PRIVACY_EMAIL__", config.PRIVACY_CONTACT_EMAIL)
    return html
