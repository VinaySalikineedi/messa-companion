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
    return {};
  }

  if (cmd === 'registerTab') {
    if (!context) throw new Error('registerTab called before connect');
    const page = await findPageByMarker(msg.marker, msg.timeoutMs ?? 8000);
    if (!page) {
      throw new Error(`no page with window.name === ${JSON.stringify(msg.marker)} appeared in time`);
    }
    // performRandomMoves=false: idle-breathing/resting-spot motion is the
    // DOM overlay's job (cursor_overlay.js) -- this driver only ever moves
    // on an explicit `move` command tied to a real upcoming action.
    const cursor = createCursor(page, { x: 0, y: 0 }, false);
    tabs.set(msg.marker, { page, cursor });
    log(`registered tab for marker ${msg.marker}`);
    return {};
  }

  if (cmd === 'move') {
    const tab = tabs.get(msg.marker);
    if (!tab) throw new Error(`move: no tab registered for marker ${JSON.stringify(msg.marker)}`);
    await tab.cursor.moveTo({ x: msg.x, y: msg.y });
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
