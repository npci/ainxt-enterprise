// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
/* Host-aware helpers. Outlook helpers stay in office.js to keep that path
   unchanged; this file adds Word/Excel/PowerPoint context + insert helpers
   and a unified `getHostContext()` that the React app can use everywhere. */

import { getEmailContext, prependToCompose } from "./office.js";

export function getHost() {
  try { return Office.context.host; } catch { return null; }
}

// ── Word ────────────────────────────────────────────────────────────────────────

async function getWordContext() {
  const selection = await new Promise(resolve => {
    Office.context.document.getSelectedDataAsync(
      Office.CoercionType.Text,
      r => resolve(r.status === Office.AsyncResultStatus.Succeeded ? (r.value || "") : "")
    );
  });
  return { host: "Word", selection, body: selection };
}

async function insertToWord(text) {
  return new Promise((resolve, reject) => {
    Office.context.document.setSelectedDataAsync(
      text,
      { coercionType: Office.CoercionType.Text },
      r => r.status === Office.AsyncResultStatus.Succeeded ? resolve() : reject(r.error)
    );
  });
}

// ── Excel ───────────────────────────────────────────────────────────────────────

async function getExcelContext() {
  const matrix = await new Promise(resolve => {
    Office.context.document.getSelectedDataAsync(
      Office.CoercionType.Matrix,
      r => resolve(r.status === Office.AsyncResultStatus.Succeeded ? (r.value || []) : [])
    );
  });
  const tsv = (matrix || []).map(row => (row || []).join("\t")).join("\n");
  return { host: "Excel", selection: tsv, body: tsv, matrix };
}

async function insertToExcel(text) {
  return new Promise((resolve, reject) => {
    Office.context.document.setSelectedDataAsync(
      text,
      { coercionType: Office.CoercionType.Text },
      r => r.status === Office.AsyncResultStatus.Succeeded ? resolve() : reject(r.error)
    );
  });
}

// ── PowerPoint ──────────────────────────────────────────────────────────────────

async function getPowerPointContext() {
  const selection = await new Promise(resolve => {
    Office.context.document.getSelectedDataAsync(
      Office.CoercionType.Text,
      r => resolve(r.status === Office.AsyncResultStatus.Succeeded ? (r.value || "") : "")
    );
  });
  return { host: "PowerPoint", selection, body: selection };
}

async function insertToPowerPoint(text) {
  return new Promise((resolve, reject) => {
    Office.context.document.setSelectedDataAsync(
      text,
      { coercionType: Office.CoercionType.Text },
      r => r.status === Office.AsyncResultStatus.Succeeded ? resolve() : reject(r.error)
    );
  });
}

// ── Unified entry points ────────────────────────────────────────────────────────

export async function getHostContext() {
  const host = getHost();
  if (host === Office.HostType.Outlook) {
    const email = await getEmailContext();
    return email ? { host: "Outlook", ...email } : { host: "Outlook" };
  }
  if (host === Office.HostType.Word)       return getWordContext();
  if (host === Office.HostType.Excel)      return getExcelContext();
  if (host === Office.HostType.PowerPoint) return getPowerPointContext();
  return { host: "Unknown" };
}

export async function insertText(text, hostCtx) {
  const host = hostCtx?.host || getHost();
  if (host === "Outlook" || host === Office.HostType.Outlook) {
    return prependToCompose(text.replace(/\n/g, "<br/>"));
  }
  if (host === "Word"       || host === Office.HostType.Word)       return insertToWord(text);
  if (host === "Excel"      || host === Office.HostType.Excel)      return insertToExcel(text);
  if (host === "PowerPoint" || host === Office.HostType.PowerPoint) return insertToPowerPoint(text);
  throw new Error(`Insert not supported in host: ${host}`);
}

// ── Per-host quick action prompts ───────────────────────────────────────────────

export function buildPrompt(host, action, ctx) {
  const sel  = (ctx?.selection || ctx?.body || "").slice(0, 2500);
  const subj = ctx?.subject || "";
  const from = ctx?.from || "";

  if (host === "Outlook") {
    const body = (ctx?.body || sel || "").slice(0, 4000);
    const fromName = ctx?.fromName || from || "";
    const userName = ctx?.userName || "";
    // Who to greet in a reply: the SENDER in read mode, the first RECIPIENT in a
    // compose/reply window (where `from` is the user themselves).
    const otherName = ctx?.isReadMode
      ? (fromName || from || "")
      : (ctx?.toNames?.[0] || ctx?.to?.[0] || fromName || "");

    if (action === "search_emails") {
      // "Thread history": the body already contains the earlier messages quoted
      // inline ("On <date> X wrote:"). Reconstruct the thread FROM THE BODY — do
      // NOT instruct the model to call Graph (this path has no Graph tool, so it
      // would just hallucinate query syntax).
      return `Below is an email. Its body usually contains the earlier messages of the same thread quoted inline. Using ONLY what's provided, reconstruct and summarise the conversation:
1. List the messages in order (who wrote to whom, and the gist of each).
2. Pull out any open action items or decisions.
3. Note any dates/deadlines mentioned.
Be concise. If only one message is present, say the thread has a single message and summarise it. Do not mention Graph, APIs, or that you lack access.

Subject: "${subj}"
From: "${fromName || from}"
Body:
${body}`;
    }
    if (action === "compliance") {
      return `Check this email for PCI/DSS and PII compliance. Flag any:
- Card numbers, CVVs, account numbers (PCI)
- Aadhaar, PAN, UPI IDs, mobile numbers (Indian PII)
- API keys, tokens, passwords
- IFSC codes or banking credentials

Subject: "${subj}"
From: "${fromName || from}"
Body:
${body}

List every finding with severity (BLOCK / WARN) and the specific data found. If none, say so.`;
    }
    if (action === "draft") {
      return `Draft a concise, professional reply to the email below.
- Greet the person being replied to${otherName ? ` by name: ${otherName}` : " (use a neutral greeting if the name is unknown)"}.
- Write AS the user${userName ? `, ${userName}` : ""}, and sign off with that exact name. NEVER output "[Your Name]" or a placeholder.
- Plain text only (no markdown). Be specific and action-oriented.

Subject: "${subj}"
From: "${fromName || from}"
Body:
${body}`;
    }
    if (action === "summarise") {
      return `Summarise this email in 3 bullet points (key message, action required, deadline if any):
Subject: "${subj}"
From: "${fromName || from}"
Body:
${body}`;
    }
  }

  if (host === "Word") {
    if (action === "summarise") {
      return `Summarise the following Word document selection in 3-5 bullet points:\n\n${sel}`;
    }
    if (action === "improve") {
      return `Rewrite the following text for clarity, concise tone and professional style. Return only the rewritten text, no preface:\n\n${sel}`;
    }
    if (action === "compliance") {
      return `Check this document text for PCI/DSS and PII compliance. Flag card numbers, CVVs, account numbers, Aadhaar, PAN, UPI IDs, mobile numbers, API keys, tokens, IFSC codes. List findings with severity (BLOCK / WARN):\n\n${sel}`;
    }
    if (action === "expand") {
      return `Expand the following point or outline into one well-written paragraph (no markdown):\n\n${sel}`;
    }
  }

  if (host === "Excel") {
    if (action === "explain") {
      return `Explain what this spreadsheet selection represents and any visible patterns. Data (TSV):\n\n${sel}`;
    }
    if (action === "formula") {
      return `Suggest an Excel formula appropriate for the following selection's likely intent. Return only the formula on the first line, then a one-line explanation:\n\nSelection (TSV):\n${sel}`;
    }
    if (action === "analyse") {
      return `Analyse this spreadsheet data: identify trends, outliers and any data-quality issues. Be concise.\n\nData (TSV):\n${sel}`;
    }
    if (action === "compliance") {
      return `Scan this spreadsheet selection for PCI/DSS and PII data. Flag any card numbers, account numbers, Aadhaar, PAN, UPI IDs, mobile numbers. List findings with severity:\n\n${sel}`;
    }
  }

  if (host === "PowerPoint") {
    if (action === "summarise") {
      return `Summarise the following slide text in 3 short bullet points suitable for speaker notes:\n\n${sel}`;
    }
    if (action === "improve") {
      return `Rewrite this slide content as crisp, presentation-ready bullet points. Return only the rewritten bullets:\n\n${sel}`;
    }
    if (action === "expand") {
      return `Expand this slide title or outline into 4 concise speaker bullet points:\n\n${sel}`;
    }
    if (action === "compliance") {
      return `Check this slide content for PCI/DSS and PII compliance. Flag card numbers, account numbers, Aadhaar, PAN, UPI IDs, mobile numbers, API keys, tokens. List findings with severity:\n\n${sel}`;
    }
  }

  return sel;
}

export const QUICK_ACTIONS = {
  Outlook: [
    { id: "search_emails", label: "Thread history", needsM365: true },
    { id: "summarise",     label: "Summarise" },
    { id: "compliance",    label: "Compliance" },
    { id: "draft",         label: "Draft reply" },
  ],
  Word: [
    { id: "summarise",  label: "Summarise" },
    { id: "improve",    label: "Improve writing" },
    { id: "expand",     label: "Expand outline" },
    { id: "compliance", label: "Compliance" },
  ],
  Excel: [
    { id: "explain",    label: "Explain selection" },
    { id: "formula",    label: "Suggest formula" },
    { id: "analyse",    label: "Analyse data" },
    { id: "compliance", label: "Compliance" },
  ],
  PowerPoint: [
    { id: "summarise",  label: "Summarise slide" },
    { id: "improve",    label: "Improve text" },
    { id: "expand",     label: "Expand outline" },
    { id: "compliance", label: "Compliance" },
  ],
};