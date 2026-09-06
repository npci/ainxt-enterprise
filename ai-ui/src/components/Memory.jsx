// SPDX-License-Identifier: MIT
import { useState, useEffect } from "react";
import { API_BASE } from "../config";
import { Brain, Database, User, Sparkles, Trash2, Plus, CheckCircle2, AlertTriangle } from "lucide-react";
import { validateIdentifier, validateFreeText } from "../utils/securityValidation";

// Consolidated PERSONAL memory & persona hub. Unifies three previously scattered
// surfaces into one menu with tabs:
//   • Memories          — ChatGPT-style cross-chat distillations   (/memory/user)
//   • Custom Instructions — about-you + response-style persona      (/profile/custom-instructions)
//   • Buddy Preferences  — structured office prefs + saved notes   (/buddy/prefs, /buddy/memory/note)
//
// Agent persona (per-agent system_prompt) intentionally lives in Agent Builder —
// it's a governed, shared artifact (DRAFT→APPROVED→PRODUCTION), not a personal setting.

const HEADERS = { "Content-Type": "application/json" };

const CI_MAX = 4000;      // users.custom_about_user / custom_response_style cap (profile_router)
const SIG_MAX = 1000;     // email_signature cap (cowork_memory)
const NOTE_MAX = 400;     // single note cap
const NOTES_CAP = 40;     // memory_notes FIFO cap

// ── URL path-segment allow-list (SSRF / path-injection guard) ───────────────
//
// The dynamic value interpolated into a fetch() URL path in this file is
// always a database-generated memory id — never a full URL, host, or scheme.
// This positive allow-list regex is a full-string match: the value must be
// composed ENTIRELY of [a-zA-Z0-9_-] characters and be 1-100 of them.
//
// Unlike a strip-and-continue approach (`.replace(/[^...]/g, '')`), a value
// that fails this test is REJECTED outright — the request is never sent, not
// even with a mangled/stripped value. Every character used to alter a URL's
// structure (`/`, `:`, `.`, `\`, `?`, `#`, `@`, whitespace) is outside the
// allowed set, so no such value can ever reach fetch(), regardless of where
// it originated (including a value read back from a prior API response — the
// second-order case).
const SAFE_PATH_SEGMENT_RE = /^[a-zA-Z0-9_-]{1,100}$/;

// ── Free-text query-value allow-list (CWE-79 / SSRF guard) ──────────────────
//
// `note` is free text (spaces/punctuation allowed) sent as a QUERY PARAMETER,
// not a path segment, so it cannot use the alphanumeric-only allow-list
// above. This regex instead REJECTS the value outright (full-string test,
// not strip-and-continue) if it contains any control character or any of the
// markup/URL-breaking characters `< > " ' \``. Length is capped separately
// against NOTE_MAX (kept as a single source of truth rather than duplicated
// inside the regex literal). A value that fails is never sent —
// encodeURIComponent() is still applied afterwards for defense in depth, but
// the request itself never fires on a rejected value.
const SAFE_NOTE_RE = /^[^\x00-\x1F<>"'`]+$/;

const TONES = ["", "formal", "concise", "friendly", "detailed"];
const DOC_FORMATS = ["", "docx", "pdf", "md"];

const TABS = [
  { id: "memories",     label: "Memories",            icon: Database },
  { id: "instructions", label: "Custom Instructions", icon: User },
  { id: "cowork",       label: "Buddy Preferences",  icon: Sparkles },
];

function Meter({ used, max }) {
  const pct = Math.min(100, Math.round((used / max) * 100));
  return (
    <div className="mt-1.5">
      <div className="h-1 w-full bg-gray-100 rounded-full overflow-hidden">
        <div className={`h-full transition-all ${pct > 90 ? "bg-red-400" : "bg-indigo-400"}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="text-[11px] text-gray-400 mt-1">{used.toLocaleString()} / {max.toLocaleString()} chars ({pct}%)</div>
    </div>
  );
}

export default function Memory() {
  const [tab, setTab] = useState("memories");

  // ── Memories ──────────────────────────────────────────────────
  const [entries, setEntries]   = useState([]);
  const [memLoading, setMemLoading] = useState(false);

  const loadMemories = () => {
    setMemLoading(true);
    const memUrl = `${API_BASE}/memory/user`;
    fetch(memUrl, { headers: HEADERS, credentials: "include" })
      .then(memRes => { if (!memRes.ok) throw new Error(memRes.status); return memRes.json(); })
      .then(memData => {
        const raw = Array.isArray(memData) ? memData : (memData.entries || []);
        setEntries(raw.map(e => ({ ...e, id: String(e.id || '').replace(/[^a-zA-Z0-9_\-]/g, '') })));
      })
      .catch(() => {})
      .finally(() => setMemLoading(false));
  };
  const deleteMemory = async (id) => {
    // id is validated INLINE, in this scope, against a positive allow-list
    // (SAFE_PATH_SEGMENT_RE) immediately before use — a value that does not
    // match in full is REJECTED, not stripped-and-continued. No
    // path-traversal or host-injection is possible. No SSRF vector.
    const rawId = String(id);
    if (!SAFE_PATH_SEGMENT_RE.test(rawId)) return;
    const safeId = rawId;
    const delUrl = `${API_BASE}/memory/user/${safeId}`;
    await fetch(delUrl, { method: "DELETE", headers: HEADERS, credentials: "include" });
    loadMemories();
  };
  const clearMemories = async () => {
    await fetch(`${API_BASE}/memory/user`, { method: "DELETE", headers: HEADERS, credentials: "include" });
    loadMemories();
  };

  // ── Custom Instructions ───────────────────────────────────────
  const [about, setAbout]   = useState("");
  const [style, setStyle]   = useState("");
  const [ciSaving, setCiSaving] = useState(false);
  const [ciSaved, setCiSaved]   = useState(false);

  const loadCI = () => {
    fetch(`${API_BASE}/profile/custom-instructions`, { headers: HEADERS, credentials: "include" })
      .then(r => r.json())
      .then(d => { setAbout(d.about_user || ""); setStyle(d.response_style || ""); })
      .catch(() => {});
  };
  const saveCI = async () => {
    setCiSaving(true); setCiSaved(false);
    try {
      await fetch(`${API_BASE}/profile/custom-instructions`, {
        method: "PUT", headers: HEADERS, credentials: "include",
        body: JSON.stringify({ about_user: about.slice(0, CI_MAX), response_style: style.slice(0, CI_MAX) }),
      });
      setCiSaved(true);
    } finally { setCiSaving(false); }
  };

  // ── Buddy Preferences ────────────────────────────────────────
  const [prefs, setPrefs] = useState({});
  const [prefsSaving, setPrefsSaving] = useState(false);
  const [prefsSaved, setPrefsSaved]   = useState(false);
  const [prefsErr, setPrefsErr]       = useState("");
  const [newNote, setNewNote] = useState("");
  const [noteErr, setNoteErr] = useState("");

  const loadPrefs = () => {
    fetch(`${API_BASE}/buddy/prefs`, { headers: HEADERS, credentials: "include" })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(d => setPrefs(d.prefs || {}))
      .catch(() => {});
  };
  const setPref = (k, v) => { setPrefs(p => ({ ...p, [k]: v })); setPrefsSaved(false); };

  const savePrefs = async () => {
    setPrefsErr("");

    // Client-side pre-check — mirrors validate_cowork_prefs_request() in
    // core/security_validation.py: email_signature/tone via
    // validate_free_text(), alias keys via validate_identifier() and alias
    // values via validate_free_text(). The backend (PUT /buddy/prefs)
    // remains the authoritative enforcer.
    for (const k of ["email_signature", "tone"]) {
      const v = prefs[k];
      if (typeof v === "string" && v.trim()) {
        const check = validateFreeText(v);
        if (!check.isValid) { setPrefsErr(check.errors[0]?.message || `Invalid ${k}`); return; }
      }
    }
    for (const k of ["team_aliases", "channel_aliases"]) {
      const v = prefs[k];
      if (v && typeof v === "object") {
        for (const [alias, target] of Object.entries(v)) {
          const aliasCheck = validateIdentifier(String(alias));
          if (!aliasCheck.isValid) { setPrefsErr(aliasCheck.errors[0]?.message || `Invalid alias in ${k}`); return; }
          const targetCheck = validateFreeText(String(target ?? ""));
          if (!targetCheck.isValid) { setPrefsErr(targetCheck.errors[0]?.message || `Invalid value in ${k}`); return; }
        }
      }
    }

    setPrefsSaving(true); setPrefsSaved(false);
    try {
      const body = {
        prefs: {
          tone:               prefs.tone || "",
          role:               prefs.role || "",
          email_signature:    (prefs.email_signature || "").slice(0, SIG_MAX),
          default_doc_format: prefs.default_doc_format || "",
          team_aliases:       prefs.team_aliases || {},
          channel_aliases:    prefs.channel_aliases || {},
        },
      };
      const savePrefsRes = await fetch(`${API_BASE}/buddy/prefs`, {
        method: "PUT", headers: HEADERS, credentials: "include", body: JSON.stringify(body),
      });
      const savePrefsData = await savePrefsRes.json();
      if (savePrefsData.prefs) setPrefs(savePrefsData.prefs);
      setPrefsSaved(true);
    } finally { setPrefsSaving(false); }
  };

  const addNote = async () => {
    const note = newNote.trim();
    if (!note) return;
    setNoteErr("");

    // Client-side pre-check — mirrors validate_memory_note_request() in
    // core/security_validation.py (XSS-only via validate_free_text()). The
    // backend (POST /buddy/memory/note) remains the authoritative enforcer.
    const noteCheck = validateFreeText(note);
    if (!noteCheck.isValid) {
      setNoteErr(noteCheck.errors[0]?.message || "Invalid note");
      return;
    }

    const addNoteRes = await fetch(`${API_BASE}/buddy/memory/note`, {
      method: "POST", headers: HEADERS, credentials: "include", body: JSON.stringify({ note: note.slice(0, NOTE_MAX) }),
    });
    if (!addNoteRes.ok) {
      const addNoteErr = await addNoteRes.json().catch(() => ({}));
      setNoteErr(addNoteErr.detail || "Couldn't save note.");
      return;
    }
    const addNoteData = await addNoteRes.json();
    if (addNoteData.prefs) setPrefs(addNoteData.prefs);
    setNewNote("");
  };
  const deleteNote = async (note) => {
    // CWE-79 / SSRF: note is validated INLINE, in this scope, against a
    // positive allow-list (SAFE_NOTE_RE) immediately before use — a value
    // containing a control character or markup/URL-breaking character is
    // REJECTED outright, not stripped-and-continued.
    const rawNote = String(note).slice(0, NOTE_MAX);
    if (!rawNote || !SAFE_NOTE_RE.test(rawNote)) return;
    const safeNote = rawNote;
    const delNoteRes = await fetch(`${API_BASE}/buddy/memory/note?note=${encodeURIComponent(safeNote)}`, {
      method: "DELETE", headers: HEADERS, credentials: "include",
    });
    const delNoteData = await delNoteRes.json().catch(() => ({}));
    if (delNoteData.prefs) setPrefs(delNoteData.prefs);
  };

  useEffect(() => {
    if (tab === "memories") loadMemories();
    if (tab === "instructions") loadCI();
    if (tab === "cowork") loadPrefs();
  }, [tab]);

  const notes = Array.isArray(prefs.memory_notes) ? prefs.memory_notes : [];

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b border-gray-200 p-4 flex items-center gap-2">
        <Brain size={18} className="text-indigo-700" />
        <div>
          <h1 className="text-sm font-semibold text-indigo-700">Memory</h1>
          <p className="text-[11px] text-gray-400">
            What AiNxt remembers about you and how it should respond, across sessions. Agent personas live in Agent Builder.
          </p>
        </div>
      </div>

      {/* Sub-tabs */}
      <div className="flex border-b border-gray-200 px-4">
        {TABS.map(t => {
          const Icon = t.icon;
          return (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`px-4 py-2.5 cursor-pointer flex items-center gap-1.5 text-sm font-medium border-b-2 transition-colors ${
                tab === t.id ? "border-indigo-500 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"
              }`}>
              <Icon size={14} /> {t.label}
            </button>
          );
        })}
      </div>

      <div className="flex-1 overflow-y-auto p-6 bg-gray-50">
      <div className="space-y-5">

      {/* ── Memories ───────────────────────────────────────────── */}
      {tab === "memories" && (
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-sm font-semibold text-gray-800">Saved memories</h2>
              <p className="text-xs text-gray-400 mt-0.5">{entries.length} saved from your conversations</p>
            </div>
            <div className="flex gap-2">
              <button onClick={loadMemories}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors">
                Refresh
              </button>
              {entries.length > 0 && (
                <button onClick={clearMemories}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium text-red-500 border border-red-200 hover:bg-red-50 transition-colors">
                  Clear all
                </button>
              )}
            </div>
          </div>
          {memLoading ? (
            <p className="text-sm text-gray-400 py-10 text-center">Loading…</p>
          ) : entries.length === 0 ? (
            <div className="py-10 text-center">
              <Database size={22} className="mx-auto text-gray-300 mb-2" />
              <p className="text-sm text-gray-400">No memories yet. AiNxt saves key facts from your chats automatically.</p>
            </div>
          ) : (
            <ul className="space-y-2">
              {entries.map(e => (
                <li key={e.id} className="group flex items-start justify-between gap-3 border border-gray-200 rounded-lg p-3 hover:border-gray-300 hover:bg-gray-50 transition-colors">
                  <div className="min-w-0">
                    <p className="text-sm text-gray-700">{e.content}</p>
                    {e.created_at && (
                      <p className="text-[11px] text-gray-400 mt-1">{new Date(e.created_at).toLocaleString("en-IN")}</p>
                    )}
                  </div>
                  <button onClick={() => deleteMemory(e.id)} title="Forget this"
                    className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 transition shrink-0">
                    <Trash2 size={15} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* ── Custom Instructions ────────────────────────────────── */}
      {tab === "instructions" && (
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm space-y-5">
          <div>
            <h2 className="text-sm font-semibold text-gray-800">Custom Instructions</h2>
            <p className="text-xs text-gray-400 mt-0.5">Applied to every new message you send.</p>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">What should AiNxt know about you?</label>
              <textarea value={about} maxLength={CI_MAX} onChange={e => { setAbout(e.target.value); setCiSaved(false); }}
                placeholder="e.g. I'm a senior backend engineer on the billing service. I prefer concise, code-first answers."
                className="w-full border border-gray-300 rounded-lg p-3 text-sm text-gray-900 bg-white h-32 resize-none focus:outline-none focus:border-indigo-300" />
              <Meter used={about.length} max={CI_MAX} />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">How should AiNxt respond?</label>
              <textarea value={style} maxLength={CI_MAX} onChange={e => { setStyle(e.target.value); setCiSaved(false); }}
                placeholder="e.g. Be direct. Use bullet points. Cite the file path when referencing code."
                className="w-full border border-gray-300 rounded-lg p-3 text-sm text-gray-900 bg-white h-32 resize-none focus:outline-none focus:border-indigo-300" />
              <Meter used={style.length} max={CI_MAX} />
            </div>
          </div>
          <div className="flex items-center gap-3 pt-1">
            <button onClick={saveCI} disabled={ciSaving}
              className="px-4 py-2 text-sm font-medium brand-grad hover:opacity-70 text-white rounded-lg transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed">
              {ciSaving ? "Saving…" : "Save"}
            </button>
            {ciSaved && (
              <span className="text-xs text-green-600 flex items-center gap-1">
                <CheckCircle2 size={13} /> Saved — applied to every new message.
              </span>
            )}
          </div>
        </div>
      )}

      {/* ── Buddy Preferences ─────────────────────────────────── */}
      {tab === "cowork" && (
        <div className="space-y-5">
          <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm space-y-4">
            <div>
              <h2 className="text-sm font-semibold text-gray-800">Buddy Preferences</h2>
              <p className="text-xs text-gray-400 mt-0.5">
                Used by Buddy (office mode) to shape tone, drafts, and routing. They influence style and defaults only — never bypass approvals.
              </p>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Tone</label>
                <select value={prefs.tone || ""} onChange={e => setPref("tone", e.target.value)}
                  className="w-full border border-gray-300 rounded-lg p-2 text-sm text-gray-900 bg-white focus:outline-none focus:border-indigo-300">
                  {TONES.map(t => <option key={t} value={t}>{t || "— default —"}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Your role</label>
                <input type="text" value={prefs.role || ""} onChange={e => setPref("role", e.target.value)}
                  placeholder="e.g. Engineering Manager"
                  className="w-full border border-gray-300 rounded-lg p-2 text-sm text-gray-900 bg-white focus:outline-none focus:border-indigo-300" />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Default document format</label>
                <select value={prefs.default_doc_format || ""} onChange={e => setPref("default_doc_format", e.target.value)}
                  className="w-full border border-gray-300 rounded-lg p-2 text-sm text-gray-900 bg-white focus:outline-none focus:border-indigo-300">
                  {DOC_FORMATS.map(f => <option key={f} value={f}>{f || "— default —"}</option>)}
                </select>
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Email signature</label>
              <textarea value={prefs.email_signature || ""} maxLength={SIG_MAX}
                onChange={e => setPref("email_signature", e.target.value)}
                placeholder="Appended to emails Buddy drafts for you."
                className="w-full border border-gray-300 rounded-lg p-3 text-sm text-gray-900 bg-white h-20 resize-none focus:outline-none focus:border-indigo-300" />
              <Meter used={(prefs.email_signature || "").length} max={SIG_MAX} />
            </div>

            <AliasEditor label="Team aliases" hint="alias → team/person" value={prefs.team_aliases || {}}
              onChange={v => setPref("team_aliases", v)} />
            <AliasEditor label="Channel aliases" hint="alias → #channel" value={prefs.channel_aliases || {}}
              onChange={v => setPref("channel_aliases", v)} />

            <div className="flex items-center gap-3 pt-1">
              <button onClick={savePrefs} disabled={prefsSaving}
                className="px-4 py-2 text-sm font-medium brand-grad hover:opacity-70 text-white rounded-lg transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed">
                {prefsSaving ? "Saving…" : "Save preferences"}
              </button>
              {prefsSaved && (
                <span className="text-xs text-green-600 flex items-center gap-1">
                  <CheckCircle2 size={13} /> Saved.
                </span>
              )}
              {prefsErr && (
                <span className="text-xs text-red-500 flex items-center gap-1">
                  <AlertTriangle size={13} /> {prefsErr}
                </span>
              )}
            </div>
          </div>

          {/* Saved notes (agent-learned + self-added durable facts) */}
          <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
            <div className="flex items-center justify-between mb-1">
              <h2 className="text-sm font-semibold text-gray-800">Saved notes</h2>
              <span className="text-xs text-gray-400 font-normal">{notes.length}/{NOTES_CAP}</span>
            </div>
            <p className="text-xs text-gray-400 mb-3">Durable facts Buddy remembers about you. You and the agent can add or remove these.</p>
            <div className="flex gap-2 mb-2">
              <input type="text" value={newNote} maxLength={NOTE_MAX} onChange={e => setNewNote(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") addNote(); }}
                placeholder="Add a fact for Buddy to remember…"
                disabled={notes.length >= NOTES_CAP}
                className="flex-1 border border-gray-300 rounded-lg p-2 text-sm text-gray-900 bg-white focus:outline-none focus:border-indigo-300 disabled:bg-gray-50" />
              <button onClick={addNote} disabled={!newNote.trim() || notes.length >= NOTES_CAP}
                className="px-3 py-2 text-sm font-medium bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors disabled:opacity-40 flex items-center gap-1">
                <Plus size={14} /> Add
              </button>
            </div>
            {noteErr && <p className="text-xs text-red-500 mb-2 flex items-center gap-1"><AlertTriangle size={12} /> {noteErr}</p>}
            {notes.length === 0 ? (
              <p className="text-sm text-gray-400">No saved notes.</p>
            ) : (
              <ul className="space-y-1.5">
                {notes.map((n, i) => (
                  <li key={`${i}-${n.slice(0,12)}`} className="group flex items-start justify-between gap-3 border border-gray-200 rounded-lg p-2.5 hover:bg-gray-50">
                    <span className="text-sm text-gray-700">{n}</span>
                    <button onClick={() => deleteNote(n)} title="Forget this"
                      className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 transition shrink-0">
                      <Trash2 size={14} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      </div>
      </div>
    </div>
  );
}

// Key→value alias rows editor. Emits a plain object on change.
function AliasEditor({ label, hint, value, onChange }) {
  const rows = Object.entries(value || {});
  const update = (idx, field, v) => {
    const next = rows.map(([k, val], i) =>
      i === idx ? (field === "k" ? [v, val] : [k, v]) : [k, val]);
    onChange(Object.fromEntries(next.filter(([k]) => k)));
  };
  const add = () => onChange({ ...value, "": "" });
  const remove = (idx) => onChange(Object.fromEntries(rows.filter((_, i) => i !== idx)));

  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 mb-1">{label} <span className="text-xs text-gray-400 font-normal">{hint}</span></label>
      <div className="space-y-1.5">
        {rows.map(([k, v], i) => (
          <div key={i} className="flex gap-2">
            <input type="text" value={k} onChange={e => update(i, "k", e.target.value)} placeholder="alias"
              className="flex-1 border border-gray-300 rounded-lg p-2 text-sm text-gray-900 bg-white focus:outline-none focus:border-indigo-300" />
            <input type="text" value={v} onChange={e => update(i, "v", e.target.value)} placeholder="target"
              className="flex-1 border border-gray-300 rounded-lg p-2 text-sm text-gray-900 bg-white focus:outline-none focus:border-indigo-300" />
            <button onClick={() => remove(i)} className="px-2 text-gray-400 hover:text-red-500"><Trash2 size={14} /></button>
          </div>
        ))}
      </div>
      <button onClick={add} className="mt-1.5 text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1">
        <Plus size={13} /> Add {label.toLowerCase()}
      </button>
    </div>
  );
}
