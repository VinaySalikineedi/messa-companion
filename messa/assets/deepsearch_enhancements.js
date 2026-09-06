// Injected into every page deepsearch opens via @playwright/mcp's
// --init-script flag, same mechanism and same file-per-feature pattern as
// cursor_overlay.js (see tools/deepsearch_tools.py's _spawn_mcp_http_server,
// which passes both paths together when their respective config flags are
// on -- --init-script accepts multiple paths). Whether this file gets
// injected at all is controlled by config.DEEPSEARCH_AUTO_DISMISS_CONSENT
// on the Python side, not by anything in here.
//
// "Faster Than Human" branch (see faster-than-human.md, Upgrade 4): auto-
// dismisses cookie/consent banners the instant they mount, so the agent
// doesn't have to spend a whole browser_snapshot + browser_click round trip
// on every single site just to get a banner out of the way before the real
// task can start.
//
// DELIBERATELY CONSERVATIVE, on purpose. A false positive here (clicking
// the wrong thing) is a real, silent action taken on the user's behalf with
// no chance for the agent to reconsider -- much worse than just leaving a
// banner up for the agent to dismiss the normal, visible way. So this only
// ever auto-clicks an element when BOTH of the following hold:
//   1. TEXT: the element's own visible text is short (<=40 chars) and is
//      one of a small, hand-picked set of affirmative cookie-consent
//      phrases ("accept all", "i agree", "got it", etc.) -- never a
//      "reject"/"decline"/"manage preferences" style option (this script
//      only ever takes the fast path Messa would take anyway; it never
//      makes a privacy CHOICE on the user's behalf that they didn't already
//      imply by using deepsearch at all).
//   2. CONTEXT: the element sits inside a container that actually looks
//      like a consent banner -- an ancestor whose id/class/aria-label
//      matches known cookie-consent vocabulary (onetrust, cookiebot, "cc-
//      window", "cookie-banner", etc.), OR an ancestor that's fixed/sticky-
//      positioned near the top or bottom of the viewport (the near-
//      universal CSS shape of a cookie banner, even a custom-built one with
//      no recognizable class name).
// Text match alone is NOT enough -- plenty of real page content contains
// the word "agree" or "accept" with no relation to cookies at all.
(() => {
  if (window.__messaConsentDismiss) return; // already installed on this page

  const ACCEPT_PHRASES = [
    "accept all", "accept all cookies", "accept cookies", "accept & close",
    "allow all", "allow all cookies", "i agree", "agree and close",
    "agree & close", "got it", "i understand", "ok, got it", "allow cookies",
    "accept", "agree", "allow",
  ];
  // Longer/more-specific phrases first so a short generic one ("accept")
  // never wins a match that a more specific phrase should have.
  ACCEPT_PHRASES.sort((a, b) => b.length - a.length);

  const CONTAINER_KEYWORD_RE = /cookie|consent|gdpr|ccpa|onetrust|cookiebot|cookie-notice|cookie-banner|cc-window|cc-banner|privacy-?choice|truste/i;
  const CLICKABLE_SELECTOR = 'button, a[role="button"], [role="button"], input[type="button"], input[type="submit"]';

  const clicked = new WeakSet();

  function normalizedText(el) {
    const t = (el.innerText || el.textContent || el.value || "").trim().toLowerCase();
    return t.replace(/\s+/g, " ");
  }

  function matchesAcceptPhrase(text) {
    if (!text || text.length > 40) return false;
    for (const phrase of ACCEPT_PHRASES) {
      if (text === phrase || text === phrase + "." || text.startsWith(phrase + " ")) return true;
    }
    return false;
  }

  function looksLikeConsentContainer(el) {
    let node = el;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      const idClass = ((node.id || "") + " " + (node.className || "") + " " + (node.getAttribute?.("aria-label") || ""));
      if (CONTAINER_KEYWORD_RE.test(idClass)) return true;
      try {
        const style = window.getComputedStyle(node);
        if (style && (style.position === "fixed" || style.position === "sticky")) {
          const rect = node.getBoundingClientRect();
          // Near the top or bottom of the viewport, and wide -- the near-
          // universal shape of a cookie banner (a full-width bar or a
          // corner/bottom card), as opposed to some small fixed widget
          // (a chat bubble, a "back to top" button) elsewhere on the page.
          const nearEdge = rect.top < 140 || rect.bottom > window.innerHeight - 140;
          const wideEnough = rect.width > window.innerWidth * 0.3;
          if (nearEdge && wideEnough) return true;
        }
      } catch (e) {
        // getComputedStyle can throw on a detached/foreign node -- ignore
        // and keep climbing, this is a best-effort heuristic either way.
      }
    }
    return false;
  }

  function scan() {
    try {
      const candidates = document.querySelectorAll(CLICKABLE_SELECTOR);
      for (const el of candidates) {
        if (clicked.has(el)) continue;
        const text = normalizedText(el);
        if (!matchesAcceptPhrase(text)) continue;
        if (!looksLikeConsentContainer(el)) continue;
        clicked.add(el);
        window.__messaConsentDismiss.lastClicked = text;
        window.__messaConsentDismiss.dismissedCount++;
        el.click();
        // One dismissal per scan pass is enough -- most sites show exactly
        // one banner, and clicking again immediately (before the DOM
        // updates) risks hitting a second, unrelated match that only
        // *looks* eligible because the banner hasn't animated out yet.
        return;
      }
    } catch (e) {
      // Never let this take the page (or the rest of this init script)
      // down -- this is a pure nice-to-have.
    }
  }

  window.__messaConsentDismiss = { dismissedCount: 0, lastClicked: null, scanNow: scan };

  const runInitialScan = () => scan();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", runInitialScan, { once: true });
  } else {
    runInitialScan();
  }

  // Many consent banners mount asynchronously (a third-party script loads
  // after the main page), so one scan on load isn't enough -- watch for new
  // nodes and re-scan, throttled to at most once per animation frame so a
  // mutation-heavy SPA doesn't pay for a full querySelectorAll on every tiny
  // DOM change.
  let scanQueued = false;
  const throttledScan = () => {
    if (scanQueued) return;
    scanQueued = true;
    requestAnimationFrame(() => {
      scanQueued = false;
      scan();
    });
  };
  try {
    const observeTarget = document.documentElement || document.body;
    if (observeTarget && window.MutationObserver) {
      new MutationObserver(throttledScan).observe(observeTarget, { childList: true, subtree: true });
    }
  } catch (e) {
    // Best-effort -- the initial scan above still covers the common case
    // of a banner that's already in the DOM by the time this script runs.
  }
})();
