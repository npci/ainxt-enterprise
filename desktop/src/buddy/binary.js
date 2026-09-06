// SPDX-License-Identifier: MIT
"use strict";
/**
 * Resolves — and if necessary installs — the `ainxt` CLI that powers Buddy and
 * the Code tab. Everything local-agent runs through the CLI, so if this module
 * returns null those features are simply unavailable.
 *
 * The binary FILENAME does not encode which protocol the CLI speaks. Protocol is
 * chosen by AINXT_CLI_PROTOCOL alone (see ./protocol.js); this module only finds
 * an executable.
 *
 * Resolution order (first hit wins):
 *   1. BUDDY_CLI_BIN            explicit absolute path — dev / CI / testing.
 *   2. resources/bin/<name>     bundled with a packaged app, when the build
 *                               ships one (see `extraResources` in package.json).
 *   3. $AINXT_BIN_DIR/ainxt     where the CLI's own installer puts it; defaults
 *      or ~/.ainxt/bin/ainxt    to ~/.ainxt/bin. This is the normal case for a
 *                               user who ran the one-line install.
 *   4. `ainxt` on PATH          for users who installed it system-wide.
 *   5. sibling repo build       ../../ainxt-cli/target/release/ainxt — a local
 *                               `cargo build --release` in a checkout next door.
 *
 * If none of those hit, `ensureCliBinary()` can install the CLI. That downloads
 * and executes a script from the network, so it NEVER runs unattended: the caller
 * must pass `{ consent: true }`, which the UI only sets after the user agrees.
 */
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { resolveProtocol } = require("./protocol");

let _app = null;
try { _app = require("electron").app; } catch { /* not in an electron context */ }

// Where the CLI installer places the binary. Mirrors AINXT_BIN_DIR in the CLI's
// install.sh; keep the default in step with it.
function cliBinDir() {
  return process.env.AINXT_BIN_DIR || path.join(os.homedir(), ".ainxt", "bin");
}

// The canonical binary name per platform. Identical across CLI versions — the
// protocol flag, not the name, selects behaviour.
function platformBinaryName() {
  if (process.platform === "win32") return "ainxt-windows-x64.exe";
  if (process.platform === "darwin") {
    return process.arch === "arm64" ? "ainxt-macos-arm64" : "ainxt-macos-x64";
  }
  return process.arch === "arm64" ? "ainxt-linux-arm64" : "ainxt-linux-x64";
}

// The plain name the installer symlinks, and what users type in a shell.
function plainBinaryName() {
  return process.platform === "win32" ? "ainxt.exe" : "ainxt";
}

function isExecutableFile(p) {
  try {
    if (!fs.statSync(p).isFile()) return false;
    if (process.platform === "win32") return true;
    fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// `ainxt` on PATH, without shelling out (`which` is not portable to Windows).
function findOnPath() {
  const name = plainBinaryName();
  const raw = process.env.PATH || "";
  const dirs = raw.split(path.delimiter).filter(Boolean);
  const exts = process.platform === "win32"
    ? (process.env.PATHEXT || ".EXE;.CMD;.BAT").split(";").filter(Boolean)
    : [""];
  for (const dir of dirs) {
    for (const ext of exts) {
      const candidate = path.join(dir, ext ? name.replace(/\.exe$/i, "") + ext : name);
      if (isExecutableFile(candidate)) return candidate;
    }
  }
  return null;
}

function resolveCliBinary() {
  const protocol = resolveProtocol();
  const found = (command) => ({ mode: "binary", command, args: [], protocol });

  // 1. Explicit override always wins.
  const override = process.env.BUDDY_CLI_BIN;
  if (override && isExecutableFile(override)) return found(override);

  // 2. Bundled inside a packaged app, if this build shipped one.
  try {
    if (_app && _app.isPackaged) {
      const bundled = path.join(process.resourcesPath, "bin", platformBinaryName());
      if (isExecutableFile(bundled)) return found(bundled);
    }
  } catch { /* not packaged */ }

  // 3. Where the CLI's own installer puts it. Both the plain name (a symlink the
  //    installer creates) and the platform name (the real file) are checked.
  const binDir = cliBinDir();
  for (const n of [plainBinaryName(), platformBinaryName()]) {
    const p = path.join(binDir, n);
    if (isExecutableFile(p)) return found(p);
  }

  // 4. Installed system-wide.
  const onPath = findOnPath();
  if (onPath) return found(onPath);

  // 5. A release build in a sibling checkout (contributors working on both repos).
  const sibling = path.resolve(
    __dirname, "..", "..", "..", "..", "ainxt-cli", "target", "release", plainBinaryName(),
  );
  if (isExecutableFile(sibling)) return found(sibling);

  return null;
}

// ── Installation ─────────────────────────────────────────────────────────────
// The CLI publishes a one-line installer. We invoke that rather than reimplement
// release-asset discovery and checksum verification here, so there is exactly one
// place where "how the CLI is installed" is defined.

const DEFAULT_INSTALL_URL =
  process.env.AINXT_CLI_INSTALL_URL
  || "https://raw.githubusercontent.com/npci/ainxt-cli/main/crates/codegen/ainxt-pager/scripts/install.sh";

const DEFAULT_INSTALL_URL_PS1 =
  process.env.AINXT_CLI_INSTALL_URL_PS1
  || "https://raw.githubusercontent.com/npci/ainxt-cli/main/crates/codegen/ainxt-pager/scripts/install.ps1";

/**
 * The command a user can run by hand — also what we run for them. Returned so the
 * UI can show exactly what it is about to execute before asking for consent.
 */
function cliInstallCommand() {
  if (process.platform === "win32") {
    return {
      shell: "powershell.exe",
      args: ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             `irm ${DEFAULT_INSTALL_URL_PS1} | iex`],
      display: `irm ${DEFAULT_INSTALL_URL_PS1} | iex`,
      url: DEFAULT_INSTALL_URL_PS1,
    };
  }
  return {
    shell: "/bin/sh",
    args: ["-c", `curl -fsSL ${DEFAULT_INSTALL_URL} | AINXT_REQUIRE_CHECKSUM=1 sh`],
    // AINXT_REQUIRE_CHECKSUM=1 makes the installer refuse anything it cannot
    // verify. We opt in deliberately: this runs without a human watching the
    // output, so "install something unverifiable" is not an acceptable outcome.
    display: `curl -fsSL ${DEFAULT_INSTALL_URL} | AINXT_REQUIRE_CHECKSUM=1 sh`,
    url: DEFAULT_INSTALL_URL,
  };
}

/**
 * Download and install the CLI. Resolves { ok, binary?, error?, log }.
 *
 * Requires explicit consent — this fetches and executes a remote script, which is
 * not something to do on a user's machine on their behalf without asking.
 */
function installCli({ consent = false, onOutput = null, timeoutMs = 180000 } = {}) {
  return new Promise((resolve) => {
    if (!consent) {
      resolve({ ok: false, error: "consent_required", log: "" });
      return;
    }
    const cmd = cliInstallCommand();
    const emit = (stream, text) => {
      if (onOutput) { try { onOutput(stream, text); } catch { /* ignore */ } }
    };
    emit("info", `Installing the AiNxt CLI:\n  ${cmd.display}\n`);

    let log = "";
    let settled = false;
    let proc;
    try {
      proc = spawn(cmd.shell, cmd.args, { env: { ...process.env } });
    } catch (e) {
      resolve({ ok: false, error: `could not start installer: ${e.message}`, log });
      return;
    }

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    // Always bounded: a hung download must not leave the UI waiting forever.
    const timer = setTimeout(() => {
      try { proc.kill("SIGKILL"); } catch { /* already gone */ }
      finish({ ok: false, error: `installer timed out after ${Math.round(timeoutMs / 1000)}s`, log });
    }, timeoutMs);

    const capture = (stream) => (buf) => {
      const text = buf.toString();
      log += text;
      emit(stream, text);
    };
    proc.stdout.on("data", capture("stdout"));
    proc.stderr.on("data", capture("stderr"));

    proc.on("error", (e) => finish({ ok: false, error: e.message, log }));

    proc.on("close", (code) => {
      // Do not trust the exit code alone — confirm a usable binary actually
      // exists, because an installer that prints a warning and exits 0 would
      // otherwise look like success.
      const binary = resolveCliBinary();
      if (binary) {
        finish({ ok: true, binary, log });
      } else {
        finish({
          ok: false,
          error: `installer exited ${code} but no CLI was found in ${cliBinDir()} or on PATH`,
          log,
        });
      }
    });
  });
}

/**
 * Resolve the CLI, installing it first if it is missing and consent was given.
 * Callers that cannot prompt should keep using resolveCliBinary() directly.
 */
async function ensureCliBinary({ consent = false, onOutput = null } = {}) {
  const existing = resolveCliBinary();
  if (existing) return { ok: true, binary: existing, installed: false };

  const result = await installCli({ consent, onOutput });
  if (result.ok) return { ok: true, binary: result.binary, installed: true };
  return { ok: false, error: result.error, log: result.log, installed: false };
}

/** One actionable sentence for the user when the CLI is missing. */
function missingCliMessage() {
  return "The AiNxt CLI is required for Buddy and the Code tab, and was not found. "
    + `Install it with:\n  ${cliInstallCommand().display}\n`
    + `It installs to ${cliBinDir()}. If it is already installed somewhere else, `
    + "point the app at it with BUDDY_CLI_BIN=/path/to/ainxt.";
}

module.exports = {
  resolveCliBinary,
  platformBinaryName,
  plainBinaryName,
  cliBinDir,
  cliInstallCommand,
  installCli,
  ensureCliBinary,
  missingCliMessage,
};
