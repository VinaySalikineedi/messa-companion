"""Renders the public Open-Source Phone community skill showcase at
GET /skills (see server.py) -- open-source-phone.md section 6's "Universal
& Community Skills" public showcase page (messa.ai/skills), listing every
device_skills row a user has explicitly published (db.publish_device_skill,
via messa/tools/android_phone_tools.py's publish_phone_skill tool -- never
anything private, is_public=TRUE only, enforced in db.list_public_device_skills
itself, not just by this page filtering client-side).

Dynamic (per-request, DB-backed) content, so this is an f-string template
like call_live_view_page.py/phone_live_view_page.py rather than the static
`.replace()`-over-an-asset-file approach landing_page.py/privacy_page.py
use for their almost-entirely-static prose -- there's nothing static to
cache here, every load reflects whatever's actually published right now.

Every user-authored field (author_handle, app_name, description,
recipe_json) is untrusted, publicly-visible content -- passed through
html.escape() before being placed in the page. recipe_json in particular
is a user-controlled string that already went through
android_phone_tools.py's _sanitize_recipe PII redaction pass before ever
reaching the database, but that pass exists to strip emails/phone numbers,
not to make the string safe to place unescaped into HTML -- escaping here
is a separate, required layer regardless of what upstream redaction did.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any


def _fmt_date(value: Any) -> str:
    """Best-effort human date for a skill's created_at -- never raises (a
    malformed/missing timestamp just falls back to an empty string rather
    than a broken page)."""
    if value is None:
        return ""
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).strftime("%b %-d, %Y")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _pretty_recipe(raw: Any) -> str:
    """Pretty-prints recipe_json for the <details> block if it parses as
    JSON (the normal case -- android_phone_tools.py's publish_phone_skill
    always stores it that way); falls back to the raw string as-is for
    anything that doesn't parse, so a malformed row still renders instead
    of 500ing the whole page."""
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(raw)


def _skill_card(skill: dict) -> str:
    slug = html.escape(str(skill.get("skill_slug") or ""))
    app_name = html.escape(str(skill.get("app_name") or "Unknown app"))
    app_package = html.escape(str(skill.get("app_package") or ""))
    description = html.escape(str(skill.get("description") or "No description provided."))
    author = html.escape(str(skill.get("author_handle") or "a Messa user"))
    success = int(skill.get("success_count") or 0)
    failure = int(skill.get("failure_count") or 0)
    total = success + failure
    rate = f"{round(100 * success / total)}%" if total > 0 else "No runs yet"
    date = _fmt_date(skill.get("created_at"))
    recipe = html.escape(_pretty_recipe(skill.get("recipe_json")))

    return f"""
    <div class="card">
      <div class="card-head">
        <div>
          <div class="skill-name">{slug}</div>
          <div class="app-name">{app_name}{f' &middot; <span class="pkg">{app_package}</span>' if app_package else ''}</div>
        </div>
        <div class="rate">{rate}</div>
      </div>
      <div class="desc">{description}</div>
      <div class="meta">by {author}{f' &middot; {date}' if date else ''} &middot; {success} success / {failure} failed</div>
      {f'<details><summary>View recipe</summary><pre>{recipe}</pre></details>' if recipe else ''}
    </div>"""


def render_skills_showcase_page(skills: list[dict], app_filter: str | None = None) -> str:
    """Returns the full showcase page HTML, ready to hand to HTMLResponse.
    `skills` should already be the is_public-filtered rows from
    db.list_public_device_skills -- this module trusts that filtering has
    already happened and does no additional gating of its own."""
    filter_note = ""
    if app_filter:
        filter_note = (
            f'<div class="filter-note">Showing skills for '
            f'<code>{html.escape(app_filter)}</code> &middot; '
            f'<a href="/skills">view all</a></div>'
        )

    if skills:
        cards_html = "\n".join(_skill_card(s) for s in skills)
    else:
        cards_html = '<div class="empty">No public skills published yet. Be the first -- ask Messa to publish one after it figures out a task on your phone.</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Messa -- community phone skills</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0b0b0d; color: #e6e6e6; padding: 40px 16px 80px;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 28px; font-weight: 700; margin: 0 0 6px; }}
  .subtitle {{ color: #9a9a9a; font-size: 15px; margin: 0 0 28px; line-height: 1.5; }}
  .filter-note {{ font-size: 13px; color: #9a9a9a; margin-bottom: 18px; }}
  .filter-note a {{ color: #34c759; }}
  .filter-note code {{ background: #1c1c1f; padding: 2px 6px; border-radius: 4px; }}
  .card {{ background: #17171a; border: 1px solid #2a2a2e; border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; }}
  .card-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
  .skill-name {{ font-size: 16px; font-weight: 600; font-family: ui-monospace, monospace; }}
  .app-name {{ color: #9a9a9a; font-size: 13px; margin-top: 2px; }}
  .pkg {{ font-family: ui-monospace, monospace; color: #6a6a6e; }}
  .rate {{ font-size: 13px; font-weight: 600; color: #34c759; white-space: nowrap; }}
  .desc {{ font-size: 14px; color: #c7c7c7; margin-top: 10px; line-height: 1.5; }}
  .meta {{ font-size: 12px; color: #6a6a6e; margin-top: 10px; }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; font-size: 12.5px; color: #9a9a9a; }}
  pre {{ background: #0e0e10; border: 1px solid #2a2a2e; border-radius: 8px; padding: 12px;
    font-size: 11.5px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; margin-top: 8px; color: #b8b8b8; }}
  .empty {{ color: #6a6a6e; text-align: center; padding: 60px 20px; font-size: 14px; }}
  footer {{ text-align: center; color: #4a4a4e; font-size: 12px; margin-top: 40px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Community phone skills</h1>
  <p class="subtitle">Automation recipes other Messa users have published from their own Open-Source Phone tasks -- how to do a specific thing in a specific app, shared so Messa can do it faster next time for everyone.</p>
  {filter_note}
  {cards_html}
  <footer>Published via Messa's Open-Source Phone (Bring Your Own Phone) feature.</footer>
</div>
</body>
</html>"""
