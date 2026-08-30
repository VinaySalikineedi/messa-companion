// cursor_driver.mjs -- one small, long-lived Node process per deepsearch RUN
// (not per tab), spawned by the top-level BrowserToolProvider alongside
// @playwright/mcp and killed when the run ends (see
// messa/tools/deepsearch_tools.py's CursorDriver class). Drives REAL
// human-cursor bezier-path mouse movement against a browser some other
// process (@playwright/mcp, connected separately) is already driving --
// confirmed viable in /tmp/hc_poc/ before this file was written for real:
// a second, independent CDP connection CAN move the mouse on a page owned
// by a different process, verified via page-side event counters, not just
// "no error was thrown."
//
// Deliberately only ever calls cursor.moveTo() -- never .click() or
// .type(). The actual click/type/etc. still goes through @playwright/mcp's
// own tools; this process ONLY ever moves the mouse to make the motion
// leading up to that action look human, same separation of concerns the
// DOM-drawn cursor_overlay.js had before it (see that file's own comment).
//
// Protocol: JSON Lines over stdin/stdout, request/response correlated by
// `id` (NOT strictly one-at-a-time -- registerTab for one tab and move for
// another can be in flight concurrently, since Node's event loop keeps
// reading stdin while an earlier request is still resolving). One process
// serves every tab in a run: the top-level tab AND every delegate_website_
// task sub-worker tab, each identified by its own `marker`.
//
//   -> {"id": 1, "cmd": "connect", "cdpUrl": "wss://..."}
//   <- {"id": 1, "ok": true}
//
//   -> {"id": 2, "cmd": "registerTab", "marker": "messa-abc123", "timeoutMs": 8000}
//   <- {"id": 2, "ok": true}
//   <- {"id": 2, "ok": false, "error": "no page with that marker appeared in time"}
//
//   -> {"id": 3, "cmd": "move", "marker": "messa-abc123", "x": 412, "y": 88}
//   <- {"id": 3, "ok": true}
//
//   -> {"id": 4, "cmd": "unregisterTab", "marker": "messa-abc123"}
//   <- {"id": 4, "ok": true}
//
//   -> {"id": 5, "cmd": "shutdown"}
//   <- {"id": 5, "ok": true}
//   (process exits after replying)
//
// Every response is exactly one JSON object per line on stdout. Anything
// on stderr is a log line, not part of the protocol -- the Python side
// only ever parses stdout.

import { createInterface } from 'node:readline';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

// createRequire rather than `import { chromium } from 'playwright'`: both
// playwright and human-cursor are CommonJS packages, and require() is the
// exact loading mechanism already proven against these two specific
// packages in /tmp/hc_poc's working POC scripts -- no need to depend on
// Node's CJS/ESM named-export interop working the same way for a package
// this driver has never actually been run against via `import` before.
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const { createCursor } = require('human-cursor');

function log(...args) {
  // stderr only -- see module comment: stdout is the protocol, stdin/stdout
  // must never carry anything but JSON Lines request/response objects.
  console.error('[cursor_driver]', ...args);
}

let browser = null;
let context = null;
// marker -> { page, cursor }
const tabs = new Map();

// Test-only knob (see pipelineMouseMoves's own comment) -- never set in
// production, where mouse.move's real cost against a real remote CDP
// endpoint is already the "network latency" this simulates locally.
const TEST_ARTIFICIAL_DELAY_MS = Number(process.env.MESSA_CURSOR_DRIVER_TEST_DELAY_MS || 0);

// cursor_overlay.js's own source, read once at connect time (see the
// `connect` handler) -- injected directly via page.evaluate() every time a
// tab is (re)registered, NOT via @playwright/mcp's --init-script flag or
// this connection's own context.addInitScript(). Both of those were
// confirmed, by direct local reproduction, to NOT reliably reach a page
// that a DIFFERENT, independent connectOverCDP connection (i.e.
// @playwright/mcp's own, completely separate from this driver's) is the
// one actually navigating -- which is deepsearch's exact real
// architecture, so neither mechanism ever actually got cursor_overlay.js
// (or the reading-animation feature sharing the same file) onto a real
// page in production. A direct, immediate page.evaluate() of the script's
// source, run through THIS driver's own connection right after it finds
// the target page (same discovery findPageByMarker already does), was
// separately confirmed to work reliably regardless of which connection
// later drives that page -- it's not a "run before future scripts"
// registration, it's just running JS in the document that already exists
// right now, which any CDP client attached to that same renderer can do.
let overlaySource = null;

async function findPageByMarker(marker, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const page of context.pages()) {
      try {
        const name = await page.evaluate(() => window.name);
        if (name === marker) return page;
      } catch {
        // Page may be mid-navigation (window.name can reset on a
        // cross-origin navigation) -- just retry on the next sweep rather
        // than treating one failed evaluate() as fatal.
      }
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  return null;
}

// See the `move` command handler's own comment for why this exists. Swaps
// `page.mouse.move` for a wrapper that fires the real CDP call immediately
// but doesn't make the caller (human-cursor's tracePath) wait for it to
// land -- turning N sequential round trips into N pipelined ones. Errors are
// swallowed the same way human-cursor's own tracePath already tolerates a
// failed move (log-and-continue, `page.isClosed()`-guarded) -- since we no
// longer let tracePath's own try/catch see the rejection (it never awaits
// long enough to), we have to replicate that tolerance here instead.
// Returns { settle } (await once all pipelined moves have actually
// completed) and `restore` (put the original page.mouse.move back --
// ALWAYS call this before returning from the `move` handler, success or
// failure, so a later command on this same page/tab doesn't inherit a
// wrapped mouse.move it never asked for).
function pipelineMouseMoves(page) {
  const originalMove = page.mouse.move.bind(page.mouse);
  const inFlight = [];
  page.mouse.move = (x, y, options) => {
    // TEST_ARTIFICIAL_DELAY_MS only: local Chromium round trips are
    // sub-millisecond, which makes it hard for a local test to tell
    // "pipelined" apart from "serial" on wall-clock time alone -- this lets
    // a test stand in a fixed per-call delay for the real network latency a
    // remote Browserbase session would add, so the serial-vs-pipelined
    // difference this whole function exists to produce is actually
    // observable. Unset (the default, always true in production), this is
    // a no-op: `delayMs` is 0 and the `if` below never fires.
    const call = TEST_ARTIFICIAL_DELAY_MS > 0
      ? new Promise((r) => setTimeout(r, TEST_ARTIFICIAL_DELAY_MS)).then(() => originalMove(x, y, options))
      : originalMove(x, y, options);
    const real = call.catch((err) => {
      if (page.isClosed()) return;
      log(`pipelined mouse.move(${x}, ${y}) failed (non-fatal, same tolerance human-cursor's own tracePath has): ${err && err.message || err}`);
    });
    inFlight.push(real);
    // Resolve immediately -- this is the whole trick: tracePath's `await
    // page.mouse.move(...)` moves on to queueing the NEXT point right away
    // instead of blocking on this one's actual network round trip.
    return Promise.resolve();
  };
  return {
    restore: () => { page.mouse.move = originalMove; },
    settle: () => Promise.all(inFlight),
    // For logging/tests only -- final once settle() has resolved.
    count: () => inFlight.length,
  };
}

async function handleCommand(msg) {
  const { id, cmd } = msg;

  if (cmd === 'connect') {
    if (browser) {
      // Idempotent: a reconnect attempt (e.g. a retry from the Python
      // side after a transient failure) shouldn't leave two live browser
      // connections open.
      try { await browser.close(); } catch { /* best-effort */ }
    }
    browser = await chromium.connectOverCDP(msg.cdpUrl);
    const contexts = browser.contexts();
    if (contexts.length === 0) {
      throw new Error('connectOverCDP succeeded but the browser has no contexts');
    }
    context = contexts[0];
    log(`connected to ${msg.cdpUrl}`);

    // Best-effort, kept as a secondary/future-proofing registration --
    // see overlaySource's own comment above for why the REAL, verified
    // injection now happens in registerTab below instead. Harmless either
    // way: cursor_overlay.js's own top-of-file guards make it safe to run
    // more than once on the same document.
    if (msg.initScriptPath) {
      try {
        overlaySource = readFileSync(msg.initScriptPath, 'utf8');
        await context.addInitScript({ path: msg.initScriptPath });
        log(`loaded overlay script (${overlaySource.length} bytes) and registered it as a best-effort context-level init script`);
      } catch (e) {
        log(`failed to load/register overlay script (non-fatal, registerTab's direct evaluate is the real mechanism): ${e && e.message || e}`);
      }
    }
    return {};
  }

  if (cmd === 'registerTab') {
    if (!context) throw new Error('registerTab called before connect');
    // Fast path: this exact marker is already tracked -- meaning this call
    // is Python's post-browser_navigate re-assertion on a tab we already
    // found once (see Python's _assert_cursor_marker, called after EVERY
    // navigate, now awaited rather than fire-and-forget -- see that
    // function's own comment for the bug this fixes). A Playwright Page
    // object stays the same instance across same-tab navigations, so
    // there's no need to re-poll context.pages() seeking a fresh match by
    // window.name at all -- that poll (up to 8s) existed for finding a
    // BRAND NEW page the first time, and running it again here was pure
    // waste on the re-navigate path, exactly the latency that made this
    // safe to await impossible before. Skipping straight to the already-
    // known Page reference makes re-registration a single evaluate() call,
    // fast enough to await inline without slowing down the agent loop.
    const already = tabs.get(msg.marker);
    const page = already ? already.page : await findPageByMarker(msg.marker, msg.timeoutMs ?? 8000);
    if (!page) {
      throw new Error(`no page with window.name === ${JSON.stringify(msg.marker)} appeared in time`);
    }
    // The actual fix for "cursor overlay never appears": directly evaluate
    // cursor_overlay.js's source onto this exact page RIGHT NOW, through
    // THIS driver's own connection -- see overlaySource's module comment
    // for why this (immediate execution in a document that already exists)
    // works reliably where addInitScript/--init-script (a "run before
    // future scripts" registration, confirmed unreliable across
    // independent connections) did not. registerTab is called once when a
    // tab first opens AND again after every browser_navigate on it (see
    // Python's _assert_cursor_marker) -- re-running this here every time is
    // exactly what re-mounts the overlay after a real navigation wipes the
    // page's JS state, with no separate call path needed for that. Best-
    // effort: a mid-navigation page (evaluate racing a new document load)
    // must never fail tab registration itself, just this cosmetic step.
    if (overlaySource) {
      try {
        await page.evaluate(overlaySource);
      } catch (e) {
        log(`overlay script evaluate failed for tab ${msg.marker} (non-fatal): ${e && e.message || e}`);
      }
    }
    // performRandomMoves=false: idle-breathing/resting-spot motion is the
    // DOM overlay's job (cursor_overlay.js) -- this driver only ever moves
    // on an explicit `move` command tied to a real upcoming action. Reuse
    // the existing cursor instance on the fast path rather than creating a
    // fresh one, so a re-navigate doesn't visibly snap the arrow back to
    // (0,0) before its next real move.
    const cursor = already ? already.cursor : createCursor(page, { x: 0, y: 0 }, false);
    tabs.set(msg.marker, { page, cursor });
    log(`registered tab for marker ${msg.marker}${already ? ' (fast path, already tracked)' : ''}`);
    return {};
  }

  if (cmd === 'move') {
    const tab = tabs.get(msg.marker);
    if (!tab) throw new Error(`move: no tab registered for marker ${JSON.stringify(msg.marker)}`);
    // Speed fix: human-cursor's own moveTo() -> tracePath() (see
    // node_modules/human-cursor/lib/spoof.js) walks the ~35-79 bezier points
    // it generates for this move ONE AT A TIME, `await page.mouse.move(v.x,
    // v.y)`-ing each before starting the next -- so the wall-clock cost of a
    // single moveTo() is N sequential CDP round trips to wherever this page
    // actually lives (locally that's sub-ms each and invisible; against a
    // remote Browserbase session it's real network latency, N times, per
    // click/hover/type in the whole run -- confirmed as a real contributor
    // to the reported 16-minute trip-planning run). tracePath isn't
    // exported, and there's no option to change this, so pipelineMouseMoves
    // below temporarily swaps page.mouse.move() out from under it: the
    // wrapped version fires the REAL move immediately (so N calls hit the
    // wire back-to-back, in order -- a single CDP connection is one
    // WebSocket, so receipt order is preserved even though we don't wait for
    // each ack) but resolves its own promise right away, so tracePath's loop
    // never blocks waiting for a round trip and races straight on to
    // queueing the next point. The bezier path itself -- still generated by
    // human-cursor's own pathWithHumanCurve/HumanizeMouseTrajectory, totally
    // untouched -- is exactly the same set of points in exactly the same
    // order; only the DISPATCH is pipelined instead of serialized. We only
    // block on the real network round trips at the very end, via settle(),
    // so this command still doesn't return until the mouse has actually
    // finished arriving -- callers can't tell the difference except that
    // it's faster.
    const pipeline = pipelineMouseMoves(tab.page);
    const moveStart = Date.now();
    try {
      await tab.cursor.moveTo({ x: msg.x, y: msg.y });
    } finally {
      pipeline.restore();
    }
    await pipeline.settle();
    log(`move for tab ${msg.marker}: ${pipeline.count()} points pipelined in ${Date.now() - moveStart}ms`);
    return {};
  }

  if (cmd === 'unregisterTab') {
    tabs.delete(msg.marker);
    return {};
  }

  if (cmd === 'shutdown') {
    for (const marker of Array.from(tabs.keys())) tabs.delete(marker);
    if (browser) {
      try { await browser.close(); } catch { /* best-effort */ }
      browser = null;
      context = null;
    }
    return { shuttingDown: true };
  }

  throw new Error(`unknown cmd: ${cmd}`);
}

const rl = createInterface({ input: process.stdin, terminal: false });

rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch (e) {
    log(`failed to parse line as JSON: ${trimmed}`);
    return;
  }
  const { id } = msg;
  // Deliberately NOT awaited here -- handleCommand for one marker (e.g. a
  // registerTab that's still polling for its page to appear) must never
  // block a `move` command for an already-registered, unrelated tab from
  // being processed in the meantime. Each command resolves independently
  // and writes its own response line whenever it's ready; id-correlation
  // on the Python side is what makes replies-out-of-order safe.
  handleCommand(msg)
    .then((result) => {
      process.stdout.write(JSON.stringify({ id, ok: true, ...result }) + '\n');
      if (msg.cmd === 'shutdown') process.exit(0);
    })
    .catch((err) => {
      process.stdout.write(JSON.stringify({ id, ok: false, error: String(err && err.message || err) }) + '\n');
    });
});

rl.on('close', () => {
  // stdin closed (parent process died/closed the pipe) -- don't linger
  // holding a CDP connection open with nobody left to talk to.
  log('stdin closed, exiting');
  process.exit(0);
});

log('ready');
