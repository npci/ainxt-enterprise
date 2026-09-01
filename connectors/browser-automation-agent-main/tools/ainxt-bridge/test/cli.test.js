// SPDX-License-Identifier: Apache-2.0
// cli.test.js — the CLI's contract with whatever is calling it: exit codes,
// what lands on stdout vs stderr, and that --yes never covers a critical gate.
// Run: node test/cli.test.js

import assert from "node:assert";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { createBridgeServer } from "../server.js";
import { connectFakeExtension } from "./fake-extension.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(here, "..", "cli.js");
const TOKEN = `cli-token-${crypto.randomUUID()}`;

let passed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// Boot a helper with a scripted extension behind it, run the CLI against it,
// and hand back what the process did.
async function withCli(args, onFrame, { stdin = "" } = {}) {
  const bridge = createBridgeServer({ token: TOKEN, port: 0, log: () => {} });
  const addr = await bridge.listen();
  await connectFakeExtension({ port: addr.port, token: TOKEN, onFrame });
  try {
    return await runCli([...args, "--port", String(addr.port), "--token", TOKEN], stdin);
  } finally {
    await bridge.close();
  }
}

function runCli(args, stdin) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [CLI, ...args], { stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.stdin.end(stdin);
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

const doneWith = (record) => (msg, api) => {
  if (msg.type !== "run") return;
  api.send({ type: "event", id: msg.id, event: "accepted", tabId: 3 });
  api.send({ type: "event", id: msg.id, event: "done", record });
};

test("a passing run exits 0 and prints the answer on stdout", async () => {
  const { code, stdout } = await withCli(
    ["run", "check the headline"],
    doneWith({ result: { status: "pass", passed_steps: 2, failed_steps: 0, skipped_steps: 0 }, answer: "The headline is X" }),
  );
  assert.equal(code, 0);
  assert.match(stdout, /The headline is X/);
});

test("a failing run exits 1", async () => {
  const { code } = await withCli(["run", "x"], doneWith({ result: { status: "fail", passed_steps: 0, failed_steps: 1 } }));
  assert.equal(code, 1);
});

test("a partial run exits 1", async () => {
  const { code } = await withCli(["run", "x"], doneWith({ result: { status: "partial", passed_steps: 1, failed_steps: 1 } }));
  assert.equal(code, 1);
});

test("needs_human exits 2", async () => {
  const { code } = await withCli(["run", "x"], doneWith({ result: { status: "needs_human" } }));
  assert.equal(code, 2);
});

test("max_steps_reached exits 2", async () => {
  const { code } = await withCli(["run", "x"], doneWith({ result: { status: "max_steps_reached" } }));
  assert.equal(code, 2);
});

test("--json puts the record on stdout and nothing else", async () => {
  const record = { result: { status: "pass", passed_steps: 1 }, summary: "ok", goal: "g" };
  const { code, stdout } = await withCli(["run", "x", "--json"], doneWith(record));
  assert.equal(code, 0);
  assert.deepEqual(JSON.parse(stdout), record);
});

test("progress goes to stderr, so --json stdout stays parseable", async () => {
  const { stdout, stderr } = await withCli(["run", "x", "--json"], (msg, api) => {
    if (msg.type !== "run") return;
    api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
    api.send({ type: "event", id: msg.id, event: "progress", message: "#1 click", level: "info" });
    api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
  });
  assert.doesNotThrow(() => JSON.parse(stdout));
  assert.equal(stderr.includes("#1 click"), false, "--json should stay quiet");
});

test("--yes approves an ordinary gate", async () => {
  let decision = null;
  const { code } = await withCli(["run", "x", "--yes"], (msg, api) => {
    if (msg.type === "run") {
      api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
      api.send({ type: "event", id: msg.id, event: "gate", gateId: "g1", critical: false, reason: "navigate somewhere" });
    }
    if (msg.type === "gate_reply") {
      decision = msg.decision;
      api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
    }
  });
  assert.equal(decision, "approve");
  assert.equal(code, 0);
});

test("--yes does NOT approve a critical gate — it still asks", async () => {
  let decision = null;
  // stdin is a pipe, not a TTY, so the answer is whatever we feed it. Feeding
  // "n" proves the CLI asked rather than auto-approving.
  const { code } = await withCli(
    ["run", "x", "--yes"],
    (msg, api) => {
      if (msg.type === "run") {
        api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
        api.send({ type: "event", id: msg.id, event: "gate", gateId: "g1", critical: true, secretKey: "GH_TOKEN", secretHost: "github.com", reason: "secret use" });
      }
      if (msg.type === "gate_reply") {
        decision = msg.decision;
        api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "needs_human" } } });
      }
    },
    { stdin: "n\n" },
  );
  assert.equal(decision, "cancel", "a critical gate must never be auto-approved by a flag");
  assert.equal(code, 2);
});

test("a critical gate prompt names the secret's key and host, never a value", async () => {
  const { stderr } = await withCli(
    ["run", "x"],
    (msg, api) => {
      if (msg.type === "run") {
        api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
        api.send({ type: "event", id: msg.id, event: "gate", gateId: "g1", critical: true, secretKey: "GH_TOKEN", secretHost: "github.com", reason: "secret use" });
      }
      if (msg.type === "gate_reply") api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
    },
    { stdin: "y\n" },
  );
  assert.match(stderr, /CRITICAL/);
  assert.match(stderr, /GH_TOKEN/);
  assert.match(stderr, /github\.com/);
});

test("a panel-handled gate is reported, not prompted", async () => {
  const { code, stderr } = await withCli(["run", "x", "--attach", "panel"], (msg, api) => {
    if (msg.type !== "run") return;
    assert.equal(msg.task.attach, "panel");
    api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1, attached: "panel" });
    api.send({ type: "event", id: msg.id, event: "gate", handledInPanel: true, critical: true, reason: "approve in the panel" });
    api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
  });
  assert.match(stderr, /side panel/);
  assert.equal(code, 0);
});

test("a non-TTY invocation defaults to refusing gates", async () => {
  let approvals = null;
  await withCli(["run", "x"], (msg, api) => {
    if (msg.type !== "run") return;
    approvals = msg.task.approvals;
    api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
    api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
  });
  assert.equal(approvals, "deny");
});

test("--deny-gates is passed through", async () => {
  let approvals = null;
  await withCli(["run", "x", "--deny-gates"], (msg, api) => {
    if (msg.type !== "run") return;
    approvals = msg.task.approvals;
    api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
    api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
  });
  assert.equal(approvals, "deny");
});

test("options reach the task: url, mode, vision, max-steps, vars, dry-run", async () => {
  let task = null;
  await withCli(
    ["run", "do it", "--url", "https://example.com", "--mode", "agentic", "--vision", "on", "--max-steps", "42", "--var", "user=ada", "--var", "pw=a=b", "--dry-run"],
    (msg, api) => {
      if (msg.type !== "run") return;
      task = msg.task;
      api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
      api.send({ type: "event", id: msg.id, event: "done", record: { result: { status: "pass" } } });
    },
  );
  assert.equal(task.instruction, "do it");
  assert.equal(task.startUrl, "https://example.com");
  assert.equal(task.mode, "agentic");
  assert.equal(task.vision, "on");
  assert.equal(task.maxSteps, 42);
  assert.equal(task.dryRun, true);
  assert.deepEqual(task.variables, { user: "ada", pw: "a=b" });
});

test("an extension-side error exits 1 with the detail on stderr", async () => {
  const { code, stderr } = await withCli(["run", "x"], (msg, api) => {
    if (msg.type === "run") api.send({ type: "event", id: msg.id, event: "error", error: "run_failed", detail: "no LLM endpoint configured" });
  });
  assert.equal(code, 1);
  assert.match(stderr, /no LLM endpoint configured/);
});

test("a stopped run exits 2, not 1 — it didn't fail, it didn't finish", async () => {
  const { code } = await withCli(["run", "x"], (msg, api) => {
    if (msg.type === "run") {
      api.send({ type: "event", id: msg.id, event: "accepted", tabId: 1 });
      api.send({ type: "event", id: msg.id, event: "error", error: "stopped", detail: "Run stopped" });
    }
  });
  assert.equal(code, 2);
});

test("a busy extension exits 3 (transport), not 1", async () => {
  const { code } = await withCli(["run", "x"], (msg, api) => {
    if (msg.type === "run") api.send({ type: "event", id: msg.id, event: "error", error: "busy", detail: "already running" });
  });
  assert.equal(code, 3);
});

test("no helper listening exits 3 with a hint", async () => {
  const { code, stderr } = await runCli(["run", "x", "--port", "1", "--token", TOKEN], "");
  assert.equal(code, 3);
  assert.match(stderr, /cannot reach the helper/);
});

test("no token exits 3", async () => {
  const { code, stderr } = await runCli(["run", "x", "--port", "9"], "");
  assert.equal(code, 3);
  assert.match(stderr, /no token/);
});

test("nothing to run exits 3", async () => {
  const { code, stderr } = await runCli(["run", "--token", TOKEN], "");
  assert.equal(code, 3);
  assert.match(stderr, /nothing to run/);
});

const run = async () => {
  for (const [name, fn] of tests) {
    try { await fn(); passed++; console.log(`  ✓ ${name}`); }
    catch (e) { console.error(`  ✗ ${name}\n    ${e.message}`); process.exitCode = 1; }
  }
  console.log(`\n${passed}/${tests.length} passed`);
};
run();
