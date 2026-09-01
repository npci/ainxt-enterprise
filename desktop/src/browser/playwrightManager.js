// SPDX-License-Identifier: Apache-2.0
"use strict";
/**
 * PlaywrightManager — Buddy's browser-automation backend (desktop-only).
 *
 * Lets the agent drive a real browser ON THE USER'S MACHINE to do office tasks
 * that no connector covers ("open the internal dashboard and read today's total",
 * "download the report from portal X"). This is the safe, web-scoped slice of
 * "computer use" — built on Playwright rather than a stubbed native
 * modules.
 *
 * SECURITY (AiNxt):
 *  - Host allowlist: navigation is rejected unless the host matches the
 *    configurable allowlist (electron-store `browserAllowlist`). Empty list =
 *    allow-all-https with an audit log (set the list in production).
 *  - Per-action confirm: mutating actions (click / type / submit) pop a native
 *    confirmation dialog before executing — the human is always in the loop.
 *  - Audit: every action is logged (action, url, selector) — never the typed
 *    values, which may contain secrets.
 *  - Launched HEADED so the user sees what the agent does and can log in manually;
 *    the agent never handles credentials.
 */
const { dialog } = require("electron");

let Store;
try { Store = require("electron-store"); } catch { Store = null; }
const _store = Store ? new Store() : { get: (_k, d) => d };

let _pw = null;           // playwright module (lazy)
let _browser = null;
let _page = null;

function _log(action, detail) {
  // Audit trail — values intentionally omitted.
  console.log(`[buddy-browser] ${action}${detail ? " · " + detail : ""}`);
}

function _hostAllowed(url) {
  let host;
  try { host = new URL(url).host.toLowerCase(); } catch { return false; }
  const allow = _store.get("browserAllowlist", []) || [];
  if (!allow.length) {
    _log("navigate.allow-all", host); // no allowlist configured → permitted but audited
    return /^https:\/\//i.test(url) || /^http:\/\/(localhost|127\.|10\.|192\.168\.|172\.)/i.test(url);
  }
  return allow.some((a) => host === a.toLowerCase() || host.endsWith("." + a.toLowerCase()));
}

// Launch a browser WITHOUT bundling Chromium: prefer the system browser via
// Playwright channels — Edge is preinstalled on every Windows machine, Chrome on
// most macs — falling back to any Playwright-managed Chromium in the global
// cache. This keeps the installer small and avoids shipping platform-wrong
// browser binaries (we build on macOS; the Windows installer must not carry mac
// Chromium).
// Stability flags. Headed Chrome/Edge on locked-down corporate Windows (VDI,
// EDR/antivirus attached, no GPU) routinely CRASHES on startup without these —
// the process launches then dies before a context can be created, surfacing as
// "Target page, context or browser has been closed" on newContext(). These flags
// are harmless on a normal machine and prevent that immediate-exit crash.
const _STABILITY_ARGS = [
  "--no-sandbox",
  "--disable-dev-shm-usage",
  "--disable-gpu",
  "--no-first-run",
  "--no-default-browser-check",
];

async function _launchBrowser() {
  if (!_pw) {
    try { _pw = require("playwright"); }
    catch (e) {
      throw new Error("Playwright module failed to load (the app may be corrupt or "
        + "incompletely unpacked): " + (e && e.message ? e.message : String(e)));
    }
  }
  // Try every system browser that might be present, then any Playwright-managed
  // Chromium, so a fresh corporate machine works without installing anything:
  //   Windows → Edge (always present) → Chrome → managed Chromium
  //   mac/Linux → Chrome → Edge → managed Chromium
  const attempts = process.platform === "win32"
    ? [{ channel: "msedge" }, { channel: "chrome" }, {}]
    : [{ channel: "chrome" }, { channel: "msedge" }, {}];
  const errs = [];
  for (const opt of attempts) {
    const label = opt.channel || "chromium(managed)";
    try {
      const browser = await _pw.chromium.launch({
        headless: false, args: _STABILITY_ARGS, ...opt,
      });
      _log("launch.ok", label);
      return browser;
    } catch (e) {
      const m = e && e.message ? e.message : String(e);
      errs.push(`${label}: ${m.split("\n")[0]}`);
      _log("launch.fail", `${label} → ${m.split("\n")[0]}`);
    }
  }
  throw new Error(
    "Could not launch a browser. Install Microsoft Edge or Google Chrome (or run "
    + "`npx playwright install chromium`). Attempts — " + errs.join(" | ")
  );
}

// Reset the shared browser/page singletons — used when the browser dies so the
// next action relaunches instead of reusing a dead handle.
function _resetBrowser() { _browser = null; _page = null; }

async function _ensurePage() {
  // Reuse only a LIVE page. A closed page, or a browser that has since
  // disconnected (window closed / crashed / handed off to an existing Chrome
  // and exited), must trigger a fresh launch — otherwise newContext() throws
  // "Target page, context or browser has been closed" on the stale handle.
  if (_page && !_page.isClosed() && _browser && _browser.isConnected()) return _page;
  if (_browser && !_browser.isConnected()) _resetBrowser();

  // One retry: if context/page creation fails because the just-launched browser
  // died, tear it down and relaunch once from scratch.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      if (!_browser || !_browser.isConnected()) {
        _browser = await _launchBrowser();
        // Clear singletons the moment the browser goes away, so we never reuse it.
        _browser.on("disconnected", _resetBrowser);
      }
      const ctx = await _browser.newContext();
      _page = await ctx.newPage();
      return _page;
    } catch (e) {
      const m = e && e.message ? e.message : String(e);
      _log("ensurePage.fail", `attempt ${attempt + 1} → ${m.split("\n")[0]}`);
      _resetBrowser();
      if (attempt === 1) {
        throw new Error(
          "The browser started but closed before it could be used. On managed/"
          + "corporate Windows this is usually the browser sandbox being blocked or a "
          + "startup crash. Details: " + m.split("\n")[0]
        );
      }
    }
  }
  return _page;
}

async function _confirm(message) {
  const { response } = await dialog.showMessageBox({
    type: "warning",
    buttons: ["Allow", "Cancel"],
    defaultId: 0,
    cancelId: 1,
    title: "Buddy — confirm browser action",
    message: "Buddy wants to perform a browser action",
    detail: message,
  });
  return response === 0;
}

// ── Gateway helpers (compliance redaction + audit). opts: {gatewayBase, jwt, sessionId} ──
function _gwPost(opts, apiPath, payload) {
  return new Promise((resolve) => {
    if (!opts || !opts.gatewayBase || !opts.jwt) return resolve(null);
    try {
      const base = String(opts.gatewayBase).replace(/\/+$/, "");
      const u = new URL(`${base}/ainxt/v1/api${apiPath}`);
      const lib = u.protocol === "https:" ? require("https") : require("http");
      const body = JSON.stringify(payload);
      const req = lib.request({
        hostname: u.hostname, port: u.port, path: u.pathname + u.search, method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body),
                   Authorization: `Bearer ${opts.jwt}` },
      }, (res) => {
        let data = ""; res.on("data", (d) => { data += d; });
        res.on("end", () => { try { resolve(JSON.parse(data || "{}")); } catch { resolve(null); } });
      });
      req.on("error", () => resolve(null));
      req.setTimeout(8000, () => { try { req.destroy(); } catch { /* ignore */ } resolve(null); });
      req.write(body); req.end();
    } catch { resolve(null); }
  });
}

// Redact extracted page text through the gateway BEFORE it enters agent context.
// Fail-safe: if compliance is unreachable, return the text unchanged (read path =
// redact-and-proceed; we never hard-block a read). Never logs the text.
async function _redactText(opts, text) {
  if (!text) return text;
  const r = await _gwPost(opts, "/compliance/scan", { text: String(text), mode: "redact" });
  return (r && typeof r.redacted_text === "string") ? r.redacted_text : text;
}

// Redact a screenshot via the gateway image scanner. Fail-CLOSED: if redaction is
// unavailable we do NOT return raw pixels (a screenshot can leak far more than text).
async function _redactImage(opts, b64) {
  const r = await _gwPost(opts, "/compliance/scan-image", { image_b64: b64 });
  if (r && r.image_b64) return { ok: true, image_b64: r.image_b64, findings: r.findings_count || 0 };
  return { ok: false };
}

function _audit(opts, action, target, allowed, reason) {
  // Fire-and-forget; values never logged, only action + target (URL/selector).
  try {
    _gwPost(opts, "/buddy/computer-use/audit", {
      session_id: (opts && opts.sessionId) || "cowork", action, target: target || "",
      allowed: !!allowed, block_reason: reason || null, findings_count: 0, redacted: false,
    });
  } catch { /* ignore */ }
}

const api = {
  /** Navigate to a URL (allowlist-gated). Returns page title + final url. */
  async navigate({ url }, opts) {
    if (!url) return { error: "url is required" };
    if (!_hostAllowed(url)) { _audit(opts, "browser_navigate", url, false, "host not allowlisted"); return { error: `Navigation to ${url} is not allowed by the browser allowlist.` }; }
    const page = await _ensurePage();
    _log("navigate", url);
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    _audit(opts, "browser_navigate", url, true);
    return { url: page.url(), title: await page.title() };
  },

  /** Read text from the page (or a CSS selector), compliance-REDACTED before return. */
  async extract({ selector }, opts) {
    const page = await _ensurePage();
    _log("extract", selector || "(page)");
    let raw;
    if (selector) {
      const el = await page.$(selector);
      if (!el) return { error: `No element matched ${selector}` };
      raw = (await el.innerText()).slice(0, 8000);
    } else {
      raw = (await page.evaluate(() => document.body.innerText) || "").slice(0, 8000);
    }
    _audit(opts, "browser_extract", selector || "(page)", true);
    return { text: await _redactText(opts, raw) };
  },

  /** Screenshot the current page, compliance-REDACTED (fail-closed) before return. */
  async screenshot(_input, opts) {
    const page = await _ensurePage();
    _log("screenshot", page.url());
    const buf = await page.screenshot({ type: "png", fullPage: false });
    // Downscale to ≤1400px long edge (Retina capture can exceed the model's ~2000px
    // tool-image cap) before redaction.
    let b64;
    try {
      const Jimp = require("jimp");
      const j = await Jimp.read(buf);
      if (Math.max(j.bitmap.width, j.bitmap.height) > 1400) j.scaleToFit(1400, 1400);
      b64 = (await j.getBufferAsync(Jimp.MIME_PNG)).toString("base64");
    } catch { b64 = buf.toString("base64"); }
    const red = await _redactImage(opts, b64);
    if (!red.ok) {
      _audit(opts, "browser_screenshot", page.url(), false, "image redaction unavailable");
      return { error: "Screenshot blocked — image PII redaction is unavailable. Use browser_extract for text instead." };
    }
    _audit(opts, "browser_screenshot", page.url(), true);
    return { image_b64: red.image_b64, findings: red.findings, mime: "image/png" };
  },

  /** Wait for an element to appear (read-only; no confirm). */
  async wait_for({ selector, timeout }, opts) {
    if (!selector) return { error: "selector is required" };
    const page = await _ensurePage();
    try { await page.waitForSelector(selector, { timeout: Math.min(timeout || 10000, 30000) }); }
    catch { return { error: `Timed out waiting for ${selector}` }; }
    _audit(opts, "browser_wait_for", selector, true);
    return { ok: true };
  },

  /** Navigate back in history (read-only). */
  async back(_input, opts) {
    const page = await _ensurePage();
    await page.goBack({ waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
    _audit(opts, "browser_back", page.url(), true);
    return { ok: true, url: page.url() };
  },

  /** Click an element — requires user confirmation. */
  async click({ selector }, opts) {
    if (!selector) return { error: "selector is required" };
    const page = await _ensurePage();
    if (!(await _confirm(`Click "${selector}" on ${page.url()}?`))) { _audit(opts, "browser_click", selector, false, "user declined"); return { error: "User declined the click." }; }
    _log("click", selector);
    await page.click(selector, { timeout: 10000 });
    _audit(opts, "browser_click", selector, true);
    return { ok: true, url: page.url() };
  },

  /** Type text into a field — requires user confirmation (value not logged). */
  async type({ selector, text }, opts) {
    if (!selector) return { error: "selector is required" };
    const page = await _ensurePage();
    if (!(await _confirm(`Enter text into "${selector}" on ${page.url()}?`))) { _audit(opts, "browser_type", selector, false, "user declined"); return { error: "User declined the input." }; }
    _log("type", selector);
    await page.fill(selector, text || "");
    _audit(opts, "browser_type", selector, true);
    return { ok: true };
  },

  /** Select an <option> in a dropdown by value — requires user confirmation. */
  async select({ selector, value }, opts) {
    if (!selector) return { error: "selector is required" };
    const page = await _ensurePage();
    if (!(await _confirm(`Select an option in "${selector}" on ${page.url()}?`))) { _audit(opts, "browser_select", selector, false, "user declined"); return { error: "User declined the selection." }; }
    _log("select", selector);
    await page.selectOption(selector, value);
    _audit(opts, "browser_select", selector, true);
    return { ok: true };
  },

  async close() {
    try { if (_browser) await _browser.close(); } catch { /* ignore */ }
    _browser = null; _page = null;
  },
};

// MCP tool descriptors (exposed to the gateway agent via the local MCP server).
const TOOLS = [
  { name: "browser_navigate", description: "Open a URL in the user's browser (allowlisted hosts only). Use for web apps with no connector.",
    input_schema: { type: "object", properties: { url: { type: "string" } }, required: ["url"] } },
  { name: "browser_extract", description: "Read visible text from the current page, or a CSS selector. Use after browser_navigate.",
    input_schema: { type: "object", properties: { selector: { type: "string" } } } },
  { name: "browser_screenshot", description: "Capture a screenshot of the current page (PII-redacted). Use when text extraction isn't enough.",
    input_schema: { type: "object", properties: {} } },
  { name: "browser_wait_for", description: "Wait until an element (CSS selector) appears, e.g. after a click loads new content.",
    input_schema: { type: "object", properties: { selector: { type: "string" }, timeout: { type: "integer" } }, required: ["selector"] } },
  { name: "browser_back", description: "Go back one page in the browser history.",
    input_schema: { type: "object", properties: {} } },
  { name: "browser_click", description: "Click an element by CSS selector. Prompts the user to confirm.",
    input_schema: { type: "object", properties: { selector: { type: "string" } }, required: ["selector"] } },
  { name: "browser_type", description: "Type text into a field by CSS selector. Prompts the user to confirm.",
    input_schema: { type: "object", properties: { selector: { type: "string" }, text: { type: "string" } }, required: ["selector"] } },
  { name: "browser_select", description: "Choose an option in a <select> dropdown by value. Prompts the user to confirm.",
    input_schema: { type: "object", properties: { selector: { type: "string" }, value: { type: "string" } }, required: ["selector"] } },
];

async function executeTool(name, input = {}, opts = {}) {
  const fn = {
    browser_navigate: api.navigate,
    browser_extract: api.extract,
    browser_screenshot: api.screenshot,
    browser_wait_for: api.wait_for,
    browser_back: api.back,
    browser_click: api.click,
    browser_type: api.type,
    browser_select: api.select,
  }[name];
  if (!fn) throw new Error(`Unknown browser tool: ${name}`);
  return fn(input, opts);
}

module.exports = { api, TOOLS, executeTool, isBrowserTool: (n) => typeof n === "string" && n.startsWith("browser_") };
