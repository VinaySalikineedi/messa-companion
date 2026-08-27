"""The public, tokenized /live/<token> page (Phase 3): a permanent per-user
link, texted alongside every deepsearch delegation (see agents/registry.py's
live_view_str), that shows Browserbase's live iframe while a browsing task
is running and a calm "nothing happening right now" state otherwise.

Kept as one self-contained HTML/CSS/JS string (no build step, no external
assets -- this needs to render correctly from a bare `GET`, including from
whatever in-app browser iMessage/SMS opens links in) rather than a template
file, matching how small this project keeps its other single-purpose
surfaces. The page itself never talks to Postgres -- it only ever calls its
own `/live/<token>/status` JSON endpoint (see server.py), polling every few
seconds so it flips from idle to live (and back) with no manual refresh,
and now also carries `description` (what deepsearch is doing *right now*,
one line) and `steps` (the running chain-of-thought log for *this* task
only -- both come from messa/live_activity.py, an in-memory, per-user log
that tools/deepsearch_tools.py's guarded tool-call wrapper writes to on
every browser action, and clears the moment the task's browser closes).

Two visual skins, same markup/JS, different CSS -- so you can compare them
side by side before picking one:

  - "polished": a soft-gradient background behind a single lifted, rounded
    card -- description above the video, chain-of-thought below, closest
    to a normal consumer product surface.
  - "terminal": black background, monospace, a fake terminal titlebar,
    "$ "-prefixed description/log lines, a blinking cursor on the last log
    line -- gritty/dev-tool looking, same information, different mood.

LIVE_VIEW_STYLE below is "the variable you can change": flip it and
redeploy to switch the *default* skin. For quick side-by-side comparisons
without redeploying, append `?style=polished` or `?style=terminal` to any
live link (see server.py's live_view_page route) -- that overrides the
default for just that one page load, nothing is stored.

Security note: `token` is only ever a value this module itself generates
(`secrets.token_urlsafe`, see db.get_or_create_live_share_token) -- alphanumeric
plus '-'/'_' only, never user-supplied free text -- so it's safe to embed
directly into the JS below with no HTML-escaping needed. Nothing else here
does string interpolation of untrusted data: descriptions, chain-of-thought
lines, task titles, and the Browserbase URL are all fetched client-side via
JSON and written with textContent/`.src` (never innerHTML), so none of that
server-generated-but-ultimately-model-influenced text can inject anything.
"""
from __future__ import annotations

# "The variable you can change" (see module docstring): pick which skin
# renders by default when a live link is opened with no ?style= override.
LIVE_VIEW_STYLE = "terminal"  # "polished" | "terminal"

_VALID_STYLES = ("polished", "terminal")


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

  /* ---- "polished" skin: soft gradient canvas, one lifted rounded card ---- */
  [data-style="polished"] {{
    --bg: #eef3f6;
    --bg-gradient: radial-gradient(circle at 28% 18%, #e4f7ef 0%, #eef3f6 42%, #edf0f8 100%);
    --panel: #ffffff;
    --card-bg: #ffffff;
    --border: #e2e8f0;
    --text: #1a2233;
    --muted: #6b7686;
    --accent: #4f8cff;
    --live: #ff5566;
    --radius: 20px;
    --shadow: 0 24px 64px -24px rgba(31, 45, 61, 0.35), 0 4px 16px rgba(31, 45, 61, 0.08);
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  @media (prefers-color-scheme: dark) {{
    [data-style="polished"] {{
      --bg: #0b0d12;
      --bg-gradient: radial-gradient(circle at 28% 18%, #16241f 0%, #0b0d12 42%, #0d0f16 100%);
      --panel: #14171f;
      --card-bg: #161a23;
      --border: #262b38;
      --text: #e7e9ee;
      --muted: #8b93a7;
      --shadow: 0 24px 64px -24px rgba(0, 0, 0, 0.6), 0 4px 16px rgba(0, 0, 0, 0.3);
    }}
  }}

  /* ---- "terminal" skin: black, monospace, gritty, always dark ---- */
  [data-style="terminal"] {{
    --bg: #0a0a0a;
    --bg-gradient: #0a0a0a;
    --panel: #000000;
    --card-bg: #050505;
    --border: #1f2a1f;
    --text: #c9f2d0;
    --muted: #5f8f68;
    --accent: #39ff88;
    --live: #ff5566;
    --radius: 4px;
    --shadow: 0 0 0 1px rgba(57, 255, 136, 0.15), 0 0 40px rgba(57, 255, 136, 0.06);
    --font: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  }}

  html, body {{ height: 100%; margin: 0; }}
  body {{
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
  }}
  header {{
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.9rem 1.1rem;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }}
  header .brand {{ font-weight: 600; letter-spacing: 0.01em; color: var(--text); }}
  [data-style="terminal"] header .brand::before {{ content: "> "; color: var(--muted); }}
  .dot {{
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--muted);
    flex: 0 0 auto;
  }}
  [data-style="terminal"] .dot {{ border-radius: 2px; }}
  .dot.live {{
    background: var(--live);
    box-shadow: 0 0 0 0 rgba(255, 85, 102, 0.6);
    animation: pulse 1.6s infinite;
  }}
  @keyframes pulse {{
    0%   {{ box-shadow: 0 0 0 0 rgba(255, 85, 102, 0.55); }}
    70%  {{ box-shadow: 0 0 0 9px rgba(255, 85, 102, 0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(255, 85, 102, 0); }}
  }}

  main {{
    flex: 1 1 auto;
    position: relative;
    display: flex;
    background: var(--bg-gradient);
  }}

  /* ---- idle / invalid states ---- */
  .state {{
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }}
  .state .card {{ text-align: center; max-width: 26rem; }}
  .state h1 {{ font-size: 1.15rem; margin: 0 0 0.5rem; color: var(--text); }}
  [data-style="terminal"] .state h1::before {{ content: "$ "; color: var(--accent); }}
  .state p {{ color: var(--muted); margin: 0; line-height: 1.5; font-size: 0.95rem; }}
  .state .icon {{ font-size: 2rem; margin-bottom: 0.75rem; opacity: 0.8; }}
  [data-style="terminal"] .state .icon {{ display: none; }}

  /* ---- active state: the lifted card (description / video / chain-of-thought) ---- */
  .stage {{
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1.4rem;
    overflow: hidden;
  }}
  .stage .card {{
    width: 100%;
    max-width: 960px;
    height: 100%;
    max-height: 820px;
    display: flex;
    flex-direction: column;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
  }}
  .titlebar {{ display: none; }}
  [data-style="terminal"] .titlebar {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0.55rem 0.75rem;
    background: #111;
    border-bottom: 1px solid var(--border);
    flex: 0 0 auto;
  }}
  .tb-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
  .tb-dot.r {{ background: #ff5f56; }}
  .tb-dot.y {{ background: #ffbd2e; }}
  .tb-dot.g {{ background: #27c93f; }}
  .tb-path {{ margin-left: 8px; color: #6b7280; font-size: 0.75rem; }}

  .desc {{
    flex: 0 0 auto;
    padding: 0.85rem 1.1rem;
    font-size: 0.92rem;
    color: var(--text);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    background: var(--card-bg);
  }}
  [data-style="polished"] .desc {{ font-weight: 500; }}
  [data-style="polished"] .desc::before {{
    content: "";
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
    margin-right: 0.55rem;
    animation: pulse-soft 1.6s infinite;
  }}
  @keyframes pulse-soft {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.3; }}
  }}
  [data-style="terminal"] .desc {{ color: var(--accent); }}
  [data-style="terminal"] .desc::before {{ content: "$ "; color: var(--muted); }}

  .video-wrap {{ flex: 1 1 auto; position: relative; background: #000; min-height: 0; }}
  .video-wrap iframe {{ position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }}

  .log {{
    flex: 0 0 auto;
    max-height: 34%;
    overflow-y: auto;
    padding: 0.55rem 1.1rem;
    font-size: 0.8rem;
    line-height: 1.65;
    color: var(--muted);
    border-top: 1px solid var(--border);
    background: var(--panel);
  }}
  .log:empty {{ display: none; }}
  .log .line {{ white-space: pre-wrap; word-break: break-word; }}
  [data-style="polished"] .log .line::before {{ content: "\\2022  "; color: var(--accent); }}
  [data-style="terminal"] .log {{ color: var(--text); }}
  [data-style="terminal"] .log .line::before {{ content: "> "; color: var(--accent); }}
  [data-style="terminal"] .log .line:last-child::after {{
    content: "\\2588";
    color: var(--accent);
    margin-left: 2px;
    animation: blink 1s step-end infinite;
  }}
  @keyframes blink {{ 50% {{ opacity: 0; }} }}

  footer {{
    flex: 0 0 auto;
    padding: 0.6rem 1.1rem;
    text-align: center;
    color: var(--muted);
    font-size: 0.75rem;
    border-top: 1px solid var(--border);
    background: var(--panel);
  }}
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <span class="brand">Messa</span>
  </header>
  <main id="main">
    <div class="state">
      <div class="card">
        <div class="icon">&#8230;</div>
        <h1>Loading&hellip;</h1>
        <p>Checking whether a browsing session is live.</p>
      </div>
    </div>
  </main>
  <footer>textmessa.com &middot; this link is permanent, bookmark it</footer>

<script>
(function () {{
  var TOKEN = "{token}";
  var main = document.getElementById("main");
  var dot = document.getElementById("dot");
  // Tracks what's *currently rendered* in #main -- "idle", "invalid", or
  // "active:<liveViewUrl>" -- so each render function only touches the DOM
  // when the state actually changed, instead of every 4s poll tick. This is
  // NOT the same thing as "have we ever rendered anything yet": the page
  // starts on the static "Loading..." markup already in the HTML, which
  // isn't any of these three states, so shownState starts at null. That
  // distinction matters -- a plain "was a URL set before" check (what an
  // earlier version of this page used) can't tell "still loading" apart
  // from "confirmed idle," so the very first idle result -- a brand new
  // link, or one visited before its first-ever deepsearch run -- would
  // never overwrite the loading spinner at all.
  var shownState = null;
  var stepsShown = 0;      // how many chain-of-thought lines are already
                            // rendered, so each poll only appends new ones
  var stopped = false;     // true once we've confirmed the token is invalid

  function showIdle() {{
    dot.classList.remove("live");
    if (shownState === "idle") return;
    shownState = "idle";
    stepsShown = 0;
    main.innerHTML =
      '<div class="state"><div class="card">' +
      '<div class="icon">&#128065;</div>' +
      '<h1>No browsing session right now</h1>' +
      '<p>Messa will show you the browser here the moment she starts checking ' +
      'something for you. This page updates on its own -- no need to refresh.</p>' +
      '</div></div>';
  }}

  function ensureStage(liveViewUrl) {{
    var key = "active:" + liveViewUrl;
    if (shownState === key) return;
    shownState = key;
    stepsShown = 0;
    main.innerHTML =
      '<div class="stage"><div class="card">' +
      '<div class="titlebar"><span class="tb-dot r"></span><span class="tb-dot y"></span>' +
      '<span class="tb-dot g"></span><span class="tb-path">deepsearch &mdash; live</span></div>' +
      '<div class="desc" id="desc"></div>' +
      '<div class="video-wrap"><iframe id="frame" allow="clipboard-read; clipboard-write"></iframe></div>' +
      '<div class="log" id="log"></div>' +
      '</div></div>';
    document.getElementById("frame").src = liveViewUrl;
  }}

  function showActive(data) {{
    dot.classList.add("live");
    ensureStage(data.live_view_url);
    var descEl = document.getElementById("desc");
    if (descEl) descEl.textContent = data.description || "Working on it\\u2026";
    var logEl = document.getElementById("log");
    var steps = data.steps || [];
    if (logEl) {{
      for (var i = stepsShown; i < steps.length; i++) {{
        var line = document.createElement("div");
        line.className = "line";
        line.textContent = steps[i];
        logEl.appendChild(line);
      }}
      stepsShown = steps.length;
      logEl.scrollTop = logEl.scrollHeight;
    }}
  }}

  function showInvalid() {{
    dot.classList.remove("live");
    if (shownState === "invalid") return;
    shownState = "invalid";
    main.innerHTML =
      '<div class="state"><div class="card">' +
      '<div class="icon">&#128683;</div>' +
      '<h1>This link isn&rsquo;t valid</h1>' +
      '<p>Double check the link Messa sent you, or ask her to resend it.</p>' +
      '</div></div>';
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
          if (data.active) {{
            showActive(data);
          }} else {{
            showIdle();
          }}
        }});
      }})
      .catch(function () {{ /* transient network hiccup -- next tick retries */ }})
      .then(function () {{
        if (!stopped) setTimeout(poll, 4000);
      }});
  }}

  poll();
}})();
</script>
</body>
</html>
"""
