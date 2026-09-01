// SPDX-License-Identifier: Apache-2.0
"use strict";
/**
 * Git clone for the Code tab. The user's GitLab PAT (fetched from their profile
 * via the gateway) is used to authenticate the clone on the user's own machine.
 *
 * Security notes:
 *  - The token is embedded in the clone URL (argv) only transiently for the
 *    duration of the clone, then the remote is rewritten to a token-less URL so
 *    the PAT is NEVER persisted in .git/config.
 *  - The token is never logged; it is scrubbed from any error text.
 *  - GIT_TERMINAL_PROMPT=0 so a bad token fails fast instead of hanging on a
 *    credential prompt.
 */
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

function repoNameFromUrl(url) {
  let u = String(url || "").trim().replace(/\/+$/, "").replace(/\.git$/i, "");
  const seg = u.split("/").pop() || "repo";
  return seg.replace(/[^A-Za-z0-9._-]/g, "") || "repo";
}

// Strip any existing credentials and return the bare https URL.
function cleanUrl(url) {
  const m = String(url || "").trim().match(/^https:\/\/(.+)$/i);
  if (!m) return String(url || "").trim();
  return `https://${m[1].replace(/^[^@/]*@/, "")}`;
}

// Inject oauth2:<token>@ for authenticated clone.
function authedUrl(url, token) {
  const m = String(url || "").trim().match(/^https:\/\/(.+)$/i);
  if (!m) return null;
  return `https://oauth2:${encodeURIComponent(token)}@${m[1].replace(/^[^@/]*@/, "")}`;
}

function run(cmd, args, opts) {
  return new Promise((resolve) => {
    let err = "";
    let p;
    try { p = spawn(cmd, args, opts); }
    catch (e) { return resolve({ code: -1, err: e.message }); }
    p.stderr.on("data", (d) => { err += d.toString(); });
    p.on("error", (e) => resolve({ code: -1, err: e.message }));
    p.on("close", (code) => resolve({ code, err }));
  });
}

async function cloneRepo({ url, branch, dest, token }) {
  url = String(url || "").trim();
  dest = String(dest || "").trim();
  if (!url || !dest) return { ok: false, error: "Repository URL and destination folder are required." };
  if (!/^https:\/\//i.test(url)) return { ok: false, error: "Only https:// GitLab URLs are supported (not SSH)." };
  if (!token) return { ok: false, error: "No GitLab token found in your profile. Add one under Profile → API Token Vault." };
  if (!fs.existsSync(dest)) return { ok: false, error: "Destination folder does not exist." };

  // The profile stores GitLab tokens as "username:PAT" (combined at save time).
  // GitLab expects just the PAT as the password in the clone URL — the username
  // part is handled by the "oauth2" prefix. Split on first ":" to extract the PAT.
  // Mirrors tools/gitlab_tools.py:set_token() which does the same split.
  const pat = String(token).includes(":") ? String(token).split(":").slice(1).join(":") : token;

  const name = repoNameFromUrl(url);
  const target = path.join(dest, name);
  if (fs.existsSync(target) && fs.readdirSync(target).length > 0) {
    return { ok: false, error: `"${target}" already exists and isn't empty. Pick another location or remove it first.` };
  }

  const au = authedUrl(url, pat);
  if (!au) return { ok: false, error: "Could not parse the repository URL." };

  const args = ["clone", "--progress"];
  if (branch && String(branch).trim()) args.push("--branch", String(branch).trim());
  args.push(au, target);

  // NOTE: `args` contains the token — never log it.
  const res = await run("git", args, { env: { ...process.env, GIT_TERMINAL_PROMPT: "0" } });
  if (res.code !== 0) {
    if ((res.err || "").includes("ENOENT")) {
      return { ok: false, error: "Git is not installed or not on PATH. Install Git to clone repositories." };
    }
    const safe = (res.err || "clone failed").split(pat).join("***").trim();
    return { ok: false, error: safe.slice(-600) || "git clone failed" };
  }

  // Remove the embedded token from the persisted remote (don't leave the PAT on disk).
  await run("git", ["-C", target, "remote", "set-url", "origin", cleanUrl(url)], {});
  return { ok: true, path: target, name };
}

module.exports = { cloneRepo, repoNameFromUrl };
