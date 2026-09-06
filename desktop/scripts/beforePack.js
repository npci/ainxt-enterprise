// SPDX-License-Identifier: MIT
// desktop/scripts/beforePack.js
//
// electron-builder hook: stage the ainxt CLI into resources/bin/ before the
// app is packed, so `extraResources` embeds it. Runs for every
// (platform, arch) pack — including direct `electron-builder` /
// `npx electron-builder --linux` invocations that bypass the npm scripts and
// the direct call inside scripts/build-bundle-zip.sh.
//
// Idempotent: fetch-cli.mjs no-ops when the binary is already present and its
// checksum matches. Set AINXT_CLI_SKIP_FETCH=1 to bypass entirely.
"use strict";
const { execFileSync } = require("child_process");
const path = require("path");

const PLATFORM_TO_TARGET = { darwin: "mac", mas: "mac", win32: "win", linux: "linux" };

module.exports = async function beforePack(context) {
  if (process.env.AINXT_CLI_SKIP_FETCH === "1") return;
  const target = PLATFORM_TO_TARGET[context.electronPlatformName] || "host";
  execFileSync(
    process.execPath,
    [path.join(__dirname, "fetch-cli.mjs"), `--target=${target}`],
    { stdio: "inherit", cwd: path.join(__dirname, "..") },
  );
};
