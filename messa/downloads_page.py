"""Renders the public Messa Companion Android APK download page at
GET /downloads and GET /download (see server.py).

Provides a fast, mobile-friendly landing page where users can download the
Messa Companion APK directly onto their Android phone, complete with
step-by-step pairing instructions and security transparency details.
"""
from __future__ import annotations

import html


def render_downloads_page() -> str:
    """Returns the full downloads page HTML, ready for HTMLResponse."""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Download Messa Companion for Android | textmessa.com</title>
<meta name="description" content="Download the open-source Messa Companion APK for Android. Automate your phone over SMS text with zero VPN subscriptions.">
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0b0b0d; color: #e6e6e6; padding: 48px 16px 80px; line-height: 1.6;
  }
  .wrap { max-width: 680px; margin: 0 auto; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px; background: #182235; color: #60a5fa;
    padding: 4px 12px; border-radius: 9999px; font-size: 13px; font-weight: 600; border: 1px solid #1e3a8a;
    margin-bottom: 20px;
  }
  h1 { font-size: 32px; font-weight: 800; margin: 0 0 10px; letter-spacing: -0.02em; color: #ffffff; }
  .subtitle { color: #a1a1aa; font-size: 16px; margin: 0 0 32px; line-height: 1.5; }
  
  .download-box {
    background: linear-gradient(180deg, #18181b 0%, #111113 100%);
    border: 1px solid #27272a; border-radius: 16px; padding: 28px 24px; text-align: center;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4); margin-bottom: 36px;
  }
  .btn-download {
    display: inline-flex; align-items: center; justify-content: center; gap: 10px;
    background: #2563eb; color: #ffffff; text-decoration: none; padding: 14px 28px;
    border-radius: 10px; font-size: 16px; font-weight: 700; transition: background 0.15s ease, transform 0.1s ease;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.4); width: 100%; max-width: 380px;
  }
  .btn-download:hover { background: #1d4ed8; transform: translateY(-1px); }
  .btn-download:active { transform: translateY(1px); }
  .download-meta { font-size: 12.5px; color: #71717a; margin-top: 14px; }
  
  h2 { font-size: 20px; font-weight: 700; margin: 36px 0 18px; color: #f4f4f5; }
  
  .steps { display: flex; flex-direction: column; gap: 14px; }
  .step-card {
    background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 18px 20px;
    display: flex; gap: 16px; align-items: flex-start;
  }
  .step-num {
    background: #27272a; color: #e4e4e7; font-weight: 700; width: 28px; height: 28px;
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-size: 14px; flex-shrink: 0; margin-top: 2px;
  }
  .step-body h3 { margin: 0 0 4px; font-size: 15px; font-weight: 600; color: #fafafa; }
  .step-body p { margin: 0; font-size: 14px; color: #a1a1aa; line-height: 1.45; }
  .code-pill {
    display: inline-block; background: #27272a; color: #38bdf8; padding: 2px 8px;
    border-radius: 6px; font-family: ui-monospace, monospace; font-size: 13px; margin-top: 6px;
  }
  
  .features-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-top: 14px;
  }
  .feature-item {
    background: #131316; border: 1px solid #222226; border-radius: 10px; padding: 16px;
  }
  .feature-item h4 { margin: 0 0 4px; font-size: 14px; font-weight: 600; color: #e4e4e7; }
  .feature-item p { margin: 0; font-size: 13px; color: #71717a; line-height: 1.4; }
  
  footer {
    text-align: center; color: #52525b; font-size: 13px; margin-top: 48px; border-top: 1px solid #1f1f23;
    padding-top: 24px;
  }
  footer a { color: #71717a; text-decoration: none; margin: 0 8px; }
  footer a:hover { color: #a1a1aa; }
</style>
</head>
<body>
<div class="wrap">
  <div class="badge">🤖 Open-Source Phone • BYOP</div>
  <h1>Messa Companion for Android</h1>
  <p class="subtitle">Control and automate your personal Android phone directly over SMS text with zero third-party VPN subscriptions.</p>

  <div class="download-box">
    <a href="/download/companion.apk" class="btn-download">
      <span>📱</span> Download APK (v0.1.0)
    </a>
    <div class="download-meta">Requires Android 8.0+ (Oreo) or later • Standalone Debug Build</div>
  </div>

  <h2>Quick Setup (Under 1 Minute)</h2>
  <div class="steps">
    <div class="step-card">
      <div class="step-num">1</div>
      <div class="step-body">
        <h3>Install the APK</h3>
        <p>Tap the download button above on your Android phone. When prompted, tap <strong>Open</strong> and allow installation from unknown sources.</p>
      </div>
    </div>
    <div class="step-card">
      <div class="step-num">2</div>
      <div class="step-body">
        <h3>Enable Wireless Debugging</h3>
        <p>Go to your phone's <strong>Settings &gt; Developer Options</strong> and toggle <strong>Wireless Debugging</strong> to <strong>ON</strong> (make sure your phone is connected to Wi-Fi).</p>
      </div>
    </div>
    <div class="step-card">
      <div class="step-num">3</div>
      <div class="step-body">
        <h3>Text the Pairing Code</h3>
        <p>Open Messa Companion. It auto-discovers your phone's local debugging port and displays a 6-digit code:</p>
        <div class="code-pill">PAIR 123-456</div>
        <p style="margin-top: 6px;">Send that exact text to your Messa number from your personal phone. You're immediately connected!</p>
      </div>
    </div>
  </div>

  <h2>Security & Architecture</h2>
  <div class="features-grid">
    <div class="feature-item">
      <h4>🔒 Zero Inbound Ports</h4>
      <p>The companion app never opens any listening server port. It only makes outbound TLS WebSocket calls to Messa.</p>
    </div>
    <div class="feature-item">
      <h4>⚡ $0 Ongoing Cost</h4>
      <p>Bypasses commercial VPNs like Tailscale entirely. Direct reverse-tunneling hosted natively by Messa.</p>
    </div>
    <div class="feature-item">
      <h4>🛑 Touch Killswitch</h4>
      <p>Physical touch on your phone screen during an automated task immediately aborts automation and returns Home.</p>
    </div>
    <div class="feature-item">
      <h4>🔑 Keystore-Backed Secret</h4>
      <p>The device secret is generated on-device and stored in hardware-backed EncryptedSharedPreferences.</p>
    </div>
  </div>

  <footer>
    <a href="https://textmessa.com">textmessa.com</a> &middot;
    <a href="/skills">Community Skills</a> &middot;
    <a href="/privacy">Privacy Policy</a>
  </footer>
</div>
</body>
</html>"""
