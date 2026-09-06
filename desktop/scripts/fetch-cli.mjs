#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// desktop/scripts/fetch-cli.mjs
//
// Stage the ainxt CLI binary/binaries into desktop/resources/bin/ so
// electron-builder's `extraResources` embeds them in the packaged app.
//
// Run automatically by the electron-builder `beforePack` hook
// (desktop/scripts/beforePack.js); also `npm run fetch-cli` for manual use.
//
//   node scripts/fetch-cli.mjs --target=mac|win|linux|host
//
// Source resolution per asset (first that applies):
//   AINXT_CLI_SKIP_FETCH=1     do nothing (bundle whatever is already there)
//   <already in resources/bin/> reuse it — verified if a checksum is pinned,
//                              trusted as-is (with a note) if not yet pinned
//   AINXT_CLI_BIN_SRC=<path>   copy from a local dir or file (air-gapped)
//   AINXT_CLI_FROM_SOURCE=1    cargo build --release from a local ainxt-cli
//   <download>                 GET <download_base>/<remote name> — see
//                              cli-version.json's per-asset `remote` field:
//                              the upstream ainxt-cli release names its
//                              assets differently from our local canonical
//                              names (e.g. "ainxt-1.0.0-win32-x86_64.exe" on
//                              the release vs. "ainxt-windows-x64.exe" here).
//                              GITHUB_TOKEN present => treat the repo as
//                              PRIVATE: fetch via the GitHub REST API by
//                              asset ID (the only way that actually
//                              authenticates for a private repo — the plain
//                              github.com/.../releases/download/ URL does
//                              not accept a bearer token). GITHUB_TOKEN
//                              absent => treat the repo as PUBLIC: plain
//                              unauthenticated URL, as before.
//
// Two DIFFERENT failure classes — do not conflate them:
//   1. Checksum mismatch on a file we DID obtain — ALWAYS a hard failure
//      (exit 1). Never ship a binary that doesn't match what's pinned.
//   2. Could not obtain the binary at all:
//        - via the default download path (no download_base configured yet,
//          404, network unreachable) — a WARNING only. The asset is skipped,
//          the build CONTINUES, and the packaged app falls back to its
//          normal runtime resolution (BUDDY_CLI_BIN / ~/.ainxt/bin / PATH /
//          the consent-gated installer) — the same thing that already
//          happened before any of this existed (a missing extraResources
//          source was always a silent skip; this is that, minus the
//          silence). This matters because cli-version.json ships with
//          placeholder values until a real ainxt-cli release exists to pin
//          — a plain `npm run build:*` must still work in that window.
//        - via an EXPLICITLY requested source (AINXT_CLI_BIN_SRC or
//          AINXT_CLI_FROM_SOURCE set but it fails) — still a hard failure.
//          You asked for a specific source; failing loudly beats silently
//          building without it.
//   Set AINXT_CLI_REQUIRE=1 to escalate case 2's default-download-path
//   warning into a hard failure too (e.g. for an official CI release build
//   that must never ship without an embedded CLI).
//
// Env:
//   AINXT_CLI_VERSION        override manifest.ref
//   AINXT_CLI_DOWNLOAD_BASE  override manifest.download_base
//   AINXT_CLI_BIN_SRC        local dir (…/ainxt-macos-arm64) or single file
//   AINXT_CLI_FROM_SOURCE=1  build from source instead of downloading
//   AINXT_CLI_REPO_DIR       path to the ainxt-cli checkout (for FROM_SOURCE)
//   AINXT_CLI_SKIP_FETCH=1   do nothing
//   AINXT_CLI_REQUIRE=1      make "couldn't download" a hard failure too
//   GITHUB_TOKEN             set => private-repo API download; unset => public URL

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync, mkdirSync, readFileSync, writeFileSync, copyFileSync, chmodSync, statSync, unlinkSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP = resolve(HERE, "..");
const BIN_DIR = join(DESKTOP, "resources", "bin");
const MANIFEST = join(DESKTOP, "cli-version.json");
let _releaseAssetsCache = null; // cache for githubApiAssets() — declared here (not near its use below) because `let` is not hoisted like `function` is, and the main loop calls it before reaching that point in the file

const TRIPLE = {
  "ainxt-windows-x64.exe": "x86_64-pc-windows-msvc",
  "ainxt-macos-arm64": "aarch64-apple-darwin",
  "ainxt-macos-x64": "x86_64-apple-darwin",
  "ainxt-linux-x64": "x86_64-unknown-linux-gnu",
  "ainxt-linux-arm64": "aarch64-unknown-linux-gnu",
};
const TARGET_ASSETS = {
  win: ["ainxt-windows-x64.exe"],
  mac: ["ainxt-macos-arm64", "ainxt-macos-x64"],
  linux: ["ainxt-linux-x64", "ainxt-linux-arm64"],
};

function hostAssets() {
  if (process.platform === "win32") return ["ainxt-windows-x64.exe"];
  if (process.platform === "darwin")
    return [process.arch === "arm64" ? "ainxt-macos-arm64" : "ainxt-macos-x64"];
  return [process.arch === "arm64" ? "ainxt-linux-arm64" : "ainxt-linux-x64"];
}
function arg(name, dflt) {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : dflt;
}
function sha256(file) {
  return "sha256:" + createHash("sha256").update(readFileSync(file)).digest("hex");
}
// cli-version.json's assets map accepts either a bare checksum string
// (legacy/placeholder scaffold — remote name assumed identical to the local
// key) or { remote, sha256 } when the upstream release names its assets
// differently from our local canonical names (e.g. ainxt-cli publishes
// "ainxt-1.0.0-win32-x86_64.exe", we store/embed it as
// "ainxt-windows-x64.exe"). Always resolves to { remote, sha256 }.
function assetInfo(manifest, asset) {
  const raw = (manifest.assets && manifest.assets[asset]) || "";
  if (typeof raw === "string") return { remote: asset, sha256: raw };
  return { remote: raw.remote || asset, sha256: raw.sha256 || "" };
}
const log = (...a) => console.log("[fetch-cli]", ...a);
const warn = (...a) => console.warn("[fetch-cli] WARNING:", ...a);
const die = (m) => { console.error("[fetch-cli] ERROR:", m); process.exit(1); };
// Case 2 from the header comment: "couldn't obtain via the default path".
// Hard-fails only if the caller opted into strict mode.
const REQUIRE = process.env.AINXT_CLI_REQUIRE === "1";
const warnOrDie = (m) => { if (REQUIRE) die(m); warn(m); };

if (process.env.AINXT_CLI_SKIP_FETCH === "1") {
  log("AINXT_CLI_SKIP_FETCH=1 — leaving resources/bin/ as-is");
  process.exit(0);
}
if (!existsSync(MANIFEST)) die(`missing ${MANIFEST}`);
const manifest = JSON.parse(readFileSync(MANIFEST, "utf8"));
const version = process.env.AINXT_CLI_VERSION || manifest.ref || manifest.version;
const base =
  process.env.AINXT_CLI_DOWNLOAD_BASE ||
  manifest.download_base ||
  (manifest.repo && version
    ? `https://github.com/${manifest.repo}/releases/download/${version}`
    : null);

const target = arg("target", "host");
const assets =
  target === "host" ? hostAssets() : TARGET_ASSETS[target] || die(`unknown --target=${target}`);

mkdirSync(BIN_DIR, { recursive: true });

let anyMissing = false;

for (const asset of assets) {
  const dest = join(BIN_DIR, asset);
  const { remote, sha256: want } = assetInfo(manifest, asset);

  if (existsSync(dest)) {
    if (!want) {
      // No checksum pinned yet (placeholder cli-version.json) — trust a
      // manually-placed / previously-staged file as-is rather than treating
      // it as missing and trying (and failing) to re-download over it.
      log(`${asset}: already present in resources/bin/ — no checksum pinned yet, trusting as-is (skip)`);
      continue;
    }
    if (sha256(dest) === want) {
      log(`${asset}: present and verified — skip`);
      continue;
    }
    // Pinned checksum exists but doesn't match what's on disk — could be a
    // stale copy from a previous CLI version. Don't hard-fail on that alone;
    // remove it and fall through to re-obtain a fresh, checksum-enforced
    // copy. Removing first matters: if the re-fetch below then ALSO fails,
    // we must not leave the known-bad file sitting in resources/bin/ to be
    // silently embedded un-verified — better to end up with nothing there
    // (the normal "no CLI embedded" fallback state) than a wrong one.
    warn(`${asset}: cached file doesn't match the pinned checksum — refetching`);
    unlinkSync(dest);
  }

  let obtained = true;

  if (process.env.AINXT_CLI_BIN_SRC) {
    // Explicit source — a failure here is a real misconfiguration, always fatal.
    const src = process.env.AINXT_CLI_BIN_SRC;
    const from = statSync(src).isDirectory() ? join(src, asset) : src;
    if (!existsSync(from)) die(`AINXT_CLI_BIN_SRC set but ${from} not found`);
    copyFileSync(from, dest);
    log(`${asset}: copied from ${from}`);
  } else if (process.env.AINXT_CLI_FROM_SOURCE === "1") {
    // Explicit source — buildFromSource() dies internally on failure, always fatal.
    buildFromSource(asset, dest);
  } else if (process.env.GITHUB_TOKEN && manifest.repo && version) {
    // GITHUB_TOKEN present => treat ainxt-cli as a private repo. The plain
    // download() URL below does not authenticate against a private repo, so
    // route through the GitHub API by asset ID instead.
    obtained = await downloadViaGithubApi(remote, dest, asset, manifest.repo, version, process.env.GITHUB_TOKEN);
  } else if (!base) {
    warnOrDie(`${asset}: no download_base/repo configured in cli-version.json yet (placeholder values?) — building without an embedded CLI for this asset.`);
    obtained = false;
  } else {
    // No token => treat ainxt-cli as public: plain, unauthenticated URL.
    obtained = await download(`${base}/${remote}`, dest, asset);
  }

  if (!obtained) { anyMissing = true; continue; }

  if (!asset.endsWith(".exe")) { try { chmodSync(dest, 0o755); } catch { /* windows */ } }

  const got = sha256(dest);
  // Checksum mismatch is ALWAYS fatal, regardless of source — integrity, not availability.
  if (want && got !== want) die(`${asset}: checksum mismatch\n  expected ${want}\n  got      ${got}`);
  log(want ? `${asset}: verified ${got}` : `${asset}: WARNING no checksum pinned (got ${got})`);
}

if (anyMissing) {
  warn(
    `Building WITHOUT an embedded CLI for one or more assets (target=${target}). `
    + "The packaged app will fall back to BUDDY_CLI_BIN / ~/.ainxt/bin / PATH at "
    + "runtime, or offer the network installer. Set AINXT_CLI_REQUIRE=1 to make "
    + "this a hard build failure instead.",
  );
}

async function download(url, dest, asset) {
  log(`downloading ${url}`);
  const headers = {};
  if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  let res;
  try {
    res = await fetch(url, { headers, redirect: "follow" });
  } catch (e) {
    warnOrDie(`${asset}: network error fetching ${url}: ${e.message}`);
    return false;
  }
  if (!res.ok) {
    warnOrDie(`${asset}: download failed: ${res.status} ${res.statusText} for ${url}`);
    return false;
  }
  writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
  return true;
}

// Private-repo path (GITHUB_TOKEN set): the plain github.com/.../releases/
// download/ URL above only works for public repos — it does not accept a
// bearer token. A private repo's release assets have to be fetched via the
// GitHub REST API by numeric asset ID instead. (_releaseAssetsCache is
// declared near the top of the file, not here — see that line for why.)
async function githubApiAssets(repo, tag, token) {
  if (_releaseAssetsCache) return _releaseAssetsCache;
  const res = await fetch(`https://api.github.com/repos/${repo}/releases/tags/${tag}`, {
    headers: { Authorization: `token ${token}`, Accept: "application/vnd.github+json" },
  });
  if (!res.ok) throw new Error(`release lookup failed: ${res.status} ${res.statusText}`);
  const json = await res.json();
  _releaseAssetsCache = json.assets || [];
  return _releaseAssetsCache;
}

async function downloadViaGithubApi(remote, dest, asset, repo, tag, token) {
  log(`downloading (private, via API) ${repo}@${tag} asset "${remote}"`);
  let assets;
  try {
    assets = await githubApiAssets(repo, tag, token);
  } catch (e) {
    warnOrDie(`${asset}: ${e.message}`);
    return false;
  }
  const match = assets.find((a) => a.name === remote);
  if (!match) {
    warnOrDie(`${asset}: no release asset named "${remote}" found in ${repo}@${tag} via API`);
    return false;
  }
  let res;
  try {
    res = await fetch(`https://api.github.com/repos/${repo}/releases/assets/${match.id}`, {
      headers: { Authorization: `token ${token}`, Accept: "application/octet-stream" },
      redirect: "follow",
    });
  } catch (e) {
    warnOrDie(`${asset}: network error fetching asset ${match.id}: ${e.message}`);
    return false;
  }
  if (!res.ok) {
    warnOrDie(`${asset}: API download failed: ${res.status} ${res.statusText}`);
    return false;
  }
  writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
  return true;
}

function buildFromSource(asset, dest) {
  const repo =
    process.env.AINXT_CLI_REPO_DIR ||
    [
      resolve(DESKTOP, "..", "..", "ainxt-cli"),
      resolve(DESKTOP, "..", "..", "..", "ainxt-cli"),
    ].find((p) => existsSync(join(p, "Cargo.toml")));
  if (!repo || !existsSync(join(repo, "Cargo.toml")))
    die("AINXT_CLI_FROM_SOURCE=1 but no ainxt-cli checkout found (set AINXT_CLI_REPO_DIR)");

  const triple = TRIPLE[asset];
  const hostTriple = execFileSync("rustc", ["-vV"]).toString().match(/host:\s*(\S+)/)?.[1];
  const cross = triple && triple !== hostTriple;
  if (cross) execFileSync("rustup", ["target", "add", triple], { stdio: "inherit" });

  const args = ["build", "--release", "--manifest-path", join(repo, "Cargo.toml")];
  if (cross) args.push("--target", triple);
  log(`cargo ${args.join(" ")}`);
  execFileSync("cargo", args, { stdio: "inherit" });

  const outName = asset.endsWith(".exe") ? "ainxt.exe" : "ainxt";
  const outDir = cross
    ? join(repo, "target", triple, "release")
    : join(repo, "target", "release");
  const built = join(outDir, outName);
  if (!existsSync(built)) die(`cargo build finished but ${built} not found`);
  copyFileSync(built, dest);
  log(`${asset}: built from source at ${repo}`);
}
