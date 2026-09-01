// SPDX-License-Identifier: Apache-2.0
"use strict";
/**
 * Cowork auth helpers. The CLI owns the real auth lifecycle (JWT in
 * ~/.ainxt/config.json, auto-refresh). Cowork only needs to know whether a token
 * is present (to gate local-agent mode) and to drive `ainxt login` for sign-in.
 *
 * Note: this is the CLI's own gateway token, separate from the web UI's
 * httpOnly session cookie. A user may be signed into the web app but still need
 * `ainxt login` once to enable local-agent mode.
 */
const path = require("path");
const fs = require("fs");
const os = require("os");
const http = require("http");
const https = require("https");
const { spawn } = require("child_process");
const { resolveCliBinary, missingCliMessage } = require("./binary");

function configPath() {
  return path.join(os.homedir(), ".ainxt", "config.json");
}

// The CLI ALSO persists its own gateway credential in ~/.ainxt/credentials.json
// (written by `ainxt login`), which can hold a DIFFERENT, still-valid token than
// config.json — e.g. after the gateway rotates/revokes the config.json api_key
// but leaves this one live. We read it as an extra candidate for
// resolveValidToken so a dead config.json token doesn't 401 every MCP call when
// a good credential is sitting right next to it.
function credentialsPath() {
  return path.join(os.homedir(), ".ainxt", "credentials.json");
}
function readCredentialsToken() {
  try {
    const c = JSON.parse(fs.readFileSync(credentialsPath(), "utf-8"));
    return c.accessToken || c.access_token || c.token || c.jwt || "";
  } catch {
    return "";
  }
}

// Gateway API path prefix — env-overridable so a routing change never needs a
// code change (mirrors main.js API_PREFIX). Trailing slashes stripped.
const API_PREFIX = String(process.env.AINXT_API_PREFIX || "/ainxt/v1/api").replace(/\/+$/, "");
// TEST-ONLY: when AINXT_TLS_INSECURE=1 the main process must ALSO skip TLS
// verification for its Node https calls (/auth/me validation, api-key mint, SSO
// exchange). The Chromium `ignore-certificate-errors` switch in main.js only
// covers the renderer/BrowserWindow — Node's https module has its own trust
// store, so without this a self-signed gateway cert silently fails every
// main-process request → no token ever validates → false "session expired".
// This is the SAME env var name the Rust CLI reads (AINXT_TLS_INSECURE) —
// they must match, or the desktop's own TLS bypass silently stays off.
const INSECURE_TLS = process.env.AINXT_INSECURE_TLS === "1" && process.env.NODE_ENV !== "production";
// Reused agent so we don't spawn a fresh TLS context per request.
const _insecureAgent = INSECURE_TLS ? new https.Agent({ rejectUnauthorized: false }) : new https.Agent({ rejectUnauthorized: true });

// Encrypted-at-rest secrets in electron-store via safeStorage (OS-backed: DPAPI /
// Keychain). Falls back to plaintext only where OS encryption is unavailable.
// Used for the long-lived CLI API key (no sid/exp → bypasses the session-registry
// 401 that broke Buddy, and survives restarts) and the Entra refresh token.
function _makeSecret(storeKey) {
  return {
    read(store, safeStorage) {
      try {
        const enc = store && store.get(storeKey, "");
        if (!enc) return "";
        if (safeStorage && safeStorage.isEncryptionAvailable()) {
          return safeStorage.decryptString(Buffer.from(enc, "base64"));
        }
        return enc; // plaintext fallback
      } catch { return ""; }
    },
    write(store, safeStorage, value) {
      if (!store || !value) return;
      try {
        if (safeStorage && safeStorage.isEncryptionAvailable()) {
          store.set(storeKey, safeStorage.encryptString(value).toString("base64"));
        } else {
          store.set(storeKey, value);
        }
      } catch { /* best-effort */ }
    },
    clear(store) { try { if (store) store.delete(storeKey); } catch { /* noop */ } },
  };
}

// Append-only diagnostics: records validateToken outcomes (HTTP status / TLS /
// network errors) to ~/.ainxt/desktop-auth.log so a "Session expired" with no
// visible cause can be diagnosed without DevTools.
function _log(...parts) {
  try {
    const dir = path.join(os.homedir(), ".ainxt");
    try { fs.mkdirSync(dir, { recursive: true }); } catch { /* exists */ }
    fs.appendFileSync(path.join(dir, "desktop-auth.log"), `[${new Date().toISOString()}] auth.js ${parts.join(" ")}\n`);
  } catch { /* best-effort */ }
}

const _apiKeySecret = _makeSecret("coworkApiKeyEnc");
const _refreshSecret = _makeSecret("ssoRefreshEnc");

const readApiKey       = (s, ss) => _apiKeySecret.read(s, ss);
const writeApiKey      = (s, ss, v) => _apiKeySecret.write(s, ss, v);
const clearApiKey      = (s) => _apiKeySecret.clear(s);
const readRefreshToken = (s, ss) => _refreshSecret.read(s, ss);
const writeRefreshToken = (s, ss, v) => _refreshSecret.write(s, ss, v);
const clearRefreshToken = (s) => _refreshSecret.clear(s);

// Gateway URL used only when the CLI's own config.json has none recorded yet.
// No hardcoded localhost: PLATFORM_BASE_URL is the same platform-wide env var
// main.js's DEFAULT_API reads, so a deployment sets it once and both agree.
// Empty string means "unknown" — callers must treat that as "not configured"
// rather than silently trying a port nothing is listening on.
function _defaultGatewayUrl() {
  return process.env.PLATFORM_BASE_URL || "";
}

function readAuthState() {
  try {
    const cfg = JSON.parse(fs.readFileSync(configPath(), "utf-8"));
    const hasToken = Boolean(
      cfg.access_token || cfg.token || cfg.jwt ||
      (cfg.oauth && (cfg.oauth.access_token || cfg.oauth.refresh_token))
    );
    return { authenticated: hasToken, gatewayUrl: cfg.gateway_url || _defaultGatewayUrl(), error: null };
  } catch {
    return { authenticated: false, gatewayUrl: null, error: "not_configured" };
  }
}

/**
 * The CLI's gateway token (JWT or API key) from ~/.ainxt/config.json. This is
 * the SAME credential the local agent authenticates to the gateway with, so
 * Cowork's gateway-side tools (connector MCP, memory, run_code, doc-gen) must
 * use it too — NOT the renderer's electron-store `lastToken`, which is a
 * separate web-login value that is often empty. Returns "" if not configured.
 */
function readToken() {
  try {
    const cfg = JSON.parse(fs.readFileSync(configPath(), "utf-8"));
    return (
      cfg.jwt || cfg.access_token || cfg.token ||
      (cfg.oauth && (cfg.oauth.access_token)) || ""
    );
  } catch {
    return "";
  }
}

/**
 * Persist a VALIDATED token into ~/.ainxt/config.json so the spawned CLI (cli.tsx
 * reads config.json → sets AINXT_JWT for the whole process, including in-process
 * sub-agents) always has a good token. Without this, a stale/bad config.json token
 * (e.g. one `ainxt login` wrote) makes getAnthropicClient drop gateway mode and a
 * sub-agent hits the real Anthropic API → "model claude-sonnet-4-6 not found" (404).
 *
 * extraFields: optional additional fields to write (e.g. { email, auth_method }).
 * Existing fields are preserved; only missing or changed fields are updated.
 */
function writeToken(token, gatewayUrl, extraFields = {}) {
  if (!token) return;
  try {
    const p = configPath();
    // Create ~/.ainxt/ and config.json if they don't exist yet (first launch).
    // Without this, writeFileSync below silently fails on a fresh install and
    // readAuthState() returns authenticated:false even after a successful silentAdopt.
    try { fs.mkdirSync(path.dirname(p), { recursive: true }); } catch { /* exists */ }
    let cfg = {};
    try { cfg = JSON.parse(fs.readFileSync(p, "utf-8")); } catch { /* new file */ }
    let changed = false;
    if (cfg.jwt !== token) { cfg.jwt = token; changed = true; }
    // Self-heal the gateway URL too: a stray CLI test mode can point config.json
    // at an ephemeral mock gateway (e.g. http://127.0.0.1:<random>), which then
    // breaks all connectivity. Restore the real gateway whenever we have it.
    if (gatewayUrl && cfg.gateway_url !== gatewayUrl) { cfg.gateway_url = gatewayUrl; changed = true; }
    // Ensure auth_method is set — the CLI uses this to know the token is an API
    // key (not a JWT/OAuth token). Without it, the CLI may default to JWT mode
    // and fail to authenticate with an API key format token (e.g. "user-uuid"),
    // causing it to exit immediately with no output on first launch.
    // Preserve any existing auth_method (e.g. "oauth" from `ainxt login`).
    if (!cfg.auth_method) { cfg.auth_method = "api_key"; changed = true; }
    // Write any extra fields (e.g. email) passed by the caller — best-effort,
    // only update if the value is non-empty and different from what's stored.
    for (const [k, v] of Object.entries(extraFields)) {
      if (v && cfg[k] !== v) { cfg[k] = v; changed = true; }
    }
    if (changed) fs.writeFileSync(p, JSON.stringify(cfg, null, 2));
  } catch { /* best-effort */ }
}

/** Gateway base URL from the CLI config, falling back to PLATFORM_BASE_URL. */
function readGatewayUrl() {
  try {
    const cfg = JSON.parse(fs.readFileSync(configPath(), "utf-8"));
    return cfg.gateway_url || cfg.gatewayUrl || _defaultGatewayUrl();
  } catch {
    return _defaultGatewayUrl();
  }
}

/**
 * Validate a gateway token by hitting the cheap authed `/auth/me` endpoint.
 * Resolves true only on HTTP 200. A spawned agent with an invalid token (e.g.
 * a stale/synthetic JWT in config.json) otherwise fails every request with 401
 * and crashes the CLI's cost-tracking on the resulting undefined usage. We catch
 * that here and surface a clean re-login instead. Empty token → false.
 */
function validateToken(token, gatewayUrl) {
  return new Promise((resolve) => {
    if (!token) return resolve(false);
    let url;
    try {
      url = new URL(`${API_PREFIX}/auth/me`, gatewayUrl || _defaultGatewayUrl());
    } catch { return resolve(false); }
    const lib = url.protocol === "https:" ? https : http;
    const opts = { method: "GET", headers: { Authorization: `Bearer ${token}` }, timeout: 4000 };
    // Honour AINXT_TLS_INSECURE for the self-signed AiNxt gateway cert (see top).
    if (url.protocol === "https:" && _insecureAgent) opts.agent = _insecureAgent;
    const req = lib.request(
      url,
      opts,
      (res) => {
        res.resume(); // drain
        _log("validateToken", `${url.href}`, `status=${res.statusCode}`, `insecure=${!!_insecureAgent}`);
        resolve(res.statusCode === 200);
      },
    );
    req.on("error", (e) => { _log("validateToken ERROR", `${url.href}`, `${e && e.code || ""} ${e && e.message || e}`, `insecure=${!!_insecureAgent}`); resolve(false); });
    req.on("timeout", () => { _log("validateToken TIMEOUT", `${url.href}`); req.destroy(); resolve(false); });
    req.end();
  });
}

/**
 * Resolve the best WORKING gateway token for a Cowork session. Prefers the CLI
 * config token (the agent's own credential), falls back to the renderer's
 * electron-store `lastToken` (the web-login JWT), validating each against the
 * gateway. Returns { token, gatewayUrl } with token="" if neither is valid —
 * the caller must then prompt re-login rather than spawn a doomed session.
 */
async function resolveValidToken(fallbackToken, gatewayUrl, apiKey) {
  const gw = gatewayUrl || readGatewayUrl();
  // Priority: long-lived API key (durable, no expiry) → CLI config token →
  // CLI credentials.json token (a separate, sometimes-fresher CLI credential) →
  // renderer's web-login JWT. First that authenticates (HTTP 200 at /auth/me)
  // wins. De-duplicated so we don't validate the same token twice.
  const candidates = [...new Set(
    [apiKey || "", readToken(), readCredentialsToken(), fallbackToken || ""].filter(Boolean),
  )];
  for (const tok of candidates) {
    if (await validateToken(tok, gw)) return { token: tok, gatewayUrl: gw };
  }
  return { token: "", gatewayUrl: gw };
}

/**
 * Run `ainxt login`, streaming stdout/stderr to onOutput so the renderer can
 * show the device-code / browser prompt. Resolves with post-login auth state.
 */
// Handle to the currently-running login subprocess so it can be KILLED on timeout
// or explicit cancel (G8). Without this, a hung `ainxt login` (waiting on an
// interactive prompt that never gets input) leaked forever, kept the UI on
// "Signing in…", and a retry spawned a SECOND concurrent login.
let _activeLoginChild = null;
const LOGIN_TIMEOUT_MS = Math.max(30000, parseInt(process.env.BUDDY_LOGIN_TIMEOUT_MS || "120000", 10));

function cancelLogin() {
  const c = _activeLoginChild;
  if (!c) return false;
  try {
    if (process.platform !== "win32" && c.pid) process.kill(-c.pid, "SIGKILL");
    else c.kill("SIGKILL");
  } catch { try { c.kill(); } catch { /* gone */ } }
  _activeLoginChild = null;
  return true;
}

function runLogin(onOutput) {
  return new Promise((resolve) => {
    const bin = resolveCliBinary();
    if (!bin) {
      onOutput("error", missingCliMessage());
      return resolve(readAuthState());
    }
    // If a previous login is still hanging around, kill it before starting a new one
    // so we never run two concurrent logins fighting over config.json.
    cancelLogin();
    const child = spawn(bin.command, [...bin.args, "login"], {
      env: { ...process.env, FORCE_COLOR: "0" },
      detached: process.platform !== "win32",   // own process group → killable as a unit
    });
    _activeLoginChild = child;
    let settled = false;
    const done = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (_activeLoginChild === child) _activeLoginChild = null;
      resolve(result);
    };
    // Self-kill on timeout so a stuck interactive prompt can't hang forever.
    const timer = setTimeout(() => {
      onOutput("error", "Sign-in timed out — closing the login process. Please try again.");
      cancelLogin();
      done(readAuthState());
    }, LOGIN_TIMEOUT_MS);
    if (timer.unref) timer.unref();
    child.stdout.on("data", (d) => onOutput("stdout", d.toString()));
    child.stderr.on("data", (d) => onOutput("stderr", d.toString()));
    child.on("error", (err) => { onOutput("error", `Failed to launch login: ${err.message}`); done(readAuthState()); });
    child.on("close", (code) => { onOutput("exit", `login exited with code ${code}`); done(readAuthState()); });
  });
}

module.exports = {
  readAuthState, runLogin, cancelLogin, configPath, readToken, readGatewayUrl,
  validateToken, resolveValidToken, writeToken,
  readApiKey, writeApiKey, clearApiKey,
  readRefreshToken, writeRefreshToken, clearRefreshToken, _log,
};
