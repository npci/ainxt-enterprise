// Dev-only harness (no framework, no build step): runs lib/pdf.js over every
// fixture under test/fixtures/pdf/*.pdf and diffs normalized text against the
// golden captured from pdf.js (test/fixtures/pdf/golden/*.txt). Whitespace
// and control characters are normalized away (not byte-exact) because
// reproducing pdf.js's own glyph-metric-based spacing heuristic exactly is
// an explicit non-goal (see docs/requirements/REQUIREMENTS-PDF_ENGINE.md,
// "no full pdf.js parity") — this harness checks the actual extracted
// characters match, not incidental whitespace/control-byte noise (pdf.js
// itself emits stray NULs for some synthetic CID fonts with no embedded
// glyph program, e.g. tounicode-cjk.pdf's golden).
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { openPdf } from "../lib/pdf.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXDIR = join(HERE, "fixtures", "pdf");
const GOLDDIR = join(FIXDIR, "golden");

function normalize(s) {
  let out = "";
  for (const ch of s) {
    const code = ch.charCodeAt(0);
    if (code <= 0x1f) continue; // drop control chars (whitespace handled below)
    out += ch;
  }
  return out.replace(/\s+/g, " ").trim();
}

async function run() {
  const files = readdirSync(FIXDIR).filter((f) => f.endsWith(".pdf"));
  let failed = 0;
  for (const f of files) {
    const bytes = new Uint8Array(readFileSync(join(FIXDIR, f)));

    // A "<name>.throws" sidecar means openPdf must throw with that error code
    // (e.g. a truly password-protected PDF ⇒ code "password"), rather than
    // producing golden text. Verified independently of the text corpus.
    const throwsPath = join(GOLDDIR, f.replace(/\.pdf$/, ".throws"));
    let expectedCode = null;
    try {
      expectedCode = readFileSync(throwsPath, "utf8").trim();
    } catch { /* not a throws fixture */ }
    if (expectedCode) {
      try {
        await openPdf(bytes);
        failed++;
        console.log(`FAIL ${f}: expected throw code "${expectedCode}", but openPdf resolved`);
      } catch (e) {
        if (e?.code === expectedCode) {
          console.log(`PASS ${f} (threw code "${expectedCode}")`);
        } else {
          failed++;
          console.log(`FAIL ${f}: expected code "${expectedCode}", got "${e?.code}" (${e?.message})`);
        }
      }
      continue;
    }

    const goldenPath = join(GOLDDIR, f.replace(/\.pdf$/, ".txt"));
    let golden;
    try {
      golden = readFileSync(goldenPath, "utf8");
    } catch {
      console.log(`SKIP ${f}: no golden file`);
      continue;
    }
    try {
      const doc = await openPdf(bytes);
      const expectedNumPages = parseInt(/numPages=(\d+)/.exec(golden)?.[1] ?? "-1", 10);
      let body = "";
      for (let p = 1; p <= doc.numPages; p++) {
        body += `--- page ${p} ---\n${await doc.getPageText(p)}\n\n`;
      }
      const okPages = doc.numPages === expectedNumPages;
      const expectedBody = golden.replace(/^numPages=\d+\n/, "");
      const gotNorm = normalize(body);
      const wantNorm = normalize(expectedBody);
      const okText = gotNorm === wantNorm || gotNorm.replace(/ /g, "") === wantNorm.replace(/ /g, "");
      if (okPages && okText) {
        console.log(`PASS ${f} (${doc.numPages} pages)`);
      } else {
        failed++;
        console.log(`FAIL ${f}`);
        if (!okPages) console.log(`  numPages: got ${doc.numPages}, want ${expectedNumPages}`);
        if (!okText) {
          console.log(`  --- got ---\n${gotNorm}`);
          console.log(`  --- want ---\n${wantNorm}`);
        }
      }
    } catch (e) {
      failed++;
      console.log(`FAIL ${f}: threw ${e?.stack || e}`);
    }
  }
  if (failed) {
    console.log(`\n${failed} fixture(s) failed.`);
    process.exitCode = 1;
  } else {
    console.log(`\nAll fixtures passed.`);
  }
}

await run();
