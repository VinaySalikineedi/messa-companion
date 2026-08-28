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
  el.setAttribute("width", "28");
  el.setAttribute("height", "28");
  el.setAttribute("viewBox", "0 0 28 28");
  el.style.position = "fixed";
  el.style.top = "0";
  el.style.left = "0";
  el.style.zIndex = "2147483647";
  el.style.pointerEvents = "none";
  el.style.filter = "drop-shadow(0 1px 2px rgba(0,0,0,0.5))";
  el.style.transition = "transform 220ms ease-in-out";
  el.style.transform = "translate(-9999px, -9999px)"; // start off-screen, hidden
  el.innerHTML =
    '<path d="M2 1 L2 21 L7.5 16.5 L11 24 L14 22.5 L10.5 15 L18 15 Z" ' +
    'fill="#3b82f6" stroke="white" stroke-width="1.2" stroke-linejoin="round"/>';

  function ensureMounted() {
    if (!el.isConnected) {
      (document.body || document.documentElement).appendChild(el);
    }
  }

  // Moves the arrow's tip to (x, y) in viewport coordinates and returns a
  // Promise that resolves once the CSS transition finishes (or after a
  // fixed fallback delay, in case transitionend never fires -- e.g. the
  // element was already at that exact position). browser_evaluate awaits
  // this, so the agent's next action (the real click) waits for the
  // animation to actually be visible first.
  function moveTo(x, y) {
    ensureMounted();
    el.style.transform = `translate(${x}px, ${y}px)`;
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        resolve();
      };
      el.addEventListener("transitionend", finish, { once: true });
      setTimeout(finish, 260);
    });
  }

  window.__messaCursor = { moveTo };
})();
