// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";

// Office desktop add-ins refuse mixed content; the dev server MUST be HTTPS.
// `npx office-addin-dev-certs install` (one-time) drops a trusted cert under
// ~/.office-addin-dev-certs/. We read it here. If it isn't there, vite still
// boots over HTTP and prints a clear hint — Office will refuse to load until
// the certs are installed.
function loadOfficeDevCerts() {
  const dir = join(homedir(), ".office-addin-dev-certs");
  try {
    return {
      key:  readFileSync(join(dir, "localhost.key")),
      cert: readFileSync(join(dir, "localhost.crt")),
      ca:   readFileSync(join(dir, "ca.crt")),
    };
  } catch {
    console.warn(
      "[ainxt addin] Office dev certs not found.\n" +
      "  Run once:  npx office-addin-dev-certs install\n" +
      "  Then restart this dev server."
    );
    return false;
  }
}

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    rollupOptions: {
      input: {
        taskpane: "src/taskpane/index.html",
      },
    },
  },
  server: {
    port: 3100,
    host: "localhost",
    https: loadOfficeDevCerts(),
  },
});
