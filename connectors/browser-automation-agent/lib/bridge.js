// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/bridge.js — the local command bridge's socket half.
//
// An MV3 extension cannot listen on a port, and that is a feature, not a
// workaround: the v1.14.8 review counted "no externally_connectable surface" as
// a security property (F-13). So the direction is inverted. A local helper
// process (tools/ainxt-bridge) listens on 127.0.0.1, this worker DIALS OUT to
// it, and a CLI or desktop app talks to the helper. Nothing can reach into the
// extension; the extension chooses to reach out, only when the user has enabled
// the bridge and only to loopback.
//
// Off by default. Enabling it needs three things set on this device: the
// toggle, a port, and a token the user copies into the helper's config.
//
// Wire format: one JSON object per WebSocket text frame.
//
//   ext → helper  {type:"hello", role:"extension", extensionId, version, nonce}
//   helper → ext  {type:"hello_ack", proof:HMAC(token, extNonce), nonce}
//   ext → helper  {type:"hello_proof", proof:HMAC(token, helperNonce)}
//   helper → ext  {type:"ready"}
//
// Both sides prove knowledge of the token against the OTHER side's nonce, so a
// random process squatting the port can neither impersonate the helper nor
// drive the browser, and a replayed transcript is useless.

import { startBridgeRun, cancelBridgeRun } from "./bridge-run.js";

const DEFAULT_PORT = 8787;
const HEARTBEAT_MS = 20000;
// Chrome 116+ (our minimum_chrome_version) resets the service worker's idle
// timer on WebSocket activity, so the heartbeat doubles as the keepalive that
// stops the worker being torn down between a request and its run.
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;

const CLOSE_AUTH_FAILED = 4001;
const CLOSE_DISABLED = 4002;

let ws = null;
let authed = false;
let backoff = BACKOFF_MIN_MS;
let reconnectTimer = null;
let heartbeatTimer = null;
let settings = { enabled: false, port: DEFAULT_PORT, token: "" };

// gateId -> resolve("approve" | "cancel"), awaiting the operator's answer at
// the far end of the socket.
const pendingGates = new Map();
let gateSeq = 0;

// ---------- token proof ----------

const enc = new TextEncoder();

async function hmac(token, nonce) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(token), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(String(nonce)));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Constant-time-ish compare. The token never leaves this device and an attacker
// would need local port access to try at all, but there's no reason to leak
// timing on the proof.
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const newNonce = () => crypto.randomUUID();

// ---------- settings ----------

export async function readBridgeSettings() {
  const stored = await chrome.storage.local.get(["bridgeEnabled", "bridgePort", "bridgeToken"]);
  const port = Number(stored.bridgePort);
  return {
    enabled: stored.bridgeEnabled === true,
    port: Number.isInteger(port) && port > 0 && port < 65536 ? port : DEFAULT_PORT,
    token: typeof stored.bridgeToken === "string" ? stored.bridgeToken : "",
  };
}

async function publishStatus(state, detail = "") {
  try {
    await chrome.storage.local.set({ bridgeStatus: { state, detail, at: Date.now() } });
  } catch { /* status is cosmetic */ }
}

// ---------- connection ----------

function clearTimers() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
}

function scheduleReconnect() {
  if (!settings.enabled || reconnectTimer) return;
  const delay = backoff;
  backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
}

export function sendFrame(frame) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try { ws.send(JSON.stringify(frame)); return true; }
  catch { return false; }
}

async function connect() {
  settings = await readBridgeSettings();
  if (!settings.enabled) { publishStatus("off"); return; }
  if (!settings.token) { publishStatus("error", "no token set"); return; }
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const extNonce = newNonce();
  publishStatus("connecting");

  try {
    ws = new WebSocket(`ws://127.0.0.1:${settings.port}/ws`);
  } catch (e) {
    publishStatus("error", String(e?.message || e));
    scheduleReconnect();
    return;
  }

  ws.addEventListener("open", () => {
    sendFrame({
      type: "hello",
      role: "extension",
      extensionId: chrome.runtime.id,
      version: chrome.runtime.getManifest().version,
      nonce: extNonce,
    });
  });

  // Handshake state machine: await_ack → await_ready → authed. Anything
  // off-script at either handshake step closes the socket — a peer that can't
  // prove the token never gets to send a "run".
  let phase = "await_ack";
  const socket = ws;
  const failHandshake = (detail) => {
    publishStatus("error", detail);
    try { socket.close(CLOSE_AUTH_FAILED); } catch {}
  };

  socket.addEventListener("message", async (ev) => {
    let frame;
    try { frame = JSON.parse(typeof ev.data === "string" ? ev.data : ""); }
    catch { return; }
    if (!frame || typeof frame !== "object") return;

    if (phase === "await_ack") {
      if (frame.type !== "hello_ack") return failHandshake("handshake failed");
      phase = "await_ready";
      const expected = await hmac(settings.token, extNonce);
      if (!safeEqual(String(frame.proof || ""), expected)) {
        return failHandshake("token mismatch — check the helper's token");
      }
      sendFrame({ type: "hello_proof", proof: await hmac(settings.token, frame.nonce) });
      return;
    }

    if (phase === "await_ready") {
      if (frame.type !== "ready") return failHandshake("handshake failed");
      phase = "authed";
      authed = true;
      backoff = BACKOFF_MIN_MS;
      publishStatus("connected", `127.0.0.1:${settings.port}`);
      heartbeatTimer = setInterval(() => sendFrame({ type: "ping" }), HEARTBEAT_MS);
      return;
    }

    handleFrame(frame);
  });

  ws.addEventListener("close", (ev) => {
    const wasAuthed = authed;
    authed = false;
    ws = null;
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
    // A dropped socket mid-run means nobody is left to answer a gate or read
    // the report — stop rather than keep driving the browser unobserved.
    cancelBridgeRun("bridge disconnected");
    for (const resolve of pendingGates.values()) resolve("cancel");
    pendingGates.clear();
    if (ev.code === CLOSE_DISABLED) { publishStatus("off"); return; }
    publishStatus(wasAuthed ? "disconnected" : "error", ev.code === CLOSE_AUTH_FAILED ? "authentication failed" : "");
    scheduleReconnect();
  });

  ws.addEventListener("error", () => { /* close fires next; backoff handled there */ });
}

// ---------- frame handling ----------

function handleFrame(frame) {
  switch (frame.type) {
    case "ready":
      return; // consumed by readyWatch
    case "ping":
      sendFrame({ type: "pong" });
      return;
    case "pong":
      return;
    case "run":
      startBridgeRun({
        id: frame.id,
        task: frame.task || {},
        emit: (event, data) => sendFrame({ type: "event", id: frame.id, event, ...data }),
        requestGate: (payload) => requestGate(frame.id, payload),
      });
      return;
    case "cancel":
      cancelBridgeRun("cancelled by client", frame.id);
      return;
    case "gate_reply": {
      const resolve = pendingGates.get(frame.gateId);
      if (!resolve) return;
      pendingGates.delete(frame.gateId);
      resolve(frame.decision === "approve" ? "approve" : "cancel");
      return;
    }
    default:
      sendFrame({ type: "event", id: frame.id, event: "error", error: `unknown frame type "${frame.type}"` });
  }
}

// Forward a gate to the operator at the far end and wait for their answer.
// Resolves "cancel" on timeout or a dead socket — a gate that nobody answered
// is not an approval.
function requestGate(runId, payload, timeoutMs = 300000) {
  const gateId = `g${++gateSeq}`;
  const sent = sendFrame({ type: "event", id: runId, event: "gate", gateId, ...payload });
  if (!sent) return Promise.resolve("cancel");
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      if (pendingGates.delete(gateId)) resolve("cancel");
    }, timeoutMs);
    pendingGates.set(gateId, (decision) => { clearTimeout(timer); resolve(decision); });
  });
}

// ---------- lifecycle ----------

export async function initBridge() {
  settings = await readBridgeSettings();
  clearTimers();
  if (settings.enabled) connect();
  else publishStatus("off");
}

// Re-dial (or hang up) when the user changes the bridge settings.
export function watchBridgeSettings() {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (!("bridgeEnabled" in changes) && !("bridgePort" in changes) && !("bridgeToken" in changes)) return;
    (async () => {
      const next = await readBridgeSettings();
      const changed =
        next.enabled !== settings.enabled || next.port !== settings.port || next.token !== settings.token;
      settings = next;
      if (!changed) return;
      clearTimers();
      backoff = BACKOFF_MIN_MS;
      if (ws) { try { ws.close(next.enabled ? 1000 : CLOSE_DISABLED); } catch {} ws = null; authed = false; }
      if (next.enabled) connect();
      else publishStatus("off");
    })();
  });
}
