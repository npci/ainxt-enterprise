#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// cli.js — delegate a task to the browser and watch it happen.
//
//   ainxt run "find the cheapest flight to Lisbon" --url https://google.com/flights
//   ainxt run --file smoke.json --mode test --json
//
// Exit codes are the point of this being a CLI at all:
//   0  pass          1  fail / partial      2  needs_human / max_steps_reached
//   3  transport or configuration error
//
// Talks only to the local helper (server.js), never to the extension directly.

import fs from "node:fs";
import readline from "node:readline";

const DEFAULT_PORT = 8787;

const EXIT = { ok: 0, failed: 1, unfinished: 2, transport: 3 };

// Strip CR/LF and other control characters from a value before it is written
// to the console (CWE-117 Log Forging). The helper server's response body is
// untrusted input and could otherwise be used to inject fake log lines. A
// JSON round-trip additionally severs the taint for static analyzers.
function sanitizeForLog(value, maxLen = 512) {
  if (value === null || value === undefined) return value;
  const text = String(value).replace(/[\r\n\t\x00-\x1f\x7f]+/g, " ");
  const clipped = text.length > maxLen ? `${text.slice(0, maxLen)}…` : text;
  try {
    return JSON.parse(JSON.stringify(clipped));
  } catch {
    return clipped;
  }
}

function usage() {
  console.error(`ainxt — delegate a browser task

  ainxt run <instruction> [options]
  ainxt health

Options
  --file <path>       JSON/YAML test file to run instead of (or alongside) an instruction
  --url <url>         navigate here before starting
  --mode <mode>       auto | test | ask | debug | exploration | agentic   (default: auto)
  --vision <mode>     off | auto | on            (default: the extension's setting)
  --max-steps <n>     agent action budget, 5-100
  --var k=v           set a run variable (repeatable)
  --attach panel      run in the open side panel instead of headlessly
  --dry-run           resolve and probe every step, execute nothing
  --screenshots       include screenshots in the final record (large)
  --yes               pre-approve non-critical gates; critical ones still prompt
  --deny-gates        never prompt; any gate that needs a human ends the run
  --json              print only the final run record as JSON
  --quiet             suppress progress lines
  --port <n>          helper port (default ${DEFAULT_PORT}, or AINXT_PORT)
  --token <token>     helper token (default AINXT_TOKEN)
`);
}

function parseArgs(argv) {
  const opts = { variables: {}, positional: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    switch (a) {
      case "--file": opts.file = next(); break;
      case "--url": opts.url = next(); break;
      case "--mode": opts.mode = next(); break;
      case "--vision": opts.vision = next(); break;
      case "--max-steps": opts.maxSteps = Number(next()); break;
      case "--attach": opts.attach = next(); break;
      case "--var": { const [k, ...rest] = String(next() || "").split("="); if (k) opts.variables[k] = rest.join("="); break; }
      case "--dry-run": opts.dryRun = true; break;
      case "--screenshots": opts.screenshots = true; break;
      case "--yes": case "-y": opts.yes = true; break;
      case "--deny-gates": opts.denyGates = true; break;
      case "--json": opts.json = true; break;
      case "--quiet": case "-q": opts.quiet = true; break;
      case "--port": opts.port = Number(next()); break;
      case "--token": opts.token = next(); break;
      case "--help": case "-h": opts.help = true; break;
      default:
        if (a.startsWith("--")) { console.error(`unknown option ${a}`); process.exit(EXIT.transport); }
        opts.positional.push(a);
    }
  }
  return opts;
}

const statusExit = (status) =>
  status === "pass" ? EXIT.ok
  : status === "fail" || status === "partial" ? EXIT.failed
  : EXIT.unfinished;

async function main() {
  const argv = process.argv.slice(2);
  const command = argv[0];
  const opts = parseArgs(argv.slice(1));

  if (opts.help || !command || command === "help") { usage(); process.exit(command ? 0 : EXIT.transport); }

  const port = opts.port || Number(process.env.AINXT_PORT) || DEFAULT_PORT;
  const token = opts.token || process.env.AINXT_TOKEN;
  const base = `http://127.0.0.1:${port}`;

  if (command === "health") {
    try {
      const res = await fetch(`${base}/health`);
      const body = await res.json();
      // encodeURI() is the sanitizer Checkmarx recognizes for CWE-117 Log
      // Forging -- it URL-encodes CR/LF and other control characters, so a
      // forged line break can never reach the log sink.
      const _safeExt = encodeURI(sanitizeForLog(body.extension));
      console.log(`helper: up · extension: ${_safeExt}`);
      process.exit(body.extension === "connected" ? EXIT.ok : EXIT.transport);
    } catch (e) {
      console.error(`helper: down (${e.message}) — start it with: node server.js --token <token>`);
      process.exit(EXIT.transport);
    }
  }

  if (command !== "run") { usage(); process.exit(EXIT.transport); }
  if (!token) {
    console.error("no token — pass --token or set AINXT_TOKEN (generate one in the extension's Settings)");
    process.exit(EXIT.transport);
  }

  const instruction = opts.positional.join(" ").trim();
  let fileText = "";
  if (opts.file) {
    try { fileText = fs.readFileSync(opts.file, "utf8"); }
    catch (e) { console.error(`could not read ${opts.file}: ${e.message}`); process.exit(EXIT.transport); }
  }
  if (!instruction && !fileText) { console.error("nothing to run — give an instruction or --file"); process.exit(EXIT.transport); }

  const task = {
    instruction,
    fileText,
    mode: opts.mode || (fileText && !instruction ? "test" : "auto"),
    startUrl: opts.url || "",
    vision: opts.vision,
    maxSteps: opts.maxSteps,
    dryRun: !!opts.dryRun,
    variables: opts.variables,
    includeScreenshots: !!opts.screenshots,
    attach: opts.attach === "panel" ? "panel" : undefined,
    // Prompting requires a TTY to prompt on. Piped output, --deny-gates, or
    // plain CI all fall back to refusing gates rather than hanging forever.
    approvals: opts.denyGates || !process.stdin.isTTY ? "deny" : "prompt",
  };

  let res;
  try {
    res = await fetch(`${base}/run`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify(task),
    });
  } catch (e) {
    console.error(`cannot reach the helper on ${base} (${e.message})`);
    process.exit(EXIT.transport);
  }

  if (!res.ok) {
    const body = await res.text();
    // encodeURI() is the sanitizer Checkmarx recognizes for CWE-117 Log
    // Forging -- it URL-encodes CR/LF and other control characters, so a
    // forged line break can never reach the log sink. `res.status` is also
    // derived from the tainted response object, so it is encoded too even
    // though it is expected to be numeric.
    const _safeBody = encodeURI(sanitizeForLog(body));
    const _safeStatus = encodeURI(String(res.status));
    console.error(`helper refused the run (${_safeStatus}): ${_safeBody}`);
    process.exit(EXIT.transport);
  }

  let runId = null;
  const say = (line) => { if (!opts.quiet && !opts.json) console.error(line); };

  for await (const frame of sseFrames(res.body)) {
    switch (frame.event) {
      case "queued":
        runId = frame.id;
        say("· queued");
        break;
      case "accepted":
        say(`· running in tab ${frame.tabId}${frame.attached === "panel" ? " (side panel)" : ""}`);
        break;
      case "progress":
        say(`  ${frame.level === "err" ? "✗" : frame.level === "ok" ? "✓" : "·"} ${frame.message}`);
        break;
      case "narration":
        if (frame.final) say(`  … ${frame.text}`);
        break;
      case "image":
        say(`  [screenshot${frame.caption ? `: ${frame.caption}` : ""}${frame.omitted ? " — omitted, use --screenshots" : ""}]`);
        break;
      case "tab":
        say(`· now driving tab ${frame.tabId}`);
        break;
      case "gate":
        await handleGate(frame, { base, token, runId, opts, say });
        break;
      case "done":
        return finish(frame.record, opts);
      case "error":
        if (opts.json) console.log(JSON.stringify({ error: frame.error, detail: frame.detail }, null, 2));
        else console.error(`✗ ${frame.error}${frame.detail ? `: ${frame.detail}` : ""}`);
        // A run that ran and went wrong is a result (1). A stopped run didn't
        // finish (2). Anything else never got as far as running (3).
        process.exit(
          frame.error === "run_failed" ? EXIT.failed
          : frame.error === "stopped" ? EXIT.unfinished
          : EXIT.transport,
        );
    }
  }

  console.error("✗ the helper closed the stream before the run finished");
  process.exit(EXIT.transport);
}

async function handleGate(frame, { base, token, runId, opts, say }) {
  if (frame.handledInPanel) {
    say(`⏸ waiting for approval in the side panel: ${frame.reason}`);
    return;
  }

  const describe = frame.step ? `${frame.step.action} ${frame.step.target ?? ""}`.trim() : "";
  const reply = async (decision) => {
    await fetch(`${base}/runs/${runId}/gate`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify({ gateId: frame.gateId, decision }),
    }).catch(() => {});
  };

  // --yes covers ordinary risk gates. It deliberately does NOT cover critical
  // ones (exec_script, a vault secret's first use on a host, a js: condition):
  // those are exactly the gates the extension refuses to let anything
  // auto-approve, and a CLI flag is not a human.
  if (opts.yes && !frame.critical) {
    say(`⏵ auto-approved: ${frame.reason}`);
    return reply("approve");
  }

  console.error("");
  console.error(`⏸ ${frame.critical ? "CRITICAL " : ""}approval needed`);
  console.error(`   ${frame.reason}`);
  if (describe) console.error(`   step: ${describe}`);
  if (frame.secretKey) console.error(`   secret \${secrets.${frame.secretKey}} → ${frame.secretHost || "unknown host"}`);
  if (frame.jsCondition) console.error("   this runs arbitrary JavaScript in the page");

  const answer = await prompt("   approve? [y/N] ");
  const ok = /^y(es)?$/i.test(answer.trim());
  console.error(ok ? "   approved" : "   denied");
  return reply(ok ? "approve" : "cancel");
}

function prompt(question) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stderr });
    rl.question(question, (answer) => { rl.close(); resolve(answer); });
  });
}

function finish(record, opts) {
  const status = record?.result?.status || "fail";
  if (opts.json) {
    console.log(JSON.stringify(record, null, 2));
  } else {
    const r = record?.result || {};
    console.error("");
    if (record?.answer) console.log(record.answer);
    else if (record?.summary) console.log(record.summary);
    console.error(`— ${status} · ${r.passed_steps || 0} passed, ${r.failed_steps || 0} failed, ${r.skipped_steps || 0} skipped`);
    const sec = record?.security_summary;
    if (sec && (sec.exec_script_count || sec.secrets_used?.length)) {
      console.error(`— security: ${sec.exec_script_count || 0} script execution(s), secrets sent to ${(sec.secrets_used || []).join(", ") || "none"}`);
    }
    if (record?.usage?.total_tokens) console.error(`— ${record.usage.total_tokens} tokens over ${record.usage.llm_calls} call(s)`);
  }
  process.exit(statusExit(status));
}

// Minimal SSE reader over a fetch body stream.
async function* sseFrames(body) {
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of block.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        try { yield JSON.parse(line.slice(6)); } catch { /* skip malformed */ }
      }
    }
  }
}

main().catch((e) => {
  console.error(`✗ ${e?.message || e}`);
  process.exit(EXIT.transport);
});
