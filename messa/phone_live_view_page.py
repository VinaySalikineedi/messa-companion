"""The public, tokenized /live/<token>/phone page: the Open-Source Phone
(BYOP) sibling of call_live_view_page.py's /live/<token>/call page (see
that module's own docstring for the shared reasoning -- one self-contained
HTML/CSS/JS string, no build step, no external assets, must render from a
bare GET including an in-app SMS/iMessage browser).

open-source-phone.md section 5 describes this as a tile integrated
directly into the existing multi-tab live_view_page.py dashboard. Built as
a separate, dedicated page instead -- same deliberate deviation
call_live_view_page.py already made for the voice-calling feature, and for
the same reason: live_view_page.py's own JS is tightly coupled to
Browserbase's multi-tab tile grid (real browser pages[], live-view iframe
urls), none of which applies to a single physical phone's screen. Reusing
its tile-grid machinery to shoehorn in one non-iframe, non-Browserbase
image tile would be a bigger, riskier change than standing up one more
small sibling page under the same /live/<token>/... namespace, which is
exactly the pattern this codebase already chose once for calls.

Shows: a live status line, the running "here's what I'm doing" thought
(phone_activity.py, updated after every planner decision), the current
screen as a polled screenshot (device_manager.take_screenshot, pulled
fresh on every poll -- open-source-phone.md's "2-3 frames/sec" is a
soft target; this polls every ~1.5s, which is plenty for watching taps
land without hammering the device or the tunnel), a milestone-confirm
prompt when the task is waiting on the user, and the "Pause / Abort"
break-glass button (server.py's POST /live/{token}/phone/abort) that
immediately presses the Android Home key and releases the device.
"""
from __future__ import annotations


def render_phone_live_view_page(token: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Messa -- live phone</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0b0b0d; color: #e6e6e6; padding: 24px 16px;
  }}
  .wrap {{ max-width: 420px; margin: 0 auto; }}
  h1 {{ font-size: 16px; font-weight: 600; color: #9a9a9a; text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 16px; }}
  .card {{ background: #17171a; border: 1px solid #2a2a2e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
  .status-line {{ display: flex; align-items: center; gap: 10px; font-size: 15px; margin-bottom: 4px; }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; background: #555; flex-shrink: 0; }}
  .dot.live {{ background: #34c759; box-shadow: 0 0 0 4px rgba(52,199,89,0.2); }}
  .dot.waiting {{ background: #ff9f0a; box-shadow: 0 0 0 4px rgba(255,159,10,0.2); }}
  .goal {{ color: #9a9a9a; font-size: 13px; margin-top: 6px; }}
  .thought {{ font-size: 13px; color: #c7c7c7; margin-top: 10px; font-style: italic; }}
  .idle {{ color: #6a6a6e; font-size: 14px; text-align: center; padding: 24px 0; }}
  .invalid {{ color: #ff6b6b; text-align: center; padding: 40px 0; }}
  .screen-frame {{ margin-top: 14px; border-radius: 10px; overflow: hidden; border: 1px solid #2a2a2e;
    background: #000; display: flex; align-items: center; justify-content: center; min-height: 200px; }}
  .screen-frame img {{ width: 100%; display: block; }}
  .waiting-box {{ margin-top: 14px; background: #241c0e; border: 1px solid #4a3a12; border-radius: 8px;
    padding: 12px; font-size: 13.5px; color: #ffd479; }}
  button#abort {{ background: #3a1414; color: #ff8a8a; border: 1px solid #5a1f1f; border-radius: 8px;
    padding: 10px 16px; font-size: 13px; cursor: pointer; margin-top: 14px; width: 100%; font-weight: 600; }}
  button#abort:active {{ opacity: 0.7; }}
  button#abort:disabled {{ opacity: 0.4; cursor: default; }}
  .steps {{ font-size: 11.5px; color: #6a6a6e; margin-top: 8px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Live phone</h1>
  <div id="content" class="card"><div class="idle">Loading...</div></div>
</div>
<script>
(function() {{
  var TOKEN = {token!r};
  var content = document.getElementById('content');
  var lastActive = false;

  function el(tag, cls, text) {{
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }}

  function renderInvalid() {{
    content.innerHTML = '';
    content.appendChild(el('div', 'invalid', "This link isn't valid."));
  }}

  function renderIdle() {{
    content.innerHTML = '';
    var row = el('div', 'status-line');
    row.appendChild(el('div', 'dot'));
    row.appendChild(el('span', null, 'No phone task in progress'));
    content.appendChild(row);
    content.appendChild(el('div', 'idle', "You'll see this update the moment a task starts."));
    lastActive = false;
  }}

  function renderActive(data) {{
    content.innerHTML = '';
    var waiting = !!data.waiting_prompt;
    var row = el('div', 'status-line');
    row.appendChild(el('div', 'dot ' + (waiting ? 'waiting' : 'live')));
    row.appendChild(el('span', null, waiting ? 'Waiting on you' : (data.status || 'in progress')));
    content.appendChild(row);
    if (data.goal) content.appendChild(el('div', 'goal', data.goal));
    if (data.thought) content.appendChild(el('div', 'thought', data.thought));

    var frame = el('div', 'screen-frame');
    var img = el('img');
    img.alt = 'Phone screen';
    img.src = '/live/' + TOKEN + '/phone/frame?t=' + Date.now();
    img.onerror = function() {{ frame.innerHTML = ''; frame.appendChild(el('div', 'idle', 'Screen not available yet.')); }};
    frame.appendChild(img);
    content.appendChild(frame);

    if (waiting) {{
      var box = el('div', 'waiting-box', data.waiting_prompt);
      content.appendChild(box);
    }}

    content.appendChild(el('div', 'steps', (data.steps_taken || 0) + ' step(s) so far'));

    var btn = el('button', null, 'Pause / Abort');
    btn.id = 'abort';
    btn.onclick = function() {{
      btn.disabled = true;
      btn.textContent = 'Stopping...';
      fetch('/live/' + TOKEN + '/phone/abort', {{ method: 'POST' }})
        .then(function() {{ poll(); }})
        .catch(function() {{ btn.disabled = false; btn.textContent = 'Pause / Abort'; }});
    }};
    content.appendChild(btn);

    lastActive = true;
  }}

  async function poll() {{
    try {{
      var resp = await fetch('/live/' + TOKEN + '/phone/status');
      if (resp.status === 404) {{ renderInvalid(); return; }}
      var data = await resp.json();
      if (data.active) {{ renderActive(data); }} else {{ renderIdle(); }}
    }} catch (e) {{ /* transient network hiccup -- keep the last render, try again next tick */ }}
  }}

  poll();
  setInterval(poll, 1500);
}})();
</script>
</body>
</html>"""
