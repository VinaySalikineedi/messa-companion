// Injected into every page deepsearch opens via @playwright/mcp's
// --init-script flag (see tools/deepsearch_tools.py's BrowserToolProvider).
// Draws a small floating SVG cursor arrow that BrowserToolProvider._move_cursor_to
// animates to the target of an upcoming click/type/hover/select, so the live
// view (which streams this page's actual rendered frames) shows where the
// agent is about to interact instead of things happening with no visible
// pointer at all. Deliberately NOT ghost-cursor-style human-motion
// simulation (bezier paths, per-frame jitter) -- a single CSS transition is
// the "cheap" version of this feature: one visible, smooth move per action,
// costing one extra tool round-trip plus the transition's own ~250ms, not
// several hundred ms to seconds of simulated human wandering.
//
// Runs before any of the page's own scripts (that's what --init-script
// guarantees), so `window.__messaCursor` is guaranteed to exist by the time
// _move_cursor_to's browser_evaluate call runs next.
(() => {
  // Hides this page's own scrollbars from the live view -- Browserbase's
  // docs recommend exactly this (inject CSS via the page rather than a
  // live-view query param, since there's no param for it) -- see
  // https://docs.browserbase.com/platform/browser/observability/session-live-view.
  // Separate idempotency guard from window.__messaCursor below: this part
  // should always run whenever this script runs at all (cursor overlay OR
  // reading animation on -- see tools/deepsearch_tools.py's
  // _spawn_mcp_http_server), regardless of which of those two features is
  // what actually triggered injecting this file.
  //
  // FOUND WHILE INVESTIGATING "the cursor never appears in the live view":
  // this used to append unconditionally, assuming document.head or
  // document.documentElement is always available by the time an
  // --init-script/addInitScript-injected script runs. It ISN'T --
  // confirmed directly (see /home/claude session notes: a real headless
  // Chromium run via Playwright's own addInitScript threw "Cannot read
  // properties of null (reading 'appendChild')" from this exact line on a
  // fresh navigation) -- addInitScript fires before the document has a
  // documentElement at all on some navigations. Because this was the FIRST
  // statement in the FIRST top-level IIFE in the file, that uncaught throw
  // aborted the entire script's evaluation right here, before the cursor
  // SVG was ever built or window.__messaCursor ever got set -- meaning the
  // whole cosmetic overlay (arrow, click ripple, typing highlight, page-
  // transition flash, reading animation, ALL of it) silently failed to
  // install on every single page, unconditionally. This is almost
  // certainly the real root cause of the human-cursor never visibly
  // appearing, independent of and more fundamental than which delivery
  // mechanism (the old real-cursor-driver's own addInitScript call, or
  // @playwright/mcp's --init-script flag) was used -- both would hit the
  // identical crash, since it's the same file either way. Fixed by
  // deferring the append until document.head/documentElement genuinely
  // exists, same DOMContentLoaded-fallback pattern already used below for
  // mounting the cursor SVG itself.
  if (!window.__messaScrollbarHidden) {
    window.__messaScrollbarHidden = true;
    const style = document.createElement("style");
    style.textContent =
      "html { scrollbar-width: none; }" +
      "html::-webkit-scrollbar { width: 0; height: 0; display: none; }";
    const mountScrollbarStyle = () => {
      const target = document.head || document.documentElement;
      if (target) target.appendChild(style);
    };
    if (document.head || document.documentElement) {
      mountScrollbarStyle();
    } else {
      document.addEventListener("DOMContentLoaded", mountScrollbarStyle, { once: true });
    }
  }

  if (window.__messaCursor) return; // already installed on this page

  const SVG_NS = "http://www.w3.org/2000/svg";
  const el = document.createElementNS(SVG_NS, "svg");
  el.setAttribute("width", "56");
  el.setAttribute("height", "56");
  el.setAttribute("viewBox", "0 0 56 56");
  el.style.position = "fixed";
  el.style.top = "0";
  el.style.left = "0";
  el.style.zIndex = "2147483647";
  el.style.pointerEvents = "none";
  el.style.filter = "drop-shadow(0 0 8px rgba(57, 255, 136, 0.5)) drop-shadow(0 4px 10px rgba(0,0,0,0.7))";
  const TRANSITION_ON = "transform 380ms cubic-bezier(0.25, 1, 0.5, 1)";
  el.style.transition = TRANSITION_ON;
  el.innerHTML =
    '<path d="M 3 3 L 50 20 L 32 32 L 20 50 Z" ' +
    'fill="#39ff88" stroke="#000000" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>';

  let currentSpotIndex = 0;
  let resetTimer = null;
  let idlePulseInterval = null;
  let lastX = 120;
  let lastY = 120;

  function getRestingSpot(index) {
    const w = window.innerWidth || 1280;
    const h = window.innerHeight || 800;
    const spots = [
      { x: w - 160, y: h - 140 }, // 0: Bottom Right
      { x: 120, y: h - 140 },     // 1: Bottom Left
      { x: w - 160, y: Math.floor(h / 2) }, // 2: Middle Right
      { x: 120, y: Math.floor(h / 2) },     // 3: Middle Left
      { x: w - 200, y: 100 }     // 4: Top Right
    ];
    return spots[index % spots.length];
  }

  // scale 0.65 (0.5 during the click-pulse) -- was 2.7/2.1, which rendered
  // the 56x56 base SVG at ~151px on the real 1280x800 browser viewport
  // (nearly 12% of its width, confirmed oversized -- explicit user
  // feedback). 0.65 renders at ~36px, proportionate for a viewport this
  // size while still being clearly visible on the live view.
  function setCursorTransform(x, y, scale = 0.65, instant = false) {
    lastX = x;
    lastY = y;
    // instant=true (real mouse events, see below) skips the CSS transition:
    // human-cursor already dispatches dozens of intermediate mousemove
    // events per bezier-path move (confirmed empirically, see
    // /tmp/test_cursor_driver_live.py's history -- ~50+ events for one
    // on-screen move), so the motion is already smooth from the real event
    // stream itself; layering the transition on TOP of that would make the
    // arrow visibly lag half a step behind where the real cursor actually
    // is. The idle-breathing/resting-spot drift below (JS-driven, not from
    // real events) keeps using the transition -- that's still just two
    // widely-spaced endpoints, which is exactly what the transition is for.
    el.style.transition = instant ? "none" : TRANSITION_ON;
    el.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  }

  function startIdleBreathing() {
    if (idlePulseInterval) clearInterval(idlePulseInterval);
    // Every 2.4s while idle (e.g. AI reading page/thinking), gently drift +/- 12px
    idlePulseInterval = setInterval(() => {
      const offsetX = Math.floor(Math.random() * 24) - 12;
      const offsetY = Math.floor(Math.random() * 24) - 12;
      setCursorTransform(lastX + offsetX, lastY + offsetY, 0.65);
    }, 2400);
  }

  function ensureMounted() {
    if (!el.isConnected) {
      (document.body || document.documentElement).appendChild(el);
      const spot = getRestingSpot(currentSpotIndex);
      setCursorTransform(spot.x, spot.y, 0.65);
      startIdleBreathing();
    }
  }

  function returnToRestingSpot() {
    ensureMounted();
    currentSpotIndex = (currentSpotIndex + 1) % 5;
    const spot = getRestingSpot(currentSpotIndex);
    setCursorTransform(spot.x, spot.y, 0.65);
    startIdleBreathing();
  }

  function moveTo(x, y) {
    ensureMounted();
    if (resetTimer) clearTimeout(resetTimer);
    if (idlePulseInterval) clearInterval(idlePulseInterval);

    setCursorTransform(x, y, 0.65);

    // Pulse down slightly to simulate a click press
    setTimeout(() => {
      setCursorTransform(x, y, 0.5);
      setTimeout(() => {
        setCursorTransform(x, y, 0.65);
      }, 120);
    }, 180);

    // After action completes, glide back to next idle resting spot
    resetTimer = setTimeout(() => {
      returnToRestingSpot();
    }, 1100);

    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        resolve();
      };
      el.addEventListener("transitionend", finish, { once: true });
      setTimeout(finish, 380);
    });
  }

  // Initial placement on DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureMounted);
  } else {
    ensureMounted();
  }

  window.__messaCursor = { moveTo, returnToRestingSpot };
})();

// ---------------------------------------------------------------------------
// Click ripple + typing highlight -- window.__messaEffects.
//
// Added after a real run showed the drawn cursor arrow above wasn't even
// visible in the live-view tiles users were watching (see the removed
// real-human-cursor driver's own history for the full story) -- these are
// the cheaper, always-visible alternative asked for: an expanding ring
// right where a click landed, and a colored outline on whatever field is
// being typed into. Both are called from the SAME evaluate() call
// BrowserToolProvider._move_cursor_to already fires for browser_click/
// browser_type (see _CURSOR_MOVE_CLICK_FN/_CURSOR_MOVE_TYPE_FN in
// tools/deepsearch_tools.py), which is itself fire-and-forget now (never
// awaited by the real action) -- so neither of these can ever add latency
// to a real click or keystroke, only decorate it a beat later.
(() => {
  if (window.__messaEffects) return;

  function ripple(x, y) {
    const ring = document.createElement("div");
    ring.style.position = "fixed";
    ring.style.left = x + "px";
    ring.style.top = y + "px";
    ring.style.width = "0px";
    ring.style.height = "0px";
    ring.style.border = "3px solid #39ff88";
    ring.style.borderRadius = "50%";
    ring.style.transform = "translate(-50%, -50%)";
    ring.style.pointerEvents = "none";
    ring.style.zIndex = "2147483646"; // one below the cursor arrow itself
    ring.style.opacity = "0.9";
    ring.style.boxShadow = "0 0 12px rgba(57, 255, 136, 0.6)";
    ring.style.transition = "width 420ms ease-out, height 420ms ease-out, opacity 420ms ease-out";
    (document.body || document.documentElement).appendChild(ring);
    // requestAnimationFrame so the browser registers the 0x0 starting
    // state before the transition target below applies -- setting both in
    // the same tick would collapse into no visible transition at all.
    requestAnimationFrame(() => {
      ring.style.width = "64px";
      ring.style.height = "64px";
      ring.style.opacity = "0";
    });
    setTimeout(() => ring.remove(), 500);
  }

  let highlighted = null;
  let highlightPrevOutline = null;
  let highlightPrevOffset = null;
  let highlightTimer = null;

  function clearTypingHighlight() {
    if (highlightTimer) {
      clearTimeout(highlightTimer);
      highlightTimer = null;
    }
    if (highlighted) {
      highlighted.style.outline = highlightPrevOutline || "";
      highlighted.style.outlineOffset = highlightPrevOffset || "";
      highlighted = null;
      highlightPrevOutline = null;
      highlightPrevOffset = null;
    }
  }

  function highlightTyping(element) {
    clearTypingHighlight();
    if (!element) return;
    highlighted = element;
    highlightPrevOutline = element.style.outline;
    highlightPrevOffset = element.style.outlineOffset;
    element.style.outline = "3px solid #39ff88";
    element.style.outlineOffset = "1px";
    // Self-clearing (no separate "typing finished" signal from Python is
    // needed) -- a new highlightTyping() call on a fresh keystroke resets
    // this timer via the clearTypingHighlight() call above, so the
    // highlight stays lit for as long as typing keeps happening and fades
    // shortly after it stops.
    highlightTimer = setTimeout(clearTypingHighlight, 1200);
  }

  window.__messaEffects = { ripple, highlightTyping, clearTypingHighlight };
})();

// ---------------------------------------------------------------------------
// Page-transition flash -- a brief, subtle full-page color wash on every
// fresh document load. This whole init-script file re-runs on every
// navigation (that's what --init-script guarantees), so "runs once per
// script load" already means "runs once per navigation" -- no signal from
// Python needed. Covers the one gap none of the other cosmetic features
// touch: the network/load time DURING a navigate itself, which otherwise
// looks like a frozen page with nothing indicating work is happening.
(() => {
  function flash() {
    const overlay = document.createElement("div");
    overlay.style.position = "fixed";
    overlay.style.inset = "0";
    overlay.style.background = "rgba(57, 255, 136, 0.08)";
    overlay.style.pointerEvents = "none";
    overlay.style.zIndex = "2147483645"; // below both the cursor arrow and ripples
    overlay.style.transition = "opacity 500ms ease-out";
    overlay.style.opacity = "1";
    (document.body || document.documentElement).appendChild(overlay);
    requestAnimationFrame(() => {
      overlay.style.opacity = "0";
    });
    setTimeout(() => overlay.remove(), 600);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", flash);
  } else {
    flash();
  }
})();

// ---------------------------------------------------------------------------
// "Reading" scroll animation -- window.__messaReader.start()/.stop().
//
// Runs while deepsearch is thinking about a browser_snapshot it just took
// (the natural LLM think-time gap that otherwise looks like the page just
// froze). Driven from Python by BrowserToolProvider._start_reading_animation
// / _stop_reading_animation in tools/deepsearch_tools.py: start() is fired
// as one long-lived background browser_evaluate call the instant a snapshot
// succeeds, and stop() is fired (fast, separate call) the instant the
// model's next real action arrives, so the two never fight over the page.
//
// Design, addressing the two things asked for specifically:
//
//   1. "scroll a little, pause, then scroll more" (not one smooth scroll,
//      not an instant jump): each hop to the next piece of content is split
//      into two smaller scrolls with a short pause between them (a glance,
//      then a settle) -- see scrollInTwoHops. Between hops there's a longer
//      "reading" pause, roughly proportional to how much text is in the
//      element being looked at (clamped so it still feels alive on a live
//      view, not literally reading-speed-accurate).
//
//   2. "humans center whatever they're looking at, I don't know how this
//      can be solved": this falls out almost for free once the scroll
//      target is a real content element instead of an arbitrary pixel
//      offset -- picking headings/paragraphs/list items/images in document
//      order and centering each one (targetY math below mirrors
//      scrollIntoView({block:"center"})) *is* what a human's gaze-driven
//      scrolling looks like. The trick isn't a scrolling technique, it's
//      picking WHAT to scroll to.
//
// The cursor overlay is reused (not reinvented) for extra believability --
// each stop nudges window.__messaCursor toward the content just centered,
// so the arrow visibly "tracks" what's being read instead of scrolling
// happening with no pointer on screen at all. Entirely best-effort: any
// failure here (page navigated away, no content matched, etc.) just ends
// the animation quietly -- it must never be able to affect the real task.
(() => {
  if (window.__messaReader) return;

  let stopRequested = false;
  let running = false;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // Real content in document order -- headings, paragraphs, list items,
  // images, table cells, whole articles. Skips tiny/invisible fragments and
  // anything bunched right on top of the previous pick, so the sequence
  // reads as "a handful of distinct things looked at," not "every single
  // node scrolled past."
  function pickContentTargets(maxCount) {
    const selector = "h1, h2, h3, h4, p, li, blockquote, img, article, td";
    const picks = [];
    const nodes = Array.from(document.querySelectorAll(selector));
    let lastBottom = -Infinity;
    for (const node of nodes) {
      const rect = node.getBoundingClientRect();
      if (rect.width < 20 || rect.height < 10) continue; // collapsed/hidden
      const text = (node.innerText || "").trim();
      if (node.tagName !== "IMG" && text.length < 25) continue; // skip tiny fragments
      const docTop = rect.top + window.scrollY;
      if (docTop - lastBottom < 80) continue; // too close to the last pick
      lastBottom = docTop + rect.height;
      picks.push({ node, textLength: text.length || 40 });
      if (picks.length >= maxCount) break;
    }
    return picks;
  }

  function readingPauseFor(textLength) {
    // Deliberately a *visualization* of reading time, not a model of actual
    // reading speed -- clamped so a live view stays watchable either way.
    const ms = 350 + textLength * 6;
    return Math.max(500, Math.min(ms, 2200));
  }

  // The "scroll a little, pause, then scroll more" hop itself: covers
  // 55-70% of the distance, a short pause, then closes the rest -- two
  // visibly distinct motions instead of one smooth glide or a hard cut.
  async function scrollInTwoHops(targetY) {
    const startY = window.scrollY;
    const delta = targetY - startY;
    if (Math.abs(delta) < 4) return; // already there
    const firstHop = startY + delta * (0.55 + Math.random() * 0.15);
    window.scrollTo({ top: firstHop, behavior: "smooth" });
    await sleep(220 + Math.random() * 120);
    if (stopRequested) return;
    window.scrollTo({ top: targetY, behavior: "smooth" });
    await sleep(280 + Math.random() * 140);
  }

  async function start(maxTargets) {
    if (running) return "already-running"; // never stack two animations
    running = true;
    stopRequested = false;
    try {
      const targets = pickContentTargets(maxTargets || 5);
      for (const { node, textLength } of targets) {
        if (stopRequested) break;
        const rect = node.getBoundingClientRect();
        // Same math as element.scrollIntoView({block: "center"}) -- this is
        // the actual answer to the centering question above.
        const targetY = rect.top + window.scrollY - window.innerHeight / 2 + rect.height / 2;
        await scrollInTwoHops(targetY);
        if (stopRequested) break;
        if (window.__messaCursor) {
          const freshRect = node.getBoundingClientRect();
          const cx = freshRect.left + Math.min(freshRect.width, 400) * (0.3 + Math.random() * 0.3);
          const cy = freshRect.top + freshRect.height / 2;
          window.__messaCursor.moveTo(cx, cy);
        }
        await sleep(readingPauseFor(textLength));
      }
      return "done";
    } finally {
      running = false;
    }
  }

  function stop() {
    stopRequested = true;
  }

  window.__messaReader = { start, stop };
})();
