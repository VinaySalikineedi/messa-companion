"""The public, tokenized /live/<token>/call page: a call-specific sibling
of live_view_page.py's /live/<token> browsing page, for the voice-calling
feature (plans/glowing-forging-pumpkin.md). Shows the current call's status
and a scrubbed running transcript, and -- while a call is actually in
progress -- opens a WebSocket to /live/<token>/call/audio to let the user
LISTEN to their own call live, one-directionally (see server.py's audio
relay route for why the browser never sees Vapi's raw listenUrl directly).

Kept as one self-contained HTML/CSS/JS string, same reasoning as
live_view_page.py's own docstring: no build step, no external assets,
needs to render correctly from a bare GET including an in-app SMS/iMessage
browser. Deliberately much simpler than the browsing page (no multi-tile
grid, no dashboard toggle) -- one call at a time, one status line, one
transcript log, one audio element.

FLAGGED (see messa/channels/vapi.py's own FLAG_FOR_GO_LIVE_VERIFICATION):
the exact audio frame format Vapi's listenUrl actually streams (sample
rate, encoding -- assumed here to be raw PCM16 mono, a common default for
this kind of real-time voice relay) is unverified against a real account.
The client-side playback code below is written defensively (falls back to
silently dropping unplayable frames rather than erroring the page) so a
wrong assumption here degrades to "no audio yet," not a broken page.
"""
from __future__ import annotations


def render_call_live_view_page(token: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Messa -- live call</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0b0b0d; color: #e6e6e6; padding: 24px 16px;
  }}
  .wrap {{ max-width: 560px; margin: 0 auto; }}
  h1 {{ font-size: 16px; font-weight: 600; color: #9a9a9a; text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 16px; }}
  .card {{ background: #17171a; border: 1px solid #2a2a2e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
  .status-line {{ display: flex; align-items: center; gap: 10px; font-size: 15px; margin-bottom: 4px; }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; background: #555; flex-shrink: 0; }}
  .dot.live {{ background: #34c759; box-shadow: 0 0 0 4px rgba(52,199,89,0.2); }}
  .task {{ color: #9a9a9a; font-size: 13px; margin-top: 6px; }}
  .transcript {{ font-family: ui-monospace, monospace; font-size: 12.5px; line-height: 1.6; color: #c7c7c7;
    max-height: 320px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}
  .transcript div {{ padding: 2px 0; border-bottom: 1px solid #232326; }}
  .idle {{ color: #6a6a6e; font-size: 14px; text-align: center; padding: 24px 0; }}
  .invalid {{ color: #ff6b6b; text-align: center; padding: 40px 0; }}
  #audio-note {{ font-size: 12px; color: #6a6a6e; margin-top: 10px; }}
  button#unmute {{ background: #2a2a2e; color: #e6e6e6; border: none; border-radius: 8px; padding: 8px 14px;
    font-size: 13px; cursor: pointer; margin-top: 10px; }}
  button#unmute:active {{ opacity: 0.7; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Live call</h1>
  <div id="content" class="card"><div class="idle">Loading...</div></div>
</div>
<script>
(function() {{
  var TOKEN = {token!r};
  var content = document.getElementById('content');
  var lastCallId = null;
  var audioCtx = null;
  var ws = null;
  var unmuted = false;

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
    row.appendChild(el('span', null, 'No call in progress'));
    content.appendChild(row);
    content.appendChild(el('div', 'idle', "You'll see this update the moment a call starts."));
    stopAudio();
  }}

  function renderActive(data) {{
    content.innerHTML = '';
    var row = el('div', 'status-line');
    var dot = el('div', 'dot live');
    row.appendChild(dot);
    row.appendChild(el('span', null, data.status || 'in progress'));
    content.appendChild(row);
    var label = data.business_name ? (data.business_name + ' -- ' + (data.task_description || '')) : (data.task_description || '');
    if (label) content.appendChild(el('div', 'task', label));

    var note = el('div', null);
    note.id = 'audio-note';
    note.textContent = unmuted ? 'Listening live...' : 'Tap to listen in live.';
    content.appendChild(note);

    if (!unmuted) {{
      var btn = el('button', null, 'Listen live');
      btn.id = 'unmute';
      btn.onclick = function() {{ unmuted = true; startAudio(); }};
      content.appendChild(btn);
    }}

    var transcript = el('div', 'transcript');
    (data.transcript_lines || []).forEach(function(line) {{
      transcript.appendChild(el('div', null, line));
    }});
    content.appendChild(transcript);
    transcript.scrollTop = transcript.scrollHeight;

    if (data.call_id !== lastCallId) {{
      lastCallId = data.call_id;
      if (unmuted) startAudio();
    }}
  }}

  function stopAudio() {{
    if (ws) {{ try {{ ws.close(); }} catch (e) {{}} ws = null; }}
    lastCallId = null;
  }}

  function startAudio() {{
    stopAudio();
    try {{
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    }} catch (e) {{ return; }}
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + '/live/' + TOKEN + '/call/audio');
    ws.binaryType = 'arraybuffer';
    ws.onmessage = function(evt) {{
      // One-directional relay: this client only ever RECEIVES frames, it
      // never sends anything upstream (see server.py's audio relay route
      // for why that's enforced server-side too, not just here).
      // FLAGGED: exact frame format unverified -- assumed raw PCM16 mono.
      try {{
        var buf = evt.data;
        var pcm16 = new Int16Array(buf);
        var floatBuf = audioCtx.createBuffer(1, pcm16.length, 16000);
        var channel = floatBuf.getChannelData(0);
        for (var i = 0; i < pcm16.length; i++) {{ channel[i] = pcm16[i] / 32768; }}
        var src = audioCtx.createBufferSource();
        src.buffer = floatBuf;
        src.connect(audioCtx.destination);
        src.start();
      }} catch (e) {{ /* unplayable frame -- drop it silently, page stays usable */ }}
    }};
    ws.onerror = function() {{}};
    ws.onclose = function() {{}};
  }}

  async function poll() {{
    try {{
      var resp = await fetch('/live/' + TOKEN + '/call/status');
      if (resp.status === 404) {{ renderInvalid(); return; }}
      var data = await resp.json();
      if (data.active) {{ renderActive(data); }} else {{ renderIdle(); }}
    }} catch (e) {{ /* transient network hiccup -- keep the last render, try again next tick */ }}
  }}

  poll();
  setInterval(poll, 3000);
}})();
</script>
</body>
</html>"""
