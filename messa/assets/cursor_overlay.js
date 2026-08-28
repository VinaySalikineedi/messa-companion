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
  el.setAttribute("width", "56");
  el.setAttribute("height", "56");
  el.setAttribute("viewBox", "0 0 56 56");
  el.style.position = "fixed";
  el.style.top = "0";
  el.style.left = "0";
  el.style.zIndex = "2147483647";
  el.style.pointerEvents = "none";
  el.style.filter = "drop-shadow(0 0 8px rgba(57, 255, 136, 0.5)) drop-shadow(0 4px 10px rgba(0,0,0,0.7))";
  el.style.transition = "transform 380ms cubic-bezier(0.25, 1, 0.5, 1)";
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

  function setCursorTransform(x, y, scale = 2.7) {
    lastX = x;
    lastY = y;
    el.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  }

  function startIdleBreathing() {
    if (idlePulseInterval) clearInterval(idlePulseInterval);
    // Every 2.4s while idle (e.g. AI reading page/thinking), gently drift +/- 12px
    idlePulseInterval = setInterval(() => {
      const offsetX = Math.floor(Math.random() * 24) - 12;
      const offsetY = Math.floor(Math.random() * 24) - 12;
      setCursorTransform(lastX + offsetX, lastY + offsetY, 2.7);
    }, 2400);
  }

  function ensureMounted() {
    if (!el.isConnected) {
      (document.body || document.documentElement).appendChild(el);
      const spot = getRestingSpot(currentSpotIndex);
      setCursorTransform(spot.x, spot.y, 2.7);
      startIdleBreathing();
    }
  }

  function returnToRestingSpot() {
    ensureMounted();
    currentSpotIndex = (currentSpotIndex + 1) % 5;
    const spot = getRestingSpot(currentSpotIndex);
    setCursorTransform(spot.x, spot.y, 2.7);
    startIdleBreathing();
  }

  function moveTo(x, y) {
    ensureMounted();
    if (resetTimer) clearTimeout(resetTimer);
    if (idlePulseInterval) clearInterval(idlePulseInterval);

    setCursorTransform(x, y, 2.7);

    // Pulse down slightly to simulate a click press
    setTimeout(() => {
      setCursorTransform(x, y, 2.1);
      setTimeout(() => {
        setCursorTransform(x, y, 2.7);
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
