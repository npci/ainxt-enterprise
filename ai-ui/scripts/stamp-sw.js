// SPDX-License-Identifier: MIT
// ============================================================
// Post-build service-worker version stamper
// ------------------------------------------------------------
// Files in public/ are copied verbatim by Vite (not hashed), so dist/sw.js
// would be byte-identical across deploys — which means browsers would never
// detect a service-worker update and users would keep the old cached UI.
//
// This script runs AFTER `vite build` and replaces the __BUILD_HASH__
// placeholder in dist/sw.js with a unique per-build token. Changing those
// bytes is exactly what triggers the browser's SW update flow on the next
// visit (updatefound → SKIP_WAITING → controllerchange → silent reload).
//
// It also writes dist/version.json for diagnostics / optional in-app checks.
//
// Safe by design: if dist/sw.js is missing or the placeholder is absent,
// the script logs a warning and exits 0 so it never breaks the build.
// ============================================================

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";
import { randomBytes } from "node:crypto";

const __dirname = dirname(fileURLToPath(import.meta.url));
const distDir = resolve(__dirname, "..", "dist");
const swPath = resolve(distDir, "sw.js");
const versionPath = resolve(distDir, "version.json");
const PLACEHOLDER = "__BUILD_HASH__";

// Build a unique, human-readable token: <git-sha?>-<timestamp>-<rand>
function buildToken() {
  let sha = "";
  try {
    sha = execSync("git rev-parse --short HEAD", { stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim();
  } catch {
    // not a git checkout / git unavailable — fall back to time+random only
  }
  const ts = Date.now().toString(36);
  const rand = randomBytes(4).toString("hex");
  return [sha, ts, rand].filter(Boolean).join("-");
}

function main() {
  if (!existsSync(swPath)) {
    console.warn(`[stamp-sw] WARNING: ${swPath} not found — skipping (build not broken).`);
    return; // exit 0
  }

  const token = buildToken();
  const original = readFileSync(swPath, "utf8");

  if (!original.includes(PLACEHOLDER)) {
    console.warn(
      `[stamp-sw] WARNING: placeholder "${PLACEHOLDER}" not found in dist/sw.js. ` +
      `SW will not be re-versioned this build.`
    );
  }

  // Replace ALL occurrences (currently one) to be safe.
  const stamped = original.split(PLACEHOLDER).join(token);
  writeFileSync(swPath, stamped);

  // Emit a small version manifest for diagnostics / optional polling.
  writeFileSync(
    versionPath,
    JSON.stringify({ version: token, builtAt: new Date().toISOString() }, null, 2)
  );

  console.log(`[stamp-sw] dist/sw.js stamped with build token: ${token}`);
  console.log(`[stamp-sw] wrote dist/version.json`);
}

main();
