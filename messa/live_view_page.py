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
seconds so it flips from idle to live (and back) with no manual refresh.

Security note: `token` is only ever a value this module itself generates
(`secrets.token_urlsafe`, see db.get_or_create_live_share_token) -- alphanumeric
plus '-'/'_' only, never user-supplied free text -- so it's safe to embed
directly into the JS below with no HTML-escaping needed. Nothing else here
does string interpolation of untrusted data: task titles and the Browserbase
URL are fetched client-side via JSON and written with textContent/`.src`
(never innerHTML), so a task title containing HTML can't inject anything.
"""
from __future__ import annotations


def render_live_view_page(token: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Messa -- live browsing</title>
<style>
  :root {{
    --bg: #0b0d12;
    --panel: #14171f;
    --border: #262b38;
    --text: #e7e9ee;
    --muted: #8b93a7;
    --accent: #7c8cff;
    --live: #ff5566;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    height: 100%;
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  body {{
    display: flex;
    flex-direction: column;
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
  header .brand {{
    font-weight: 600;
    letter-spacing: 0.01em;
  }}
  header .task {{
    color: var(--muted);
    font-size: 0.9rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1 1 auto;
  }}
  .dot {{
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--muted);
    flex: 0 0 auto;
  }}
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
  }}
  iframe {{
    flex: 1 1 auto;
    width: 100%;
    height: 100%;
    border: 0;
    background: #000;
  }}
  .state {{
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }}
  .state .card {{
    text-align: center;
    max-width: 26rem;
  }}
  .state h1 {{
    font-size: 1.15rem;
    margin: 0 0 0.5rem;
  }}
  .state p {{
    color: var(--muted);
    margin: 0;
    line-height: 1.5;
    font-size: 0.95rem;
  }}
  .state .icon {{
    font-size: 2rem;
    margin-bottom: 0.75rem;
    opacity: 0.8;
  }}
  footer {{
    flex: 0 0 auto;
    padding: 0.6rem 1.1rem;
    text-align: center;
    color: var(--muted);
    font-size: 0.75rem;
    border-top: 1px solid var(--border);
    background: var(--panel);
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f5f6fa;
      --panel: #ffffff;
      --border: #e3e6ee;
      --text: #1a1d27;
      --muted: #6b7280;
    }}
  }}
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <span class="brand">Messa</span>
    <span class="task" id="task"></span>
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
  var taskEl = document.getElementById("task");
  var currentUrl = null;   // the iframe src we're currently showing, so we
                            // don't reload the iframe every poll tick
  var stopped = false;     // true once we've confirmed the token is invalid

  function showIdle() {{
    dot.classList.remove("live");
    taskEl.textContent = "";
    if (currentUrl !== null) {{
      currentUrl = null;
      main.innerHTML =
        '<div class="state"><div class="card">' +
        '<div class="icon">&#128065;</div>' +
        '<h1>No browsing session right now</h1>' +
        '<p>Messa will show you the browser here the moment she starts checking ' +
        'something for you. This page updates on its own -- no need to refresh.</p>' +
        '</div></div>';
    }}
  }}

  function showActive(liveViewUrl, task) {{
    dot.classList.add("live");
    taskEl.textContent = task || "Browsing";
    if (currentUrl !== liveViewUrl) {{
      currentUrl = liveViewUrl;
      main.innerHTML = "";
      var iframe = document.createElement("iframe");
      iframe.setAttribute("allow", "clipboard-read; clipboard-write");
      iframe.src = liveViewUrl;
      main.appendChild(iframe);
    }}
  }}

  function showInvalid() {{
    dot.classList.remove("live");
    taskEl.textContent = "";
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
            showActive(data.live_view_url, data.task);
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
