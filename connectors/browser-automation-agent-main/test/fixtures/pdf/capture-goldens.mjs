// Dev-only script: run the CURRENT (still-vendored) pdf.js over every fixture
// and save its extracted text as the golden baseline, using the exact same
// item.str + (item.hasEOL ? "\n" : " ") join/normalize logic as the real
// readPdfData() in lib/documents.js, so the comparison is apples-to-apples
// with what the new lib/pdf.js must reproduce. Must run BEFORE lib/vendor/
// pdf.js is deleted.
import { readFileSync, writeFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..", "..", "..");
const pdfjs = await import(join(ROOT, "lib", "vendor", "pdf.min.mjs"));
pdfjs.GlobalWorkerOptions.workerSrc = join(ROOT, "lib", "vendor", "pdf.worker.min.mjs");

async function extractAllPages(bytes) {
  const doc = await pdfjs.getDocument({ data: bytes, disableWorker: true, useWorkerFetch: false }).promise;
  const pages = [];
  for (let p = 1; p <= doc.numPages; p++) {
    const page = await doc.getPage(p);
    const content = await page.getTextContent();
    let text = "";
    for (const item of content.items) text += item.str + (item.hasEOL ? "\n" : " ");
    text = text.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    pages.push(`--- page ${p} ---\n${text}`);
  }
  await doc.destroy().catch(() => {});
  return { numPages: doc.numPages, body: pages.join("\n\n") };
}

const dir = HERE;
const files = readdirSync(dir).filter((f) => f.endsWith(".pdf"));
for (const f of files) {
  const bytes = new Uint8Array(readFileSync(join(dir, f)));
  try {
    const { numPages, body } = await extractAllPages(bytes);
    const out = `numPages=${numPages}\n${body}\n`;
    writeFileSync(join(dir, "golden", f.replace(/\.pdf$/, ".txt")), out);
    console.log(`OK   ${f} (${numPages} pages)`);
  } catch (e) {
    console.log(`FAIL ${f}: ${e?.message || e}`);
  }
}
