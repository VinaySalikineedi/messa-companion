"""The public, tokenized /live/<token> page (Phase 3): a permanent per-user
link, texted alongside every deepsearch delegation (see agents/registry.py's
live_view_str), that shows Browserbase's live browser view while a browsing
task is running and a calm "nothing happening right now" state otherwise.

Kept as one self-contained HTML/CSS/JS string (no build step, no external
assets -- this needs to render correctly from a bare `GET`, including from
whatever in-app browser iMessage/SMS opens links in) rather than a template
file, matching how small this project keeps its other single-purpose
surfaces. The page itself never talks to Postgres -- it only ever calls its
own `/live/<token>/status` JSON endpoint (see server.py), polling every few
seconds.

Multi-tab grid (this round): earlier versions of this page showed exactly
one fixed video, the top-level tab's own -- real work happening on a
delegate_website_task sub-worker's tab was invisible no matter what, since
that one video never pointed at it (see README's "Live view showed a blank
page during multi-site delegation" section for the full history of that
bug). Real feedback from a real run made clear that following just ONE
"currently active" tab still wasn't right either -- when several sub-workers
run concurrently, the whole point is seeing ALL of them at once, the way
Browserbase's own dashboard shows one live link per open tab (see
https://docs.browserbase.com/platform/browser/observability/session-live-view).
So this page now renders a responsive GRID of tiles, one per currently-open
browser tab (the top-level orchestrator's own tab plus every live
delegate_website_task sub-worker) -- each with its own heading (the site
it's working on), its own live video, and its own short (3-4 line) log,
added the moment server.py's /status route reports a new tab and removed
the moment that tab closes. See server.py's `_build_live_tiles` for how
each tile's own live_view_url is resolved from Browserbase's per-page debug
urls, and live_activity.py for where each tab's own description/steps/url
are recorded.

Dashboard page (this round): a second toggle-able page on the same
permanent /live/<token> link, showing the user's own reminders, this
week's schedule, tasks, projects, and contacts -- independent of whether a
browsing session is active ("I want to add more to the live view page for
the user on top of the live browsing windows... The browser windows still
stay on the first page and second page we can keep these and add a toggle
on the top to switch between them"). Both pages are permanent siblings of
`#main` (a `.page-area` each), toggled purely via the `hidden` attribute --
never torn down and rebuilt on switch, so the browsing grid's careful
"write an iframe's src exactly once" state (see updateTileVideo below)
survives switching to the dashboard and back. The dashboard polls its own
`/live/<token>/dashboard` JSON endpoint (see server.py) on a much slower
cadence than the 4s browsing poll, since tasks/reminders/schedule change
far less often than a live browser tab does.

Two visual skins, same markup/JS, different CSS variables:

  - "terminal" (default, for now): black background, monospace, a fake
    terminal titlebar per tile. Back to being the default per direct
    feedback on the "polished" redesign below ("I did not like it to be
    honest, lets just switch to our terminal style for now, we can work on
    the UI later") -- so this is a deliberate reversion of the *skin*
    only, not the underlying multi-tile grid mechanism (see the "Multi-tab
    grid" paragraph above), which stayed and is what the terminal skin
    below now renders.
  - "polished": a clean, light, editorial look -- soft neutral background,
    one tile per tab as a lifted rounded card, generous whitespace,
    restrained motion. Built for a later round ("apple like website that is
    clean but elegant and good quality") but shelved as the default until
    that gets revisited -- still fully working, reachable via
    `?style=polished`, not deleted.

LIVE_VIEW_STYLE below is "the variable you can change": flip it and
redeploy to switch the *default* skin. `?style=polished` / `?style=terminal`
on any live link overrides the default for just that one page load.

Security note: `token` is only ever a value this module itself generates
(`secrets.token_urlsafe`, see db.get_or_create_live_share_token) -- alphanumeric
plus '-'/'_' only, never user-supplied free text -- so it's safe to embed
directly into the JS below with no HTML-escaping needed. Nothing else here
does string interpolation of untrusted data: headings, descriptions,
chain-of-thought lines, task titles, and every live_view_url are all
fetched client-side via JSON and written with textContent/`.src` (never
innerHTML), so none of that server-generated-but-ultimately-model-influenced
text can inject anything.
"""
from __future__ import annotations

# "The variable you can change" (see module docstring): pick which skin
# renders by default when a live link is opened with no ?style= override.
LIVE_VIEW_STYLE = "terminal"  # "polished" | "terminal"

_VALID_STYLES = ("polished", "terminal")

# Must match channels/browserbase.py's VIEWPORT_WIDTH/HEIGHT -- each tile's
# video area uses this as a plain CSS aspect-ratio, so Browserbase's
# embedded live-view page fills it edge-to-edge with no letterboxing.
BROWSER_VIEWPORT_WIDTH = 1280
BROWSER_VIEWPORT_HEIGHT = 800

# How many of a tile's most recent chain-of-thought lines to show in its
# (deliberately small, per-tile) log -- per explicit ask: "bottom log
# updates maybe max of 3-5 lines showing as we are fitting multiple browser
# screens." The full history is still capped/kept server-side
# (live_activity.MAX_STEPS = 60) for anything that wants it later; this is
# purely how much of it one tile displays at once.
TILE_LOG_LINES = 4


def render_live_view_page(token: str, style: str | None = None) -> str:
    resolved_style = style if style in _VALID_STYLES else LIVE_VIEW_STYLE
    return f"""<!doctype html>
<html lang="en" data-style="{resolved_style}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Messa -- live browsing</title>
<style>
  * {{ box-sizing: border-box; }}

  /* ---- "polished" skin: clean, light, editorial -- the designed-for
     production look. Restrained palette, one accent color, soft shadows
     instead of hard borders where possible. ---- */
  [data-style="polished"] {{
    --bg: #f5f5f7;
    --panel: #ffffff;
    --panel-translucent: rgba(255, 255, 255, 0.86);
    --card-bg: #ffffff;
    --border: #e5e5ea;
    --border-soft: #eeeef1;
    --text: #1d1d1f;
    --muted: #86868b;
    --muted-strong: #6e6e73;
    --accent: #0071e3;
    --accent-soft: rgba(0, 113, 227, 0.12);
    --live: #ff3b30;
    --warn: #ff9f0a;
    --warn-soft: rgba(255, 159, 10, 0.12);
    --radius: 18px;
    --radius-sm: 12px;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 12px 32px -16px rgba(0, 0, 0, 0.18);
    --shadow-lifted: 0 2px 6px rgba(0, 0, 0, 0.05), 0 24px 48px -20px rgba(0, 0, 0, 0.25);
    --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Helvetica, Arial, sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }}
  @media (prefers-color-scheme: dark) {{
    [data-style="polished"] {{
      --bg: #000000;
      --panel: #1c1c1e;
      --panel-translucent: rgba(28, 28, 30, 0.86);
      --card-bg: #1c1c1e;
      --border: #2c2c2e;
      --border-soft: #262628;
      --text: #f5f5f7;
      --muted: #8e8e93;
      --muted-strong: #aeaeb2;
      --accent: #0a84ff;
      --accent-soft: rgba(10, 132, 255, 0.16);
      --warn-soft: rgba(255, 159, 10, 0.16);
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 12px 32px -16px rgba(0, 0, 0, 0.6);
      --shadow-lifted: 0 2px 6px rgba(0, 0, 0, 0.4), 0 24px 48px -20px rgba(0, 0, 0, 0.7);
    }}
  }}

  /* ---- "terminal" skin: black, monospace, gritty -- kept for old links,
     not the design focus this round (see module docstring). ---- */
  [data-style="terminal"] {{
    --bg: #0a0a0a;
    --panel: #000000;
    --panel-translucent: rgba(0, 0, 0, 0.86);
    --card-bg: #050505;
    --border: #1f2a1f;
    --border-soft: #16201a;
    --text: #c9f2d0;
    --muted: #5f8f68;
    --muted-strong: #7fb389;
    --accent: #39ff88;
    --accent-soft: rgba(57, 255, 136, 0.12);
    --live: #ff5566;
    --warn: #ffd166;
    --warn-soft: rgba(255, 209, 102, 0.14);
    --radius: 4px;
    --radius-sm: 3px;
    --shadow: 0 0 0 1px rgba(57, 255, 136, 0.1);
    --shadow-lifted: 0 0 0 1px rgba(57, 255, 136, 0.18), 0 0 40px rgba(57, 255, 136, 0.06);
    --font: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    --font-mono: var(--font);
  }}

  html, body {{ height: 100%; margin: 0; }}
  body {{
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    -webkit-font-smoothing: antialiased;
  }}

  header {{
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.95rem 1.4rem;
    border-bottom: 1px solid var(--border-soft);
    background: var(--panel-translucent);
    backdrop-filter: saturate(180%) blur(14px);
    -webkit-backdrop-filter: saturate(180%) blur(14px);
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  header .brand {{ font-weight: 600; letter-spacing: -0.01em; color: var(--text); font-size: 0.95rem; }}
  [data-style="terminal"] header .brand::before {{ content: "> "; color: var(--muted); }}
  header .tile-count {{
    color: var(--muted);
    font-size: 0.82rem;
    margin-left: 0.15rem;
  }}

  /* ---- page toggle (Live Browsing / Dashboard) ---- */
  .page-toggle {{
    margin-left: auto;
    flex: 0 0 auto;
    display: flex;
    gap: 0.25rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.2rem;
  }}
  [data-style="terminal"] .page-toggle {{ border-radius: var(--radius-sm); }}
  .page-toggle .toggle-btn {{
    appearance: none;
    border: 0;
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    padding: 0.34rem 0.75rem;
    border-radius: 999px;
    cursor: pointer;
    transition: background-color 0.2s ease, color 0.2s ease;
  }}
  [data-style="terminal"] .page-toggle .toggle-btn {{
    border-radius: var(--radius-sm);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .page-toggle .toggle-btn.active {{
    background: var(--card-bg);
    color: var(--text);
    box-shadow: var(--shadow);
  }}
  [data-style="terminal"] .page-toggle .toggle-btn.active {{ color: var(--accent); }}

  /* ---- live indicator: a solid core + two staggered outward-fading rings
     (a "radar ping"). ---- */
  .live-indicator {{
    position: relative;
    width: 10px;
    height: 10px;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }}
  .live-indicator .core {{
    position: relative;
    z-index: 1;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--muted);
    transition: background-color 0.25s ease;
  }}
  [data-style="terminal"] .live-indicator .core {{ border-radius: 2px; }}
  .live-indicator .ring {{
    position: absolute;
    inset: 0;
    border-radius: 50%;
    border: 1.5px solid var(--live);
    opacity: 0;
  }}
  [data-style="terminal"] .live-indicator .ring {{ border-radius: 2px; }}
  .live-indicator.live .core {{
    background: var(--live);
    box-shadow: 0 0 7px 1px rgba(255, 59, 48, 0.55);
  }}
  .live-indicator.live .ring {{ animation: ring-ping 1.8s cubic-bezier(0.2, 0.6, 0.4, 1) infinite; }}
  .live-indicator.live .ring.d2 {{ animation-delay: 0.9s; }}
  @keyframes ring-ping {{
    0%   {{ transform: scale(1); opacity: 0.55; }}
    100% {{ transform: scale(2.8); opacity: 0; }}
  }}
  .live-label {{
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    color: var(--muted);
    opacity: 0;
    transform: translateX(-2px);
    transition: opacity 0.25s ease, transform 0.25s ease, color 0.25s ease;
  }}
  .live-indicator.live + .live-label {{
    opacity: 1;
    transform: translateX(0);
    color: var(--live);
  }}

  .page-area {{
    flex: 1 1 auto;
    min-height: 0;
    position: relative;
    display: flex;
  }}
  .page-area[hidden] {{ display: none; }}

  /* ---- idle / invalid / closing single-card states ---- */
  .state {{
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }}
  .state .card {{ text-align: center; max-width: 27rem; }}
  .state h1 {{ font-size: 1.2rem; margin: 0 0 0.5rem; color: var(--text); font-weight: 600; letter-spacing: -0.01em; }}
  [data-style="terminal"] .state h1::before {{ content: "$ "; color: var(--accent); }}
  .state p {{ color: var(--muted); margin: 0; line-height: 1.55; font-size: 0.95rem; }}
  .state .icon {{ font-size: 2.1rem; margin-bottom: 0.85rem; opacity: 0.85; }}
  [data-style="terminal"] .state .icon {{ display: none; }}

  .closing-card {{
    width: 100%;
    max-width: 420px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1rem;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-lifted);
    padding: 2.5rem 2rem;
  }}
  .closing-card .spinner {{
    width: 28px;
    height: 28px;
    border-radius: 50%;
    border: 2.5px solid var(--border);
    border-top-color: var(--accent);
    animation: spin 0.75s linear infinite;
  }}
  [data-style="terminal"] .closing-card .spinner {{ border-radius: 2px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .closing-card .label {{ font-size: 0.98rem; color: var(--text); font-weight: 500; }}
  [data-style="terminal"] .closing-card .label::before {{ content: "$ "; color: var(--muted); }}

  /* ---- the grid of tiles ---- */
  .grid-wrap {{
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 1.5rem;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(380px, 460px));
    justify-content: center;
    gap: 1.35rem;
    max-width: 1480px;
    margin: 0 auto;
  }}

  .tile {{
    display: flex;
    flex-direction: column;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
    transition: box-shadow 0.25s ease, border-color 0.25s ease, opacity 0.3s ease, transform 0.3s ease;
  }}
  .tile.tile-active {{
    border-color: var(--accent);
    box-shadow: var(--shadow), 0 0 0 3px var(--accent-soft);
  }}
  /* enter/exit transitions -- driven by a class toggled in JS one frame
     after insertion, and removed just before a closed tab's tile is torn
     out of the DOM, so tiles visibly settle in and fade out instead of
     the grid re-flowing abruptly under an operator's eyes every poll. */
  .tile.tile-enter {{ opacity: 0; transform: translateY(6px) scale(0.98); }}
  .tile.tile-exit {{ opacity: 0; transform: scale(0.97); }}

  .tile-head {{
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.8rem 1rem;
    border-bottom: 1px solid var(--border-soft);
  }}
  .tile-head .dot {{
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
    flex: 0 0 auto;
    animation: pulse-soft 1.8s infinite;
  }}
  [data-style="terminal"] .tile-head .dot {{ border-radius: 1px; }}
  @keyframes pulse-soft {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.35; }}
  }}
  .tile-head .heading {{
    font-weight: 600;
    font-size: 0.92rem;
    color: var(--text);
    letter-spacing: -0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  [data-style="terminal"] .tile-head .heading::before {{ content: "$ "; color: var(--muted); }}
  .tile-head .waiting-pill {{
    margin-left: auto;
    flex: 0 0 auto;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: var(--warn);
    background: var(--warn-soft);
    padding: 0.2rem 0.5rem;
    border-radius: 999px;
    white-space: nowrap;
  }}
  [data-style="terminal"] .tile-head .waiting-pill {{ border-radius: 2px; }}

  .tile-desc {{
    flex: 0 0 auto;
    padding: 0.55rem 1rem;
    font-size: 0.82rem;
    color: var(--muted-strong);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    border-bottom: 1px solid var(--border-soft);
  }}
  [data-style="terminal"] .tile-desc {{ color: var(--accent); }}
  [data-style="terminal"] .tile-desc::before {{ content: "$ "; color: var(--muted); }}

  .tile-video {{
    flex: 0 0 auto;
    width: 100%;
    aspect-ratio: {BROWSER_VIEWPORT_WIDTH} / {BROWSER_VIEWPORT_HEIGHT};
    position: relative;
    background: #000;
  }}
  .tile-video iframe {{ position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }}
  .tile-video .connecting {{
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.6rem;
    color: #8e8e93;
    background: #0b0b0c;
  }}
  .tile-video .connecting .spinner {{
    width: 20px;
    height: 20px;
    border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.18);
    border-top-color: #b8b8bd;
    animation: spin 0.8s linear infinite;
  }}
  [data-style="terminal"] .tile-video .connecting .spinner {{ border-radius: 2px; }}
  .tile-video .connecting span.label {{ font-size: 0.78rem; }}

  /* ---- disconnected: swapped in on Browserbase's own "browserbase-
     disconnected" postMessage (see the module docstring's "Handling
     disconnects" note) -- deliberately blank/calm, no error text, no red,
     nothing that reads as a fault. Just Messa's own mark on black, the way
     a video call shows a paused tile instead of an error screen. ---- */
  .tile-video .disconnected {{
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
  }}
  .tile-video .disconnected .mark {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #3a3a3c;
  }}
  [data-style="terminal"] .tile-video .disconnected .mark {{ border-radius: 1px; background: #1f2a1f; }}

  .tile-log {{
    flex: 1 1 auto;
    min-height: 5.6rem;
    max-height: 5.6rem;
    overflow-y: auto;
    padding: 0.55rem 1rem;
    font-size: 0.76rem;
    line-height: 1.55;
    color: var(--muted);
    background: var(--panel);
  }}
  .tile-log .empty-hint {{ color: var(--muted); opacity: 0.7; font-style: italic; }}
  [data-style="terminal"] .tile-log .empty-hint {{ font-style: normal; }}
  .tile-log .line {{ white-space: pre-wrap; word-break: break-word; }}
  [data-style="polished"] .tile-log .line::before {{ content: "\\2022  "; color: var(--accent); }}
  [data-style="terminal"] .tile-log {{ color: var(--text); }}
  [data-style="terminal"] .tile-log .line::before {{ content: "> "; color: var(--accent); }}

  /* ---- dashboard page: schedule/tasks/reminders/projects/contacts,
     reusing the same --card-bg/--border/--radius/--shadow variables as the
     browsing tiles so both skins apply automatically with no extra work. ---- */
  .dash-wrap {{
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 1.5rem;
  }}
  .dash-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    align-items: start;
    gap: 1.35rem;
    max-width: 1480px;
    margin: 0 auto;
  }}
  .dash-card {{
    display: flex;
    flex-direction: column;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
  }}
  .dash-card.schedule {{ grid-column: 1 / -1; }}
  .dash-card-head {{
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    padding: 0.8rem 1rem;
    font-weight: 600;
    font-size: 0.92rem;
    color: var(--text);
    letter-spacing: -0.01em;
    border-bottom: 1px solid var(--border-soft);
  }}
  [data-style="terminal"] .dash-card-head::before {{ content: "$ "; color: var(--muted); }}
  .dash-card-title-text {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .week-nav {{ flex: 0 0 auto; display: flex; align-items: center; gap: 0.35rem; }}
  .week-nav-btn {{
    appearance: none;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font: inherit;
    font-size: 0.8rem;
    font-weight: 600;
    line-height: 1;
    padding: 0.25rem 0.55rem;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background-color 0.15s ease, border-color 0.15s ease;
  }}
  .week-nav-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .week-nav-today {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.03em; color: var(--muted); }}
  .week-nav-today:hover {{ color: var(--accent); }}
  .dash-card-body {{ padding: 0.5rem 0; }}
  .dash-card:not(.schedule) .dash-card-body {{ padding: 0.7rem 1rem 0.9rem; }}
  .dash-empty {{
    color: var(--muted);
    font-size: 0.85rem;
    font-style: italic;
    padding: 0.4rem 1rem 0.6rem;
  }}
  [data-style="terminal"] .dash-empty {{ font-style: normal; }}
  .dash-profile-text {{
    font-size: 0.85rem;
    line-height: 1.5;
    color: var(--text);
    white-space: pre-wrap;
  }}

  /* week schedule: one native <details> disclosure per day, time on the
     left of each event, title (+ location) on the right -- per explicit
     spec ("each day is a drop down with time on the left and scheduled
     event on the right"). */
  .day-row {{ border-bottom: 1px solid var(--border-soft); }}
  .day-row:last-child {{ border-bottom: 0; }}
  .day-row summary {{
    list-style: none;
    cursor: pointer;
    padding: 0.65rem 1rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    font-size: 0.88rem;
    color: var(--text);
  }}
  .day-row summary::-webkit-details-marker {{ display: none; }}
  .day-row summary .day-label {{ font-weight: 600; }}
  .day-row.today summary .day-label {{ color: var(--accent); }}
  .day-row summary .count {{ color: var(--muted); font-size: 0.78rem; white-space: nowrap; }}
  .day-row .events {{
    padding: 0 1rem 0.85rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }}
  .event-row {{ display: flex; gap: 0.85rem; font-size: 0.85rem; }}
  .event-row .time {{
    flex: 0 0 auto;
    width: 5.6rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
  .event-row .info {{ flex: 1 1 auto; color: var(--text); min-width: 0; }}
  .event-row .info .loc {{ color: var(--muted); font-size: 0.78rem; }}

  /* tasks / reminders / projects / contacts -- simple label + meta rows */
  .dash-list {{ display: flex; flex-direction: column; gap: 0.6rem; }}
  .dash-item {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.85rem;
    font-size: 0.87rem;
  }}
  .dash-item .primary {{ color: var(--text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .dash-item .meta {{ color: var(--muted); font-size: 0.78rem; white-space: nowrap; flex: 0 0 auto; }}

  /* ---- emails page: third toggle-able page, mail-app-style list + a
     click-to-open thread view of Messa's OWN email address (a different
     inbox from the user's connected Gmail -- see live_view_page.py's
     module docstring / server.py's /live/<token>/emails route). Reuses the
     same --card-bg/--border/--radius/--shadow variables as the browsing
     tiles and dashboard cards above, and the same .page-toggle/.toggle-btn
     segmented-control look for its own Inbox/Sent tabs, so both skins keep
     applying automatically with no new variables introduced. ---- */
  .emails-wrap {{
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 1.5rem;
  }}
  .emails-toolbar {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    max-width: 900px;
    margin: 0 auto 1rem;
  }}
  .email-address-chip {{
    font-size: 0.78rem;
    color: var(--muted);
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.3rem 0.7rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 15rem;
  }}
  [data-style="terminal"] .email-address-chip {{ border-radius: var(--radius-sm); }}
  .email-address-chip:empty {{ display: none; }}
  .email-tabs {{ margin-left: 0; }}
  .email-search {{
    flex: 1 1 12rem;
    min-width: 8rem;
    margin-left: auto;
    background: var(--card-bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.75rem;
    font: inherit;
    font-size: 0.85rem;
  }}
  .email-search::placeholder {{ color: var(--muted); }}
  .email-search:focus {{ outline: none; border-color: var(--accent); }}

  .email-list {{
    display: flex;
    flex-direction: column;
    max-width: 900px;
    margin: 0 auto;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
  }}
  .email-row {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.85rem 1.1rem;
    border-bottom: 1px solid var(--border-soft);
    cursor: pointer;
    transition: background-color 0.15s ease;
  }}
  .email-row:last-child {{ border-bottom: 0; }}
  .email-row:hover, .email-row:focus-visible {{ background: var(--accent-soft); outline: none; }}
  .email-row-main {{ min-width: 0; flex: 1 1 auto; }}
  .email-row-top {{ display: flex; align-items: center; gap: 0.45rem; margin-bottom: 0.15rem; }}
  .email-row-counterpart {{
    font-weight: 600;
    font-size: 0.88rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .email-row-subject {{
    font-size: 0.85rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .email-row-snippet {{
    font-size: 0.8rem;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .email-row-time {{
    flex: 0 0 auto;
    font-size: 0.76rem;
    color: var(--muted);
    white-space: nowrap;
    padding-top: 0.1rem;
  }}
  .email-badge {{
    flex: 0 0 auto;
    font-size: 0.64rem;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    padding: 0.1rem 0.4rem;
    border-radius: 999px;
  }}
  [data-style="terminal"] .email-badge {{ border-radius: 2px; }}
  .email-badge.autonomous {{ color: var(--accent); background: var(--accent-soft); }}
  .email-badge.attachment {{
    text-transform: none;
    font-size: 0.78rem;
    color: var(--muted);
    background: transparent;
    padding: 0;
  }}

  #emailListFooter {{ display: flex; justify-content: center; padding: 1rem 0 0.25rem; }}
  .email-loadmore-btn {{
    background: var(--card-bg);
    border: 1px solid var(--border);
  }}

  .email-thread-back {{
    appearance: none;
    border: 0;
    background: transparent;
    color: var(--accent);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    padding: 0.3rem 0;
    max-width: 900px;
    margin: 0 auto 0.85rem;
    display: block;
  }}
  .email-thread {{
    display: flex;
    flex-direction: column;
    max-width: 900px;
    margin: 0 auto;
  }}
  .email-thread-msg {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-left: 3px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.85rem 1rem;
    margin-bottom: 0.85rem;
  }}
  .email-thread-msg.outbound {{ border-left-color: var(--accent); }}
  .email-thread-msg-head {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    font-size: 0.8rem;
    color: var(--muted);
    margin-bottom: 0.5rem;
  }}
  .email-thread-msg-badges {{ display: flex; align-items: center; gap: 0.5rem; }}
  .email-thread-msg-subject {{ font-weight: 600; font-size: 0.9rem; color: var(--text); margin-bottom: 0.35rem; }}
  .email-thread-msg-body {{
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 0.87rem;
    line-height: 1.6;
    color: var(--text);
  }}

  footer {{
    flex: 0 0 auto;
    padding: 0.6rem 1.1rem;
    text-align: center;
    color: var(--muted);
    font-size: 0.74rem;
    border-top: 1px solid var(--border-soft);
    background: var(--panel);
  }}
</style>
</head>
<body>
  <header>
    <span class="live-indicator" id="dot">
      <span class="ring d1"></span>
      <span class="ring d2"></span>
      <span class="core"></span>
    </span>
    <span class="live-label">LIVE</span>
    <span class="brand">Messa</span>
    <span class="tile-count" id="tileCount"></span>
    <nav class="page-toggle" id="pageToggle">
      <button type="button" class="toggle-btn active" data-page="browsing">Live browsing</button>
      <button type="button" class="toggle-btn" data-page="dashboard">Dashboard</button>
      <button type="button" class="toggle-btn" data-page="emails">Emails</button>
    </nav>
  </header>
  <main id="main" class="page-area">
    <div class="state">
      <div class="card">
        <div class="icon">&#8230;</div>
        <h1>Loading&hellip;</h1>
        <p>Checking whether a browsing session is live.</p>
      </div>
    </div>
  </main>
  <div id="page-dashboard" class="page-area" hidden>
    <div class="state">
      <div class="card">
        <div class="icon">&#8230;</div>
        <h1>Loading&hellip;</h1>
        <p>Fetching your tasks, reminders, and schedule.</p>
      </div>
    </div>
  </div>
  <div id="page-emails" class="page-area" hidden>
    <div class="state">
      <div class="card">
        <div class="icon">&#8230;</div>
        <h1>Loading&hellip;</h1>
        <p>Fetching your email.</p>
      </div>
    </div>
  </div>
  <footer>textmessa.com &middot; this link is permanent, bookmark it</footer>

<script>
(function () {{
  var TOKEN = "{token}";
  var TILE_LOG_LINES = {TILE_LOG_LINES};
  var DASHBOARD_POLL_MS = 30000; // far slower than the 4s browsing poll --
                                  // tasks/reminders/schedule don't change
                                  // second to second the way a live tab does
  var main = document.getElementById("main");
  var dot = document.getElementById("dot");
  var tileCountEl = document.getElementById("tileCount");
  var dashboardPage = document.getElementById("page-dashboard");
  var emailsPage = document.getElementById("page-emails");
  var pageToggle = document.getElementById("pageToggle");
  // "loading" | "idle" | "invalid" | "closing" | "grid" -- which of the
  // non-tile states is currently rendered in #main, so those states only
  // touch the DOM when they actually change. The grid itself is reconciled
  // separately (see renderGrid) since it can have 0..N tiles at once and
  // needs to add/update/remove individual tile elements, not just flip
  // between a handful of fixed states.
  var shownState = "loading";
  var tileEls = {{}};  // tile id -> {{ root, video, log, desc, heading, dot, waiting, stepsRendered }}
  var stopped = false; // true once we've confirmed the token is invalid (shared by both pollers below)

  // ---- page toggle: all three pages are permanent siblings, switching
  // only ever flips `hidden` -- never rebuilds any page's DOM, so the
  // browsing grid's iframes (see updateTileVideo's "write src exactly
  // once" comment) are untouched by a trip to the dashboard/emails page
  // and back. ----
  function showPage(name) {{
    main.hidden = name !== "browsing";
    dashboardPage.hidden = name !== "dashboard";
    emailsPage.hidden = name !== "emails";
    if (pageToggle) {{
      var btns = pageToggle.querySelectorAll(".toggle-btn");
      for (var i = 0; i < btns.length; i++) {{
        btns[i].classList.toggle("active", btns[i].getAttribute("data-page") === name);
      }}
    }}
  }}
  if (pageToggle) {{
    pageToggle.addEventListener("click", function (event) {{
      var btn = event.target.closest(".toggle-btn");
      if (!btn) return;
      showPage(btn.getAttribute("data-page"));
    }});
  }}

  function clearTiles() {{
    tileEls = {{}};
  }}

  function showIdle() {{
    dot.classList.remove("live");
    tileCountEl.textContent = "";
    if (shownState === "idle") return;
    shownState = "idle";
    clearTiles();
    main.innerHTML =
      '<div class="state"><div class="card">' +
      '<div class="icon">&#128065;</div>' +
      '<h1>No browsing session right now</h1>' +
      '<p>Messa will show you the browser here the moment she starts checking ' +
      'something for you. This page updates on its own -- no need to refresh.</p>' +
      '</div></div>';
  }}

  function showClosing() {{
    // Proactive hand-off (see live_activity.set_closing/server.py's
    // `closing` field): the backend sets this the instant the run is done
    // but *before* the Browserbase session is actually released, so this
    // replaces every tile with one clean "wrapping up" card well before
    // Browserbase's own embedded pages would otherwise render their
    // "Debugging connection was closed" banners as the CDP connection
    // tears down.
    dot.classList.remove("live");
    tileCountEl.textContent = "";
    if (shownState === "closing") return;
    shownState = "closing";
    clearTiles();
    main.innerHTML =
      '<div class="state"><div class="closing-card">' +
      '<span class="spinner"></span>' +
      '<span class="label">Compiling your results&hellip;</span>' +
      '</div></div>';
  }}

  function showInvalid() {{
    dot.classList.remove("live");
    tileCountEl.textContent = "";
    if (shownState === "invalid") return;
    shownState = "invalid";
    clearTiles();
    main.innerHTML =
      '<div class="state"><div class="card">' +
      '<div class="icon">&#128683;</div>' +
      '<h1>This link isn&rsquo;t valid</h1>' +
      '<p>Double check the link Messa sent you, or ask her to resend it.</p>' +
      '</div></div>';
  }}

  function ensureGridShown() {{
    if (shownState === "grid") return;
    shownState = "grid";
    clearTiles();
    main.innerHTML = '<div class="grid-wrap"><div class="grid" id="grid"></div></div>';
  }}

  function buildTile(tile) {{
    var root = document.createElement("div");
    root.className = "tile tile-enter";
    root.innerHTML =
      '<div class="tile-head">' +
        '<span class="dot"></span>' +
        '<span class="heading"></span>' +
        '<span class="waiting-pill" hidden></span>' +
      '</div>' +
      '<div class="tile-desc"></div>' +
      '<div class="tile-video"></div>' +
      '<div class="tile-log"></div>';
    var refs = {{
      root: root,
      heading: root.querySelector(".heading"),
      waiting: root.querySelector(".waiting-pill"),
      desc: root.querySelector(".tile-desc"),
      video: root.querySelector(".tile-video"),
      log: root.querySelector(".tile-log"),
      // Deliberately NOT initialized to `null` here: a tile's live_view_url
      // legitimately starts out `null` too (still connecting), and
      // updateTileVideo below skips re-rendering when the new value equals
      // currentSrc -- starting both at `null` would mean that FIRST real
      // "still connecting" render never happens at all (silently blank
      // instead of showing the connecting placeholder). `undefined` never
      // equals `null`, so the first call always proceeds.
      currentSrc: undefined,
      iframe: null,      // the live <iframe>, once one exists -- matched
                          // against postMessage's event.source to know
                          // WHICH tile just disconnected (see
                          // handleDisconnectMessage below)
      disconnected: false,
    }};
    return refs;
  }}

  function updateTileVideo(refs, liveViewUrl) {{
    // Once a tile has a live, still-connected iframe, its src is written
    // EXACTLY ONCE for that tile's whole lifetime and never touched again
    // here, no matter what url shows up on a later poll. This was the real
    // cause of "each tab going white and reloading every few seconds":
    // Browserbase's own live-view url for the very same tab isn't
    // guaranteed to come back byte-identical on every GET .../debug call
    // (it can carry its own per-request token), so the old dedup check
    // (`liveViewUrl === refs.currentSrc`) treated that as a brand new tab
    // and rebuilt the iframe from scratch on every single poll -- a full
    // reload of an otherwise perfectly healthy connection, every ~4s.
    // Browserbase's live view is a genuinely LIVE stream once it's loaded
    // (it reflects whatever that tab is doing right now, including
    // in-tab navigation) -- it was never something that needed refreshing
    // on our polling cadence in the first place, only something that needed
    // to be created once. A real reconnect after this point only ever
    // happens two ways: showTileDisconnected (a real
    // "browserbase-disconnected" postMessage) sets refs.iframe back to
    // null, or this tile's id disappears from a poll and a fresh buildTile
    // call (a genuinely new tab) hands back a brand new refs object with no
    // iframe yet -- both correctly fall through to the rebuild below.
    if (refs.iframe && !refs.disconnected) return;

    if (liveViewUrl === refs.currentSrc) return;
    refs.currentSrc = liveViewUrl;
    refs.disconnected = false;
    refs.iframe = null;
    if (!liveViewUrl) {{
      refs.video.innerHTML =
        '<div class="connecting"><span class="spinner"></span>' +
        '<span class="label">Connecting&hellip;</span></div>';
      return;
    }}
    var iframe = document.createElement("iframe");
    // allow-same-origin + allow-scripts matches Browserbase's own embedding
    // example (see module docstring) -- without a sandbox at all the frame
    // still loads, but this is the documented shape.
    iframe.setAttribute("sandbox", "allow-same-origin allow-scripts");
    iframe.setAttribute("allow", "clipboard-read; clipboard-write");
    iframe.src = liveViewUrl;
    refs.video.innerHTML = "";
    refs.video.appendChild(iframe);
    refs.iframe = iframe;
  }}

  function showTileDisconnected(refs) {{
    // Browserbase's live-view iframe posts this the moment its session/tab
    // goes away (see module docstring's "Handling disconnects") and would
    // otherwise render its own raw "could not connect"-style page inside
    // the iframe -- replaced here with a calm, blank, unmistakably-Messa
    // placeholder instead, per explicit ask ("disconnected screen shows
    // error loading is not good to show for the users, we need to show a
    // blank messa screen"). A tile in this state that's still present on
    // the next poll (rare -- usually the tab just closes, and the tile
    // itself is removed by renderGrid) simply stays on this screen; a
    // fresh, different live_view_url arriving would still rebuild it
    // normally via updateTileVideo above.
    if (refs.disconnected) return;
    refs.disconnected = true;
    refs.iframe = null;
    refs.video.innerHTML = '<div class="disconnected"><span class="mark"></span></div>';
  }}

  // One listener for the whole page (not one per iframe) -- matches the
  // event's source window against whichever tile's iframe is still live,
  // so a disconnect on tile 2 never affects tiles 1 or 3.
  window.addEventListener("message", function (event) {{
    if (event.data !== "browserbase-disconnected") return;
    Object.keys(tileEls).forEach(function (id) {{
      var refs = tileEls[id];
      if (refs.iframe && refs.iframe.contentWindow === event.source) {{
        showTileDisconnected(refs);
      }}
    }});
  }});

  function updateTileLog(refs, steps) {{
    var recent = (steps || []).slice(-TILE_LOG_LINES);
    refs.log.innerHTML = "";
    if (recent.length === 0) {{
      var hint = document.createElement("div");
      hint.className = "empty-hint";
      hint.textContent = "Getting started\\u2026";
      refs.log.appendChild(hint);
      return;
    }}
    for (var i = 0; i < recent.length; i++) {{
      var line = document.createElement("div");
      line.className = "line";
      line.textContent = recent[i];
      refs.log.appendChild(line);
    }}
    refs.log.scrollTop = refs.log.scrollHeight;
  }}

  function updateTile(refs, tile) {{
    refs.heading.textContent = tile.heading || "New tab";
    refs.desc.textContent = tile.description || "Working on it\\u2026";
    refs.root.classList.toggle("tile-active", !!tile.active);
    if (tile.waiting_for_human) {{
      refs.waiting.hidden = false;
      refs.waiting.textContent = "Waiting for you";
      refs.waiting.title = tile.waiting_for_human;
    }} else {{
      refs.waiting.hidden = true;
      refs.waiting.removeAttribute("title");
    }}
    updateTileVideo(refs, tile.live_view_url);
    updateTileLog(refs, tile.steps);
  }}

  function renderGrid(tiles) {{
    dot.classList.add("live");
    ensureGridShown();
    var grid = document.getElementById("grid");
    if (!grid) return;

    tileCountEl.textContent = tiles.length > 1 ? tiles.length + " tabs active" : "";

    var seen = {{}};
    tiles.forEach(function (tile, index) {{
      seen[tile.id] = true;
      var refs = tileEls[tile.id];
      if (!refs) {{
        refs = buildTile(tile);
        tileEls[tile.id] = refs;
        grid.appendChild(refs.root);
        // Next frame: drop tile-enter so the CSS transition actually
        // animates from its initial (faded/offset) state instead of
        // snapping straight to final -- browsers coalesce a same-frame
        // class add+remove into a no-op transition otherwise.
        requestAnimationFrame(function () {{
          requestAnimationFrame(function () {{ refs.root.classList.remove("tile-enter"); }});
        }});
      }} else if (grid.children[index] !== refs.root) {{
        // Keep DOM order matching the backend's own (stable, insertion-
        // ordered) tile order -- cheap since this only reorders on an
        // actual add/remove, not every poll.
        grid.insertBefore(refs.root, grid.children[index] || null);
      }}
      updateTile(refs, tile);
    }});

    Object.keys(tileEls).forEach(function (id) {{
      if (seen[id]) return;
      var refs = tileEls[id];
      delete tileEls[id];
      refs.root.classList.add("tile-exit");
      setTimeout(function () {{
        if (refs.root.parentNode) refs.root.parentNode.removeChild(refs.root);
      }}, 320);
    }});
  }}

  function poll() {{
    if (stopped) return;
    fetch("/live/" + TOKEN + "/status", {{cache: "no-store"}})
      .then(function (res) {{
        if (res.status === 404) {{
          stopped = true;
          showInvalid();
          return;
        }}
        return res.json().then(function (data) {{
          if (!data.active) {{
            showIdle();
          }} else if (data.closing) {{
            showClosing();
          }} else {{
            renderGrid(data.tiles || []);
          }}
        }});
      }})
      .catch(function () {{ /* transient network hiccup -- next tick retries */ }})
      .then(function () {{
        if (!stopped) setTimeout(poll, 4000);
      }});
  }}

  // ---------------------------------------------------------------------
  // Dashboard page: reminders, this week's schedule, tasks, projects, and
  // contacts -- the user's own data, independent of browsing state (see
  // server.py's /live/<token>/dashboard and the module docstring above).
  // ---------------------------------------------------------------------
  var dashShownState = "loading"; // "loading" | "invalid" | "grid"
  var dashWeekOffset = 0; // weeks forward(+)/back(-) from the current week --
                           // the schedule card's prev/next arrows change
                           // this and refetch immediately (see changeWeek/
                           // goToToday below); server.py's own
                           // DASHBOARD_WEEK_OFFSET_MAX bounds it further.
  var openDays = {{}}; // date string -> bool: remembers which schedule days
                       // the user expanded/collapsed across polls, since
                       // renderDashboard rebuilds the whole card every poll
                       // (this data is small and changes slowly enough that
                       // a full rebuild is simpler than reconciling it the
                       // way the browsing grid's tiles are, and would
                       // otherwise silently re-close whatever the user just
                       // opened out from under them every 30s)

  function showDashboardLoading() {{
    if (dashShownState === "loading") return;
    dashShownState = "loading";
    dashboardPage.innerHTML =
      '<div class="state"><div class="card">' +
      '<div class="icon">&#8230;</div>' +
      '<h1>Loading&hellip;</h1>' +
      '<p>Fetching your tasks, reminders, and schedule.</p>' +
      '</div></div>';
  }}

  function showDashboardInvalid() {{
    if (dashShownState === "invalid") return;
    dashShownState = "invalid";
    dashboardPage.innerHTML =
      '<div class="state"><div class="card">' +
      '<div class="icon">&#128683;</div>' +
      '<h1>This link isn&rsquo;t valid</h1>' +
      '<p>Double check the link Messa sent you, or ask her to resend it.</p>' +
      '</div></div>';
  }}

  function ensureDashboardGridShown() {{
    if (dashShownState === "grid") return;
    dashShownState = "grid";
    dashboardPage.innerHTML = '<div class="dash-wrap"><div class="dash-grid" id="dashGrid"></div></div>';
  }}

  function dashCard(title, extraClass) {{
    var card = document.createElement("div");
    card.className = "dash-card" + (extraClass ? " " + extraClass : "");
    var head = document.createElement("div");
    head.className = "dash-card-head";
    head.textContent = title;
    var body = document.createElement("div");
    body.className = "dash-card-body";
    card.appendChild(head);
    card.appendChild(body);
    return {{ card: card, head: head, body: body }};
  }}

  function dashEmpty(text) {{
    var el = document.createElement("div");
    el.className = "dash-empty";
    el.textContent = text;
    return el;
  }}

  function buildScheduleCard(week) {{
    var built = dashCard("", "schedule");
    built.head.textContent = ""; // clear the plain-text title dashCard set -- built manually below so the nav buttons can sit alongside it
    var titleSpan = document.createElement("span");
    titleSpan.className = "dash-card-title-text";
    titleSpan.textContent = (week && week.label) || "This week";
    built.head.appendChild(titleSpan);

    var nav = document.createElement("span");
    nav.className = "week-nav";
    var prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "week-nav-btn";
    prevBtn.setAttribute("aria-label", "Previous week");
    prevBtn.textContent = "\\u2039";
    prevBtn.addEventListener("click", function () {{ changeWeek(-1); }});
    nav.appendChild(prevBtn);
    if (dashWeekOffset !== 0) {{
      var todayBtn = document.createElement("button");
      todayBtn.type = "button";
      todayBtn.className = "week-nav-btn week-nav-today";
      todayBtn.textContent = "Today";
      todayBtn.addEventListener("click", function () {{ goToToday(); }});
      nav.appendChild(todayBtn);
    }}
    var nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "week-nav-btn";
    nextBtn.setAttribute("aria-label", "Next week");
    nextBtn.textContent = "\\u203a";
    nextBtn.addEventListener("click", function () {{ changeWeek(1); }});
    nav.appendChild(nextBtn);
    built.head.appendChild(nav);

    var list = document.createElement("div");
    list.className = "day-list";
    var days = (week && week.days) || [];
    days.forEach(function (day) {{
      var row = document.createElement("details");
      row.className = "day-row" + (day.is_today ? " today" : "");
      var hasOverride = Object.prototype.hasOwnProperty.call(openDays, day.date);
      row.open = hasOverride ? openDays[day.date] : !!day.is_today;
      row.addEventListener("toggle", function () {{ openDays[day.date] = row.open; }});

      var summary = document.createElement("summary");
      var label = document.createElement("span");
      label.className = "day-label";
      label.textContent = day.label + (day.is_today ? " \\u2022 today" : "");
      var count = document.createElement("span");
      count.className = "count";
      var n = (day.events || []).length;
      count.textContent = n === 0 ? "" : (n === 1 ? "1 event" : n + " events");
      summary.appendChild(label);
      summary.appendChild(count);
      row.appendChild(summary);

      var events = document.createElement("div");
      events.className = "events";
      if (!day.events || day.events.length === 0) {{
        events.appendChild(dashEmpty("Nothing scheduled."));
      }} else {{
        day.events.forEach(function (ev) {{
          var er = document.createElement("div");
          er.className = "event-row";
          var time = document.createElement("div");
          time.className = "time";
          time.textContent = ev.time + (ev.end_time ? "\\u2013" + ev.end_time : "");
          var info = document.createElement("div");
          info.className = "info";
          var title = document.createElement("div");
          title.textContent = ev.title;
          info.appendChild(title);
          if (ev.location) {{
            var loc = document.createElement("div");
            loc.className = "loc";
            loc.textContent = ev.location;
            info.appendChild(loc);
          }}
          er.appendChild(time);
          er.appendChild(info);
          events.appendChild(er);
        }});
      }}
      row.appendChild(events);
      list.appendChild(row);
    }});
    built.body.appendChild(list);
    return built.card;
  }}

  function dashItem(primaryText, metaText) {{
    var row = document.createElement("div");
    row.className = "dash-item";
    var primary = document.createElement("span");
    primary.className = "primary";
    primary.textContent = primaryText;
    row.appendChild(primary);
    if (metaText) {{
      var meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = metaText;
      row.appendChild(meta);
    }}
    return row;
  }}

  function buildProfileCard(profileText) {{
    // "What Messa's learned about you" -- a read-only copy of the same
    // digest Messa's own system prompt reads every turn (server.py's
    // /live/<token>/dashboard, users.memory_profile). textContent, never
    // innerHTML: this text is derived from the user's own conversations
    // via an LLM extraction step, not something this page should ever
    // trust as markup. Editing from this page is a noted fast-follow, not
    // built this round -- the product decision was "silent in the
    // backend, show a copy" first.
    var built = dashCard("What I've learned about you");
    var p = document.createElement("div");
    p.className = "dash-profile-text";
    p.textContent = profileText;
    built.body.appendChild(p);
    return built.card;
  }}

  function buildListCard(title, items, renderItem, emptyText) {{
    var built = dashCard(title);
    if (!items || items.length === 0) {{
      built.body.appendChild(dashEmpty(emptyText));
      return built.card;
    }}
    var list = document.createElement("div");
    list.className = "dash-list";
    items.forEach(function (item) {{ list.appendChild(renderItem(item)); }});
    built.body.appendChild(list);
    return built.card;
  }}

  function renderTaskItem(t) {{
    return dashItem(t.title || "Untitled task", [t.priority, t.due].filter(Boolean).join(" \\u00b7 "));
  }}
  function renderReminderItem(r) {{
    return dashItem(r.message || "Reminder", r.time);
  }}
  function renderProjectItem(p) {{
    return dashItem(p.title || "Untitled project", p.status);
  }}
  function renderContactItem(c) {{
    var meta = [c.relationship, c.phone_number, c.email].filter(Boolean).join(" \\u00b7 ");
    return dashItem(c.name || "Contact", meta);
  }}

  function renderDashboard(data) {{
    ensureDashboardGridShown();
    // Keep the client's own offset in sync with whatever the server
    // actually honored (it clamps to DASHBOARD_WEEK_OFFSET_MAX) -- falls
    // back to leaving it alone if an older cached response somehow lacks
    // the field, rather than resetting to 0 under the user.
    if (data.week && typeof data.week.week_offset === "number") {{
      dashWeekOffset = data.week.week_offset;
    }}
    var grid = document.getElementById("dashGrid");
    if (!grid) return;
    grid.innerHTML = "";
    if (data.memory_profile) {{
      grid.appendChild(buildProfileCard(data.memory_profile));
    }}
    grid.appendChild(buildScheduleCard(data.week));
    grid.appendChild(buildListCard("Tasks", data.tasks, renderTaskItem, "No tasks right now."));
    grid.appendChild(buildListCard("Reminders", data.reminders, renderReminderItem, "No reminders pending."));
    grid.appendChild(buildListCard("Projects", data.projects, renderProjectItem, "No active projects."));
    grid.appendChild(buildListCard("Contacts", data.contacts, renderContactItem, "No contacts yet."));
  }}

  function fetchDashboard() {{
    if (stopped) return Promise.resolve();
    return fetch("/live/" + TOKEN + "/dashboard?week_offset=" + dashWeekOffset, {{cache: "no-store"}})
      .then(function (res) {{
        if (res.status === 404) {{
          stopped = true;
          showInvalid();
          showDashboardInvalid();
          return;
        }}
        return res.json().then(function (data) {{ renderDashboard(data); }});
      }})
      .catch(function () {{ /* transient network hiccup -- next tick retries */ }});
  }}

  // Prev/next/Today buttons in the schedule card's own header (see
  // buildScheduleCard) call these directly, so a click refetches right
  // away instead of waiting for the next 30s poll tick.
  function changeWeek(delta) {{
    dashWeekOffset += delta;
    fetchDashboard();
  }}
  function goToToday() {{
    dashWeekOffset = 0;
    fetchDashboard();
  }}

  function pollDashboard() {{
    if (stopped) return;
    fetchDashboard().then(function () {{
      if (!stopped) setTimeout(pollDashboard, DASHBOARD_POLL_MS);
    }});
  }}

  // ---- Emails page: Inbox/Sent list + click-to-open thread view of
  // Messa's OWN email address (server.py's /live/<token>/emails and
  // /live/<token>/emails/thread routes; NOT the user's connected Gmail --
  // that has no equivalent page here). Same untrusted-content discipline
  // as the rest of this file: subject/body/address text can originate
  // from an arbitrary external sender, so every such value below is
  // written with textContent, never innerHTML (see this module's own
  // docstring). ----
  var emailsShownState = "loading"; // "loading" | "invalid" | "list" | "thread"
  var emailsTab = "inbox"; // "inbox" | "sent"
  var emailsQuery = "";
  var EMAILS_PAGE_SIZE = 25;
  var emailsLimit = EMAILS_PAGE_SIZE;
  var emailsActiveThreadId = null;
  var emailsSearchDebounce = null;

  function showEmailsInvalid() {{
    if (emailsShownState === "invalid") return;
    emailsShownState = "invalid";
    emailsPage.innerHTML =
      '<div class="state"><div class="card">' +
      '<div class="icon">&#128683;</div>' +
      '<h1>This link isn&rsquo;t valid</h1>' +
      '<p>Double check the link Messa sent you, or ask her to resend it.</p>' +
      '</div></div>';
  }}

  // Builds the persistent list shell (toolbar + list container) once;
  // later refreshes only touch #emailList/#emailListFooter so the search
  // box never loses focus/its typed value under a background poll tick --
  // same "flip state, don't rebuild" discipline as ensureDashboardGridShown.
  function ensureEmailsShellShown() {{
    if (emailsShownState === "list") return;
    emailsShownState = "list";
    emailsPage.innerHTML =
      '<div class="emails-wrap">' +
        '<div class="emails-toolbar">' +
          '<span class="email-address-chip" id="emailAddressChip"></span>' +
          '<nav class="page-toggle email-tabs" id="emailTabs">' +
            '<button type="button" class="toggle-btn' + (emailsTab === "inbox" ? " active" : "") + '" data-tab="inbox">Inbox</button>' +
            '<button type="button" class="toggle-btn' + (emailsTab === "sent" ? " active" : "") + '" data-tab="sent">Sent</button>' +
          '</nav>' +
          '<input type="search" class="email-search" id="emailSearch" placeholder="Search subject or body…">' +
        '</div>' +
        '<div class="email-list" id="emailList"></div>' +
        '<div id="emailListFooter"></div>' +
      '</div>';
    document.getElementById("emailTabs").addEventListener("click", function (event) {{
      var btn = event.target.closest(".toggle-btn");
      if (!btn) return;
      var tab = btn.getAttribute("data-tab");
      if (tab === emailsTab) return;
      emailsTab = tab;
      emailsLimit = EMAILS_PAGE_SIZE;
      var btns = document.getElementById("emailTabs").querySelectorAll(".toggle-btn");
      for (var i = 0; i < btns.length; i++) {{
        btns[i].classList.toggle("active", btns[i].getAttribute("data-tab") === tab);
      }}
      fetchEmailsList();
    }});
    var searchInput = document.getElementById("emailSearch");
    searchInput.value = emailsQuery;
    searchInput.addEventListener("input", function () {{
      var val = searchInput.value;
      if (emailsSearchDebounce) clearTimeout(emailsSearchDebounce);
      emailsSearchDebounce = setTimeout(function () {{
        emailsQuery = val;
        emailsLimit = EMAILS_PAGE_SIZE;
        fetchEmailsList();
      }}, 350);
    }});
  }}

  function emailRow(msg) {{
    var row = document.createElement("div");
    row.className = "email-row";
    row.tabIndex = 0;
    row.addEventListener("click", function () {{ openEmailThread(msg.thread_id); }});
    row.addEventListener("keydown", function (event) {{
      if (event.key === "Enter" || event.key === " ") {{ event.preventDefault(); openEmailThread(msg.thread_id); }}
    }});

    var mainCol = document.createElement("div");
    mainCol.className = "email-row-main";

    var top = document.createElement("div");
    top.className = "email-row-top";
    var counterpart = document.createElement("span");
    counterpart.className = "email-row-counterpart";
    counterpart.textContent = msg.direction === "inbound" ? msg.from_address : msg.to_address;
    top.appendChild(counterpart);
    if (msg.sent_autonomously) {{
      var auto = document.createElement("span");
      auto.className = "email-badge autonomous";
      auto.textContent = "Auto";
      auto.title = "Messa sent this on her own judgment, without checking with you first";
      top.appendChild(auto);
    }}
    if (msg.attachment_filename) {{
      var clip = document.createElement("span");
      clip.className = "email-badge attachment";
      clip.textContent = "📎";
      clip.title = "Attached: " + msg.attachment_filename;
      top.appendChild(clip);
    }}
    mainCol.appendChild(top);

    var subject = document.createElement("div");
    subject.className = "email-row-subject";
    subject.textContent = msg.subject;
    mainCol.appendChild(subject);

    var snippet = document.createElement("div");
    snippet.className = "email-row-snippet";
    snippet.textContent = msg.snippet;
    mainCol.appendChild(snippet);

    row.appendChild(mainCol);

    var time = document.createElement("div");
    time.className = "email-row-time";
    time.textContent = msg.time;
    row.appendChild(time);

    return row;
  }}

  function renderEmailsList(data) {{
    ensureEmailsShellShown();
    var chip = document.getElementById("emailAddressChip");
    if (chip) chip.textContent = data.messa_email || "";
    var list = document.getElementById("emailList");
    var footer = document.getElementById("emailListFooter");
    if (!list || !footer) return;
    list.innerHTML = "";
    var messages = data.messages || [];
    if (messages.length === 0) {{
      list.appendChild(dashEmpty(emailsTab === "sent" ? "Nothing sent yet." : "No emails yet."));
    }} else {{
      messages.forEach(function (msg) {{ list.appendChild(emailRow(msg)); }});
    }}
    footer.innerHTML = "";
    if (data.has_more) {{
      var more = document.createElement("button");
      more.type = "button";
      more.className = "toggle-btn email-loadmore-btn";
      more.textContent = "Load more";
      more.addEventListener("click", function () {{
        emailsLimit += EMAILS_PAGE_SIZE;
        fetchEmailsList();
      }});
      footer.appendChild(more);
    }}
  }}

  function emailThreadMsg(msg) {{
    var card = document.createElement("div");
    card.className = "email-thread-msg " + (msg.direction === "outbound" ? "outbound" : "inbound");

    var head = document.createElement("div");
    head.className = "email-thread-msg-head";
    var who = document.createElement("span");
    who.textContent = (msg.direction === "inbound" ? "From " : "To ") +
      (msg.direction === "inbound" ? msg.from_address : msg.to_address);
    head.appendChild(who);

    var badges = document.createElement("span");
    badges.className = "email-thread-msg-badges";
    if (msg.sent_autonomously) {{
      var auto = document.createElement("span");
      auto.className = "email-badge autonomous";
      auto.textContent = "Auto";
      auto.title = "Messa sent this on her own judgment, without checking with you first";
      badges.appendChild(auto);
    }}
    if (msg.attachment_filename) {{
      var clip = document.createElement("span");
      clip.className = "email-badge attachment";
      clip.textContent = "📎 " + msg.attachment_filename;
      badges.appendChild(clip);
    }}
    var time = document.createElement("span");
    time.textContent = msg.time;
    badges.appendChild(time);
    head.appendChild(badges);
    card.appendChild(head);

    var subject = document.createElement("div");
    subject.className = "email-thread-msg-subject";
    subject.textContent = msg.subject;
    card.appendChild(subject);

    var body = document.createElement("div");
    body.className = "email-thread-msg-body";
    body.textContent = msg.body;
    card.appendChild(body);

    return card;
  }}

  function renderEmailThread(data) {{
    emailsShownState = "thread";
    emailsPage.innerHTML =
      '<div class="emails-wrap">' +
        '<button type="button" class="email-thread-back" id="emailThreadBack">&larr; Back to list</button>' +
        '<div class="email-thread" id="emailThread"></div>' +
      '</div>';
    document.getElementById("emailThreadBack").addEventListener("click", function () {{
      emailsActiveThreadId = null;
      emailsShownState = "loading"; // forces ensureEmailsShellShown to rebuild the list shell
      fetchEmailsList();
    }});
    var container = document.getElementById("emailThread");
    var messages = data.messages || [];
    if (messages.length === 0) {{
      container.appendChild(dashEmpty("No messages in this thread."));
    }} else {{
      messages.forEach(function (msg) {{ container.appendChild(emailThreadMsg(msg)); }});
    }}
  }}

  function fetchEmailThread(threadId) {{
    return fetch("/live/" + TOKEN + "/emails/thread?thread_id=" + encodeURIComponent(threadId), {{cache: "no-store"}})
      .then(function (res) {{
        if (res.status === 404) {{
          stopped = true;
          showInvalid();
          showDashboardInvalid();
          showEmailsInvalid();
          return null;
        }}
        return res.json();
      }});
  }}

  function openEmailThread(threadId) {{
    if (stopped || !threadId) return;
    emailsActiveThreadId = threadId;
    fetchEmailThread(threadId)
      .then(function (data) {{ if (data) renderEmailThread(data); }})
      .catch(function () {{ /* transient -- the list stays showing, the click can be retried */ }});
  }}

  function fetchEmailsList() {{
    if (stopped) return Promise.resolve();
    var url = "/live/" + TOKEN + "/emails?direction=" + encodeURIComponent(emailsTab) +
      "&limit=" + emailsLimit + "&offset=0";
    if (emailsQuery) url += "&q=" + encodeURIComponent(emailsQuery);
    return fetch(url, {{cache: "no-store"}})
      .then(function (res) {{
        if (res.status === 404) {{
          stopped = true;
          showInvalid();
          showDashboardInvalid();
          showEmailsInvalid();
          return;
        }}
        return res.json().then(function (data) {{
          if (emailsShownState !== "thread") renderEmailsList(data);
        }});
      }})
      .catch(function () {{ /* transient network hiccup -- next tick retries */ }});
  }}

  function pollEmails() {{
    if (stopped) return;
    var work = emailsActiveThreadId
      ? fetchEmailThread(emailsActiveThreadId)
          .then(function (data) {{ if (data && emailsShownState === "thread") renderEmailThread(data); }})
          .catch(function () {{}})
      : fetchEmailsList();
    work.then(function () {{ if (!stopped) setTimeout(pollEmails, DASHBOARD_POLL_MS); }});
  }}

  poll();
  pollDashboard();
  pollEmails();
}})();
</script>
</body>
</html>
"""
