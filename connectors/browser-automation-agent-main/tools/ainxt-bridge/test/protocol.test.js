// SPDX-License-Identifier: Apache-2.0
// protocol.test.js — the helper's transport and auth behaviour.
// Run: node test/protocol.test.js

import assert from "node:assert";
import crypto from "node:crypto";
import { createBridgeServer } from "../server.js";
import { connectFakeExtension } from "./fake-extension.js";

const TOKEN = `test-token-${crypto.randomUUID()}`;
// Deliberately not TOKEN — used to assert that a mismatch is rejected.
const WRONG_TOKEN = `wrong-${crypto.randomUUID()}`;
let passed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// Port 0 lets the OS pick, so tests never collide with a real helper.
async function withServer(fn, { token = TOKEN } = {}) {
  const bridge = createBridgeServer({ token, port: 0, log: () => {} });
  const addr = await bridge.listen();
  try { return await fn({ bridge, port: addr.port, token }); }
  finally { await bridge.close(); }
}

const authHeaders = (token = TOKEN) => ({ authorization: `Bearer ${token}`, "content-type": "application/json" });

test("a correct token completes the handshake in both directions", () =>
  withServer(async ({ port, bridge }) => {
    const ext = await connectFakeExtension({ port, token: TOKEN });
    assert.equal(ext.authed, true);
    assert.equal(bridge.extensionConnected, true);
  }));

test("a wrong token on the extension side is rejected", () =>
  withServer(async ({ port, bridge }) => {
    // The fake extension verifies the helper's proof first, so a token
    // mismatch surfaces as a rejected connect rather than a silent accept.
    await assert.rejects(
      connectFakeExtension({ port, token: WRONG_TOKEN }),
      /challenge/,
    );
    assert.equal(bridge.extensionConnected, false);
  }));

test("a run request round-trips and streams to done", () =>
  withServer(async ({ port }) => {
    await connectFakeExtension({
      port,
      token: TOKEN,
      onFrame: (msg, api) => {
        if (msg.type !== "run") return;
        assert.equal(msg.task.instruction, "hello");
        api.send({ type: "event", id: msg.id, event: "accepted", tabId: 7 });
        api.send({ type: "event", id: msg.id, event: "progress", message: "#1 success click", level: "ok" });
        api.send({
          type: "event", id: msg.id, event: "done",
          record: { result: { status: "pass", passed_steps: 1, failed_steps: 0, skipped_steps: 0 }, summary: "did it" },
        });
      },
    });

    const res = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST", headers: authHeaders(), body: JSON.stringify({ instruction: "hello" }),
    });
    assert.equal(res.status, 200);
    const events = await collect(res);
    assert.deepEqual(events.map((e) => e.event), ["queued", "accepted", "progress", "done"]);
    assert.equal(events.at(-1).record.result.status, "pass");
  }));

test("a gate reply reaches the extension", () =>
  withServer(async ({ port }) => {
    let gotDecision = null;
    await connectFakeExtension({
      port,
      token: TOKEN,
      onFrame: (msg, api) => {
        if (msg.type === "run") {
          api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
          api.send({ type: "event", id: msg.id, event: "gate", gateId: "g1", critical: true, reason: "secret" });
        }
        if (msg.type === "gate_reply") {
          gotDecision = msg.decision;
          api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
        }
      },
    });

    const res = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST", headers: authHeaders(), body: JSON.stringify({ instruction: "x" }),
    });
    const events = [];
    for await (const ev of sse(res)) {
      events.push(ev);
      if (ev.event === "gate") {
        await fetch(`http://127.0.0.1:${port}/runs/${events[0].id}/gate`, {
          method: "POST", headers: authHeaders(), body: JSON.stringify({ gateId: ev.gateId, decision: "approve" }),
        });
      }
      if (ev.event === "done") break;
    }
    assert.equal(gotDecision, "approve");
  }));

test("an extension that rejects the run ends the stream with the reason", () =>
  withServer(async ({ port }) => {
    await connectFakeExtension({
      port,
      token: TOKEN,
      onFrame: (msg, api) => {
        if (msg.type === "run") api.send({ type: "event", id: msg.id, event: "error", error: "busy", detail: "already running" });
      },
    });
    const res = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST", headers: authHeaders(), body: JSON.stringify({ instruction: "x" }),
    });
    const events = await collect(res);
    assert.equal(events.at(-1).event, "error");
    assert.equal(events.at(-1).error, "busy");
  }));

test("a missing or wrong bearer token is refused", () =>
  withServer(async ({ port }) => {
    const noAuth = await fetch(`http://127.0.0.1:${port}/run`, { method: "POST", body: "{}" });
    assert.equal(noAuth.status, 401);
    const wrongAuth = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST", headers: authHeaders("wrong-token-entirely"), body: "{}",
    });
    assert.equal(wrongAuth.status, 401);
  }));

test("an Origin header is refused even with the right token", () =>
  withServer(async ({ port }) => {
    // A page in the user's own browser must not be able to drive the CLI surface.
    const res = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST",
      headers: { ...authHeaders(), origin: "https://evil.example" },
      body: JSON.stringify({ instruction: "x" }),
    });
    assert.equal(res.status, 401);
  }));

test("/health needs no token and reports the extension state", () =>
  withServer(async ({ port }) => {
    let body = await (await fetch(`http://127.0.0.1:${port}/health`)).json();
    assert.equal(body.extension, "disconnected");
    await connectFakeExtension({ port, token: TOKEN });
    body = await (await fetch(`http://127.0.0.1:${port}/health`)).json();
    assert.equal(body.extension, "connected");
  }));

test("a run with no extension connected fails fast", () =>
  withServer(async ({ port }) => {
    const res = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST", headers: authHeaders(), body: JSON.stringify({ instruction: "x" }),
    });
    assert.equal(res.status, 503);
  }));

test("a large payload survives 16-bit and 64-bit frame lengths", () =>
  withServer(async ({ port }) => {
    const big = "x".repeat(200000);
    await connectFakeExtension({
      port,
      token: TOKEN,
      onFrame: (msg, api) => {
        if (msg.type !== "run") return;
        assert.equal(msg.task.instruction.length, big.length); // 64-bit path inbound
        api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
        api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" }, summary: big } });
      },
    });
    const res = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST", headers: authHeaders(), body: JSON.stringify({ instruction: big }),
    });
    const events = await collect(res);
    assert.equal(events.at(-1).record.summary.length, big.length);
  }));

test("losing the extension mid-run tells the client instead of hanging", () =>
  withServer(async ({ port }) => {
    const ext = await connectFakeExtension({
      port,
      token: TOKEN,
      onFrame: (msg, api) => {
        if (msg.type === "run") {
          api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
          setTimeout(() => api.close(), 20);
        }
      },
    });
    assert.equal(ext.authed, true);
    const res = await fetch(`http://127.0.0.1:${port}/run`, {
      method: "POST", headers: authHeaders(), body: JSON.stringify({ instruction: "x" }),
    });
    const events = await collect(res);
    assert.equal(events.at(-1).event, "error");
    assert.equal(events.at(-1).error, "disconnected");
  }));

// ---------- helpers ----------

async function* sse(res) {
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of res.body) {
    buffer += decoder.decode(chunk, { stream: true });
    let i;
    while ((i = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, i);
      buffer = buffer.slice(i + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data: ")) yield JSON.parse(line.slice(6));
      }
    }
  }
}

async function collect(res) {
  const out = [];
  for await (const ev of sse(res)) {
    out.push(ev);
    if (ev.event === "done" || ev.event === "error") break;
  }
  return out;
}

const run = async () => {
  for (const [name, fn] of tests) {
    try { await fn(); passed++; console.log(`  ✓ ${name}`); }
    catch (e) { console.error(`  ✗ ${name}\n    ${e.message}`); process.exitCode = 1; }
  }
  console.log(`\n${passed}/${tests.length} passed`);
};
run();
