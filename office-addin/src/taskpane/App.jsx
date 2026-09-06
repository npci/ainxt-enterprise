// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
import { useState, useEffect, useRef } from "react";
import { getHostContext, insertText, buildPrompt, QUICK_ACTIONS, getHost } from "./host-helpers.js";
import { officeSSO, isSSOFallback } from "./auth.js";

// AiNxt backend.
// 1. If `window.__AINXT_API__` is set (override in index.html), use that.
// 2. Else if the pane was loaded over HTTPS from the backend itself,
//    use `window.location.origin` — addin and API are co-hosted.
// 3. Else fall back to localhost (dev on Mac).
function resolveBackendBase() {
  if (window.__AINXT_API__) return window.__AINXT_API__;
  try {
    const origin = window.location.origin;
    if (origin && origin.startsWith("https://")) return origin;
  } catch { /* */ }
  return "http://localhost:8000";
}
const BACKEND_BASE = resolveBackendBase();
const AINXT = BACKEND_BASE + "/ainxt/v1/api";
// Auth routes are mounted under the API prefix too — `/ainxt/v1/api/auth/...`.
// (BACKEND_BASE + "/auth" hits the SPA host and 404s.)
const AUTH_URL = AINXT + "/auth";

// ── Auth helpers ────────────────────────────────────────────────────────────────

function getToken() {
  try { return sessionStorage.getItem("ainxt_addin_token") || ""; } catch { return ""; }
}
function saveToken(t) {
  try { sessionStorage.setItem("ainxt_addin_token", t); } catch { /* */ }
}

async function apiFetch(path, opts = {}) {
  const base = AINXT.replace("/ainxt/v1/api", "");
  const res = await fetch(`${base}${path}`, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${getToken()}`,
      ...(opts.headers || {}),
    },
  });
  return res;
}

async function checkM365Connected() {
  const res = await apiFetch("/ainxt/v1/api/connectors/status", { method: "GET" });
  if (!res.ok) return false;
  const list = await res.json();
  return (list || []).some(s => s.name === "microsoft_365" && s.connected);
}

// ── Host display name ──────────────────────────────────────────────────────────

function hostFromOffice() {
  const h = getHost();
  try {
    if (h === Office.HostType.Outlook)    return "Outlook";
    if (h === Office.HostType.Word)       return "Word";
    if (h === Office.HostType.Excel)      return "Excel";
    if (h === Office.HostType.PowerPoint) return "PowerPoint";
  } catch { /* */ }
  return "Office";
}

// ── Login screen ────────────────────────────────────────────────────────────────

function LoginScreen({ onLogin, host }) {
  const [email, setEmail]     = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr]         = useState("");
  const [busy, setBusy]       = useState(false);

  const submit = async () => {
    if (!email || !password) { setErr("Email and password required"); return; }
    setBusy(true); setErr("");
    try {
      const res = await fetch(`${AUTH_URL}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) { setErr("Invalid credentials"); return; }
      const data = await res.json();
      const tok = data.token || data.access_token || "";
      saveToken(tok);
      onLogin(tok);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col h-full bg-gray-950 text-gray-100 items-center justify-center p-5">
      <img src="/office-addin/icon-80.png" alt="AiNxt" className="w-12 h-12 mb-3 rounded-xl" />
      <h1 className="text-base font-bold text-white mb-1">Sign in to AiNxt</h1>
      <p className="text-xs text-gray-400 mb-4 text-center">
        {host === "Outlook"
          ? "Uses your Microsoft 365 connector token for Graph API access"
          : `AI assistant for ${host}`}
      </p>
      <div className="w-full space-y-2">
        <input className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm"
          placeholder="Email" value={email} onChange={e => setEmail(e.target.value)} />
        <input className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm"
          type="password" placeholder="Password" value={password}
          onChange={e => setPassword(e.target.value)}
          onKeyDown={e => e.key === "Enter" && submit()} />
        {err && <p className="text-xs text-red-400">{err}</p>}
        <button onClick={submit} disabled={busy}
          className="w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded py-2 text-sm font-medium transition">
          {busy ? "Signing in…" : "Sign In"}
        </button>
      </div>
    </div>
  );
}

// ── Main App ────────────────────────────────────────────────────────────────────

export default function App() {
  const [host, setHost]         = useState(hostFromOffice);
  const [token, setToken]       = useState(getToken);
  const [hostCtx, setHostCtx]   = useState(null);
  const [m365, setM365]         = useState(null); // null=checking, true/false (Outlook only)
  const [messages, setMessages] = useState([]);
  const [input, setInput]       = useState("");
  const [busy, setBusy]         = useState(false);
  const [insertDone, setInsertDone] = useState(false);
  const [ssoState, setSsoState] = useState("idle"); // idle | trying | fallback
  const endRef = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  // Entra SSO via Office.auth.getAccessToken() → /auth/sso/office (OBO).
  // Runs once on mount when no token; falls back to the password LoginScreen
  // on Office SSO errors (13xxx) or when Azure SSO is not configured.
  useEffect(() => {
    if (token || ssoState !== "idle") return;
    let cancelled = false;
    setSsoState("trying");
    officeSSO(AUTH_URL)
      .then(({ token: tok }) => {
        if (cancelled) return;
        saveToken(tok); setToken(tok); setSsoState("idle");
      })
      .catch((e) => {
        if (cancelled) return;
        if (!isSSOFallback(e)) console.warn("Office SSO failed:", e?.message || e);
        setSsoState("fallback");
      });
    return () => { cancelled = true; };
  }, [token, ssoState]);

  useEffect(() => {
    if (!token) return;
    getHostContext().then(setHostCtx).catch(() => setHostCtx(null));
    if (host === "Outlook") {
      checkM365Connected().then(setM365).catch(() => setM365(false));
    }
  }, [token, host]);

  // Refresh host context on Outlook item change so the pane follows the user
  useEffect(() => {
    if (host !== "Outlook") return;
    try {
      Office.context.mailbox?.addHandlerAsync?.(
        Office.EventType.ItemChanged,
        () => getHostContext().then(setHostCtx).catch(() => {}),
      );
    } catch { /* */ }
  }, [host]);

  const logout = () => {
    saveToken(""); setToken(""); setMessages([]); setHostCtx(null); setM365(null);
  };

  // Update the trailing assistant message (the streaming placeholder).
  const setAssistant = (content, error) => setMessages(prev => {
    const c = [...prev];
    for (let i = c.length - 1; i >= 0; i--) {
      if (c[i].role === "assistant") { c[i] = { role: "assistant", content, error }; break; }
    }
    return c;
  });

  const ask = async (prompt, displayLabel) => {
    if (busy) return;
    const display = displayLabel || prompt;
    // Add the user turn AND an empty assistant placeholder to stream into.
    setMessages(prev => [...prev, { role: "user", content: display }, { role: "assistant", content: "" }]);
    setBusy(true);
    try {
      const res = await apiFetch("/ainxt/v1/api/ask", {
        method: "POST",
        // /ask reads the prompt from `question` (NOT `message`). rag_mode off — the
        // add-in works off connectors/the open item, not KB retrieval; ephemeral so
        // these turns don't clutter the user's saved chat history.
        body: JSON.stringify({ question: prompt, stream: false, rag_mode: "off", ephemeral: true }),
      });
      if (res.status === 401) { logout(); return; }
      if (!res.ok) throw new Error(`Request failed (${res.status})`);

      // /ask responds with Server-Sent Events — lines like `data: {"t":"…"}`.
      // Accumulate the token deltas; ignore control frames (__meta__, [DONE]).
      // Calling res.json() on this stream is what threw "Unexpected token 'd'".
      let reply = "";
      const onFrame = (raw) => {
        const s = raw.trim();
        if (!s || s === "[DONE]") return;
        try {
          const o = JSON.parse(s);
          const tok = typeof o.t === "string" ? o.t
                    : typeof o.token === "string" ? o.token
                    : typeof o.delta === "string" ? o.delta : "";
          if (tok) { reply += tok; setAssistant(reply); }
        } catch { /* keep-alive / non-JSON frame — ignore */ }
      };
      const drain = (buf) => {
        const parts = buf.split("\n");
        const rest = parts.pop();
        for (const ln of parts) { const t = ln.trim(); if (t.startsWith("data:")) onFrame(t.slice(5)); }
        return rest;
      };

      if (res.body && res.body.getReader) {
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          buf = drain(buf);
        }
        const last = buf.trim();
        if (last.startsWith("data:")) onFrame(last.slice(5));
      } else {
        // Webview without streaming-body support: read it all, then parse.
        const txt = await res.text();
        for (const ln of txt.split("\n")) { const t = ln.trim(); if (t.startsWith("data:")) onFrame(t.slice(5)); }
      }
      if (!reply) setAssistant("No response");
    } catch (e) {
      setAssistant(`Error: ${e.message}`, true);
    } finally {
      setBusy(false);
    }
  };

  const sendInput = async () => {
    const text = input.trim();
    if (!text) return;
    setInput("");
    // Refresh selection at send time so prompts reflect what the user has now
    const fresh = await getHostContext().catch(() => hostCtx);
    setHostCtx(fresh);
    const ctxLine = fresh && (fresh.subject || fresh.selection)
      ? (host === "Outlook"
          ? `[Current email — Subject: "${fresh.subject || ""}", From: "${fresh.from || ""}"]\n\n${text}`
          : `[Current ${host} selection]\n${(fresh.selection || "").slice(0, 1500)}\n\n[User]\n${text}`)
      : text;
    ask(ctxLine, text);
  };

  const quickAction = async (actionId) => {
    const fresh = await getHostContext().catch(() => hostCtx);
    setHostCtx(fresh);
    if (!fresh) return;
    const prompt = buildPrompt(host, actionId, fresh);
    const action = (QUICK_ACTIONS[host] || []).find(a => a.id === actionId);
    ask(prompt, action?.label || actionId);
  };

  const insertReply = async () => {
    const last = [...messages].reverse().find(m => m.role === "assistant");
    if (!last) return;
    try {
      await insertText(last.content, hostCtx);
      setInsertDone(true);
      setTimeout(() => setInsertDone(false), 3000);
    } catch (e) {
      alert(`Cannot insert: ${e.message || e}\n\nMake sure you have a target selected${host === "Outlook" ? " or a reply/compose window open" : ""}.`);
    }
  };

  if (!token && ssoState === "trying") {
    return (
      <div className="flex flex-col h-full bg-gray-950 text-gray-100 items-center justify-center p-5">
        <img src="/office-addin/icon-80.png" alt="AiNxt" className="w-12 h-12 mb-3 rounded-xl" />
        <p className="text-sm text-gray-300">Signing in with Microsoft 365…</p>
      </div>
    );
  }
  if (!token) return <LoginScreen host={host} onLogin={tok => { saveToken(tok); setToken(tok); }} />;

  const hasReply = messages.some(m => m.role === "assistant");
  const actions = QUICK_ACTIONS[host] || [];

  // Header status text per host
  const ctxLine = host === "Outlook"
    ? (hostCtx?.subject ? `${hostCtx.subject} · ${hostCtx.from || ""}` : "(no email selected)")
    : (hostCtx?.selection
        ? `Selection: ${hostCtx.selection.slice(0, 80)}${hostCtx.selection.length > 80 ? "…" : ""}`
        : `(select something in ${host} to begin)`);

  const insertLabel = host === "Outlook" ? "Insert reply ↑" : `Insert into ${host} ↑`;

  return (
    <div className="flex flex-col h-screen bg-gray-950 text-gray-100 text-sm">

      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 bg-gray-900 border-b border-gray-800 shrink-0">
        <img src="/office-addin/icon-32.png" alt="" className="w-5 h-5 rounded" />
        <span className="font-semibold text-blue-400 flex-1">AiNxt · {host}</span>
        {host === "Outlook" && m365 === true  && <span className="text-xs text-green-400">M365 ✓</span>}
        {host === "Outlook" && m365 === false && <span className="text-xs text-yellow-400" title="Connect Microsoft 365 in AiNxt Settings → Connectors">M365 not connected</span>}
        <button onClick={logout} className="text-xs text-gray-500 hover:text-gray-300">Sign out</button>
      </div>

      {/* Context pill */}
      <div className="px-3 py-1.5 bg-gray-900/50 border-b border-gray-800 shrink-0">
        <p className="text-xs text-gray-400 truncate" title={ctxLine}>{ctxLine}</p>
      </div>

      {/* M365 not connected warning (Outlook only) */}
      {host === "Outlook" && m365 === false && (
        <div className="mx-3 mt-2 p-2 bg-yellow-900/20 border border-yellow-700/40 rounded text-xs text-yellow-300 shrink-0">
          Graph API features need Microsoft 365 connected in{" "}
          <span className="underline cursor-pointer" onClick={() => window.open("http://localhost:8000/?view=connectors", "_blank")}>
            Settings → Connectors
          </span>
        </div>
      )}

      {/* Quick actions */}
      <div className="flex flex-wrap gap-1.5 p-2 border-b border-gray-800 bg-gray-900/40 shrink-0">
        {actions.map(a => (
          <Btn
            key={a.id}
            label={a.label}
            onClick={() => quickAction(a.id)}
            disabled={busy || (a.needsM365 && !m365)}
            title={a.needsM365 && !m365 ? "Requires M365 connection" : undefined}
          />
        ))}
        {hasReply && (
          <Btn
            label={insertDone ? "Inserted ✓" : insertLabel}
            onClick={insertReply}
            disabled={busy || insertDone}
            accent
          />
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {messages.length === 0 && (
          <div className="text-center text-gray-600 mt-6 space-y-1">
            <p className="text-2xl">{host === "Outlook" ? "✉️" : host === "Excel" ? "📊" : host === "PowerPoint" ? "🖼️" : "📝"}</p>
            <p className="text-xs">Use the quick actions above or ask anything about your {host.toLowerCase()} content.</p>
            {host === "Outlook" && m365 && <p className="text-xs text-green-700">Graph API active — thread history available.</p>}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[88%] rounded-xl px-3 py-2 text-xs leading-relaxed ${
              m.role === "user"
                ? "bg-blue-700 text-white"
                : m.error
                  ? "bg-red-900/40 border border-red-800 text-red-300"
                  : "bg-gray-800 text-gray-100"
            }`}>
              <pre className="whitespace-pre-wrap font-sans">{m.content}</pre>
            </div>
          </div>
        ))}
        {busy && (
          <div className="flex justify-start">
            <div className="bg-gray-800 rounded-xl px-3 py-2 text-xs text-gray-500 flex gap-1 items-center">
              <span className="animate-pulse">●</span>
              <span className="animate-pulse" style={{animationDelay:"150ms"}}>●</span>
              <span className="animate-pulse" style={{animationDelay:"300ms"}}>●</span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* Input */}
      <div className="flex gap-2 p-2 border-t border-gray-800 bg-gray-900/50 shrink-0">
        <input
          className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-xs focus:outline-none focus:border-blue-500"
          placeholder={`Ask AiNxt about your ${host.toLowerCase()} content…`}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && !e.shiftKey && sendInput()}
          disabled={busy}
        />
        <button onClick={sendInput} disabled={busy || !input.trim()}
          className="bg-blue-600 hover:bg-blue-500 disabled:opacity-40 rounded-lg px-3 py-1.5 text-xs transition">
          Send
        </button>
      </div>
    </div>
  );
}

function Btn({ label, onClick, disabled, accent, title }) {
  return (
    <button onClick={onClick} disabled={disabled} title={title}
      className={`px-2 py-1 text-xs rounded transition disabled:opacity-40 ${
        accent ? "bg-green-700 hover:bg-green-600 text-white" : "bg-gray-700 hover:bg-gray-600 text-gray-200"
      }`}>
      {label}
    </button>
  );
}