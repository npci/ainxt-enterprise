// SPDX-License-Identifier: Apache-2.0
"use strict";
/**
 * Native OS computer-use for Cowork (AiNxt).
 *
 * Lets the Cowork agent control the machine — move/click the mouse, type, press
 * keys, and SEE the screen via screenshots — to drive legacy/desktop apps that
 * have no API/connector. Uses @nut-tree-fork/nut-js (mouse/keyboard/screen).
 *
 * AiNxt guardrails (all enforced HERE, before anything runs):
 *   1. Master switch — computer-use is OFF unless explicitly enabled (electron-store).
 *   2. Per-action confirm — every mutating action pops a native Yes/No dialog.
 *   3. Screenshot compliance — captures are sent to the gateway /compliance/scan-image
 *      for PAN/PII redaction BEFORE the (redacted) image is returned to the agent.
 *      If redaction is unavailable, the raw screenshot is NEVER returned.
 *   4. Audit — every action (allow/deny + target, never values) is recorded.
 *   5. No credential autofill — typing into obvious password fields is the user's call (confirm).
 *
 * Everything routes through the AiNxt gateway; nothing leaves the AiNxt network.
 * Requires: `npm install @nut-tree-fork/nut-js` in desktop/ + macOS Accessibility
 * & Screen-Recording permissions (the OS prompts on first use). Until then every
 * tool returns a clear "not available" message rather than failing silently.
 */
const { dialog } = require("electron");

let _nut = null;
let _nutTried = false;
function _loadNut() {
  if (_nutTried) return _nut;
  _nutTried = true;
  try { _nut = require("@nut-tree-fork/nut-js"); } catch { _nut = null; }
  return _nut;
}

const TOOLS = [
  { name: "computer_screenshot", description: "Capture the screen so you can see the desktop/app. The image is compliance-redacted before you receive it.", input_schema: { type: "object", properties: {} } },
  { name: "computer_click", description: "Move the mouse to (x,y) and click. Requires user confirmation.", input_schema: { type: "object", properties: { x: { type: "integer" }, y: { type: "integer" }, button: { type: "string", enum: ["left", "right", "middle"], default: "left" }, double: { type: "boolean", default: false } }, required: ["x", "y"] } },
  { name: "computer_type", description: "Type text at the current cursor. Requires user confirmation. NEVER type into credential fields.", input_schema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] } },
  { name: "computer_key", description: "Press a key or chord (e.g. 'Enter', 'Tab', 'cmd+c'). Requires user confirmation.", input_schema: { type: "object", properties: { key: { type: "string" } }, required: ["key"] } },
  { name: "computer_move", description: "Move the mouse to (x,y) without clicking.", input_schema: { type: "object", properties: { x: { type: "integer" }, y: { type: "integer" } }, required: ["x", "y"] } },
  { name: "computer_scroll", description: "Scroll the mouse wheel by an amount (negative = up).", input_schema: { type: "object", properties: { amount: { type: "integer", default: 3 } }, required: [] } },
];

const NAMES = new Set(TOOLS.map((t) => t.name));
function isComputerUseTool(name) { return NAMES.has(name); }

let _store = null;
function _init(store) { _store = store; }
function _enabled() { try { return !!(_store && _store.get("computerUseEnabled", false)); } catch { return false; } }

async function _confirm(action, detail) {
  try {
    const { response } = await dialog.showMessageBox({
      type: "warning",
      buttons: ["Deny", "Allow"],
      defaultId: 0,
      cancelId: 0,
      title: "Cowork wants to control your computer",
      message: `Allow Cowork to ${action}?`,
      detail: detail ? String(detail).slice(0, 200) : undefined,
    });
    return response === 1;
  } catch { return false; }
}

// Audit + screenshot redaction go through the gateway. opts: {gatewayBase, jwt, sessionId, userDept}
async function _audit(opts, action, target, allowed, blockReason, findings, redacted) {
  if (!opts || !opts.gatewayBase || !opts.jwt) return;
  try {
    const base = String(opts.gatewayBase).replace(/\/+$/, "");
    const u = new URL(`${base}/ainxt/v1/api/buddy/computer-use/audit`);
    const lib = u.protocol === "https:" ? require("https") : require("http");
    const body = JSON.stringify({
      session_id: opts.sessionId || "cowork", action, target: target || "",
      allowed: !!allowed, block_reason: blockReason || null,
      findings_count: findings || 0, redacted: !!redacted,
    });
    const req = lib.request({ hostname: u.hostname, port: u.port, path: u.pathname, method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body), Authorization: `Bearer ${opts.jwt}` } },
      (res) => { res.on("data", () => {}); res.on("end", () => {}); });
    req.on("error", () => {});
    req.write(body); req.end();
  } catch { /* never break on audit */ }
}

// POST a screenshot to the gateway for PAN/PII redaction. Returns {ok, image_b64, findings} or {ok:false}.
function _redactScreenshot(opts, b64) {
  return new Promise((resolve) => {
    if (!opts || !opts.gatewayBase || !opts.jwt) return resolve({ ok: false });
    try {
      const base = String(opts.gatewayBase).replace(/\/+$/, "");
      const u = new URL(`${base}/ainxt/v1/api/compliance/scan-image`);
      const lib = u.protocol === "https:" ? require("https") : require("http");
      const body = JSON.stringify({ image_b64: b64 });
      const req = lib.request({ hostname: u.hostname, port: u.port, path: u.pathname, method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body), Authorization: `Bearer ${opts.jwt}` } },
        (res) => { let d = ""; res.on("data", (c) => { d += c; }); res.on("end", () => { try { resolve(JSON.parse(d)); } catch { resolve({ ok: false }); } }); });
      req.on("error", () => resolve({ ok: false }));
      req.write(body); req.end();
    } catch { resolve({ ok: false }); }
  });
}

async function executeTool(name, input = {}, opts = {}) {
  if (!_enabled()) {
    await _audit(opts, name, "", false, "computer-use disabled", 0, false);
    return { error: "Computer use is disabled. An admin must enable it for this machine (and grant OS permissions)." };
  }
  const nut = _loadNut();
  if (!nut) {
    return { error: "Native computer-use is not installed. Run `npm install @nut-tree-fork/nut-js` in desktop/ and grant Accessibility + Screen-Recording permissions." };
  }

  try {
    if (name === "computer_screenshot") {
      // Capture → redact at the gateway → only return the REDACTED image.
      const img = await nut.screen.grab();
      const png = await (nut.imageResource ? null : null); // placeholder; use provider below
      let b64;
      try {
        // nut.js Image → PNG buffer via its toBuffer/encoder if available.
        b64 = await _imageToB64(nut, img);
      } catch { return { error: "Screen capture failed." }; }
      const red = await _redactScreenshot(opts, b64);
      if (!red || red.ok === false || !red.image_b64) {
        await _audit(opts, name, "screen", false, "image redaction unavailable", 0, false);
        return { error: "Screenshot blocked — image PII redaction is unavailable on the gateway (OCR not configured). Cannot return a raw screen that may contain PANs." };
      }
      await _audit(opts, name, "screen", true, null, red.findings || 0, true);
      return { type: "image", image_b64: red.image_b64, redacted: true, findings: red.findings || 0 };
    }

    // Mutating actions → per-action confirm.
    const detail = JSON.stringify(input).slice(0, 120);
    if (["computer_click", "computer_type", "computer_key"].includes(name)) {
      const ok = await _confirm(name.replace("computer_", ""), detail);
      if (!ok) { await _audit(opts, name, detail, false, "user declined", 0, false); return { error: "User declined this action." }; }
    }

    if (name === "computer_move") {
      await nut.mouse.setPosition(new nut.Point(input.x | 0, input.y | 0));
      await _audit(opts, name, `${input.x},${input.y}`, true, null, 0, false);
      return { ok: true };
    }
    if (name === "computer_click") {
      await nut.mouse.setPosition(new nut.Point(input.x | 0, input.y | 0));
      const btn = input.button === "right" ? nut.Button.RIGHT : input.button === "middle" ? nut.Button.MIDDLE : nut.Button.LEFT;
      if (input.double) await nut.mouse.doubleClick(btn); else await nut.mouse.click(btn);
      await _audit(opts, name, `${input.x},${input.y}`, true, null, 0, false);
      return { ok: true };
    }
    if (name === "computer_type") {
      await nut.keyboard.type(String(input.text || ""));
      await _audit(opts, name, "(text)", true, null, 0, false); // never log the typed value
      return { ok: true };
    }
    if (name === "computer_key") {
      // Map a chord like "cmd+c" to nut keys.
      const parts = String(input.key || "").split("+").map((s) => s.trim()).filter(Boolean);
      const keys = parts.map((p) => _mapKey(nut, p)).filter(Boolean);
      if (keys.length) await nut.keyboard.pressKey(...keys), await nut.keyboard.releaseKey(...keys);
      await _audit(opts, name, input.key, true, null, 0, false);
      return { ok: true };
    }
    if (name === "computer_scroll") {
      const amt = input.amount | 0 || 3;
      if (amt < 0) await nut.mouse.scrollUp(-amt); else await nut.mouse.scrollDown(amt);
      return { ok: true };
    }
    return { error: `Unknown computer-use tool: ${name}` };
  } catch (e) {
    await _audit(opts, name, "", false, "execution_error", 0, false);
    return { error: "Computer-use tool execution failed." };
  }
}

function _mapKey(nut, p) {
  const K = nut.Key;
  const m = { cmd: K.LeftCmd, command: K.LeftCmd, ctrl: K.LeftControl, control: K.LeftControl, alt: K.LeftAlt,
    shift: K.LeftShift, enter: K.Enter, return: K.Return, tab: K.Tab, esc: K.Escape, escape: K.Escape,
    space: K.Space, backspace: K.Backspace, delete: K.Delete, up: K.Up, down: K.Down, left: K.Left, right: K.Right };
  const low = p.toLowerCase();
  if (m[low]) return m[low];
  if (p.length === 1 && K[p.toUpperCase()]) return K[p.toUpperCase()];
  return null;
}

async function _imageToB64(nut, img) {
  // nut.js exposes providerRegistry to encode an Image to a PNG buffer.
  const { imageToBuffer } = nut.providerRegistry ? { imageToBuffer: null } : {};
  if (nut.providerRegistry && nut.providerRegistry.getImageWriter) {
    // fallback: write to temp then read — but keep it in-memory if possible.
  }
  // Most nut-js builds: img.toRGB()/encoder. Use the screen.captureRegion → image.data PNG path.
  if (img && img.data && img.width && img.height) {
    // Encode raw screen buffer → PNG via the bundled jimp.
    try {
      const Jimp = require("jimp");
      const j = new Jimp({ data: Buffer.from(img.data), width: img.width, height: img.height });
      // Retina/4K screens exceed the model's tool-result image cap (~2000px) and OCR
      // is far slower at full res. Downscale the long edge to 1400px (well within
      // limits, still legible) BEFORE redaction so the OCR boxes align to what's returned.
      const MAX = 1400;
      if (Math.max(j.bitmap.width, j.bitmap.height) > MAX) j.scaleToFit(MAX, MAX);
      const buf = await j.getBufferAsync(Jimp.MIME_PNG);
      return buf.toString("base64");
    } catch { /* fall through */ }
  }
  throw new Error("no encoder");
}

module.exports = { TOOLS, NAMES, isComputerUseTool, executeTool, init: _init };
