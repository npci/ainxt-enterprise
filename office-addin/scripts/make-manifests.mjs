// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
/**
 * Generates deployable Office manifests from the checked-in `.template` files.
 *
 * Why this exists: an Office manifest cannot use relative paths. Every
 * `SourceLocation`, `IconUrl` and `AppDomain` must be an absolute HTTPS URL, so
 * the manifest is inherently deployment-specific. Rather than ship a manifest
 * carrying one organisation's hostname — which every adopter would then have to
 * find and edit in four files, 41 places — the templates carry placeholders and
 * this script fills them in.
 *
 *   AINXT_ADDIN_BASE_URL   (required) origin serving the built task pane,
 *                          e.g. https://ainxt.example.com
 *   AINXT_ADDIN_SUPPORT_URL          defaults to the base URL
 *   AINXT_ADDIN_PROVIDER_NAME        defaults to "AiNxt"
 *
 * Usage:  AINXT_ADDIN_BASE_URL=https://ainxt.example.com npm run manifests
 * Output: build/manifest.xml (Outlook), build/manifest-word.xml, -excel, -powerpoint
 *
 * The add-in GUIDs in the templates are deliberately NOT parameterised: they
 * identify the add-in, not the deployment, so every install of *this* add-in
 * should share them. A fork that changes the add-in's behaviour should mint new
 * GUIDs by hand — see DEPLOY.md.
 */
import { readFileSync, writeFileSync, mkdirSync, readdirSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(ROOT, "build");

function fail(msg) {
  console.error(`\n[ainxt addin] ${msg}\n`);
  process.exit(1);
}

// ── Inputs ──────────────────────────────────────────────────────────────────
let base = (process.env.AINXT_ADDIN_BASE_URL || "").trim();
if (!base) {
  fail(
    "AINXT_ADDIN_BASE_URL is not set.\n" +
    "  It is the origin that serves the built task pane — the same host your\n" +
    "  users' Office clients will fetch it from.\n\n" +
    "  Example:\n" +
    "    AINXT_ADDIN_BASE_URL=https://ainxt.example.com npm run manifests\n\n" +
    "  For local development against the vite dev server:\n" +
    "    AINXT_ADDIN_BASE_URL=https://localhost:3100 npm run manifests"
  );
}
base = base.replace(/\/+$/, "");

// Office desktop clients refuse to load a task pane over plain HTTP. localhost
// is the documented exception, because the dev-cert flow makes it HTTPS anyway.
const isLocal = /^https?:\/\/localhost(:\d+)?$/i.test(base);
if (!base.startsWith("https://") && !isLocal) {
  fail(
    `AINXT_ADDIN_BASE_URL must be an https:// origin (got ${base}).\n` +
    "  Office desktop clients silently refuse to load a task pane over HTTP.\n" +
    "  Use https://, or https://localhost:3100 for development."
  );
}
if (/\/office-addin$/i.test(base)) {
  fail(
    `AINXT_ADDIN_BASE_URL should be the origin only, without the /office-addin\n` +
    `  path (got ${base}). The templates already append it.`
  );
}

const supportUrl = (process.env.AINXT_ADDIN_SUPPORT_URL || base).trim().replace(/\/+$/, "");
const providerName = (process.env.AINXT_ADDIN_PROVIDER_NAME || "AiNxt").trim();

const subs = {
  ADDIN_BASE_URL: base,
  SUPPORT_URL: supportUrl,
  PROVIDER_NAME: providerName,
};

// ── Generate ────────────────────────────────────────────────────────────────
const templates = readdirSync(ROOT).filter((f) => f.endsWith(".xml.template"));
if (templates.length === 0) fail(`no *.xml.template files found in ${ROOT}`);

mkdirSync(OUT, { recursive: true });

let wrote = 0;
for (const tpl of templates.sort()) {
  let text = readFileSync(join(ROOT, tpl), "utf8");
  for (const [key, value] of Object.entries(subs)) {
    text = text.split(`{{${key}}}`).join(value);
  }

  // Assert on the artifact, not on the fact that we ran. An unsubstituted
  // placeholder produces a manifest Office rejects with an unhelpful error, so
  // catch it here where the message can be useful.
  const leftover = text.match(/\{\{[A-Z_]+\}\}/g);
  if (leftover) {
    fail(
      `${tpl} still contains unsubstituted placeholders: ${[...new Set(leftover)].join(", ")}\n` +
      "  This is a bug in make-manifests.mjs — the template gained a placeholder\n" +
      "  the script does not know about."
    );
  }

  const outName = tpl.replace(/\.template$/, "");
  writeFileSync(join(OUT, outName), text);
  console.log(`  wrote build/${outName}`);
  wrote += 1;
}

console.log(
  `\n[ainxt addin] ${wrote} manifest(s) generated for ${base}\n` +
  `  provider: ${providerName}\n` +
  `  support:  ${supportUrl}\n\n` +
  "Next: serve the built task pane at that origin (npm run build), then sideload\n" +
  "the manifest for each Office host you want. See DEPLOY.md.\n"
);
