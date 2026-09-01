// SPDX-License-Identifier: Apache-2.0
/* Buddy — the AI office assistant (for everyone, aimed at non-engineers).
 *
 * The "AI office employee": reads documents, drafts content, generates
 * Word/Excel/PowerPoint, and uses connectors (Outlook/Teams, Jira, Confluence)
 * to get office work done. Unlike "Code" (the local coding agent, desktop-only),
 * Buddy runs SERVER-SIDE through the gateway orchestrator, so it works in the
 * browser AND the desktop app with no local runtime.
 *
 * It talks to the same streaming endpoint as Chat (`POST /ask`, text/event-stream)
 * but passes `mode: "office"`, which tells the gateway to use the office persona
 * and enable the document + connector action set in the planner. Document outputs
 * arrive inline as `[DOCJOB:id:fmt:name]` markers (rendered as download cards via
 * the shared helpers in Message.jsx).
 */
import { useEffect, useRef, useState, useCallback } from "react";
import {
  SendHorizontal, CirclePauseIcon, Loader2, Paperclip, X, Briefcase,
  Plug, FileText, FileSpreadsheet, Wrench, CircleX, ShieldOff,
} from "lucide-react";
import { isCoworkOfficeAvailable } from "../hooks/useDesktop.js";
// Message.jsx must be imported BEFORE CoworkDesktop.jsx to avoid a TDZ
// (Temporal Dead Zone) crash on `mdComponents` (export const) in the
// production bundle. Both files import from Message.jsx, and CoworkDesktop
// is a transitive dependency of Office — if CoworkDesktop loads first it
// requests Message.jsx while Office.jsx is still mid-evaluation, returning
// a partially-initialised module where the const binding hasn't been set yet.
import { mdComponents, parseDocMarkers, DocDownloadButton } from "./Message.jsx";
import CoworkDesktop from "./CoworkDesktop.jsx";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import { API_BASE as API, authFetch } from "../config";
import { usePromptQueue } from "../hooks/usePromptQueue.js";
import { validateFreeText } from "../utils/securityValidation";

// Channel tag for every Buddy → backend call.
const OFFICE_CLIENT_HEADER = { "X-AiNxt-Client": "office" };

const MODELS = [
  { key: "auto",   label: "Auto" },
  { key: "claude", label: "Claude Sonnet 4.6" },
  { key: "gpt",    label: "GPT-5.4" },
  { key: "gemini", label: "Gemini 2.5 Flash" },
];

const SUGGESTIONS = [
  { icon: FileText,        text: "Summarize the attached PDFs and give me a Word document" },
  { icon: Plug,            text: "How many unread emails do I have in Outlook?" },
  { icon: FileSpreadsheet, text: "Turn this data into an Excel sheet with totals" },
  { icon: Briefcase,       text: "Draft a status update from my Jira board" },
];

// Document-intent detection (mirrors Chat.jsx / chat_worker patterns).
const _DOC_VERB_RE = /\b(generate|create|make|write|export|produce|draft|build|prepare|give|get|want|need|provide|download)\b[\s\S]{0,80}\b(document|report|presentation|slides|powerpoint|excel|spreadsheet|word|doc|pdf|deck)\b/i;
const _DOC_EXT_RE  = /\.(pptx?|pdf|docx?|xlsx?|txt|md)\b|\bdownloadable\b/i;
function isDocIntent(text) { return _DOC_VERB_RE.test(text) || _DOC_EXT_RE.test(text); }
function detectDocFormat(text) {
  const t = text.toLowerCase();
  if (/\b(pptx?|presentation|slides?|slide[\s-]?deck|powerpoint)\b/.test(t)) return "pptx";
  if (/\b(xlsx?|excel|spreadsheet)\b/.test(t)) return "xlsx";
  if (/\b(docx?|word\s+(doc|document|file))\b/.test(t)) return "docx";
  if (/\bpdf\b/.test(t)) return "pdf";
  if (/\bmarkdown\b|\.md\b/.test(t)) return "md";
  if (/\btext\b|\.txt\b/.test(t)) return "txt";
  return "pdf";
}

// Friendly label for a streamed tool_event (shape varies — be defensive).
function toolLabel(te) {
  if (!te || typeof te !== "object") return "working";
  const name = te.name || te.tool || te.action || te.connector || te.label || "working";
  const map = {
    read_document: "Reading document",
    doc_generate: "Generating document",
    connector_call: "Using connector",
    retrieve: "Searching knowledge base",
    symbol_lookup: "Looking up",
    generate: "Composing",
  };
  const base = map[name] || name;
  const detail = te.detail || te.tool || te.query || te.connector || "";
  return detail && detail !== name ? `${base} · ${detail}` : base;
}

// Confirm-and-send card for a [SENDPROPOSAL:{...}] marker emitted by the office
// persona. The user reviews/edits the fields and clicks Send → POST /connectors/action
// (the ONLY path that performs a connector write; the agent never sends on its own).
const SENDPROPOSAL_RE = /\[SENDPROPOSAL:(\{[\s\S]*\})\]/;
const ACTIONPROPOSAL_RE = /\[ACTIONPROPOSAL:(\{[\s\S]*\})\]/;

const FIELD_LABELS = {
  to: "To", subject: "Subject", body: "Message",
  team_id: "Team ID", channel_id: "Channel ID", chat_id: "Chat ID", message: "Message",
  attachment_id: "Attachment ID", attachment_ids: "Attachment IDs",
  event_id: "Event ID", start: "New start", end: "New end", comment: "Comment",
};

function ConnectorActionCard({ proposal, kind = "send" }) {
  const initialParams = (() => {
    const p = { ...(proposal.params || {}) };
    const inherited = Array.isArray(proposal.attachment_ids) ? proposal.attachment_ids.filter(Boolean) : [];
    if (inherited.length && !p.attachment_id && !p.attachment_ids && ["outlook_send_mail", "teams_send_message", "teams_send_chat_message"].includes(proposal.tool)) {
      p.attachment_ids = inherited;
    }
    return p;
  })();
  const [params, setParams] = useState(initialParams);
  const [status, setStatus] = useState("idle"); // idle | sending | sent | error
  const [err, setErr] = useState("");
  const target = kind === "send"
    ? (proposal.tool === "outlook_send_mail" ? "email" : "Teams message")
    : (proposal.tool === "calendar_cancel_event" ? "meeting cancellation" : "calendar update");
  const buttonLabel = kind === "send" ? "Send" : "Confirm";

  const send = async () => {
    setStatus("sending"); setErr("");

    // Client-side pre-check — mirrors validate_connector_action_request()'s
    // _CONNECTOR_FREE_TEXT_PARAMS list in core/security_validation.py (XSS-only
    // via validate_free_text() on body/subject/message/content/text). The
    // backend (connectors_router.py's POST /connectors/action) remains the
    // authoritative enforcer.
    for (const key of ["body", "subject", "message", "content", "text"]) {
      if (params[key] == null) continue;
      const check = validateFreeText(String(params[key]));
      if (!check.isValid) {
        setStatus("error");
        setErr(`${FIELD_LABELS[key] || key}: ${check.errors[0]?.message || "invalid input"}`);
        return;
      }
    }

    try {
      const r = await authFetch(`${API}/connectors/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...OFFICE_CLIENT_HEADER },
        body: JSON.stringify({ connector: proposal.connector, tool: proposal.tool, params }),
      });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `failed (${r.status})`); }
      setStatus("sent");
    } catch (e) { setStatus("error"); setErr(e.message); }
  };

  if (status === "sent") {
    return <div className="my-2 text-sm bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2 text-emerald-700">✓ {target} completed.</div>;
  }
  const keys = Object.keys(params);
  return (
    <div className="my-2 border border-indigo-200 rounded-lg overflow-hidden">
      <div className="bg-indigo-50 px-3 py-1.5 text-xs font-medium text-indigo-700 flex items-center gap-1.5">
        <Plug className="w-3.5 h-3.5" /> Review &amp; {kind === "send" ? "send" : "confirm"} {target}
      </div>
      <div className="p-3 space-y-2 bg-white">
        {keys.map((k) => (
          <div key={k}>
            <label className="block text-[11px] text-gray-500 mb-0.5">{FIELD_LABELS[k] || k}</label>
            {(k === "body" || k === "message" || k === "comment") ? (
              <textarea rows={k === "comment" ? 2 : 4} value={params[k] || ""} onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.value }))}
                className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:border-indigo-500" />
            ) : (
              <input value={params[k] || ""} onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.value }))}
                placeholder={k === "to" ? "recipient@yourdomain.com" : ""}
                className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:border-indigo-500" />
            )}
          </div>
        ))}
        {status === "error" && <p className="text-xs text-red-600">{err}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={send} disabled={status === "sending"}
            className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-md px-3 py-1 text-sm">
            {status === "sending" ? `${buttonLabel}ing…` : buttonLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function SendCard({ proposal }) {
  return <ConnectorActionCard proposal={proposal} kind="send" />;
}

function ActionCard({ proposal }) {
  return <ConnectorActionCard proposal={proposal} kind="action" />;
}

export default function Office() {
  // On the desktop, Buddy runs the FULL local agent (sub-agents + Skills + the
  // connector MCP bridge) via CoworkDesktop; in the browser it stays the
  // server-side office-mode SSE flow below.
  if (isCoworkOfficeAvailable) return <CoworkDesktop />;
  return <OfficeServer />;
}

function OfficeServer() {
  const [messages, setMessages] = useState([]); // {id, role, content, events?, streaming?}
  const [input, setInput] = useState("");
  const [model, setModel] = useState("auto");
  const [busy, setBusy] = useState(false);
  const [attachments, setAttachments] = useState([]); // {id, name}
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadPhase, setUploadPhase] = useState("uploading"); // "uploading" | "processing"
  const [fileLimitError, setFileLimitError] = useState(false);
  const uploadXhrRef = useRef(null);
  const scrollRef = useRef(null);
  const fileRef = useRef(null);
  const abortRef = useRef(null);
  // ── Prompt queue ──────────────────────────────────────────────────────────
  const [maxWait, setMaxWait] = useState(5);
  const [queuedCount, setQueuedCount] = useState(0);
  const [queueExpanded, setQueueExpanded] = useState(false);
  const { enqueue, dequeueNext, removeAt, clearQueue, getQueue, isFull } = usePromptQueue(maxWait);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // Auto-dismiss the file-limit error banner after 5 seconds (mirrors Chat.jsx).
  useEffect(() => {
    if (!fileLimitError) return;
    const t = setTimeout(() => setFileLimitError(false), 5000);
    return () => clearTimeout(t);
  }, [fileLimitError]);

  // Fetch the admin-configurable Buddy prompt queue limit once on mount.
  useEffect(() => {
    let alive = true;
    authFetch(`${API}/buddy/queue-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d?.max_wait != null) setMaxWait(d.max_wait); })
      .catch(() => { /* keep default */ });
    return () => { alive = false; };
  }, []);

  const cancelUpload = useCallback(() => {
    if (uploadXhrRef.current) {
      uploadXhrRef.current.abort();
      uploadXhrRef.current = null;
    }
    setUploading(false);
    setUploadProgress(0);
    setUploadPhase("uploading");
  }, []);

  const upload = useCallback(async (fileList) => {
    const files = Array.from(fileList || []);
    if (!files.length) return;

    const MAX_FILES = 5;
    if (attachments.length + files.length > MAX_FILES) {
      setFileLimitError(true);
      return;
    }

    setUploading(true);
    setUploadProgress(0);
    setUploadPhase("uploading");

    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));

    try {
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        uploadXhrRef.current = xhr;
        xhr.open("POST", `${API}/chat/upload`);
        xhr.withCredentials = true;
        Object.entries(OFFICE_CLIENT_HEADER).forEach(([k, v]) => xhr.setRequestHeader(k, v));
        xhr.upload.onprogress = (ev) => {
          if (ev.lengthComputable) setUploadProgress(Math.round((ev.loaded / ev.total) * 100));
        };
        xhr.upload.onload = () => { setUploadProgress(100); setUploadPhase("processing"); };
        xhr.onload = () => {
          uploadXhrRef.current = null;
          if (xhr.status >= 200 && xhr.status < 300) {
            try { resolve(JSON.parse(xhr.responseText)); }
            catch { reject(new Error("Invalid response")); }
          } else {
            reject(new Error(`Upload failed: ${xhr.status}`));
          }
        };
        xhr.onerror = () => { uploadXhrRef.current = null; reject(new Error("Network error")); };
        xhr.onabort = () => { uploadXhrRef.current = null; reject(new Error("Upload cancelled")); };
        xhr.send(fd);
      });

      // Server returns { uploaded: [{ id, filename, ... }] } or { id, filename }
      const uploaded = result.uploaded || [];
      if (uploaded.length) {
        const valid = uploaded.filter((u) => !u.blocked);
        if (valid.length) {
          setAttachments((a) => [...a, ...valid.map((u) => ({ id: u.id, name: u.file_name || u.filename }))]);
        }
        const blocked = uploaded.filter((u) => u.blocked);
        if (blocked.length) {
          setMessages((m) => [...m, { id: crypto.randomUUID(), role: "assistant", content: `Couldn't attach ${blocked.map((b) => b.file_name).join(", ")}: blocked by compliance`, error: true }]);
        }
      } else if (result.id || result.attachment_id) {
        // Legacy single-file response shape
        setAttachments((a) => [...a, { id: result.id || result.attachment_id, name: result.filename || files[0].name }]);
      }
    } catch (e) {
      if (e.message !== "Upload cancelled") {
        setMessages((m) => [...m, { id: crypto.randomUUID(), role: "assistant", content: `Couldn't attach files: ${e.message}`, error: true }]);
      }
    } finally {
      uploadXhrRef.current = null;
      setUploading(false);
      setUploadProgress(0);
      setUploadPhase("uploading");
    }
  }, [attachments]);

  const send = useCallback(async (text, overrideAttachments) => {
    const question = (text ?? input).trim();
    if (!question) return;

    // ── Prompt queue: if Buddy is busy and this is a user-initiated send ──────
    // Auto-dequeue calls pass overrideAttachments as an Array (even if empty),
    // so Array.isArray(overrideAttachments) distinguishes them from user sends.
    const isAutoDequeue = Array.isArray(overrideAttachments);
    if (busy && !isAutoDequeue) {
      const payload = { text: question, attachments: [...attachments] };
      const accepted = enqueue(payload);
      if (!accepted) {
        // Queue full — show a brief inline notice (no toast API in OfficeServer).
        setMessages((m) => [...m, {
          id: crypto.randomUUID(), role: "assistant", error: true,
          content: `Queue full — max ${maxWait} message${maxWait === 1 ? "" : "s"} allowed. Please wait for the current response to finish.`,
        }]);
      } else {
        setQueuedCount((c) => c + 1);
        setInput("");
      }
      return;
    }

    setInput("");
    const effectiveAttachments = isAutoDequeue ? overrideAttachments : attachments;
    const assistantId = crypto.randomUUID();
    const attachment_ids = effectiveAttachments.map((a) => a.id);
    setMessages((m) => [
      ...m,
      { id: crypto.randomUUID(), role: "user", content: question, attachments: effectiveAttachments },
      { id: assistantId, role: "assistant", content: "", events: [], streaming: true },
    ]);
    if (!isAutoDequeue) setAttachments([]);
    setBusy(true);

    const controller = new AbortController();
    abortRef.current = controller;

    const patch = (fn) => setMessages((m) => m.map((msg) => (msg.id === assistantId ? fn(msg) : msg)));

    // Document request → produce a downloadable file via /docs/generate. Use the
    // most recent answer as the content when present ("put that in a Word doc");
    // otherwise let the worker structure it from the request ("create a report").
    if (isDocIntent(question)) {
      const fmt = detectDocFormat(question);
      const prior = [...messages].reverse().find((m) => m.role === "assistant" && m.content && !m.error);
      try {
        const docBody = { format: fmt, title: question.slice(0, 80) };
        if (prior?.content) docBody.content_md = prior.content;
        else docBody.question = question;
        const r = await authFetch(`${API}/docs/generate`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...OFFICE_CLIENT_HEADER },
          body: JSON.stringify(docBody),
          signal: controller.signal,
        });
        const d = await r.json();
        if (r.ok && d.job_id) {
          patch((msg) => ({ ...msg, content: `Preparing your ${fmt.toUpperCase()} now:\n\n[DOCJOB:${d.job_id}:${fmt}:document.${fmt}]`, streaming: false }));
        } else {
          throw new Error(d.detail || "document generation failed");
        }
      } catch (e) {
        patch((msg) => ({ ...msg, content: `Couldn't generate the document: ${e.message}`, streaming: false, error: true }));
      } finally {
        setBusy(false);
        abortRef.current = null;
        // Auto-dequeue next queued prompt after doc generation completes.
        const next = dequeueNext();
        if (next) {
          setQueuedCount((c) => Math.max(0, c - 1));
          setTimeout(() => send(next.text, next.attachments ?? []), 150);
        }
      }
      return;
    }

    try {
      const body = { question, mode: "office", attachment_ids };
      if (model !== "auto") body.model = model;
      const resp = await authFetch(`${API}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...OFFICE_CLIENT_HEADER },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        const t = await resp.text().catch(() => "");
        throw new Error(`Server error ${resp.status}: ${t.slice(0, 200)}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: false });
      let acc = "";
      let sse = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value) continue;
        sse += decoder.decode(value, { stream: true });
        const parts = sse.split("\n\n");
        sse = parts.pop() ?? "";
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data: ")) continue;
          let obj;
          try { obj = JSON.parse(line.slice(6)); } catch { continue; }
          if (obj.t !== undefined) {
            acc += obj.t;
            patch((msg) => ({ ...msg, content: acc }));
          } else if (obj.tool_event) {
            patch((msg) => ({ ...msg, events: [...(msg.events || []), obj.tool_event] }));
          } else if (typeof obj.tool_call === "string") {
            patch((msg) => ({ ...msg, events: [...(msg.events || []), { name: obj.tool_call }] }));
          }
          // __meta__ and others are ignored for the office view
        }
      }
      patch((msg) => ({ ...msg, content: acc || msg.content, streaming: false }));
    } catch (e) {
      if (e.name === "AbortError") {
        patch((msg) => ({ ...msg, streaming: false }));
      } else {
        patch((msg) => ({ ...msg, content: (msg.content || "") + `\n\n_Error: ${e.message}_`, streaming: false, error: true }));
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
      // Auto-dequeue: send the next queued prompt (FIFO) after this turn completes.
      const next = dequeueNext();
      if (next) {
        setQueuedCount((c) => Math.max(0, c - 1));
        setTimeout(() => send(next.text, next.attachments ?? []), 150);
      }
    }
  }, [input, busy, attachments, model, messages, enqueue, dequeueNext, isFull, maxWait, setQueuedCount]);

  const stop = useCallback(() => { abortRef.current?.abort(); }, []);

  // Render assistant content the same way Chat does: markdown via mdComponents,
  // with [DOCJOB:] download cards and connector confirmation cards spliced in.
  const md = (value) => (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeHighlight, rehypeKatex]} components={mdComponents}>
      {value}
    </ReactMarkdown>
  );
  const renderAssistant = (content) => {
    const text = content || "";
    const sm = text.match(SENDPROPOSAL_RE);
    const am = text.match(ACTIONPROPOSAL_RE);
    let proposal = null;
    let actionProposal = null;
    let rest = text;
    if (sm) {
      try {
        proposal = JSON.parse(sm[1]); rest = rest.replace(sm[0], "").trim();
        const lastWithAttachments = [...messages].reverse().find((m) => m.role === "user" && m.attachments?.length);
        const ids = lastWithAttachments?.attachments?.map((a) => a.id).filter(Boolean) || [];
        if (ids.length && proposal && !proposal.attachment_ids) proposal.attachment_ids = ids;
      }
      catch { proposal = null; }
    }
    if (am) {
      try { actionProposal = JSON.parse(am[1]); rest = rest.replace(am[0], "").trim(); }
      catch { actionProposal = null; }
    }
    const parts = parseDocMarkers(rest).map((p, i) => {
      if (p.type === "docjob") return <DocDownloadButton key={i} jobId={p.jobId} format={p.format} filename={p.filename} />;
      if (p.type === "text") return p.value?.trim() ? <div key={i}>{md(p.value)}</div> : null;
      return null;
    });
    return <>{parts}{proposal && <SendCard proposal={proposal} />}{actionProposal && <ActionCard proposal={actionProposal} />}</>;
  };

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Messages — same layout as Chat.jsx (centered column, gray user blocks,
          transparent markdown assistant). */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto overflow-x-hidden px-6 py-8 leading-5">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-4">
            <div className="w-16 h-16 rounded-full brand-grad-vivid flex items-center justify-center shadow-lg">
              <Briefcase size={28} className="text-white" />
            </div>
            <div className="text-center">
              <p className="text-lg font-semibold text-gray-800">Your AI office assistant</p>
              <p className="text-sm text-gray-400 mt-1">Read documents, draft, build files, and use your connected apps.</p>
            </div>
            <div className="flex flex-wrap gap-2 justify-center max-w-md mt-2">
              {SUGGESTIONS.map((s, i) => (
                <button key={i} onClick={() => send(s.text)}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-full border border-gray-200 transition cursor-pointer">
                  <s.icon className="w-3.5 h-3.5 text-gray-400 shrink-0" />{s.text}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m) => (
            <div key={m.id} className={m.role === "user" ? "flex justify-end mb-6" : "flex justify-start mb-6"}>
              <div className={m.role === "user" ? "bg-gray-100 px-4 py-3 rounded-md text-sm max-w-4xl" : "px-4 py-3 rounded-md text-sm max-w-5xl"}>
                {m.role === "assistant" && (m.events?.length > 0) && (
                  <div className="flex flex-col gap-1 mb-2">
                    {m.events.map((te, i) => (
                      <div key={i} className="flex items-center gap-1.5 text-xs text-gray-500 bg-gray-50 border border-gray-200 rounded-md px-2 py-1 w-fit">
                        <Wrench className="w-3 h-3 text-indigo-500" />{toolLabel(te)}
                      </div>
                    ))}
                  </div>
                )}
                {m.role === "user" ? (
                  <div>
                    <div className="whitespace-pre-wrap">{m.content}</div>
                    {m.attachments?.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {m.attachments.map((a) => (
                          <span key={a.id} className="text-[11px] bg-white/70 border border-gray-200 rounded px-1.5 py-0.5 text-gray-600">{a.name}</span>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  m.error ? <div className="text-red-600 whitespace-pre-wrap">{m.content}</div> : renderAssistant(m.content)
                )}
                {m.role === "assistant" && m.streaming && !m.content && (
                  <span className="inline-flex gap-0.5 align-middle">
                    <span className="w-1 h-1 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                    <span className="w-1 h-1 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                    <span className="w-1 h-1 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                  </span>
                )}
              </div>
            </div>
          ))
        )}
      </div>

      {/* Composer — same shell as Chat.jsx */}
      <div className="border-t border-gray-100 bg-white px-4 pb-4 pt-3 shrink-0">
        <input ref={fileRef} type="file" multiple className="hidden"
          onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />

        {fileLimitError && (
          <div className="mb-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            <span className="mt-0.5 shrink-0">⚠️</span>
            <span className="flex-1">
              The number of files you are trying to add exceeds the maximum limit. Ainxt currently supports adding up to 5 files at a time.
            </span>
            <button
              onClick={() => setFileLimitError(false)}
              className="shrink-0 text-red-400 hover:text-red-600"
              aria-label="Dismiss"
            >
              <X size={13} />
            </button>
          </div>
        )}

        {/* ── Prompt queue indicator ─────────────────────────────────────── */}
        {queuedCount > 0 && (
          <div className="mb-1 rounded-lg border border-amber-200 bg-amber-50 text-xs text-amber-700">
            <div className="flex items-center gap-2 px-3 py-1.5">
              <svg className="w-3 h-3 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <span className="flex-1">
                {queuedCount} message{queuedCount > 1 ? "s" : ""} queued — will send automatically
              </span>
              <button
                onClick={() => setQueueExpanded((e) => !e)}
                className="text-amber-600 hover:text-amber-800 font-medium"
                title={queueExpanded ? "Collapse queue list" : "Show queued messages"}
              >
                {queueExpanded ? "▴ Hide" : "▾ Show"}
              </button>
              <button
                onClick={() => { clearQueue(); setQueuedCount(0); setQueueExpanded(false); }}
                className="text-amber-500 hover:text-amber-700 font-medium"
                title="Clear all queued messages"
              >
                Clear all
              </button>
            </div>
            {queueExpanded && (
              <ul className="border-t border-amber-200 divide-y divide-amber-100">
                {getQueue().map((item, idx) => (
                  <li key={item.timestamp} className="flex items-center gap-2 px-3 py-1.5">
                    <span className="text-amber-400 font-mono w-4 shrink-0 select-none">{idx + 1}.</span>
                    <span className="flex-1 truncate text-amber-800" title={item.text}>
                      {item.text.length > 60 ? item.text.slice(0, 60) + "…" : item.text}
                    </span>
                    <button
                      onClick={() => {
                        const removed = removeAt(idx);
                        if (removed) {
                          const newCount = queuedCount - 1;
                          setQueuedCount(newCount);
                          if (newCount === 0) setQueueExpanded(false);
                        }
                      }}
                      className="text-amber-400 hover:text-red-500 shrink-0"
                      title="Remove this message from queue"
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <div className={`border rounded-xl bg-gray-50 transition-colors ${busy ? "border-gray-200" : "border-gray-300 focus-within:border-gray-400 focus-within:bg-white"}`}>
          {attachments.length > 0 && (
            <div className="px-3 pt-2.5 flex flex-wrap gap-1.5">
              {attachments.map((a) => (
                <div key={a.id} className="flex items-center gap-1 bg-blue-50 border border-blue-200 text-blue-700 text-xs px-2 py-0.5 rounded-full">
                  <FileText size={10} /><span className="max-w-[110px] truncate">{a.name}</span>
                  <button onClick={() => setAttachments((arr) => arr.filter((x) => x.id !== a.id))} className="text-blue-400 hover:text-blue-600 ml-0.5"><X size={10} /></button>
                </div>
              ))}
            </div>
          )}
          {uploading && (
            <div className="mb-1 flex items-center gap-2 px-3 pt-2">
              {uploadPhase === "processing" ? (
                <>
                  <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
                    <div className="h-full w-2/5 bg-indigo-500 rounded-full animate-upload-indeterminate" />
                  </div>
                  <span className="text-xs text-indigo-600 font-medium flex items-center gap-1 shrink-0">
                    <ShieldOff size={12} className="animate-pulse" />
                    Scanning &amp; parsing document…
                  </span>
                </>
              ) : (
                <>
                  <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-blue-500 rounded-full transition-all duration-200"
                      style={{ width: `${uploadProgress}%` }}
                    />
                  </div>
                  <span className="text-xs text-blue-500 font-medium w-9 text-right shrink-0">
                    {uploadProgress}%
                  </span>
                </>
              )}
              {uploadPhase === "uploading" && (
                <button
                  onClick={cancelUpload}
                  title="Cancel upload"
                  className="shrink-0 flex items-center justify-center w-6 h-6 text-red-500 rounded-full hover:bg-red-50 transition cursor-pointer"
                >
                  <CircleX size={16} />
                </button>
              )}
            </div>
          )}
          <textarea value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
            placeholder={busy ? "Type your next message — it will be queued automatically…" : "Ask Buddy to read, draft, build a document, or use your apps…"}
            rows={3}
            className="w-full resize-none bg-transparent px-3 pt-3 pb-1 outline-none text-sm text-gray-800 placeholder-gray-400" />
          <div className="flex items-center gap-1 px-2 pb-2">
            <button onClick={() => fileRef.current?.click()} disabled={busy || uploading}
              title={uploading ? `Uploading… ${uploadProgress}%` : "Attach documents"}
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-200 rounded-lg transition disabled:opacity-40">
              {uploading ? <Loader2 size={16} className="animate-spin text-blue-500" /> : <Paperclip size={16} />}
            </button>
            <div className="flex-1" />
            <select value={model} onChange={(e) => setModel(e.target.value)}
              className="text-xs border border-gray-200 rounded-lg px-2 py-1.5 outline-none bg-white text-gray-500 cursor-pointer hover:border-gray-300" title="Select model">
              {MODELS.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
            </select>
            <button
              onClick={busy ? stop : () => send()}
              disabled={(!busy && !input.trim()) || (busy && isFull())}
              title={busy && isFull() ? `Queue full — max ${maxWait} messages allowed` : undefined}
              className="p-1.5 cursor-pointer text-gray-500 hover:text-gray-800 transition disabled:opacity-30">
              {busy ? <CirclePauseIcon size={20} /> : <SendHorizontal size={20} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
