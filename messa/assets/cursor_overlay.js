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
  if (window.__messaCursor) return; // already installed on this page

  const SVG_NS = "http://www.w3.org/2000/svg";
  const el = document.createElementNS(SVG_NS, "svg");
  el.setAttribute("width", "100");
  el.setAttribute("height", "40");
  el.setAttribute("viewBox", "0 0 100 40");
  el.style.position = "fixed";
  el.style.top = "0";
  el.style.left = "0";
  el.style.zIndex = "2147483647";
  el.style.pointerEvents = "none";
  el.style.filter = "drop-shadow(0 0 6px rgba(57, 255, 136, 0.4)) drop-shadow(0 3px 8px rgba(0,0,0,0.6))";
  const TRANSITION_ON = "transform 380ms cubic-bezier(0.25, 1, 0.5, 1)";
  el.style.transition = TRANSITION_ON;
  el.innerHTML =
    '<path d="M 3 3 L 18 16 L 11 16 L 8 22 Z" fill="#39ff88" stroke="#000000" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>' +
    '<rect x="18" y="3" width="48" height="17" rx="5" fill="rgba(6, 10, 18, 0.92)" stroke="#39ff88" stroke-width="1.2"/>' +
    '<text x="42" y="15" fill="#39ff88" font-family="-apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif" font-size="10" font-weight="700" text-anchor="middle" letter-spacing="0.06em">messa</text>';

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

  function setCursorTransform(x, y, scale = 1.0, instant = false) {
    lastX = x;
    lastY = y;
    el.style.transition = instant ? "none" : TRANSITION_ON;
    el.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  }

  function startIdleBreathing() {
    if (idlePulseInterval) clearInterval(idlePulseInterval);
    // Every 2.4s while idle (e.g. AI reading page/thinking), gently drift +/- 12px
    idlePulseInterval = setInterval(() => {
      const offsetX = Math.floor(Math.random() * 24) - 12;
      const offsetY = Math.floor(Math.random() * 24) - 12;
      setCursorTransform(lastX + offsetX, lastY + offsetY, 1.0);
    }, 2400);
  }

  function ensureMounted() {
    if (!el.isConnected) {
      (document.body || document.documentElement).appendChild(el);
      const spot = getRestingSpot(currentSpotIndex);
      setCursorTransform(spot.x, spot.y, 1.0);
      startIdleBreathing();
    }
  }

  function returnToRestingSpot() {
    ensureMounted();
    currentSpotIndex = (currentSpotIndex + 1) % 5;
    const spot = getRestingSpot(currentSpotIndex);
    setCursorTransform(spot.x, spot.y, 1.0);
    startIdleBreathing();
  }

  function moveTo(x, y) {
    ensureMounted();
    if (resetTimer) clearTimeout(resetTimer);
    if (idlePulseInterval) clearInterval(idlePulseInterval);

    setCursorTransform(x, y, 1.0);

    // Pulse down slightly to simulate a click press
    setTimeout(() => {
      setCursorTransform(x, y, 0.85);
      setTimeout(() => {
        setCursorTransform(x, y, 1.0);
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

  // ---------------------------------------------------------------------
  // Real human-cursor mode (config.DEEPSEARCH_HUMAN_CURSOR_DRIVER, see
  // tools/deepsearch_tools.py's CursorDriver/cursor_driver.mjs): when that
  // driver is active, Python never calls window.__messaCursor.moveTo()
  // above at all -- it moves the mouse via a SECOND, independent CDP
  // connection instead, which dispatches genuine mousemove/mousedown/
  // mouseup DOM events on this very page. This arrow needs to follow THOSE
  // events directly rather than sitting there with nothing telling it to
  // move. Both mechanisms coexist harmlessly in this one script (it has no
  // way to know at injection time which mode Python is using, and doesn't
  // need to): if real events never arrive (driver off, or this is the
  // DOM-only fallback), this listener simply never fires and moveTo()
  // above behaves exactly as before.
  // ---------------------------------------------------------------------
  let realEventIdleTimer = null;

  function onRealPointerActivity(x, y, scale) {
    ensureMounted();
    if (idlePulseInterval) { clearInterval(idlePulseInterval); idlePulseInterval = null; }
    if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
    setCursorTransform(x, y, scale, /* instant */ true);
    // No explicit "action finished" signal in a raw DOM event stream the
    // way the old moveTo() path had (it knew when ITS OWN transition
    // ended) -- so use a short quiet period after the last real event as
    // that same signal instead: once real events stop arriving for a
    // moment (the actual click/type, driven separately by @playwright/mcp,
    // has presumably happened by then), glide back to an idle resting spot
    // exactly like moveTo() already does after 1100ms.
    if (realEventIdleTimer) clearTimeout(realEventIdleTimer);
    realEventIdleTimer = setTimeout(returnToRestingSpot, 1100);
  }

  // capture:true, passive:true: listen at the document level regardless of
  // which element the real event actually targets, and never interfere
  // with the page's own event handling (this arrow has pointer-events:none
  // and must stay purely observational).
  document.addEventListener("mousemove", (e) => {
    onRealPointerActivity(e.clientX, e.clientY, 1.0);
  }, { capture: true, passive: true });

  document.addEventListener("mousedown", (e) => {
    onRealPointerActivity(e.clientX, e.clientY, 0.85);
  }, { capture: true, passive: true });

  document.addEventListener("mouseup", (e) => {
    onRealPointerActivity(e.clientX, e.clientY, 1.0);
  }, { capture: true, passive: true });
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
