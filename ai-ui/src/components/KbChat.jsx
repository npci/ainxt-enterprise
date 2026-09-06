// SPDX-License-Identifier: MIT
import { useState, useRef, useEffect, useCallback } from "react";
import {
  CirclePauseIcon,
  SendHorizontal,
  MessageSquare,
  FileText,
  ShieldOff,
  X,
  Loader2,
  ThumbsUp,
  ThumbsDown,
  Copy,
  Check,
  Volume2,
  VolumeX,
  Mic,
  MicOff,
  Headphones,
  RotateCcw,
  Download,
  CircleX,
  BookOpen,
  Wand2,
  Users,
  Eye,
  ChevronRight,
  Globe2,
  Package,
  GitBranch,
  Pencil,
  Share2,
} from "lucide-react";
import ArtifactsPanel from "./ArtifactsPanel";
import MessageMeta from "./MessageMeta";
import { motion } from "framer-motion";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { mdComponents, mdUrlTransform, ExpandableMessageBody } from "./Message";
import VoiceMode from "./VoiceMode";
import AiNxtSpinner from "./AiNxtSpinner";
import { ChatMessageSkeleton, StreamingMessageSkeleton } from "./Skeleton";

import { API_BASE as API, authFetch } from '../config';
import { toIST } from '../utils/time';
import { useConfirm, useToast } from './ui/DialogProvider.jsx';
import { usePPTChat } from '../hooks/usePPTChat.js';
import { usePPTConversation } from '../hooks/usePPTConversation.js';
import DocumentPreviewModal from './DocumentPreviewModal.jsx';
import { cacheStore, cachePurgeExpired, cachedGet } from '../utils/previewCache';
import { stripMemoryTag, stripSystemPrefix, detectTone } from '../utils/messageContent.js';
import { formatKbScopePath } from '../utils/kbFormat.js';
import { validateFreeText } from '../utils/securityValidation';
import DocPickerCard from './DocPickerCard.jsx';

// ── extractDurationFromPrompt: parse a desired video duration from natural
// language. Returns a clamped integer in [min, max], or `fallback` if no
// duration phrase is present. Recognised forms (case-insensitive):
//   "10 second video", "10-second clip", "10s", "10 sec", "10 secs",
//   "10 seconds", "for 10 seconds", "duration 10", "duration: 10",
//   "duration of 10 seconds", "lasting 10 seconds"
// Numeric words 1–20 ("ten seconds") are also supported.
export function extractDurationFromPrompt(prompt, fallback = 8, min = 2, max = 16) {
  if (!prompt || typeof prompt !== "string") return fallback;
  const text = prompt.toLowerCase();

  const wordToNum = {
    one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7,
    eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13,
    fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18,
    nineteen: 19, twenty: 20,
  };

  // Try numeric forms first: "10s", "10 sec", "10-second", "10 seconds"
  // followed by optional " video"/"clip"/"long" keyword tolerance.
  const numericRe = /(\d{1,3})\s*(?:-\s*)?(?:s\b|sec(?:ond)?s?\b)/i;
  const wordRe    = /\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s*(?:-\s*)?(?:sec(?:ond)?s?)\b/i;
  // "duration[: of] 10" — number without an explicit unit but contextual.
  const durationRe = /\bduration(?:\s*(?:of|is|:|=))?\s*(\d{1,3})\b/i;

  let parsed = null;
  let m;
  if ((m = text.match(numericRe)))    parsed = parseInt(m[1], 10);
  else if ((m = text.match(wordRe)))  parsed = wordToNum[m[1].toLowerCase()];
  else if ((m = text.match(durationRe))) parsed = parseInt(m[1], 10);

  if (parsed == null || !Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

// ── AttachmentChip: cache-aware file chip for sent user messages ──────────
// If the file is still in the browser cache → interactive "View" button.
// If the cache entry has expired       → static chip with expired notice.
function AttachmentChip({ attachment, onPreview }) {
  const [cacheStatus, setCacheStatus] = useState("checking"); // "checking" | "available" | "expired"

  useEffect(() => {
    let cancelled = false;
    cachedGet(attachment.id).then(res => {
      if (!cancelled) setCacheStatus(res ? "available" : "expired");
    }).catch(() => {
      if (!cancelled) setCacheStatus("expired");
    });
    return () => { cancelled = true; };
  }, [attachment.id]);

  if (cacheStatus === "checking") {
    return (
      <span className="flex items-center gap-1 bg-gray-50 border border-gray-200 text-gray-400 text-xs px-2 py-0.5 rounded-full">
        <Loader2 size={10} className="animate-spin" />
        <span className="max-w-[120px] truncate">{attachment.file_name}</span>
      </span>
    );
  }

  if (cacheStatus === "available") {
    return (
      <button
        onClick={onPreview}
        title="Preview file"
        className="flex items-center gap-1 bg-white border border-gray-300 text-gray-600 text-xs px-2 py-0.5 rounded-full hover:border-indigo-400 hover:text-indigo-600 transition cursor-pointer"
      >
        <FileText size={10} />
        <span className="max-w-[120px] truncate">{attachment.file_name}</span>
        <Eye size={10} className="flex-shrink-0" />
      </button>
    );
  }

  // expired
  return (
    <span
      title="Preview expired — re-upload to preview again"
      className="flex items-center gap-1 bg-gray-50 border border-gray-200 text-gray-400 text-xs px-2 py-0.5 rounded-full"
    >
      <FileText size={10} />
      <span className="max-w-[120px] truncate">{attachment.file_name}</span>
      <span className="text-[9px] text-amber-500 ml-0.5">preview expired</span>
    </span>
  );
}

// stripMemoryTag / stripSystemPrefix / detectTone are imported from
// ../utils/messageContent.js so both Chat.jsx and KbChat.jsx use the
// same single source. CASUAL_PATTERNS lives in that file too.

function getFirstName(user) {
  if (!user) return "there";
  if (user.name) return user.name.split(/\s+/)[0];
  if (user.email) return user.email.split("@")[0];
  return "there";
}

const BASE_MODEL_OPTIONS = [
  { value: "auto",   label: "Auto" },
  { value: "claude", label: "Claude Sonnet 4.6" },
  { value: "gpt",    label: "GPT-5.4" },
];

// Document formats that the skill-based generator (POST /docs/generate) supports.
const DOC_FORMATS = ["pdf", "docx", "xlsx", "pptx", "md", "txt"];

// Status banner shown while a doc job is queued (keyed by format).
const DOC_STATUS_MAP = {
  pdf:  "📄 PDF document generation started",
  docx: "📝 Word document generation started",
  xlsx: "📈 Excel spreadsheet generation started",
  pptx: "📊 Presentation generation started via AI skillset…",
  md:   "📝 Markdown document generation started",
  txt:  "📃 Text document generation started",
};

// Cheap pre-filter: skip the local-model classifier entirely when the prompt
// shows no document-creation signal. Keeps median chat latency unchanged.
const DOC_KEYWORD_RE = /\b(pdf|docx?|word|xlsx?|excel|csv|pptx?|powerpoint|slide|slides|deck|presentation|markdown|md|txt|text|file|document|spreadsheet|sheet|report)\b/i;
const DOC_ACTION_RE  = /\b(generate|create|make|build|prepare|draft|write|compose|produce|export|download|give|need|want|send)\b/i;

// Brace-depth scanner: returns the first balanced JSON object containing
// "is_doc", or null. Tolerates nested objects and quoted braces in strings.
function _tryExtractJSON(text) {
  const start = text.indexOf("{");
  if (start < 0) return null;
  let depth = 0, inStr = false, esc = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (esc) { esc = false; continue; }
    if (c === "\\") { esc = true; continue; }
    if (c === '"') { inStr = !inStr; continue; }
    if (inStr) continue;
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) {
        const candidate = text.slice(start, i + 1);
        if (!candidate.includes('"is_doc"')) return null;
        try { return JSON.parse(candidate); } catch { return null; }
      }
    }
  }
  return null;
}

// Strict JSON schema instruction for the local-model intent classifier.
// Hoisted to module scope so we don't reallocate the ~600-char string per send.
const DOC_CLASSIFIER_SYS_PROMPT =
  "You are an intent classifier. Reply with ONLY a single JSON object — " +
  "no prose, no code fences, no commentary.\n" +
  'Schema: {"is_doc": boolean, "format": "pdf"|"docx"|"xlsx"|"pptx"|"md"|"txt"|null}\n' +
  "Set is_doc=true ONLY when the user explicitly wants you to CREATE / GENERATE " +
  "a downloadable file. Pick the format that best matches the request: " +
  "presentation/slides/deck/powerpoint → pptx, spreadsheet/excel/xls → xlsx, " +
  "word/doc → docx, pdf → pdf, markdown → md, plain text → txt. " +
  'If the user is just chatting or asking a question, return {"is_doc": false, "format": null}.';

const ACCEPT_TYPES = [
  ".pdf", ".docx", ".xlsx", ".xls", ".csv",
  ".html", ".htm", ".rtf", ".txt", ".json", ".md",
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
].join(",");

// KbChat — Knowledge Base chat surface. Forked from Chat.jsx so KB and
// regular chat can evolve independently. KbChat differs from Chat in:
//   - No internal sidebar (KbChatList renders the left rail).
//   - Outer container hard-coded to h-full (fits KB tab's flex box).
//   - Header is a 4-chip scope breadcrumb, not the chat title.
//   - Welcome shows the resolved KB scope path, not "Hey {firstName}!".
//   - Voice mode kept (intentional behavior change from embedded mode).
//   - Memory panel, Share, file/image attach, drag-drop, PPT wizard,
//     doc-generation classifier are all REMOVED — KB chats are typed
//     or voiced text-only conversations against the KB scope.
// SYNC WITH Chat.jsx sendMessage — any change to sendMessage here must
// also be applied in Chat.jsx until a shared `useChatSend` hook is
// extracted. See plan at .ainxt/plans/wild-roaming-treasure.md.
export default function KbChat({
  chats, setChats, activeChatId, setActiveChatId, user,
  chatsLoading = false, pendingPrompt, onPendingPromptConsumed,
}) {
  const { toast } = useToast();

  // No sidebar state in KbChat — chat-list interactions live in
  // KbChatList in the parent KB tab.

  // ── Message state ──────────────────────────────────────────
  const [input, setInput]           = useState("");
  const [loadingMap, setLoadingMap] = useState({});   // per-chat loading state keyed by chatId
  const [historyLoading, setHistoryLoading] = useState(false);

  // ── Multimodal state ───────────────────────────────────────
  // selectedModel is stored per-chat so that changing the model in one KB
  // chat does not bleed into other KB chats. KbChat is mounted once for the
  // entire KB tab (activeChatId changes on switch, not the component), so a
  // plain useState would be shared across all chats. A useRef map keyed by
  // chatId gives each chat its own isolated model selection.
  const modelPerChat = useRef({});                              // { [chatId]: modelValue }
  const [, setModelVersion] = useState(0);                     // bump to trigger re-render on model change
  const selectedModel = modelPerChat.current[activeChatId] ?? "auto";
  const setSelectedModel = useCallback((value) => {
    modelPerChat.current[activeChatId] = value;
    setModelVersion(v => v + 1);                               // force re-render
  }, [activeChatId]);
  const [attachments, setAttachments]     = useState([]);
  const [uploading, setUploading]         = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [fileLimitError, setFileLimitError] = useState(false);
  const [previewAttachment, setPreviewAttachment] = useState(null);


  // No PPT wizard / inline PPT chat in KbChat. The PPT conversation
  // hook below is still wired only because sendMessage references
  // `pptConversation.isActive()` from the suggested-reply handler;
  // for KB chats it always reports inactive and is effectively inert.
  const {
    generateOutline,
    confirmAndGenerate,
    downloadPresentation,
  } = usePPTChat(user);

  // ── PPT Conversation Hook (conversational flow) ───────────
  const pptConversation = usePPTConversation({
    insertMessage: (msg) => {
      setChats(prev => prev.map(chat => 
        chat.id === activeChatId 
          ? { ...chat, messages: [...chat.messages, msg], updatedAt: Date.now() }
          : chat
      ));
      setTimeout(() => {
        containerRef.current?.scrollTo({ top: containerRef.current.scrollHeight, behavior: "smooth" });
      }, 100);
    },
    updateMessage: (msgId, updates) => {
      setChats(prev => prev.map(chat => 
        chat.id === activeChatId 
          ? { ...chat, messages: chat.messages.map(m => m.id === msgId ? { ...m, ...updates } : m), updatedAt: Date.now() }
          : chat
      ));
    },
    chats,
    activeChatId,
    setChats,
    generateOutline,
    confirmAndGenerate,
    downloadPresentation,
  });

  // Note: PPT state is NOT reset when switching chats to allow background generation
  // The PPT conversation will persist even when user switches to other chats
  // Reset doc-generation lock when switching chats
  // to prevent loading state from persisting across chats
  useEffect(() => {
    pptConversation.reset();
    setDocGenerating(false);
    setEditingMsgId(null);
    // Clear the input box on chat switch so a draft typed in one KB chat
    // does not bleed into another KB chat when the user switches tabs.
    setInput("");
  }, [activeChatId]);

  // ── Budget exhausted banner ────────────────────────────────
  const [budgetExhausted, setBudgetExhausted] = useState(false);

  // ── Document generation in-progress flag ──────────────────
  // Separate from `loading` because doc jobs are background — loading is cleared immediately.
  const [docGenerating, setDocGenerating] = useState(false);

  // ── Image generation in-progress flag ─────────────────────
  // Used to disable the model selector during /image or Generate Image.
  const [imageGenerating, setImageGenerating] = useState(false);

  // No Memory panel in KbChat.

  // ── Saved prompt templates ("/" slash-command menu) ──────────────
  const [templates, setTemplates]   = useState([]);
  const [tplMenuOpen, setTplMenu]   = useState(false);
  const [tplFilter, setTplFilter]   = useState("");

  useEffect(() => {
    authFetch(`${API}/prompt-templates`)
        .then(r => r.ok ? r.json() : { templates: [] })
        .then(d => setTemplates(Array.isArray(d?.templates) ? d.templates : []))
        .catch(() => setTemplates([]));
  }, []);

  function handleInputChange(e) {
    const v = e.target.value;
    setInput(v);

    // Trigger "/" template menu only when slash is the very first character
    // (avoid hijacking mid-sentence slashes like file paths).
    if (v.startsWith("/") && !v.includes("\n")) {
      setTplFilter(v.slice(1).toLowerCase());
      setTplMenu(true);
    } else if (tplMenuOpen) {
      setTplMenu(false);
    }
  }

  function applyTemplate(tpl) {
    setInput(tpl.body || "");
    setTplMenu(false);
    setTimeout(() => document.getElementById("chat-input")?.focus(), 0);
  }

  async function saveSelectionAsTemplate() {
    const body = (input || "").trim();
    if (!body) return;
    const name = window.prompt("Template name?");
    if (!name) return;
    try {
      const r = await authFetch(`${API}/prompt-templates`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ name: name.trim(), body, scope: "private" }),
      });
      if (!r.ok) {
        toast.error("Save failed.");
        return;
      }
      const d = await authFetch(`${API}/prompt-templates`).then(x => x.json()).catch(() => ({}));
      setTemplates(Array.isArray(d?.templates) ? d.templates : []);
      toast.success(`Saved "${name.trim()}"`);
    } catch { toast.error("Save failed."); }
  }

  // ── Artifacts / Canvas panel ───────────────────────────────
  const [openArtifactId, setOpenArtifactId] = useState(null);

  // Detect & persist artifacts from a completed assistant message.
  // Heuristic: any fenced block in html / svg / mermaid that is
  // > 300 chars (i.e. a real document, not a snippet).
  async function maybeExtractArtifacts(messageId, content) {
    if (!messageId || !content) return;
    const re = /```([a-zA-Z0-9_+-]+)\s*\n([\s\S]*?)```/g;
    const blocks = [];
    let m;
    while ((m = re.exec(content)) !== null) {
      const lang = (m[1] || "").toLowerCase().trim();
      const body = m[2] || "";
      if (body.length < 300) continue;
      let type = null;
      if (lang === "html") type = "html";
      else if (lang === "svg") type = "svg";
      else if (lang === "mermaid") type = "mermaid";
      if (type) blocks.push({ type, body });
    }
    if (!blocks.length || !activeChatId) return;
    for (const b of blocks) {
      try {
        const r = await authFetch(`${API}/chats/${activeChatId}/artifacts`, {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          body:    JSON.stringify({
            type:       b.type,
            content:    b.body,
            title:      `${b.type.toUpperCase()} block`,
            message_id: messageId,
          }),
        });
        if (!r.ok) continue;
        const data = await r.json();
        // Tag the assistant message so the UI can render an "Open in Canvas" button
        setChats(prev => prev.map(c =>
            c.id === activeChatId
                ? {
                  ...c,
                  messages: c.messages.map(msg =>
                      msg.id === messageId
                          ? { ...msg, artifacts: [...(msg.artifacts || []), {
                              id: data.id, title: data.title, type: data.type,
                            }] }
                          : msg
                  ),
                }
                : c
        ));
      } catch (_e) { /* swallow */ }
    }
  }

  // ── Desktop pending prompt (from LocalFiles / clipboard) ──
  useEffect(() => {
    if (pendingPrompt) {
      setInput(pendingPrompt);
      if (onPendingPromptConsumed) onPendingPromptConsumed();
    }
  }, [pendingPrompt]);

    // Auto-dismiss the file-limit error banner after 5 seconds
  useEffect(() => {
    if (!fileLimitError) return;
    const t = setTimeout(() => setFileLimitError(false), 5000);
    return () => clearTimeout(t);
  }, [fileLimitError]);


  // ── All-provider model discovery ──────────────────────────
  const [localModels, setLocalModels] = useState([]);         // kept for backward compat
  const [allModelProviders, setAllModelProviders] = useState([]); // from /all-models
  const [allowedModels, setAllowedModels] = useState([]);     // from /model-governance/my-models

  // Flattened MODEL_OPTIONS filtered to only models the user is permitted to use
  const MODEL_OPTIONS = (() => {
    const raw = allModelProviders.length > 0
      ? allModelProviders.flatMap((group, gi) => [
          ...(gi > 0 ? [{ value: `__div_${gi}__`, label: `── ${group.provider} ──`, disabled: true }] : []),
          ...group.models.map(m => ({ value: m.id, modelId: m.modelId, label: m.label, modality: m.modality })),
        ])
      : BASE_MODEL_OPTIONS;

    if (allowedModels.length === 0) return raw;  // no restriction loaded yet — show all

    // Keep "auto" always, dividers always; filter real model entries by allowedModels
    return raw.filter(o =>
      o.value === 'auto' ||
      o.disabled ||            // section dividers
      allowedModels.includes(o.modelId || o.value)
    );
  })();

  // ── Image attachment state ─────────────────────────────────
  const [imageFiles, setImageFiles] = useState([]);  // Array of { file, previewUrl }
  const MAX_IMAGES = 3;

  const containerRef    = useRef(null);
  // Tracks whether the user has manually scrolled up — prevents auto-scroll
  // from hijacking their reading position during doc-generation polling.
  const userScrolledUp  = useRef(false);
  const requestIdMapRef = useRef({});    // chatId → X-Request-ID of the active stream
  const linkRef = useRef(null);
  const abortMapRef    = useRef({});    // chatId → AbortController for the active stream
  // No doc-intent classifier abort, no file/image input refs in KbChat.
  const textareaRef = useRef(null);
  const uploadXhrRef = useRef(null);
  // No confirm dialog hook (no destructive sidebar operations in KbChat).

  // ── Derived ────────────────────────────────────────────────
  const activeChat = chats.find(c => c.id === activeChatId);
  const messages   = activeChat?.messages || [];

  // Per-chat loading: only the active chat's loading state drives the UI.
  const loading = !!loadingMap[activeChatId];
  /** Set loading for a specific chat (defaults to the currently active chat). */
  const setLoading = useCallback((val, chatId) => {
    const id = chatId ?? activeChatId;
    setLoadingMap(prev => ({ ...prev, [id]: typeof val === "function" ? val(!!prev[id]) : val }));
  }, [activeChatId]);

  const inputDisabled = loading || docGenerating || uploading;

  // RAG toggle: per-chat. Default 'off' for new chats — standard AI chat default.
  const ragMode    = (activeChat?.rag_mode || "off");

  // Per-chat KB scope. Phase 1 wiring — when the user picks a product /
  // version (and optionally a specific doc), the /ask gateway server-side
  // reads these off the Chat row and injects them into _user_ctx so
  // hybrid_search filters deterministically. Persists across reloads.
  const chatScope = {
    product_id:   activeChat?.product_id   || null,
    domain:       activeChat?.domain       || null,
    spec_version: activeChat?.spec_version || null,
    kb_doc_id:    activeChat?.kb_doc_id    || null,
  };

  // ── Auto-scroll-to-bottom ─────────────────────────────────
  // Fires on:
  //   1. message count change (user sent / assistant added)
  //   2. last assistant message content growing (token-by-token streaming)
  //   3. spinnerStage change (so the pulsing dot stays visible)
  // We use scrollTop (instant) so the chat keeps pace with fast tokens;
  // a final smooth scroll happens once streaming completes.
  const _lastMsg     = messages[messages.length - 1];
  const _lastLen     = _lastMsg?.content?.length ?? 0;
  const _lastStage   = _lastMsg?.spinnerStage ?? null;
  const _isStreaming = !!_lastMsg?.streaming;

  // Called by the scroll container's onScroll — records whether the user has
  // scrolled up so the auto-scroll effect below can respect their position.
  const handleChatScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    userScrolledUp.current = (el.scrollHeight - el.scrollTop - el.clientHeight) > 120;
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // If the user has manually scrolled up, don't hijack their position.
    // This prevents doc-generation polling re-renders from snapping back to bottom.
    if (userScrolledUp.current) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (_isStreaming || nearBottom) {
      el.scrollTo({
        top: el.scrollHeight,
        behavior: _isStreaming ? "auto" : "smooth",
      });
    }
  }, [messages.length, _lastLen, _lastStage, _isStreaming]);

  const setChatRagMode = (mode) => {
    if (!activeChatId || !["off", "auto", "on"].includes(mode)) return;
    // Optimistic local update so the toggle reflects immediately
    setChats(prev => prev.map(c =>
        c.id === activeChatId ? { ...c, rag_mode: mode } : c
    ));
    authFetch(`${API}/chats/${activeChatId}/rag-mode`, {
      method:  "PATCH",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ rag_mode: mode }),
    }).catch(() => {
      // Best-effort: don't roll back local state — server-side default is the same.
    });
  };

  // PATCH the Chat row's KB scope columns. Debounced (~350 ms) so a
  // rapid dropdown sequence doesn't fire one round-trip per change.
  //
  // Timers are keyed PER CHAT (audit Fix #3) — switching to chat B between
  // an edit on chat A and the 350 ms deadline would otherwise have
  // clearTimeout() cancel A's pending PATCH, silently losing the edit.
  // The map keeps each chat's pending PATCH independent so it always lands.
  //
  // Cleanup on unmount + on chat-switch flushes any pending timers so an
  // SPA route change can't drop a write (audit Fix #2).
  const _patchTimers = useRef({}); // { [chat_id]: { timeoutId, pendingBody } }
  const _flushScopePatch = (cid) => {
    const slot = _patchTimers.current[cid];
    if (!slot) return;
    clearTimeout(slot.timeoutId);
    const body = slot.pendingBody;
    delete _patchTimers.current[cid];
    if (body) {
      authFetch(`${API}/chats/${cid}/scope`, {
        method:  "PATCH",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      }).catch(() => { /* best-effort */ });
    }
  };
  const setChatScope = (next) => {
    if (!activeChatId) return;
    const cid = activeChatId; // snapshot — closure must NOT see future switches
    setChats(prev => prev.map(c =>
      c.id === cid ? { ...c, ...next } : c
    ));
    const body = {
      product_id:   next.product_id   ?? null,
      domain:       next.domain       ?? null,
      spec_version: next.spec_version ?? null,
      kb_doc_id:    next.kb_doc_id    ?? null,
    };
    // Cancel only THIS chat's previous pending PATCH; other chats untouched.
    const existing = _patchTimers.current[cid];
    if (existing) clearTimeout(existing.timeoutId);
    const timeoutId = setTimeout(() => _flushScopePatch(cid), 350);
    _patchTimers.current[cid] = { timeoutId, pendingBody: body };
  };
  // Flush every pending timer on unmount so SPA route changes don't lose
  // in-flight edits.
  useEffect(() => {
    return () => {
      for (const cid of Object.keys(_patchTimers.current)) {
        _flushScopePatch(cid);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Feedback (thumbs-up / thumbs-down) ────────────────────────────────
  // feedbackMap: { [messageId]: 1 | -1 }  — persists within the session
  const [feedbackMap, setFeedbackMap] = useState({});
  // copiedId: messageId currently showing the ✓ check icon (resets after 1.5s)
  const [copiedId, setCopiedId]       = useState(null);

  // ── Tone / personalization ─────────────────────────────────
  const [toneMode, setToneMode]   = useState(null);   // "casual" | null
  const [toneScore, setToneScore] = useState(0);      // -10 to 10 RL score

  // ── Feedback modal ────────────────────────────────────────────────────
  const [feedbackModal, setFeedbackModal]       = useState({ open: false, msgId: null });
  const [feedbackIssue, setFeedbackIssue]       = useState("");
  const [feedbackSubIssue, setFeedbackSubIssue] = useState("");
  const [feedbackComment, setFeedbackComment]   = useState("");

  const FEEDBACK_ISSUES = [
    { label: "Incorrect or incomplete",  sub: [] },
    { label: "Not what I asked for",     sub: [] },
    { label: "Response quality / Style", sub: [] },
    {
      label: "Compliance concern",
      sub: [
        "PCI / DSS risk",
        "PII / AADHAAR / PAN exposure",
        "Regulatory non-compliance",
        "Audit trail concern",
        "Data residency issue",
      ],
    },
    {
      label: "Policy violation",
      sub: [
        "Unauthorised data access",
        "Data classification issue",
        "Internal policy breach",
        "Confidential data in response",
      ],
    },
    { label: "Other", sub: [] },
  ];

  // ── Document intent detection (mirrors Python chat_worker patterns) ──────────
  const _DOC_VERB_FORMAT_RE = /\b(generate|create|make|write|export|produce|draft|build|prepare|give|get|want|need|show|provide|send|share|download|fetch|output)\b[\s\S]{0,80}\b(document|report|presentation|slides|powerpoint|excel|spreadsheet|markdown)\b/i;
  const _DOC_EXT_RE         = /\.(pptx?|pdf|docx?|xlsx?|txt|md)\b|downloadable\b|\bdownload\s+(the\s+)?(file|doc|report|ppt|pdf|slide|deck)\b/i;
  const _DOC_NOUN_RE        = /\b(pptx|docx|xlsx)\b|\b(a|the|my|downloadable)\s+(ppt|pptx|pdf|excel|slide[\s-]?deck|presentation)\b|\bword\s+(doc(ument)?|file)\b/i;

  function isNonPPTDocIntent(text) {
    return _DOC_VERB_FORMAT_RE.test(text) || _DOC_EXT_RE.test(text) || _DOC_NOUN_RE.test(text);
  }

  function detectNonPPTDocFormat(text) {
    const t = text.toLowerCase();
    if (/\b(pptx?|presentation|slides?|slide[\s-]?deck|powerpoint)\b/.test(t)) return "pptx";
    if (/\b(xlsx?|excel|spreadsheet)\b/.test(t))                                  return "xlsx";
    if (/\b(docx?|word\s+(doc|document|file))\b/.test(t))                         return "docx";
    if (/\bpdf\b/.test(t))                                                         return "pdf";
    if (/\bmarkdown\b|\.md\b/.test(t))                                             return "md";
    if (/\btext\b|\.txt\b/.test(t))                                                return "txt";
    return "pdf";
  }

  // ── Text-to-Speech ─────────────────────────────────────────────────────
  const [speakingId, setSpeakingId]   = useState(null);   // messageId being spoken
  const [ttsVoices, setTtsVoices]     = useState([]);     // available OS/browser voices

  // ── Speech-to-Text ─────────────────────────────────────────────────────
  const [isListening, setIsListening] = useState(false);
  const recognitionRef                = useRef(null);
  const [micLang, setMicLang]         = useState("en-IN");

  // ── Voice mode ─────────────────────────────────────────────────────────
  const [voiceModeActive, setVoiceModeActive] = useState(false);

  // Sends text via /ask, streams response, appends messages, returns full answer string
  // mode: "platform" → uses docs_kb:platform RAG; "generic" → no RAG, pure model
  async function sendMessageForVoice(text, mode = "platform", onToken = null) {
    const assistantId = crypto.randomUUID();
    const currentMsgs = chats.find(c => c.id === activeChatId)?.messages || [];
    const newMessages = [
      ...currentMsgs,
      { id: crypto.randomUUID(), role: "user",      content: text, streaming: false },
      { id: assistantId,         role: "assistant", content: "", streaming: true,
        tokenUsage: null, costUsd: null, modelLabel: null, latency: null,
        inTok: null, outTok: null },
    ];
    updateMessages(newMessages);

    const controller = new AbortController();
    let accumulated = "";
    let sseBuffer = "";

    try {
      const response = await authFetch(`${API}/kb/ask`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          question:       text,
          chat_id:        activeChatId,
          voice_platform: mode === "platform",
        }),
        signal:  controller.signal,
      });

      if (!response.ok || !response.body) throw new Error("Voice request failed");

      const reader  = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: false });
      let modelLabel = null, latency = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        sseBuffer += decoder.decode(value, { stream: true });
        const parts = sseBuffer.split("\n\n");
        sseBuffer = parts.pop() ?? "";
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data: ")) continue;
          try {
            const obj = JSON.parse(line.slice(6));
            if (obj.t !== undefined) {
              accumulated += obj.t;
              if (onToken) onToken(accumulated);
              setChats(prev => prev.map(chat =>
                chat.id === activeChatId
                  ? { ...chat, messages: newMessages.map(m =>
                      m.id === assistantId ? { ...m, content: stripMemoryTag(accumulated) } : m), updatedAt: Date.now() }
                  : chat
              ));
            } else if (obj.__meta__) {
              modelLabel = obj.__meta__.model || null;
              latency    = obj.__meta__.latency || null;
            }
          } catch { /* skip */ }
        }
      }

      // Final strip of any <!--MEMORY:{...}--> footer
      const cleanVoiceAccum = stripMemoryTag(accumulated);

      setChats(prev => prev.map(chat =>
        chat.id === activeChatId
          ? { ...chat, messages: newMessages.map(m =>
              m.id === assistantId
                ? { ...m, content: cleanVoiceAccum, streaming: false, modelLabel, latency }
                : m), updatedAt: Date.now() }
          : chat
      ));
      fetchBudget();
      return cleanVoiceAccum;
    } catch (err) {
      setChats(prev => prev.map(chat =>
        chat.id === activeChatId
          ? { ...chat, messages: newMessages.map(m =>
              m.id === assistantId
                ? { ...m, content: `Error: ${err.message}`, streaming: false }
                : m), updatedAt: Date.now() }
          : chat
      ));
      throw err;
    }
  }

  // ── Auto-grow textarea ───────────────────────────────────────────────────
  const adjustTextareaHeight = useCallback((el) => {
    if (!el) return;
    // Reset height to auto to get the actual scroll height
    el.style.height = 'auto';
    // Calculate new height: min 60px (3 rows), max 200px
    const newHeight = Math.min(Math.max(el.scrollHeight, 60), 200);
    el.style.height = `${newHeight}px`;
  }, []);

  // Update textarea height when input changes
  useEffect(() => {
    if (textareaRef.current) {
      adjustTextareaHeight(textareaRef.current);
    }
  }, [input, adjustTextareaHeight]);
  const [budget, setBudget] = useState(null);
  const fetchBudget = () => {
    const uid = user?.userId || "";
    authFetch(`${API}/budget/me`, { headers: { "X-User-Id": uid } })
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setBudget(d))
      .catch(() => {});
  };
  useEffect(() => { fetchBudget(); }, []);

  // ── Purge expired preview cache entries on mount ──────────────────────────
  useEffect(() => { cachePurgeExpired(); }, []);

  // ── Fetch all available models + user's allowed models ───────────────────
  // Extracted so it can run both on mount AND right before the model dropdown
  // opens (see the <select>'s onFocus below) — otherwise a model an admin
  // adds/syncs via the "LLM Providers" screen while this tab is already open
  // never appears until a full page reload.
  const refreshModelLists = useCallback(() => {
    authFetch(`${API}/all-models`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d?.providers?.length) {
          setAllModelProviders(d.providers);
          // Keep localModels in sync for any code that still references it
          const localGroup = d.providers.find(p => p.provider.startsWith("Local"));
          if (localGroup) setLocalModels(localGroup.models.map(m => m.id?.replace("local:", "")).filter(Boolean));
        }
      })
      .catch(() => {});

    // Fetch user-specific allowed models from governance rules
    authFetch(`${API}/model-governance/my-models`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d?.models?.length) setAllowedModels(d.models); })
      .catch(() => {});
  }, []);

  useEffect(() => { refreshModelLists(); }, [refreshModelLists]);

  // ── Lazy-load messages from backend when opening a backend-only chat ──
  // Depends on BOTH activeChatId AND activeChat?.fromBackend so the effect
  // re-runs once the App-level /chats hydration arrives. Earlier this used
  // [activeChatId] only, which caused a race in the KB embed:
  //   1. User clicks a KB chat before App.jsx's /chats fetch resolves.
  //   2. Chat mounts, effect runs with activeChat=undefined → early return.
  //   3. /chats arrives, chat becomes available with fromBackend=true.
  //   4. Effect did NOT re-run (activeChatId unchanged) → messages stayed
  //      empty forever, making the chat look like a brand-new empty KB chat.
  useEffect(() => {
    if (!activeChatId) return;
    if (!activeChat || !activeChat.fromBackend || activeChat.messages.length > 0) return;
    setHistoryLoading(true);
    authFetch(`${API}/chats/${activeChatId}/messages`)
      .then(r => r.json())
      .then(data => {
        const loaded = (data.messages || []).map(m => ({
          id:         m.id,
          role:       m.role,
          content:    stripSystemPrefix(m.content),
          streaming:  false,
          modelLabel: m.model_used  || null,
          tokenUsage: m.tokens_used || null,
          costUsd:    m.cost_usd    || null,
          inTok:      m.in_tok      ?? null,
          outTok:     m.out_tok     ?? null,
          latency:    m.latency     ?? null,
          // Phase 3 transparency persistence — the coverage badge survives
          // a page reload because the trace dict is restored from the
          // assistant message row (kn_rewrite.md §8x). NULL on user turns
          // and on pre-Phase-1 history.
          coverageTrace: m.coverage_trace ?? null,
        }));
        setChats(prev => prev.map(c =>
            c.id === activeChatId
                ? { ...c, messages: loaded, fromBackend: false,
                  rag_mode: data.rag_mode || c.rag_mode || "off",
                  // Hydrate the per-chat KB scope picker from the server so
                  // it stays in sync after reload / chat switch.
                  product_id:   data.product_id   ?? c.product_id   ?? null,
                  domain:       data.domain       ?? c.domain       ?? null,
                  spec_version: data.spec_version ?? c.spec_version ?? null,
                  kb_doc_id:    data.kb_doc_id    ?? c.kb_doc_id    ?? null,
                }
                : c
        ));
      })
      .catch(() => {})
      .finally(() => setHistoryLoading(false));
  }, [activeChatId, activeChat?.fromBackend]);

  // ── Scroll to bottom when switching chats (after messages are loaded) ──
  useEffect(() => {
    if (historyLoading || !messages.length) return;
    // Use instant scroll (no animation) so it doesn't feel laggy on chat switch
    containerRef.current?.scrollTo({ top: containerRef.current.scrollHeight, behavior: "instant" });
  }, [activeChatId, historyLoading, messages.length]);

  // ── Revoke image preview URLs when switching chats or unmounting ──────
  useEffect(() => {
    return () => {
      setImageFiles(prev => {
        prev.forEach(img => URL.revokeObjectURL(img.previewUrl));
        return [];
      });
    };
  }, [activeChatId]);

  // No sidebar in KbChat — chat-list operations (rename, pin, delete,
  // new-chat) all live in KbChatList in the parent KB tab.

  // ── Message helpers ────────────────────────────────────────

  function updateMessages(updater) {
    setChats(prev =>
      prev.map(chat => {
        if (chat.id !== activeChatId) return chat;
        const next = typeof updater === "function" ? updater(chat.messages) : updater;
        return { ...chat, messages: next, updatedAt: Date.now() };
      })
    );
  }

  // ── Message feedback ───────────────────────────────────────

  async function handleFeedback(msgId, rating) {
    // Thumbs-up: direct submit. Thumbs-down: open modal.
    if (rating === -1) {
      setFeedbackIssue(""); setFeedbackSubIssue(""); setFeedbackComment("");
      setFeedbackModal({ open: true, msgId });
      return;
    }
    setFeedbackMap(prev => ({ ...prev, [msgId]: rating }));
    // RL: positive feedback in casual mode reinforces it
    if (rating === 1 && toneMode === "casual") setToneScore(s => Math.min(10, s + 2));
    try {
      await authFetch(`${API}/chat/messages/${msgId}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rating, rag_mode: ragMode }),
      });
    } catch (_) {
      setFeedbackMap(prev => { const n = { ...prev }; delete n[msgId]; return n; });
    }
  }

  async function submitFeedback() {
    const { msgId } = feedbackModal;
    setFeedbackMap(prev => ({ ...prev, [msgId]: -1 }));
    setFeedbackModal({ open: false, msgId: null });

    // Find the assistant message and the user prompt that preceded it
    const msgs = activeChat?.messages || [];
    const assistantIdx = msgs.findIndex(m => m.id === msgId);
    const assistantMsg = msgs[assistantIdx];
    const userMsg      = assistantIdx > 0
      ? msgs.slice(0, assistantIdx).reverse().find(m => m.role === "user")
      : null;

    try {
      await authFetch(`${API}/chat/messages/${msgId}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rating:             -1,
          rag_mode:           ragMode,
          issue:              feedbackIssue,
          sub_issue:          feedbackSubIssue,
          comment:            feedbackComment || null,
          user_prompt:        userMsg?.content?.slice(0, 2000) || null,
          assistant_summary:  assistantMsg?.content?.slice(0, 1000) || null,
        }),
      });
    } catch (_) {}
  }

  function handleCopy(msgId, content) {
    navigator.clipboard.writeText(content).then(() => {
      setCopiedId(msgId);
      setTimeout(() => setCopiedId(null), 1500);
    }).catch(() => {});
  }

  // ── User message actions (edit / copy) ─────────────────────
  // Track which message is being edited — messages are only removed on submit, not on click.
  const [editingMsgId, setEditingMsgId] = useState(null);

  // Count how many messages will be removed if the user submits the edit
  const editDiscardCount = (() => {
    if (!editingMsgId) return 0;
    const idx = messages.findIndex(m => m.id === editingMsgId);
    return idx === -1 ? 0 : messages.length - idx;
  })();

  function startEditUserMsg(msgId, content) {
    setEditingMsgId(msgId);
    setInput(content);
    setTimeout(() => textareaRef.current?.focus(), 50);
  }

  function cancelEditMsg() {
    setEditingMsgId(null);
    setInput("");
  }

  function handleShare(content) {
    if (navigator.share) {
      navigator.share({ title: "AiNxt Response", text: content }).catch(() => {});
    } else {
      navigator.clipboard.writeText(content).then(() => {
        toast.info("Copied to clipboard — paste to share.");
      }).catch(() => {});
    }
  }

  const handleTeamsShare = (content) => {
    try {
      const msg = encodeURIComponent(content);

      // Teams deep link
      const teamsUrl = `msteams:/l/chat/0/0?users=&message=${msg}`;

      if (linkRef.current) {
        linkRef.current.href = teamsUrl;
        linkRef.current.click(); // trigger click
      }
    } catch (err) {
      navigator.clipboard.writeText(content);
      toast.info("Copied to clipboard - paste in Teams");
    }
  };


  // ── Regenerate last response ───────────────────────────────────────────
  async function handleRegenerate() {
    if (loading) return;
    const msgs = activeChat?.messages || [];
    const lastUserIdx = [...msgs].map((m,i) => m.role === "user" ? i : -1).filter(i => i >= 0).at(-1);
    if (lastUserIdx === undefined) return;
    const lastUserMsg = msgs[lastUserIdx];
    // Drop the last assistant reply (everything after last user msg)
    const trimmed = msgs.slice(0, lastUserIdx + 1).filter((_, i) => i !== lastUserIdx);
    updateMessages(trimmed);
    setInput(lastUserMsg.content);
    setTimeout(() => document.getElementById("chat-send-btn")?.click(), 80);
  }

  // Generate an image inline via Imagen / DALL-E (routed through the LLM proxy).
  // Triggered when the user types `/image <prompt>` or `/img <prompt>`.
  // Appends a user-bubble (the prompt) and an assistant-bubble (the image).
  async function handleImageGenerate(prompt) {
    const trimmed = (prompt || "").trim();
    if (!trimmed) return;

    const imgChatId = activeChatId;   // snapshot for async safety
    const userMsgId = crypto.randomUUID();
    const astMsgId  = crypto.randomUUID();
    const baseMsgs = (activeChat?.messages || []);

    // Optimistic placeholder
    setChats(prev => prev.map(c =>
        c.id === activeChatId
            ? {
              ...c,
              messages: [
                ...baseMsgs,
                { id: userMsgId, role: "user",      content: `/image ${trimmed}` },
                { id: astMsgId,  role: "assistant", content: "", streaming: true },
              ],
            }
            : c
    ));

    setLoading(true, imgChatId);
    setImageGenerating(true);
    try {
      const resp = await authFetch(`${API}/chat/image-generate`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          prompt:        trimmed,
          chat_id:       activeChatId,
          aspect_ratio:  "16:9",
          provider:      "gemini",
          message_id:    astMsgId,
        }),
      });
      if (!resp.ok) {
        const err = await resp.text().catch(() => "");
        throw new Error(err || `Image generation failed (${resp.status})`);
      }
      const artifactId = resp.headers.get("X-Artifact-Id") || null;
      const provider   = resp.headers.get("X-Provider") || "imagen";
      const blob       = await resp.blob();
      const dataUrl    = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onloadend = () => res(r.result);
        r.onerror   = () => rej(new Error("read failed"));
        r.readAsDataURL(blob);
      });
      const md = `![generated image](${dataUrl})`;
      setChats(prev => prev.map(c =>
          c.id === activeChatId
              ? {
                ...c,
                messages: c.messages.map(m =>
                    m.id === astMsgId
                        ? {
                          ...m,
                          content:   md,
                          streaming: false,
                          artifacts: artifactId
                              ? [{ id: artifactId, title: "Generated image", type: "html" }]
                              : [],
                          modelLabel: provider,
                        }
                        : m
                ),
              }
              : c
      ));
    } catch (e) {
      setChats(prev => prev.map(c =>
          c.id === activeChatId
              ? {
                ...c,
                messages: c.messages.map(m =>
                    m.id === astMsgId
                        ? { ...m, content: `Error: ${e?.message || "image generation failed"}`, streaming: false }
                        : m
                ),
              }
              : c
      ));
    } finally {
      setLoading(false, imgChatId);
      setDocGenerating(false);
      setImageGenerating(false);
    }
  }

  // Continue a truncated/stopped assistant response. Backend endpoint
  // POST /ask/continue/{message_id} re-streams from the cut point.
  async function handleContinue(messageId) {
    if (loading) return;
    try {
      const resp = await authFetch(`${API}/ask/continue/${messageId}`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          chat_id:  activeChatId,
          rag_mode: ragMode,
        }),
      });
      if (!resp.ok) return;
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: false });
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const events = buf.split(/\n\n/);
        buf = events.pop() || "";
        for (const evt of events) {
          if (!evt.startsWith("data:")) continue;
          try {
            const o = JSON.parse(evt.slice(5).trim());
            if (o.t) {
              setChats(prev => prev.map(c =>
                  c.id === activeChatId
                      ? {
                        ...c,
                        messages: c.messages.map(m =>
                            m.id === messageId
                                ? { ...m, content: (m.content || "") + o.t }
                                : m
                        ),
                      }
                      : c
              ));
            }
            if (o.__meta__) {
              setChats(prev => prev.map(c =>
                  c.id === activeChatId
                      ? {
                        ...c,
                        messages: c.messages.map(m =>
                            m.id === messageId
                                ? { ...m, continuable: false }
                                : m
                        ),
                      }
                      : c
              ));
            }
          } catch { /* ignore */ }
        }
      }
    } catch (_e) {
      /* swallow — UI stays usable */
    }
  }

  // ── Share chat as public read-only link ────────────────────────────────
  async function handleShareChat() {
    if (!activeChatId || !messages.length) return;
    try {
      const r = await authFetch(`${API}/chats/${activeChatId}/share`, { method: "POST" });
      if (!r.ok) {
        toast.error("Share failed — please retry.");
        return;
      }
      const data = await r.json();
      const url = data?.url || (data?.token ? `${window.location.origin}/shared/${data.token}` : "");
      if (!url) {
        toast.error("Share endpoint returned no link.");
        return;
      }
      try {
        await navigator.clipboard.writeText(url);
        toast.success("Share link copied to clipboard");
      } catch {
        window.prompt("Copy this share link:", url);
      }
    } catch (_e) {
      toast.error("Share failed.");
    }
  }

  // ── Export chat ────────────────────────────────────────────────────────
  function handleExport() {
    const msgs = activeChat?.messages || [];
    if (!msgs.length) return;
    const title = activeChat?.title || "Chat";
    const lines = [`# ${title}`, `Exported: ${toIST(new Date())}`, ""];
    msgs.forEach(m => {
      lines.push(`**${m.role === "user" ? "You" : "AiNxt"}**`);
      lines.push(m.content || "");
      lines.push("");
    });
    const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href = url;
    a.download = `${title.replace(/[^a-z0-9]/gi, "_")}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // ── Text-to-Speech ─────────────────────────────────────────────────────

  function stripMarkdown(text) {
    return text
      .replace(/```[\s\S]*?```/g, "")            // fenced code blocks
      .replace(/`[^`\n]+`/g, "")                 // inline code
      .replace(/^#{1,6}\s+/gm, "")               // headings
      .replace(/\*{1,2}([^*\n]+)\*{1,2}/g, "$1") // bold / italic
      .replace(/_([^_\n]+)_/g, "$1")             // _italic_
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")   // [link](url) → link text
      .replace(/!\[[^\]]*\]\([^)]+\)/g, "")      // images
      .replace(/^[-*+]\s+/gm, "")                // list bullets
      .replace(/^\d+\.\s+/gm, "")                // numbered lists
      .replace(/[|>]/g, "")                      // tables / blockquotes
      .trim();
  }

  // Active backend-TTS audio element (one at a time)
  const ttsAudioRef = useRef(null);

  async function handleSpeak(msgId, content) {
    // Stop any in-flight speech (backend audio or fallback Web Speech)
    try { window.speechSynthesis?.cancel(); } catch { /* ignore */ }
    if (ttsAudioRef.current) {
      try { ttsAudioRef.current.pause(); } catch { /* ignore */ }
      ttsAudioRef.current = null;
    }
    if (speakingId === msgId) {
      setSpeakingId(null);
      return;
    }

    setSpeakingId(msgId);

    // Try backend TTS first (OpenAI TTS via /voice/tts — higher quality than browser).
    try {
      const resp = await authFetch(`${API}/voice/tts`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          text:  stripMarkdown(content).slice(0, 2000),
          voice: "nova",
          model: "tts-1-hd",
          speed: 0.95,
        }),
      });
      if (resp.ok) {
        const blob = await resp.blob();
        const url  = URL.createObjectURL(blob);
        const audio = new Audio(url);
        ttsAudioRef.current = audio;
        audio.onended = () => {
          setSpeakingId(null);
          URL.revokeObjectURL(url);
          ttsAudioRef.current = null;
        };
        audio.onerror = () => {
          setSpeakingId(null);
          URL.revokeObjectURL(url);
          ttsAudioRef.current = null;
        };
        await audio.play();
        return;
      }
      // Backend not available → fall through to browser speech
    } catch (_e) {
      /* fall through */
    }

    // Fallback: Web Speech API (covers offline / OPENAI_API_KEY missing)
    const langPrefix = micLang.split("-")[0];
    const voice =
        ttsVoices.find(v => v.lang === micLang) ||
        ttsVoices.find(v => v.lang.startsWith(langPrefix)) ||
        null;
    if (!voice && micLang !== "en-US") {
      toast.warn(`No TTS voice found for "${micLang}". Install via OS language settings or Chrome offline voices.`);
      setSpeakingId(null);
      return;
    }

    const utterance = new SpeechSynthesisUtterance(stripMarkdown(content));
    utterance.lang  = micLang;
    utterance.rate  = 1.0;
    if (voice) utterance.voice = voice;
    utterance.onend   = () => setSpeakingId(null);
    utterance.onerror = () => setSpeakingId(null);

    setSpeakingId(msgId);
    window.speechSynthesis.speak(utterance);
  }

  // Load available TTS voices — voices are populated asynchronously by the browser
  useEffect(() => {
    if (!window.speechSynthesis) return;
    const load = () => {
      const v = window.speechSynthesis.getVoices();
      if (v.length) setTtsVoices(v);
    };
    load(); // already available in some browsers
    window.speechSynthesis.addEventListener("voiceschanged", load);
    return () => window.speechSynthesis.removeEventListener("voiceschanged", load);
  }, []);

  // Stop TTS when the chat changes or component unmounts
  useEffect(() => {
    return () => { window.speechSynthesis?.cancel(); };
  }, [activeChatId]);

  // ── Speech-to-Text ─────────────────────────────────────────────────────

  function handleMicToggle() {
    if (isListening) {
      recognitionRef.current?.stop();
      setIsListening(false);
      return;
    }

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      toast.warn("Speech recognition is not supported in this browser. Try Chrome or Edge.");
      return;
    }

    const rec = new SR();
    rec.lang            = micLang;
    rec.continuous      = true;   // keep mic open until user stops it
    rec.interimResults  = true;
    rec.maxAlternatives = 1;

    rec.onstart  = () => setIsListening(true);
    rec.onend    = () => setIsListening(false);
    rec.onerror  = () => setIsListening(false);

    rec.onresult = (e) => {
      const transcript = Array.from(e.results)
        .map(r => r[0].transcript)
        .join("");
      setInput(transcript);
    };

    recognitionRef.current = rec;
    rec.start();
  }

  // ── File upload ────────────────────────────────────────────

   function cancelUpload() {
    if (uploadXhrRef.current) {
      uploadXhrRef.current.abort();
      uploadXhrRef.current = null;
    }
    setUploading(false);
    setUploadProgress(0);
    toast.info("Upload cancelled.");
  }

  async function handleFileUpload(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const MAX_FILES = 3;
    if (attachments.length + files.length > MAX_FILES) {
      setFileLimitError(true);
      e.target.value = "";
      return;
    }
    setUploading(true);
    setUploadProgress(0);
    if (e.target?.value !== undefined) e.target.value = "";

    const fd = new FormData();
    fd.append("chat_id", activeChatId);
    files.forEach(f => fd.append("files", f));

    try {
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        uploadXhrRef.current = xhr;
        xhr.open("POST", `${API}/chat/upload`);
        xhr.withCredentials = true; // sends httpOnly auth_token cookie
        xhr.upload.onprogress = (ev) => {
          if (ev.lengthComputable) setUploadProgress(Math.round((ev.loaded / ev.total) * 100));
        };
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
      const uploaded = result.uploaded || [];
      setAttachments(prev => [...prev, ...uploaded.filter(u => !u.blocked)]);

      // ── Cache file bytes in browser for client-side preview ──
      // Match uploaded entries to original File objects by filename
      for (const entry of uploaded) {
        if (entry.blocked) continue;
        const originalFile = files.find(f => f.name === entry.file_name);
        if (originalFile) {
          cacheStore(entry.id, originalFile, originalFile.type || "application/octet-stream");
        }
      }

      const blocked = uploaded.filter(u => u.blocked);
      if (blocked.length > 0) {
        // Inject a compliance block card into the chat for each blocked file
        const complianceMsgs = blocked.map(b => ({
          id:           crypto.randomUUID(),
          role:         "compliance_block",
          filename:     b.file_name,
          block_reason: b.block_reason || null,
          reasons:      b.compliance_reasons || [],
          streaming:    false,
        }));



        updateMessages([...messages, ...complianceMsgs]);
      }
    } catch (err) {
      // Don't show an error card for intentional cancellations
      if (err.message !== "Upload cancelled") {
      updateMessages([...messages, {
        id: crypto.randomUUID(), role: "assistant",
        content: `Upload error: ${err.message}`, streaming: false,
      }]);
    }
    } finally {
      uploadXhrRef.current = null;
      setUploading(false);
      setUploadProgress(0);
    }
  }

  function removeAttachment(id) {
    setAttachments(prev => prev.filter(a => a.id !== id));
  }

  // ── Image attachment helpers ────────────────────────────────

  const IMAGE_ACCEPTED = "image/jpeg,image/png,image/gif,image/webp";
  const IMAGE_MAX_BYTES = 10 * 1024 * 1024; // 10 MB

  function handleImageSelect(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    const validFiles = files.filter(file => {
      if (!["image/jpeg", "image/png", "image/gif", "image/webp"].includes(file.type)) {
        toast.error(`Unsupported format for "${file.name}". Use JPEG, PNG, GIF, or WebP.`);
        return false;
      }
      if (file.size > IMAGE_MAX_BYTES) {
        toast.error(`"${file.name}" is too large. Maximum size is 10 MB.`);
        return false;
      }
      return true;
    });

    if (!validFiles.length) return;

    setImageFiles(prev => {
      const remaining = MAX_IMAGES - prev.length;
      if (remaining <= 0) return prev;
      const toAdd = validFiles.slice(0, remaining);
      if (validFiles.length > remaining) {
        toast.error(`You can attach up to ${MAX_IMAGES} images. Only ${remaining} more allowed.`);
      }
      return [...prev, ...toAdd.map(file => ({ file, previewUrl: URL.createObjectURL(file) }))];
    });
  }

  function removeImage(index) {
    setImageFiles(prev => {
      const updated = [...prev];
      URL.revokeObjectURL(updated[index].previewUrl);
      updated.splice(index, 1);
      return updated;
    });
  }

   // ── Paste image from clipboard ────────────────────────────────────────
   const handlePaste = useCallback((e) => {
     const items = Array.from(e.clipboardData?.items || []);
    const imageItem = items.find(item => item.type.startsWith('image/'));

    if (!imageItem) return;
     e.preventDefault();
     let blob = imageItem.getAsFile();
     if (!blob && e.clipboardData && e.clipboardData.items) {
       // Try to get the first item that looks like an image
       for (const item of e.clipboardData.items) {
         if (item.type && item.type.startsWith('image/')) {
           try {
             blob = item.getAsFile();
             if (blob) break;
           } catch (err) {
             console.error('Error getting file from item:', err);
           }
         }
       }
     }
     
     if (!blob) {
       console.log('No image found from clipboard data');
       return;
     }
     
     // Determine appropriate file type for the blob
     let fileType = blob.type || 'image/png';
     if (fileType.includes('emf') || fileType.includes('wmf') || fileType.includes('metafile')) {
       // Convert Office metafile formats to PNG for compatibility
       fileType = 'image/png';
     }
     
     const file = new File([blob], `paste-${Date.now()}.png`, { type: fileType });
     // Reuse existing image select flow
      setImageFiles(prev => {
       if (prev.length >= MAX_IMAGES) {
         toast.error(`You can attach up to ${MAX_IMAGES} images.`);
         return prev;
       }
       return [...prev, { file, previewUrl: URL.createObjectURL(file) }];
     });
   }, []);

  // ── Drag-and-drop files onto the chat input area ──────────────────────
  const [dropError, setDropError] = useState(null);

  // Auto-dismiss drop error after 6 seconds
  useEffect(() => {
    if (!dropError) return;
    const t = setTimeout(() => setDropError(null), 6000);
    return () => clearTimeout(t);
  }, [dropError]);

  const ACCEPTED_DROP_MIME_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv", "application/csv",
    "text/html", "text/plain", "application/json", "text/rtf",
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
  ];

  // No drag-and-drop in KbChat.

  // ── @AgentName mention detection ───────────────────────────
  // Messages starting with @AiNxt or @AgentName are routed
  // to the agent runner instead of the /ask orchestrator.

  function parseMention(text) {
    const m = text.match(/^@([\w\-]+)\s+([\s\S]+)/i);
    if (!m) return null;
    return { agentName: m[1].toLowerCase().replace(/-/g, "_"), message: m[2].trim() };
  }

  // KbChat: no doc generation. Always returns is_doc:false so the
  // sendMessage flow short-circuits and routes straight to /ask.
  async function classifyDocIntent(_prompt) {
    return { is_doc: false, format: null };
  }

  // POST /docs/generate → _skill_generate() in the backend doc worker.
  // Used for ALL formats: pdf | docx | xlsx | pptx | md | txt.
  // Returns { jobId, filename } on success; throws on error.
  async function submitDocJob({ question, format, pendingAttachments }) {
    const body = {
      question,
      format,
      ...(format === "md" ? { mode: "generate" } : {}),
      chat_id:         activeChatId,
      title:           question.slice(0, 80),
      attachment_ids:  pendingAttachments.map(a => a.id),
      source_doc_name: pendingAttachments[0]?.file_name,
      // Pass the user's selected chat model so the worker can honour it.
      // Resolution on the server (workers/doc_worker._resolve_doc_model_hint):
      //   1. explicit user choice (e.g. "openai-deep") wins,
      //   2. else DOC_MODEL_PROVIDER env var (for auto-routing users),
      //   3. else "complex" (Claude Sonnet).
      user_model_hint: selectedModel || "auto",
      chat_context: pendingAttachments.length === 0
        ? (activeChat?.messages || [])
            .filter(m => m.role === "user" || m.role === "assistant")
            .slice(-12)
            .map(m => `${m.role === "user" ? "User" : "Assistant"}: ${(m.content || "").slice(0, 2000)}`)
            .join("\n\n") || undefined
        : undefined,
    };
    const r = await authFetch(`${API}/docs/generate`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`Server error ${r.status}: ${text.replace(/<[^>]+>/g, "").trim().slice(0, 200)}`);
    }
    const d = await r.json();
    if (!d.job_id) throw new Error(d.detail || "Document generation failed");
    return { jobId: d.job_id, filename: `document.${format}` };
  }

  // ── DocPickerCard re-query ─────────────────────────────────
  // Called when the user confirms a document selection from the
  // disambiguation picker. Re-sends the original question scoped
  // to the user-selected doc_ids via kb_doc_ids in the /ask body.
  // Adds a user message bubble so the conversation history is intact.
  async function sendDisambigMessage(question, kbDocIds, { addUserBubble = true } = {}) {
    if (!question || !kbDocIds?.length || inputDisabled) return;

    const chatId      = activeChatId;
    const assistantId = crypto.randomUUID();
    const controller  = new AbortController();
    abortMapRef.current[chatId] = controller;

    setLoading(true, chatId);
    updateMessages(prev => {
      const newEntries = [];
      if (addUserBubble) {
        newEntries.push({ id: crypto.randomUUID(), role: "user", content: question, streaming: false });
      }
      newEntries.push({
        id: assistantId, role: "assistant", content: "", streaming: true,
        // Anchor the elapsed-time clock so AiNxtSpinner can resume from the
        // correct value if the user switches KB chats and comes back while
        // the stream is still running. Without this, startAt is null and the
        // spinner resets to (0s) on every remount.
        streamStartAt: Date.now(),
        spinnerStage: 1,
        liveOutTok: 0, statusLine: "Thinking…",
        tokenUsage: null, costUsd: null, modelLabel: null, latency: null,
        inTok: null, outTok: null,
      });
      return [...prev, ...newEntries];
    });

    // Fix 3: track whether the message was properly finalized so the
    // finally block can force streaming:false if __meta__ never arrived
    // (network drop, backend crash) — prevents the spinner running forever.
    let _finalized = false;

    // Fix 4: accumulate all __meta__ fields so sources, model label,
    // latency, confidence and coverage badge all render correctly.
    let accumulated     = "";
    let sseBuffer       = "";
    let sources         = [];
    let modelLabel      = null;
    let latency         = null;
    let inTok           = null;
    let outTok          = null;
    let tokenUsage      = null;
    let costUsd         = null;
    let confidence      = null;
    let chunkCount      = 0;
    let coverageTrace   = null;
    let serverMessageId = null;

    try {
      const _kbChat = (chats || []).find(c => c.id === chatId);
      const body = {
        question,
        chat_id:    chatId,
        rag_mode:   ragMode,
        kb_doc_ids: kbDocIds,
      };
      if (_kbChat) {
        if (_kbChat.product_id)   body.product_id   = _kbChat.product_id;
        if (_kbChat.domain)       body.domain       = _kbChat.domain;
        if (_kbChat.spec_version) body.spec_version = _kbChat.spec_version;
      }
      // This resubmission dropped the user's model choice entirely — the
      // backend then had no model hint at all and silently fell back to a
      // cloud-only tier, failing with "Error: no gateway available" on any
      // install without an OpenAI/Claude key. Mirrors the same model
      // selection logic used for the initial /ask send (below).
      if (selectedModel !== "auto") {
        if (selectedModel.startsWith("local:")) {
          body.model       = "local";
          body.local_model = selectedModel.slice(6);
        } else {
          body.model = selectedModel;
        }
      }

      const response = await authFetch(`${API}/kb/ask`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
        signal:  controller.signal,
      });

      if (!response.ok || !response.body) throw new Error(`Server error ${response.status}`);

      // Capture server request-id so stopGeneration() can cooperatively cancel.
      requestIdMapRef.current[chatId] = response.headers.get("X-Request-ID") || null;

      const reader  = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: false });

      while (true) {
        if (controller.signal.aborted) { reader.cancel(); break; }
        const { done, value } = await reader.read();
        if (done) break;
        sseBuffer += decoder.decode(value, { stream: true });
        const parts = sseBuffer.split("\n\n");
        sseBuffer = parts.pop() ?? "";
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data: ")) continue;
          try {
            const obj = JSON.parse(line.slice(6));
            if (obj.status !== undefined) {
              // Live backend status — drives the spinner label.
              updateMessages(prev =>
                prev.map(m => m.id === assistantId ? { ...m, statusLine: obj.status } : m)
              );
            } else if (obj.t !== undefined) {
              accumulated += obj.t;
              const _liveOutTok = Math.ceil(stripMemoryTag(accumulated).length / 4);
              updateMessages(prev =>
                prev.map(m => m.id === assistantId
                  ? {
                      ...m,
                      content:      stripMemoryTag(accumulated),
                      spinnerStage: 3,
                      liveOutTok:   _liveOutTok,
                      statusLine:   null,  // clear pre-token label once tokens flow
                    }
                  : m)
              );
            } else if (obj.__meta__) {
              // Fix 4: extract all metadata fields — mirrors sendMessage path.
              const meta = obj.__meta__;
              if (meta.tokens         != null) tokenUsage    = String(meta.tokens);
              if (meta.cost           != null) costUsd       = String(meta.cost);
              if (meta.model          != null) modelLabel    = meta.model;
              if (meta.latency        != null) latency       = meta.latency;
              if (meta.in_tok         != null) inTok         = meta.in_tok;
              if (meta.out_tok        != null) outTok        = meta.out_tok;
              if (meta.confidence     != null) confidence    = meta.confidence;
              if (meta.chunk_count    != null) chunkCount    = meta.chunk_count;
              if (meta.sources        != null) sources       = meta.sources;
              if (meta.coverage_trace != null) coverageTrace = meta.coverage_trace;
              if (meta.message_id)             serverMessageId = meta.message_id;

              _finalized = true;
              updateMessages(prev =>
                prev.map(m => m.id === assistantId
                  ? {
                      ...m,
                      content:    stripMemoryTag(accumulated),
                      streaming:  false,
                      tokenUsage: tokenUsage ? parseInt(tokenUsage) : null,
                      costUsd:    costUsd    ? parseFloat(costUsd)  : null,
                      modelLabel,
                      latency,
                      inTok,
                      outTok,
                      confidence,
                      chunkCount,
                      sources,
                      coverageTrace,
                      id: serverMessageId || m.id,
                    }
                  : m)
              );
            }
          } catch { /* ignore malformed SSE events */ }
        }
      }
    } catch (err) {
      _finalized = true;
      if (err.name === "AbortError") {
        // User clicked stop — mark cancelled so the Continue button renders.
        updateMessages(prev =>
          prev.map(m => m.id === assistantId
            ? { ...m, streaming: false, cancelled: true, continuable: true }
            : m)
        );
      } else {
        updateMessages(prev =>
          prev.map(m => m.id === assistantId
            ? { ...m, content: "Error retrieving answer. Please try again.", streaming: false }
            : m)
        );
      }
    } finally {
      // Fix 3: safety net — if the stream closed without a __meta__ frame
      // (backend crash, network drop) the message stays streaming:true and
      // the spinner runs forever. Force-finalize with whatever was accumulated.
      if (!_finalized) {
        updateMessages(prev =>
          prev.map(m => m.id === assistantId
            ? {
                ...m,
                content:   stripMemoryTag(accumulated) || m.content,
                streaming: false,
                sources,
                modelLabel,
                latency,
                inTok,
                outTok,
                confidence,
                chunkCount,
                coverageTrace,
              }
            : m)
        );
      }
      setLoading(false, chatId);
      delete abortMapRef.current[chatId];
      delete requestIdMapRef.current[chatId];
    }
  }

  // ── Send message ───────────────────────────────────────────
  // SYNC WITH Chat.jsx sendMessage — any change here must also be
  // applied to Chat.jsx::sendMessage. Both files duplicate this ~700
  // line streaming pipeline verbatim. Until the duplication is
  // extracted into a useChatSend hook, treat the two implementations
  // as a single source — diffs between them are bugs.
  async function sendMessage() {
    if (!input.trim() || inputDisabled) return;

    // Client-side pre-check mirroring validate_free_text(q.question) in
    // routers/kb_ask_router.py's POST /kb/ask handler. Backend remains the
    // authoritative enforcer.
    const questionCheck = validateFreeText(input);
    if (!questionCheck.isValid) {
      toast.error(questionCheck.errors[0]?.message || "Invalid question");
      return;
    }

    // Phase 1 — flush any pending scope PATCH for this chat BEFORE /ask so
    // the gateway server-side reads the freshly-saved Chat row, not the
    // stale one. Without this, hitting Send within 350 ms of changing the
    // scope dropdown sends a request whose retrieval is still scoped to
    // the previous product/version. (Audit Fix #4)
    if (activeChatId && _patchTimers.current[activeChatId]) {
      _flushScopePatch(activeChatId);
    }

    // Snapshot the chat this request belongs to — so async continuations
    // clear loading / abort the correct stream even if the user switches tabs.
    const chatId = activeChatId;

    const question = input;
    // ── Slash command: /image <prompt> | /img <prompt> ──────────────
    // Routes to Imagen / DALL-E via the LLM proxy. No 3rd-party stock APIs.
    const _imgMatch = question.trim().match(/^\/(?:image|img|imagine)\s+([\s\S]+)$/i);
    if (_imgMatch) {
      const _prompt = _imgMatch[1].trim();
      setInput("");
      await handleImageGenerate(_prompt);
      return;
    }

    const assistantId = crypto.randomUUID();
    const pendingImages = [...imageFiles];   // snapshot before clearing

    setInput("");
    userScrolledUp.current = false;   // new message → resume auto-scroll
    setLoading(true, chatId);
    // Clear image state immediately so UI feels responsive
    setImageFiles([]);

    // If the user is editing a previous message, trim the history at that point
    // so everything from the edited message onward is discarded before appending.
    // Compute baseMessages for use throughout — `messages` is stale until next render.
    let baseMessages = messages;
    if (editingMsgId) {
      const editIdx = messages.findIndex(m => m.id === editingMsgId);
      if (editIdx !== -1) {
        baseMessages = messages.slice(0, editIdx);
      }
      setEditingMsgId(null);
    }

    // Generic @agent routing — dispatches user agents like @sql_agent <msg>.
    // Document intent (@ppt, @new_doc, @edit_doc) is now handled by the
    // local-model classifier below, not by @-mentions.
    const mention = parseMention(question.trim());
    if (mention) {
      // Route to agent runner
      const pendingAttachments = [...attachments];
      setAttachments([]);
      const userContent = `@${mention.agentName} ${mention.message}`;
      updateMessages([
        ...baseMessages,
        { id: crypto.randomUUID(), role: "user",      content: userContent, streaming: false,
        imageUrls: pendingImages.map(i => i.previewUrl) },
        { id: assistantId,         role: "assistant", content: "⚡ Routing to agent…", streaming: true,
          modelLabel: null, latency: null, inTok: null, outTok: null },
      ]);
      try {
        const r = await authFetch(`${API}/agents/${mention.agentName}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: mention.message, session_id: activeChatId }),
        });
        const d = await r.json();
        const rawAnswer = r.ok ? (d.answer || d.result || JSON.stringify(d)) : `Agent error: ${d.detail || r.statusText}`;
        // Build inline tool call status lines from tool_outputs
        const toolLines = (d.tool_outputs || []).map(t => {
          const status = t.success ? "✓" : "✗";
          return `\`🔧 Tool: ${t.tool} → ${status}\``;
        });
        const answer = toolLines.length > 0
          ? `${toolLines.join("\n")}\n\n${rawAnswer}`
          : rawAnswer;
        const currentMsgs1 = chats.find(c => c.id === activeChatId)?.messages || [];
        updateMessages(currentMsgs1.map(m =>
          m.id === assistantId
            ? { ...m, content: answer, streaming: false, modelLabel: `agent:${mention.agentName}` }
            : m
        ));
      } catch (e) {
        const currentMsgs2 = chats.find(c => c.id === activeChatId)?.messages || [];
        updateMessages(currentMsgs2.map(m =>
          m.id === assistantId ? { ...m, content: `Error: ${e.message}`, streaming: false } : m
        ));
      } finally {
        setLoading(false, chatId);
      }
      return;  // don't fall through to /ask
    }

    const pendingAttachments = [...attachments];
    setAttachments([]);

    const userContent = pendingAttachments.length > 0
      ? `${question}\n\n📎 ${pendingAttachments.map(a => a.file_name).join(", ")}`
      : question;

    const newMessages = [
      ...baseMessages,
      {
        id: crypto.randomUUID(), role: "user", content: userContent, streaming: false,
        // Store attachment metadata so sent messages can show a preview button
        attachments: pendingAttachments.length > 0
          ? pendingAttachments.map(a => ({
              id:         a.id,
              file_name:  a.file_name,
              file_type:  a.file_type,
              file_size:  a.file_size,
              parsed_text: a.parsed_text || "",
            }))
          : undefined,
        // Attach the local object URLs so the bubble can show thumbnails
        imageUrls: pendingImages.map(i => i.previewUrl),
      },
      { id: assistantId,         role: "assistant", content: "",          streaming: true,
        // spinnerStage 0=Understanding, 1=Searching (RAG), 2=Tools, 3=Generating
        spinnerStage: ragMode === "off" ? 0 : 1,
        // Live status-line clock anchor + running output-token estimate.
        streamStartAt: Date.now(), liveOutTok: 0, statusLine: "Thinking…",
        tokenUsage: null, costUsd: null, modelLabel: null, latency: null,
        inTok: null, outTok: null,
        tokensToday: null, maxTokensToday: null,
        requestsToday: null, maxRequestsToday: null },
    ];

    // Auto-title: use first user question as chat name
    if (baseMessages.length === 0) {
      const raw   = question.trim();
      const title = raw.length > 50 ? raw.slice(0, 50).trimEnd() + "…" : raw;
      setChats(prev =>
        prev.map(chat =>
          chat.id === activeChatId
            ? { ...chat, title, messages: newMessages, updatedAt: Date.now() }
            : chat
        )
      );
    } else {
      updateMessages(newMessages);
    }

    setTimeout(() => {
      containerRef.current?.scrollTo({ top: containerRef.current.scrollHeight, behavior: "smooth" });
    }, 100);

    // Local-model gates doc-intent routing. Doc → POST /docs/generate
    // (skill-based), non-doc → fall through to /ask SSE chat stream.
    //
    // The single assistant placeholder (`assistantId`) stays mounted across
    // BOTH the classifier hop and the doc-job submission — only its
    // `docStage` / `docFormat` fields update, so the user sees ONE unified
    // "AiNxt is thinking" panel that progresses through:
    //   "Understanding" → "Detecting format" → "Drafting <FMT>" → marker
    //   mounts → DocDownloadButton owns polling/progress UI from there.
    updateMessages(newMessages.map(m =>
      m.id === assistantId
        ? { ...m, docStage: "classify", spinnerStage: 0 }
        : m
    ));

    const _docIntent = await classifyDocIntent(question);
    if (_docIntent.is_doc && _docIntent.format) {
      const fmt = _docIntent.format;
      updateMessages(newMessages.map(m =>
        m.id === assistantId
          ? { ...m, docStage: "submitting", docFormat: fmt, spinnerStage: 1 }
          : m
      ));
      setDocGenerating(true);
      let jobSubmitted = false;
      try {
        const { jobId, filename } = await submitDocJob({
          question, format: fmt, pendingAttachments,
        });
        updateMessages(newMessages.map(m =>
          m.id === assistantId
            ? {
                ...m,
                content: buildDocJobMarker(jobId, fmt, filename),
                streaming: false,
                docStage: undefined, docFormat: undefined,
                timestamp: Date.now(),
              }
            : m
        ));
        jobSubmitted = true;
      } catch (e) {
        updateMessages(newMessages.map(m =>
          m.id === assistantId
            ? {
                ...m,
                content: `⚠ Document generation failed: ${e.message}`,
                streaming: false,
                docStage: undefined, docFormat: undefined,
                timestamp: Date.now(),
              }
            : m
        ));
      } finally {
        if (!jobSubmitted) setDocGenerating(false);
        setLoading(false, chatId);
      }
      return;
    }

    // Non-doc → fall through to /ask. Clear the doc stage so the standard
    // chat spinner labels render again.
    updateMessages(newMessages.map(m =>
      m.id === assistantId
        ? { ...m, docStage: undefined, docFormat: undefined }
        : m
    ));

    // Veo returns a finished MP4, not an SSE token stream — short-circuit
    // before the streaming code path and store the URL on the assistant msg.
    // Detected via the selected model's own "modality" (set by the backend's
    // /all-models response), not a hardcoded model id — so this keeps working
    // if the video model id ever changes via admin config.
    const _selectedModelMeta = MODEL_OPTIONS.find(o => o.value === selectedModel);
    if (_selectedModelMeta?.modality === "video") {
      // Backend doesn't emit progress events for the long-running Veo LRO,
      // so we drive the spinner stages locally: "submitting" immediately,
      // then "rendering" after VEO_RENDER_STAGE_DELAY_MS for visible movement.
      const VEO_RENDER_STAGE_DELAY_MS = 4000;
      // Backend clamps to [2, 16] (routers/chat_router.py:549-550); mirror
      // the same range here so user-facing rejections happen client-side.
      const VEO_DEFAULT_DURATION = 8;
      const VEO_MIN_DURATION = 2;
      const VEO_MAX_DURATION = 16;
      const durationSecs = extractDurationFromPrompt(
        question, VEO_DEFAULT_DURATION, VEO_MIN_DURATION, VEO_MAX_DURATION,
      );

      updateMessages(newMessages.map(m =>
        m.id === assistantId ? { ...m, videoStage: "submitting" } : m
      ));

      const veoStageTimer = setTimeout(() => {
        setChats(prev => prev.map(c => {
          if (c.id !== chatId) return c;
          let changed = false;
          const nextMessages = c.messages.map(m => {
            if (m.id === assistantId && m.videoStage === "submitting") {
              changed = true;
              return { ...m, videoStage: "rendering" };
            }
            return m;
          });
          return changed ? { ...c, messages: nextMessages } : c;
        }));
      }, VEO_RENDER_STAGE_DELAY_MS);

      try {
        const vbody = {
          prompt:        question,
          chat_id:       activeChatId,
          aspect_ratio:  "16:9",
          duration_secs: durationSecs,
        };
        const vres = await authFetch(`${API}/chat/video-generate`, {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          body:    JSON.stringify(vbody),
        });
        if (!vres.ok) {
          const errTxt = await vres.text().catch(() => "");
          throw new Error(`Video generation failed (${vres.status}): ${errTxt.slice(0, 300)}`);
        }
        const vdata  = await vres.json();
        const vurl   = `${API}${vdata.url}`;
        // Mirror the same shape as text /ask responses so MessageMeta
        // renders the standard footer (model · tokens · cost · latency)
        // beneath the generated video. Veo is per-second billed, so
        // tokenUsage is 0 by design — the duration chip carries the
        // per-second analog.
        const vidMsg = {
          id:         assistantId,
          role:       "assistant",
          content:    `🎬 Video generated (${vdata.duration}s, ${vdata.mime}). Click play to preview below.`,
          videoUrl:   vurl,
          videoMime:  vdata.mime || "video/mp4",
          streaming:  false,
          timestamp:  Date.now(),
          // Footer fields (rendered by MessageMeta below the video):
          modelLabel: vdata.model || null,
          tokenUsage: typeof vdata.total_tokens === "number" ? vdata.total_tokens : 0,
          inTok:      typeof vdata.input_tokens  === "number" ? vdata.input_tokens  : 0,
          outTok:     typeof vdata.output_tokens === "number" ? vdata.output_tokens : 0,
          costUsd:    typeof vdata.cost_usd      === "number" ? vdata.cost_usd      : null,
          // LatencyChip renders `${latency.toFixed(1)}s`, so it expects seconds.
          // Server returns latency_ms — convert here to match the text-chat path.
          latency:    typeof vdata.latency_ms    === "number" ? vdata.latency_ms / 1000 : null,
          // Video-only metadata (used by the small badge above the player):
          meta:       {
            model:    vdata.model,
            cost:     vdata.cost_usd,
            duration: vdata.duration,
            endpoint: vdata.endpoint || "/chat/video-generate",
          },
        };
        // Replace the streaming placeholder in-place by assistantId so the
        // spinner disappears and the <video> player renders.
        updateMessages(newMessages.map(m => m.id === assistantId ? vidMsg : m));
      } catch (e) {
        const errMsg = {
          id:        assistantId,
          role:      "assistant",
          content:   `⚠ Video generation failed: ${e.message}`,
          streaming: false,
          timestamp: Date.now(),
        };
        updateMessages(newMessages.map(m => m.id === assistantId ? errMsg : m));
      } finally {
        clearTimeout(veoStageTimer);
        setLoading(false, chatId);
      }
      return;
    }

    // Declared outside try so finally can clearTimeout without ReferenceError
    let streamTimeout;
    try {
      const controller = new AbortController();
      abortMapRef.current[chatId] = controller;

      // Auto-abort stream after 5 minutes — prevents zombie fetch if LLM hangs
      streamTimeout = setTimeout(() => controller.abort(), 5 * 60_000);

      let response;
      if (pendingImages.length > 0) {
        // Image path: POST /ask/image with multipart/form-data
        const fd = new FormData();
        fd.append("question", question);
        pendingImages.forEach(({ file }) => fd.append("image", file));
        fd.append("chat_id", activeChatId);
        // Mirror the same model selection logic used for text requests
        if (selectedModel !== "auto") {
          if (selectedModel.startsWith("local:")) {
            fd.append("model",       "local");
            fd.append("local_model", selectedModel.slice(6));  // e.g. "Kimi-k2.5"
          } else {
            fd.append("model", selectedModel);
          }
        }
        response = await authFetch(`${API}/ask/image`, {
          method: "POST",
          body:   fd,
          signal: controller.signal,
        });
      } else {
        // Regular path: POST /ask with JSON
        const body = {
          question,
          chat_id:        activeChatId,
          attachment_ids: pendingAttachments.map(a => a.id),
          rag_mode:       ragMode,
        };
        if (selectedModel !== "auto") {
          // local:model-name → send as model_hint="local" + local_model=name
          if (selectedModel.startsWith("local:")) {
            body.model       = "local";
            body.local_model = selectedModel.slice(6);
          } else {
            body.model = selectedModel;
          }
        }
        // ── KB scope (inline fallback for turn 1) ──────────────────────
        // Chats handed off from KnowledgeBase → Chat (KbChatPanel) carry
        // kbScopePending=true: the Chat row only exists server-side AFTER
        // this first /ask lazy-creates it, so the gateway's DB lookup for
        // product_id / spec_version / kb_doc_id finds nothing on turn 1
        // and retrieval falls back to unscoped (the entire KB). To make
        // the very first message respect the scope picked from the
        // drilldown, we send those fields inline whenever the local chat
        // object has them — the gateway treats them as a fallback that
        // only fires when the DB row has the column NULL.
        const _kbChat = (chats || []).find(c => c.id === activeChatId);
        if (_kbChat) {
          if (_kbChat.product_id)   body.product_id   = _kbChat.product_id;
          if (_kbChat.domain)       body.domain       = _kbChat.domain;
          if (_kbChat.spec_version) body.spec_version = _kbChat.spec_version;
          if (_kbChat.kb_doc_id)    body.kb_doc_id    = _kbChat.kb_doc_id;
        }
        const detectedTone = detectTone(question);
        if (detectedTone === "casual") setToneScore(s => Math.min(10, s + 1));
        const activeTone = (toneScore >= 2 || detectedTone === "casual") ? "casual" : toneMode;
        if (activeTone) body.tone = activeTone;
        const firstName = getFirstName(user);
        if (firstName !== "there") body.user_name = firstName;

        response = await authFetch(`${API}/kb/ask`, {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          body:    JSON.stringify(body),
          signal:  controller.signal,
        });
      }

      if (!response.ok) {
        if (response.status === 429) {
          // Parse budget-exceeded response — backend sends inhouse_ok: true
          // to signal in-house models are still available.
          let errBody = {};
          try { errBody = await response.json(); } catch { /* ignore */ }
          const isBudget = errBody.code === "BUDGET_EXCEEDED" || errBody.error === "budget_exceeded";
          if (isBudget) {
            setBudgetExhausted(true);
            const reason = errBody.detail || "Budget allocation exhausted";
            throw new Error(`BUDGET_EXCEEDED: ${reason}`);
          }
        }
        const errText = await response.text().catch(() => "");
        throw new Error(`Server error ${response.status}: ${errText.slice(0, 200)}`);
      }

      // Clear any previous budget warning on successful response
      if (budgetExhausted) setBudgetExhausted(false);

      if (!response.body) throw new Error("No response body");

      // Capture the server-assigned request_id so stopGeneration() can signal
      // the backend to cancel the in-progress generation cooperatively.
      requestIdMapRef.current[chatId] = response.headers.get("X-Request-ID") || null;

      // Read headers for token/cost display (fallback if not in body)
      let tokenUsage      = response.headers.get("X-Token-Usage");
      let costUsd         = response.headers.get("X-Cost-USD");
      let modelLabel      = null;
      let latency         = null;
      let inTok           = null;
      let outTok          = null;
      let tokensToday     = null;
      let maxTokensToday  = null;
      let requestsToday   = null;
      let maxRequestsToday = null;
      let confidence      = null;
      let chunkCount      = 0;
      let sources         = [];
      let metaRagMode     = null;
      let toolEvents      = [];
      let thinking        = "";
      let serverMessageId = null;
      // Phase 3 transparency — coverage tier decision from hybrid_retriever
      // (kn_rewrite.md §8x). Rendered as a small badge under the answer.
      let coverageTrace   = null;

      const reader  = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: false });
      let accumulated      = "";
      let sseBuffer        = "";
      let _clarifyTriggered = false;   // true when __clarify__ frame received — skip post-stream finalize

      // Backend sends text/event-stream: each event is `data: <json>\n\n`
      // Tokens arrive as {"t":"..."}, metadata arrives as {"__meta__":{...}}
      while (true) {
        // Check if aborted before reading next chunk
        if (controller.signal.aborted) {
          reader.cancel();  // Stop the stream
          break;
        }

        const { done, value } = await reader.read();
        if (done) break;
        if (!value) continue;

        sseBuffer += decoder.decode(value, { stream: true });

        // Split on SSE event boundary — keep any incomplete trailing event
        const parts = sseBuffer.split("\n\n");
        sseBuffer = parts.pop() ?? "";

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data: ")) continue;
          const data = line.slice(6);
          try {
            const obj = JSON.parse(data);
            if (obj.status !== undefined) {
              // Live backend status narration ("Thinking…", "Reading
              // sources…", "Generating response…") — drives the live status
              // line so the KB chat shows what the backend is actually doing.
              const _statusText = obj.status;
              updateMessages(
                  newMessages.map(msg =>
                      msg.id === assistantId ? { ...msg, statusLine: _statusText } : msg
                  )
              );
            } else if (obj.tool_event) {
              // Structured tool-call card. Accumulate on the message so the
              // UI can render expandable cards above the answer.
              toolEvents = toolEvents.concat([obj.tool_event]);
              updateMessages(
                  newMessages.map(msg =>
                      msg.id === assistantId ? { ...msg, content: stripMemoryTag(accumulated), toolEvents, spinnerStage: 2 } : msg
                  )
              );
            } else if (obj.tool_call !== undefined) {
              // Legacy string-form tool_call (back-compat). Skip if a structured
              // tool_event already rendered this turn.
              if (!toolEvents.length) {
                accumulated += `\n\`${obj.tool_call}\`\n`;
                updateMessages(
                    newMessages.map(msg =>
                        msg.id === assistantId ? { ...msg, content: stripMemoryTag(accumulated) } : msg
                    )
                );
              }
            } else if (obj.t !== undefined) {
              accumulated += obj.t;
              const _liveOutTok = Math.ceil(stripMemoryTag(accumulated).length / 4);
              updateMessages(
                  newMessages.map(msg =>
                      msg.id === assistantId
                        ? {
                            ...msg,
                            content:    stripMemoryTag(accumulated),
                            spinnerStage: 3,
                            liveOutTok: _liveOutTok,
                            // Clear statusLine once tokens start flowing so the
                            // pre-token spinner ("Thinking…") doesn't stay frozen
                            // on that label for the entire stream. The second
                            // AiNxtSpinner (msg.content truthy) uses its own
                            // hardcoded "Generating response" label instead.
                            statusLine: null,
                          }
                        : msg
                  )
              );
            } else if (obj.__meta__) {
              const meta = obj.__meta__;
              // The synthetic doc-edit placeholder meta (gateway emits
              // {model:"doc_generator", in_tok:0, out_tok:0, cost:0, latency:0,
              // source:"doc_edit"} alongside the "Applying your edit…" text on
              // a doc-revise turn) must NOT populate the message-level footer —
              // it would paint a bogus "doc_generator · ↑0 ↓0 tok" chip. The real
              // doc metadata is owned by the DocDownloadButton below. We still
              // process message_id / chat_id so persistence + reload behave.
              const _isDocEditMeta = meta.source === "doc_edit" || meta.model === "doc_generator";
              if (!_isDocEditMeta) {
                if (meta.tokens             != null) tokenUsage       = String(meta.tokens);
                if (meta.cost               != null) costUsd          = String(meta.cost);
                if (meta.model              != null) modelLabel       = meta.model;
                if (meta.latency            != null) latency          = meta.latency;
                if (meta.in_tok             != null) inTok            = meta.in_tok;
                if (meta.out_tok            != null) outTok           = meta.out_tok;
                if (meta.tokens_today       != null) tokensToday      = meta.tokens_today;
                if (meta.max_tokens_today   != null) maxTokensToday   = meta.max_tokens_today;
                if (meta.requests_today     != null) requestsToday    = meta.requests_today;
                if (meta.max_requests_today != null) maxRequestsToday = meta.max_requests_today;
                if (meta.confidence         != null) confidence       = meta.confidence;
                if (meta.chunk_count        != null) chunkCount       = meta.chunk_count;
                if (meta.sources            != null) sources          = meta.sources;
                if (meta.rag_mode           != null) metaRagMode      = meta.rag_mode;
                if (meta.thinking           != null) thinking         = meta.thinking;
                if (meta.coverage_trace     != null) coverageTrace    = meta.coverage_trace;
              }
              if (meta.message_id)                 serverMessageId  = meta.message_id;
              if (meta.chat_id) {
                setChats(prev => prev.map(c =>
                  c.id === meta.chat_id ? { ...c, fromBackend: false } : c
                ));
              }
            } else if (obj.__clarify__) {
              // ── KB Disambiguation: backend found 4+ docs, asks user to pick ──
              // Set flag so the post-stream finalize block is skipped — otherwise
              // it maps over the stale newMessages snapshot and overwrites the
              // picker card we're about to inject.
              _clarifyTriggered = true;
              const clarify = obj.__clarify__;
              const pickerCard = {
                id:          crypto.randomUUID(),
                role:        "doc_picker_card",
                question:    clarify.question,
                message:     clarify.message,
                candidates:  clarify.candidates || [],
                multiSelect: clarify.multi_select !== false,
                streaming:   false,
              };
              // Use functional update so we read live state, not the stale
              // newMessages snapshot captured at stream-start.
              updateMessages(prev =>
                prev
                  .filter(m => m.id !== assistantId)   // drop empty streaming placeholder
                  .concat([pickerCard])
              );
            }
          } catch { /* ignore malformed events */ }
        }
      }

      // Final strip of any <!--MEMORY:{...}--> footer
      const cleanAccumulated = stripMemoryTag(accumulated);

      // Skip finalize when disambiguation picker was shown — the picker card
      // was already injected into messages by the __clarify__ handler above.
      // Running this block would overwrite it with the stale newMessages snapshot.
      if (!_clarifyTriggered) updateMessages(
        newMessages.map(msg =>
          msg.id === assistantId
            ? {
                ...msg,
                content:          cleanAccumulated,
                streaming:        false,
                tokenUsage:       tokenUsage ? parseInt(tokenUsage) : null,
                costUsd:          costUsd    ? parseFloat(costUsd)  : null,
                modelLabel,
                latency,
                inTok,
                outTok,
                tokensToday,
                maxTokensToday,
                requestsToday,
                maxRequestsToday,
                confidence,
                chunkCount,
                sources,
                ragMode: metaRagMode,
                toolEvents,
                thinking,
                coverageTrace,
                // Replace the client-temp id with the persisted server id
                // (used by Continue / Edit / Regenerate endpoints).
                id: serverMessageId || msg.id,
              }
            : msg
        )
      );
      if (!_clarifyTriggered) fetchBudget();   // refresh remaining budget after reply

      // ── Artifact extraction (fire-and-forget) ───────────────────
      if (!_clarifyTriggered) try {
        const _aid = serverMessageId || assistantId;
        maybeExtractArtifacts(_aid, accumulated);
      } catch { /* ignore */ }

      // Auto follow-up chips removed from KbChat — the chips were stored on
      // msg.followups but never rendered in the message loop, and the
      // /chat/followups call was firing a redundant network request after
      // every KB answer with no visible benefit. (The Prompt Enhancer
      // feature that used to consume this data has since been removed
      // from KB Chat entirely.)

      // ── LLM-based auto-title (first turn only) ─────────────────
      // Replaces the 50-char slice. Runs once per chat when title is
      // still default ("New Chat" or "New KB Chat") and we have at least the
      // first Q+A. KB chats are created with title "New KB Chat" by the
      // eager POST /chats path (KbChatPanel) — must be treated the same as
      // "New Chat" here, otherwise the auto-title fires never runs for them
      // and the KB sidebar shows the placeholder title forever.
      try {
        const cur = chats.find(c => c.id === activeChatId);
        const _ttl = (cur?.title || "").trim();
        const stillDefault =
          !_ttl ||
          _ttl === "New Chat" ||
          _ttl === "New KB Chat" ||
          _ttl.length < 2;
        if (stillDefault && accumulated && accumulated.length > 20) {
          authFetch(`${API}/chats/${activeChatId}/auto-title`, { method: "POST" })
              .then(r => r.ok ? r.json() : null)
              .then(data => {
                if (data?.title) {
                  setChats(prev => prev.map(c =>
                      c.id === activeChatId ? { ...c, title: data.title } : c
                  ));
                }
              })
              .catch(() => {});
        }
      } catch { /* ignore */ }

    } catch (err) {
      if (err?.name === "AbortError") {
        // User clicked stop — mark current assistant message as done and cancelled
        // and set `continuable` so the Continue button renders.
        updateMessages(
          newMessages.map(msg =>
            msg.id === assistantId
              ? { ...msg, streaming: false, cancelled: true, continuable: true }
              : msg
          )
        );
      } else {
        // Replace placeholder assistant message with error in-place
        updateMessages(
          newMessages.map(msg =>
            msg.id === assistantId
              ? { ...msg, content: `Error: ${err?.message || "Failed to get response"}`, streaming: false }
              : msg
          )
        );
      }
    } finally {
      clearTimeout(streamTimeout);
      setLoading(false, chatId);
      delete abortMapRef.current[chatId];
      delete requestIdMapRef.current[chatId];
      // Revoke object URLs created for image previews to prevent memory leaks
      pendingImages.forEach(img => URL.revokeObjectURL(img.previewUrl));

      // ── KB scope back-patch ────────────────────────────────────────
      // Chats created from Knowledge Base → Chat (KbChatPanel) carry
      // kbScopePending=true because the Chat row only exists server-side
      // after this first /ask call lazy-creates it (gateway L953+). The
      // KbChatPanel did fire upfront PATCHes for /scope and /rag-mode but
      // both 404'd. Now that the row exists, retry once so subsequent
      // /ask calls read scope from DB and inject it into
      // user_ctx['scope_filter'], and rag_mode is persisted across reloads.
      const _chatForPatch = (chats || []).find(c => c.id === chatId);
      if (_chatForPatch?.kbScopePending) {
        try {
          await Promise.all([
            authFetch(`${API}/chats/${chatId}/scope`, {
              method:  "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                product_id:   _chatForPatch.product_id   ?? null,
                domain:       _chatForPatch.domain       ?? null,
                spec_version: _chatForPatch.spec_version ?? null,
                kb_doc_id:    _chatForPatch.kb_doc_id    ?? null,
              }),
            }),
            authFetch(`${API}/chats/${chatId}/rag-mode`, {
              method:  "PATCH",
              headers: { "Content-Type": "application/json" },
              body:    JSON.stringify({ rag_mode: _chatForPatch.rag_mode || "on" }),
            }),
          ]);
        } catch { /* best-effort */ }
        // Clear the marker locally so we don't retry on every subsequent
        // turn — the 350 ms debounced path in setChatScope owns it now.
        setChats(prev => prev.map(c =>
          c.id === chatId ? { ...c, kbScopePending: false } : c
        ));
      }
    }
  }

  function stopGeneration() {
    // Only stop the stream that belongs to the *currently visible* chat.
    const cid = activeChatId;

    // 1. Abort the browser-side fetch so the SSE reader stops immediately.
    abortMapRef.current[cid]?.abort();
    delete abortMapRef.current[cid];

    // 2. Tell the backend to stop the generator cooperatively so it doesn't
    //    keep burning tokens after the client disconnects.
    const rid = requestIdMapRef.current[cid];
    if (rid) {
      // Fire-and-forget — we don't await this; the UI should feel instant.
      authFetch(`${API}/chat/stop`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ request_id: rid }),
      }).catch(() => {/* ignore network errors on stop */});
      delete requestIdMapRef.current[cid];
    }

    setLoading(false);
    updateMessages(messages.map(msg => msg.streaming ? { ...msg, streaming: false, cancelled: true } : msg));
  }

  // ── UI ─────────────────────────────────────────────────────

  return (
    <div className="flex h-full bg-white overflow-hidden">

      {/* KbChat has no left sidebar — KbChatList renders the chat list
          in the parent KB tab. The right panel below is the only column. */}

      {/* ── ACTIVE CHAT PANEL ── */}
      <div className="flex flex-col flex-1 min-w-0 bg-white relative">

        {/* Chat header */}
        <div className="border-b border-gray-200 px-6 py-3 flex-shrink-0 flex items-center justify-between">
          {(chatsLoading || historyLoading) ? (
            <>
              <div className="h-5 w-48 bg-gray-200 rounded-md relative overflow-hidden">
                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/30 to-transparent animate-shimmer" />
              </div>
              <div className="flex items-center gap-2">
                <div className="h-8 w-20 bg-gray-200 rounded-lg relative overflow-hidden">
                  <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer" />
                </div>
                <div className="h-8 w-24 bg-gray-200 rounded-lg relative overflow-hidden">
                  <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer" />
                </div>
              </div>
            </>
          ) : (
            <>
              {/* KB chat header — resolved scope as a breadcrumb of
                  color-coded chips (Domain › Product › Version ›
                  Document). Palette matches KbScopeGraph LAYER colors
                  (indigo / amber / teal / rose). */}
              {(() => {
                const scope = {
                  domain:       activeChat?.domain,
                  product_name: activeChat?._kb_scope_labels?.productName
                                || activeChat?.product_name,
                  spec_version: activeChat?.spec_version,
                  kb_doc_name:  activeChat?._kb_scope_labels?.documentName,
                };
                const crumbs = [];
                if (scope.domain) {
                  crumbs.push({ key: "domain",  label: scope.domain,       Icon: Globe2,    chip: "bg-indigo-50 text-indigo-700 border-indigo-200" });
                }
                if (scope.product_name) {
                  crumbs.push({ key: "product", label: scope.product_name, Icon: Package,   chip: "bg-amber-50  text-amber-700  border-amber-200" });
                }
                if (scope.spec_version) {
                  crumbs.push({ key: "version", label: scope.spec_version, Icon: GitBranch, chip: "bg-teal-50   text-teal-700   border-teal-200" });
                }
                if (scope.kb_doc_name) {
                  crumbs.push({ key: "document", label: scope.kb_doc_name, Icon: FileText, chip: "bg-rose-50   text-rose-700   border-rose-200" });
                }
                if (crumbs.length === 0) {
                  crumbs.push({ key: "_empty", label: "Knowledge Base", Icon: Globe2, chip: "bg-gray-50 text-gray-500 border-gray-200" });
                }
                const fullPath = crumbs.map(c => c.label).join(" / ");
                return (
                  <nav
                    aria-label="KB chat scope"
                    title={fullPath}
                    className="flex items-center gap-1.5 min-w-0 overflow-hidden"
                  >
                    {crumbs.map((c, i) => (
                      <span key={c.key} className="flex items-center gap-1.5 min-w-0">
                        <ChevronRight
                          size={12}
                          className="text-gray-300 flex-shrink-0"
                          style={{ opacity: i > 0 ? 1 : 0, width: i > 0 ? 12 : 0 }}
                        />
                        <span
                          className={`flex items-center gap-1 px-2 py-0.5 rounded-full border text-[11px] font-medium max-w-[180px] truncate ${c.chip}`}
                        >
                          <c.Icon size={11} className="flex-shrink-0" />
                          <span className="truncate">{c.label}</span>
                        </span>
                      </span>
                    ))}
                  </nav>
                );
              })()}
              <div className="flex items-center gap-2">
                {/* Voice mode is kept in KbChat — KB chats benefit from
                    voice for hands-free knowledge lookup. Memory + Share
                    + image-gen are dropped (out of scope for KB). */}
                <button
                  onClick={() => setVoiceModeActive(true)}
                  title="Voice conversation mode"
                  className="cursor-pointer flex items-center gap-1.5 px-3 py-1.5 text-xs brand-grad hover:opacity-70 rounded-sm text-white transition-colors "
                >
                  <Headphones size={13} />
                  Voice
                </button>
                <button
                  onClick={handleExport}
                  disabled={!messages.length}
                  title="Export chat as Markdown"
                  className="cursor-pointer flex items-center gap-1.5 px-3 py-1.5 text-xs text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-sm transition-colors border border-gray-200 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Download size={13} />
                  Export
                </button>
              </div>
            </>
          )}
        </div>

        {openArtifactId && (
            <ArtifactsPanel
                artifactId={openArtifactId}
                chatId={activeChatId}
                onClose={() => setOpenArtifactId(null)}
            />
        )}

        {/* Messages */}
        <div
          ref={containerRef}
          onScroll={handleChatScroll}
          className="flex-1 overflow-y-auto overflow-x-hidden px-6 py-8 leading-5"
        >
          {/* ── Modern Skeleton: shown while initial chat list OR per-chat history is loading ── */}
          {(chatsLoading || historyLoading) && <ChatMessageSkeleton />}

          {/* KB welcome — single scope-summary line so the user knows
              what context they're chatting within. Uses the shared
              formatKbScopePath helper from utils/kbFormat.js so the
              slot-omitting logic stays in sync with KbChatPanel + the
              chat-title format. */}
          {!chatsLoading && !historyLoading && messages.length === 0 && (() => {
            const scope = {
              domain:       activeChat?.domain,
              product_name: activeChat?._kb_scope_labels?.productName
                            || activeChat?.product_name,
              spec_version: activeChat?.spec_version,
              kb_doc_name:  activeChat?._kb_scope_labels?.documentName,
            };
            const path = formatKbScopePath(scope);
            return (
              <div className="flex flex-col items-center justify-center h-full gap-3 px-6">
                <div className="w-14 h-14 rounded-full brand-grad-vivid flex items-center justify-center shadow-md">
                  <MessageSquare size={24} className="text-white" />
                </div>
                <div className="text-center">
                  <p className="text-sm text-gray-500">
                    You&apos;re chatting within scope:
                  </p>
                  <p className="text-sm font-medium text-gray-800 mt-1 break-words">
                    {path === "—" ? "Knowledge Base" : path}
                  </p>
                </div>
              </div>
            );
          })()}

          {(Array.isArray(messages) ? messages : []).map(msg => {
            const Wrapper = msg.streaming ? "div" : motion.div;
            const wrapperProps = msg.streaming
              ? {}
              : { initial: { opacity: 0, y: 8 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.2 } };

            // ── KB Disambiguation picker card ─────────────────────────
            if (msg.role === "doc_picker_card") {
              return (
                <Wrapper key={msg.id} {...wrapperProps}>
                  <DocPickerCard
                    message={msg.message}
                    candidates={msg.candidates}
                    multiSelect={msg.multiSelect}
                    onConfirm={(selectedDocIds) => {
                      // Remove the picker card, then re-send the original question
                      // scoped to the user-selected documents.
                      updateMessages(prev => prev.filter(m => m.id !== msg.id));
                      sendDisambigMessage(msg.question, selectedDocIds, { addUserBubble: false });
                    }}
                  />
                </Wrapper>
              );
            }

            // ── Compliance block card ─────────────────────────────────
            if (msg.role === "compliance_block") {
              const isComplianceViolation = msg.reasons && msg.reasons.length > 0;
              return (
                <Wrapper key={msg.id} {...wrapperProps} className="flex justify-start mb-4">
                  <div className="max-w-md rounded-xl border border-red-200 bg-red-50 px-4 py-3">
                    <div className="flex items-center gap-2 mb-2">
                      <ShieldOff size={14} className="text-red-500 flex-shrink-0" />
                      <span className="text-xs font-semibold text-red-700">
                        {isComplianceViolation ? "File blocked by compliance policy" : "File type not supported"}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5 mb-2">
                      <FileText size={11} className="text-red-400" />
                      <span className="text-xs text-red-600 font-medium truncate">{msg.filename}</span>
                    </div>
                    {isComplianceViolation && (
                      <>
                        <div className="text-[10px] text-red-400 mb-1.5">Sensitive data detected:</div>
                        <div className="flex flex-wrap gap-1">
                          {msg.reasons.map(r => (
                            <span key={r} className="bg-red-100 border border-red-200 text-red-700 text-[10px] font-semibold px-2 py-0.5 rounded">
                              {r}
                            </span>
                          ))}
                        </div>
                      </>
                    )}
                    <div className="text-[10px] text-red-400 mt-2">
                      {isComplianceViolation
                        ? "Remove the sensitive data from this file and upload again."
                        : (msg.block_reason || "This file format cannot be uploaded.")}
                    </div>
                  </div>
                </Wrapper>
              );
            }

            return (
              <Wrapper
                key={msg.id}
                {...wrapperProps}
                className={msg.role === "user" ? "flex justify-end mb-6" : "flex justify-start mb-6"}
              >
                <div className={msg.role === "user"
                  ? "group/usermsg relative bg-gray-100 px-4 py-3 rounded-md text-sm max-w-4xl break-words min-w-0"
                  : "px-4 py-3 rounded-md text-sm max-w-5xl break-words min-w-0 overflow-hidden"
                }>
                  {/* No PPT renderer in KbChat — KB chats can't produce
                      presentation messages. */}

                  {/* ── Reasoning / extended-thinking panel ─────────────── */}
                  {msg.role === "assistant" && msg.thinking && msg.thinking.trim().length > 0 && (
                      <details className="mb-2 text-xs border border-purple-100 bg-purple-50/40 rounded-md group">
                        <summary className="px-2 py-1.5 cursor-pointer select-none flex items-center gap-2 text-purple-700 hover:bg-purple-50">
                          <span className="inline-block w-1.5 h-1.5 rounded-full bg-purple-500" />
                          <span className="font-medium">Reasoning</span>
                          <span className="ml-auto text-purple-400 group-open:rotate-90 transition">›</span>
                        </summary>
                        <div className="px-3 py-2 border-t border-purple-100 whitespace-pre-wrap text-purple-900/80 font-mono text-[11px] max-h-64 overflow-auto">
                          {msg.thinking}
                        </div>
                      </details>
                  )}

                  {/* ── Structured tool-call cards (above the answer text) ── */}
                  {msg.role === "assistant" && Array.isArray(msg.toolEvents) && msg.toolEvents.length > 0 && (
                      <div className="mb-2 space-y-1">
                        {msg.toolEvents.map((te, i) => (
                            <details key={i} className="text-xs border border-gray-200 rounded-md group">
                              <summary className="px-2 py-1.5 cursor-pointer select-none flex items-center gap-2 text-gray-600 hover:bg-gray-50">
                            <span className={
                              te.status === "success"
                                  ? "inline-block w-1.5 h-1.5 rounded-full bg-green-500"
                                  : te.status === "error"
                                      ? "inline-block w-1.5 h-1.5 rounded-full bg-red-500"
                                      : "inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse"
                            } />
                                <span className="font-medium">{te.name || "tool"}</span>
                                <span className="text-gray-400">— {te.status || "running"}</span>
                                <span className="ml-auto text-gray-400 group-open:rotate-90 transition">›</span>
                              </summary>
                              <div className="px-3 py-2 border-t border-gray-100 space-y-1 bg-gray-50/50">
                                {te.args && Object.keys(te.args).length > 0 && (
                                    <div>
                                      <div className="text-[10px] text-gray-400 uppercase">Input</div>
                                      <pre className="text-[11px] whitespace-pre-wrap text-gray-700 max-h-32 overflow-auto">
                                  {typeof te.args === "string" ? te.args : JSON.stringify(te.args, null, 2)}
                                </pre>
                                    </div>
                                )}
                                {te.output && (
                                    <div>
                                      <div className="text-[10px] text-gray-400 uppercase">Output</div>
                                      <pre className="text-[11px] whitespace-pre-wrap text-gray-700 max-h-40 overflow-auto">
                                  {typeof te.output === "string" ? te.output : JSON.stringify(te.output, null, 2)}
                                </pre>
                                    </div>
                                )}
                              </div>
                            </details>
                        ))}
                      </div>
                  )}

                  {/* Regular Assistant Messages */}
                  {/* videoUrl points at the auth-gated /chat/video/{id} route —
                      browser cookies travel with the same-origin <video> fetch. */}
                  {msg.role === "assistant" && msg.videoUrl && (
                    <div className="my-3">
                      <div className="flex items-center gap-2 mb-2 text-xs font-medium text-gray-600">
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-600 border border-indigo-100">
                          🎬 Video preview
                        </span>
                        {msg.meta?.duration && (
                          <span className="text-gray-400">{msg.meta.duration}s</span>
                        )}
                        {msg.meta?.model && (
                          <span className="text-gray-400">· {msg.meta.model}</span>
                        )}
                      </div>
                      <div className="relative group">
                        <video
                          controls
                          preload="metadata"
                          playsInline
                          className="w-full max-w-2xl rounded-lg border border-gray-200 shadow-sm bg-black"
                        >
                          <source src={msg.videoUrl} type={msg.videoMime || "video/mp4"} />
                          Your browser does not support the video tag.
                        </video>
                        <button
                          onClick={async () => {
                            try {
                              const r = await authFetch(msg.videoUrl);
                              const blob = await r.blob();
                              const blobUrl = URL.createObjectURL(blob);
                              const a = document.createElement("a");
                              a.href = blobUrl;
                              const ext = (msg.videoMime || "video/mp4").split("/")[1] || "mp4";
                              a.download = `generated-video.${ext}`;
                              document.body.appendChild(a);
                              a.click();
                              document.body.removeChild(a);
                              URL.revokeObjectURL(blobUrl);
                            } catch {
                              window.open(msg.videoUrl, "_blank");
                            }
                          }}
                          className="absolute top-2 right-2 p-2 rounded-lg bg-black/60 text-white
                                     opacity-0 group-hover:opacity-100 hover:bg-black/80
                                     transition-all duration-200 backdrop-blur-sm shadow-md cursor-pointer"
                          title="Download video"
                          type="button"
                        >
                          <Download size={16} />
                        </button>
                      </div>
                    </div>
                  )}

                  {msg.role === "assistant" && !msg.pptType && (
                    (() => {
                      const _cleanContent = stripMemoryTag(msg.content || "");
                      const _body = (
                        <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeHighlight, rehypeKatex]} urlTransform={mdUrlTransform} components={mdComponents}>
                          {_cleanContent}
                        </ReactMarkdown>
                      );
                      return (
                        <ExpandableMessageBody content={_cleanContent} isStreaming={!!msg.streaming}>
                          {_body}
                        </ExpandableMessageBody>
                      );
                    })()
                  )}

                  {/* ── Floating action buttons (user messages only) ── */}
                  {msg.role === "user" && !msg.streaming && (
                    <div className="absolute -bottom-7 right-0 flex items-center gap-0.5 opacity-0 group-hover/usermsg:opacity-100 transition-opacity duration-150">
                      <button
                        onClick={() => startEditUserMsg(msg.id, stripSystemPrefix(msg.content))}
                        title="Edit message"
                        className="p-1.5 rounded cursor-pointer text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        onClick={() => handleCopy(msg.id, stripSystemPrefix(msg.content))}
                        title="Copy message"
                        className="p-1.5 rounded cursor-pointer text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors"
                      >
                        {copiedId === msg.id ? <Check size={13} className="text-green-500" /> : <Copy size={13} />}
                      </button>
                    </div>
                  )}

                  {/* User Messages */}
                  {msg.role === "user" && !msg.pptType && (
                    <div>
                      {msg.imageUrls?.length > 0 && (
                        <div className="mb-2 flex flex-wrap gap-2">
                          {msg.imageUrls.map((url, i) => (
                            <img
                              key={i}
                              src={url}
                              alt={`Attached image ${i + 1}`}
                              className="max-h-48 max-w-xs rounded-md object-contain border border-gray-200"
                            />
                          ))}
                        </div>
                      )}
                      {/* Strip the "📎 file1, file2" line from displayed text when
                          attachment metadata is present — the chips below replace it */}
                      <div className="whitespace-pre-wrap">{
                        msg.attachments?.length > 0
                          ? stripSystemPrefix(msg.content)?.replace(/\n\n📎\s*.+$/, "").trimEnd()
                          : stripSystemPrefix(msg.content)
                      }</div>
                      {/* Attachment chips: cache-aware preview button or expired notice */}
                      {msg.attachments?.length > 0 && (
                        <div className="flex flex-wrap gap-1.5 mt-2">
                          {msg.attachments.map(a => (
                            <AttachmentChip
                              key={a.id}
                              attachment={a}
                              onPreview={() => setPreviewAttachment({ id: a.id, fileName: a.file_name, fileType: a.file_type, parsedText: a.parsed_text || "" })}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Quick Reply Chips (for PPT conversation) */}
                  {msg.quickReplies && msg.quickReplies.length > 0 && !msg.quickRepliesUsed && (
                    <div className="flex flex-wrap gap-1.5 mt-2">
                      {msg.quickReplies.map(reply => (
                        <button
                          key={reply}
                          onClick={() => {
                            // Mark chips as used
                            updateMessages(messages.map(m =>
                              m.id === msg.id ? { ...m, quickRepliesUsed: true } : m
                            ));
                            
                            // Add user message to chat
                            const userMsg = {
                              id: crypto.randomUUID(),
                              role: "user",
                              content: reply,
                              streaming: false,
                              timestamp: Date.now(),
                            };
                            setChats(prev => prev.map(chat => 
                              chat.id === activeChatId 
                                ? { ...chat, messages: [...chat.messages, userMsg], updatedAt: Date.now() }
                                : chat
                            ));
                            
                            setTimeout(() => {
                              containerRef.current?.scrollTo({ top: containerRef.current.scrollHeight, behavior: "smooth" });
                            }, 100);
                            
                            // Process as PPT conversation reply
                            setTimeout(() => {
                              if (pptConversation.isActive()) {
                                pptConversation.processUserReply(reply);
                              } else {
                                // If not in PPT conversation, treat as regular message
                                setInput(reply);
                                sendMessage();
                              }
                            }, 50);
                          }}
                          className="px-3 py-1 text-xs bg-indigo-50 hover:bg-indigo-100 text-indigo-700 rounded-full border border-indigo-200 transition"
                        >
                          {reply}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* Grayed-out chips after selection */}
                  {msg.quickReplies && msg.quickRepliesUsed && (
                    <div className="flex flex-wrap gap-1.5 mt-2 opacity-40">
                      {msg.quickReplies.map(reply => (
                        <span key={reply} className="px-3 py-1 text-xs bg-gray-100 text-gray-400 rounded-full border border-gray-200">
                          {reply}
                        </span>
                      ))}
                    </div>
                  )}

                  {msg.streaming && !msg.content && (
                    msg.videoStage ? (
                      <AiNxtSpinner
                        steps={[
                          { id: 1, label: "Understanding prompt" },
                          { id: 2, label: "Submitting to Veo" },
                          { id: 3, label: "Rendering video frames" },
                          { id: 4, label: "Finalizing MP4" },
                        ]}
                        stage={msg.videoStage === "submitting" ? 1 : msg.videoStage === "rendering" ? 2 : 3}
                      />
                    ) : msg.docStage ? (
                      <AiNxtSpinner
                        steps={[
                          { id: 1, label: "Understanding" },
                          { id: 2, label: `Detecting format${msg.docFormat ? ` (${msg.docFormat.toUpperCase()})` : ""}` },
                          { id: 3, label: "Drafting document" },
                          { id: 4, label: "Generating file" },
                        ]}
                        stage={msg.docStage === "classify" ? 0 : msg.docStage === "submitting" ? 1 : 2}
                      />
                    ) : (
                      <AiNxtSpinner
                        stage={msg.spinnerStage ?? null}
                        label={msg.statusLine ?? null}
                        outTok={msg.liveOutTok ?? null}
                        startAt={msg.streamStartAt ?? null}
                      />
                    )
                  )}
                  {msg.streaming && msg.content && (
                    <AiNxtSpinner
                      label="Generating response"
                      outTok={msg.liveOutTok ?? null}
                      startAt={msg.streamStartAt ?? null}
                    />
                  )}

                  {/* ── Confidence badge (assistant only, not streaming, confidence > 0) ── */}
                  {msg.role === "assistant" && !msg.streaming && msg.confidence !== undefined && msg.confidence > 0 && (
                    <div className="flex items-center gap-2 mt-2 text-xs">
                      <div className={`flex items-center gap-1 px-2 py-0.5 rounded-full font-medium ${
                        msg.confidence >= 0.85
                          ? 'bg-green-500/20 text-green-400'
                          : msg.confidence >= 0.5
                          ? 'bg-yellow-500/20 text-yellow-400'
                          : 'bg-red-500/20 text-red-400'
                      }`}>
                        <span>{msg.confidence >= 0.85 ? '●' : msg.confidence >= 0.5 ? '◐' : '○'}</span>
                        <span>{Math.round(msg.confidence * 100)}% confidence</span>
                        {msg.confidence < 0.5 && <span className="ml-1">(verify manually)</span>}
                      </div>
                      {msg.chunkCount > 0 && (
                        <span className="text-gray-500">{msg.chunkCount} source{msg.chunkCount !== 1 ? 's' : ''}</span>
                      )}
                    </div>
                  )}

                  {/* ── Artifact chips (Open in Canvas) ──────────────────── */}
                  {msg.role === "assistant" && !msg.streaming && Array.isArray(msg.artifacts) && msg.artifacts.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {msg.artifacts.map(a => (
                            <button
                                key={a.id}
                                type="button"
                                onClick={() => setOpenArtifactId(a.id)}
                                className="text-xs px-2.5 py-1 rounded-md border border-purple-200 text-purple-600 bg-purple-50 hover:bg-purple-100 transition"
                                title="Open in Canvas"
                            >
                              ◳ {a.title || a.type}
                            </button>
                        ))}
                      </div>
                  )}

                  {/* ── KB grounding indicator + expandable Sources panel ─────── */}
                  {msg.role === "assistant" && !msg.streaming && Array.isArray(msg.sources) && msg.sources.length > 0 && (() => {
                    // Determine whether all sources come from the same document.
                    // When true: show the doc name once as a header and suppress
                    // it from each individual chunk row.
                    // When false (multi-doc): show the doc name per row so the
                    // user can see which document each chunk came from.
                    const _srcDocIds = msg.sources.map(s => s.doc_id || s.title || s.file_path || "");
                    const _uniqueDocs = [...new Set(_srcDocIds.filter(Boolean))];
                    const _isSingleDoc = _uniqueDocs.length <= 1;
                    const _singleDocName = _isSingleDoc
                      ? (msg.sources[0]?.doc_name || msg.sources[0]?.title || msg.sources[0]?.file_path || "")
                      : null;
                    return (
                      <details className="mt-2 text-xs group">
                        <summary className="flex items-center gap-2 cursor-pointer select-none text-gray-500 hover:text-gray-700">
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-500/15 text-blue-600 font-medium">
                            <BookOpen size={11} />
                            KB
                          </span>
                          <span>Sources ({msg.sources.length})</span>
                          <span className="text-gray-400 group-open:rotate-90 transition">›</span>
                        </summary>
                        <div className="mt-2 space-y-2 pl-2 border-l border-gray-200">
                          {/* Single-doc: show the document name once as a header */}
                          {_isSingleDoc && _singleDocName && (
                            <div className="font-medium text-gray-800 truncate mb-1" title={_singleDocName}>
                              {_singleDocName}
                            </div>
                          )}
                          {msg.sources.map((s, i) => (
                            <div key={i} className="text-gray-700">
                              {/* Multi-doc: show doc name per row so the user knows which doc each chunk is from */}
                              {!_isSingleDoc && (
                                <div className="font-medium text-gray-800 truncate flex items-center gap-2" title={s.file_path || s.title}>
                                  <span className="truncate">
                                    {s.doc_name || s.title || s.file_path || "(untitled)"}
                                  </span>
                                  {/* ── Part U11 (docx §8) — typed source badge ── */}
                                  {s.source_type && (
                                    <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-medium bg-indigo-50 text-indigo-700 border border-indigo-100 shrink-0">
                                      {s.source_type}
                                    </span>
                                  )}
                                  {s.score != null && (
                                    <span className="ml-auto text-[10px] text-gray-400 shrink-0">
                                      {Math.round((s.score || 0) * 100)}%
                                    </span>
                                  )}
                                </div>
                              )}
                              <div className="text-gray-500 line-clamp-3">
                                {s.snippet || ""}
                              </div>
                              {/* ── Part U11 — section + page footer (no "Open original" link) ── */}
                              {(s.section_name || s.page_number != null || s.namespace) && (
                                <div className="text-[10px] text-gray-400 mt-0.5 flex items-center gap-2 flex-wrap">
                                  {s.section_name && (
                                    <span>📍 {s.section_name}</span>
                                  )}
                                  {s.page_number != null && (
                                    <span>· page {s.page_number}</span>
                                  )}
                                  {s.namespace && (
                                    <span>· {s.namespace}</span>
                                  )}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </details>
                    );
                  })()}

                  {/* ── Feedback + Copy + TTS action bar (assistant only, not streaming, not cancelled, has content, NOT PPT message) ── */}
                  {msg.role === "assistant" && !msg.streaming && !msg.cancelled && (msg.content?.trim() || '') && !msg.pptType && !msg.pptConversation && (
                    <div className="flex items-center gap-1 mt-2 pt-1">
                      <button
                        onClick={() => handleFeedback(msg.id, 1)}
                        title="Helpful"
                        className={`p-1 cursor-pointer rounded transition-colors ${
                          feedbackMap[msg.id] === 1
                            ? "text-green-500"
                            : "text-gray-600 hover:text-green-500"
                        }`}
                      >
                        <ThumbsUp size={14} />
                      </button>
                      <button
                        onClick={() => handleFeedback(msg.id, -1)}
                        title="Not helpful"
                        className={`p-1 rounded cursor-pointer transition-colors ${
                          feedbackMap[msg.id] === -1
                            ? "text-red-400"
                            : "text-gray-600 hover:text-red-400"
                        }`}
                      >
                        <ThumbsDown size={14} />
                      </button>
                      <button
                        onClick={() => handleCopy(msg.id, stripMemoryTag(msg.content))}
                        title="Copy response"
                        className="p-1 rounded cursor-pointer text-gray-600 hover:text-gray-600 transition-colors ml-0.5"
                      >
                        {copiedId === msg.id ? <Check size={14} className="text-green-500" /> : <Copy size={14} />}
                      </button>
                      <button
                        onClick={() => handleShare(stripMemoryTag(msg.content))}
                        title="Share response"
                        className="p-1 rounded cursor-pointer text-gray-600 hover:text-blue-500 transition-colors ml-0.5"
                      >
                        <Share2 size={14} />
                      </button>
                         <a
                        ref={linkRef}
                        style={{ display: "none" }}
                        rel="noopener noreferrer"
                       />
                      <button
                        onClick={() => handleTeamsShare(stripMemoryTag(msg.content))}
                        title="Share to Teams"
                        className="p-1 rounded cursor-pointer text-gray-500 hover:text-[#6264A7] hover:bg-[#f3f2f1] transition-all ml-0.5"
                      >
                        <Users size={16} strokeWidth={2} />
                      </button>
                      <button
                        onClick={() => handleSpeak(msg.id, stripMemoryTag(msg.content))}
                        title={speakingId === msg.id ? "Stop reading" : "Read aloud"}
                        className={`p-1 rounded cursor-pointer transition-colors ml-0.5 ${
                          speakingId === msg.id
                            ? "text-blue-500 animate-pulse"
                            : "text-gray-600 hover:text-blue-500"
                        }`}
                      >
                        {speakingId === msg.id ? <VolumeX size={14} /> : <Volume2 size={14} />}
                      </button>
                    </div>
                  )}

                  {/* ── Cancelled indicator (assistant only, cancelled) ── */}
                  {msg.role === "assistant" && msg.cancelled && (
                    <div className="flex items-center gap-1.5 mt-2 rounded-sm text-sm text-gray-600">
                      <CircleX size={14} />
                      <span>OK, I've stopped generating the response.</span>
                    </div>
                  )}

                  {/* Phase 3 coverage badge — kn_rewrite.md §8x.
                      Shown only when the gateway emitted coverage_trace
                      (i.e. the chat had a scope set OR KB_RETRIEVAL_SCOPE
                      forced a mode). Tooltip carries the gate reason + signals
                      + the active retrieval_scope mode so the user can audit
                      which path the answer came from. Badge text is set
                      server-side (e.g. "Both (Fast+Coverage reranked): …"
                      / "Full-file: …" / "RAG only (KB_RETRIEVAL_SCOPE=rag)"). */}
                  {msg.role === "assistant" && msg.coverageTrace && (
                    <div
                      className={
                        "mt-2 inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[10px] font-medium border " +
                        (msg.coverageTrace.escalate
                          ? "border-amber-200 bg-amber-50 text-amber-800"
                          : "border-emerald-200 bg-emerald-50 text-emerald-800")
                      }
                      title={
                        `retrieval_scope=${msg.coverageTrace.retrieval_scope || "auto"}` +
                        ` mode=${msg.coverageTrace.mode || "fast"}` +
                        ` sufficiency=${(msg.coverageTrace.sufficiency ?? 0).toFixed(2)}` +
                        ` reason=${msg.coverageTrace.reason || "—"}` +
                        (msg.coverageTrace.sections_examined != null
                          ? ` examined=${msg.coverageTrace.sections_examined}`
                          : "") +
                        (msg.coverageTrace.sections_included != null
                          ? ` included=${msg.coverageTrace.sections_included}`
                          : "") +
                        (msg.coverageTrace.merged_top_k != null
                          ? ` merged_top_k=${msg.coverageTrace.merged_top_k}`
                          : "") +
                        (msg.coverageTrace.fast_kept != null
                          ? ` fast_kept=${msg.coverageTrace.fast_kept}`
                          : "") +
                        (msg.coverageTrace.cov_kept != null
                          ? ` cov_kept=${msg.coverageTrace.cov_kept}`
                          : "")
                      }
                    >
                      <BookOpen size={10} />
                      <span>{msg.coverageTrace.badge || (msg.coverageTrace.escalate ? "Coverage tier" : "Fast tier")}</span>
                    </div>
                  )}

                  <MessageMeta
                    msg={msg}
                    budget={budget}
                    isLast={msg.id === [...messages].filter(m => m.role === "assistant").pop()?.id}
                  />
                </div>
              </Wrapper>
            );
          })}
        </div>

        {/* ── Feedback Modal (thumbs-down) ── */ }
        { feedbackModal.open && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center"
            style={ { background: "rgba(0,0,0,0.3)" } }
            onClick={ e => { e.stopPropagation(); } }
          >
            <div style={ { background: "#fff", borderRadius: 12, width: "100%", maxWidth: 420, margin: "0 16px", boxShadow: "0 8px 32px rgba(0,0,0,0.15)", overflow: "hidden" } }>
              {/* Header */ }
              <div style={ { padding: "16px 20px", borderBottom: "1px solid #f3f4f6", display: "flex", alignItems: "center", justifyContent: "space-between" } }>
                <span style={ { fontWeight: 600, fontSize: 14, color: "#111827" } }>Response Feedback</span>
                <button onClick={ () => setFeedbackModal( { open: false, msgId: null } ) } style={ { background: "none", border: "none", cursor: "pointer", color: "#9ca3af", fontSize: 18, lineHeight: 1 } }>×</button>
              </div>

              <div style={ { padding: "16px 20px", display: "flex", flexDirection: "column", gap: 12 } }>
                {/* Category buttons */ }
                <div style={ { fontSize: 11, color: "#6b7280", fontWeight: 500, marginBottom: 2 } }>WHAT WENT WRONG?</div>
                <div style={ { display: "flex", flexDirection: "column", gap: 6 } }>
                  { FEEDBACK_ISSUES.map( issue => (
                    <button
                      key={ issue.label }
                      onClick={ () => { setFeedbackIssue( feedbackIssue === issue.label ? "" : issue.label ); setFeedbackSubIssue( "" ); } }
                      className={ `${ feedbackIssue === issue.label ? 'brand-grad hover:opacity-70 text-white' : 'hover:!bg-gray-50 text-gray-700' } border border-gray-100 outline-none` }
                      style={ {
                        display: "flex", alignItems: "center", justifyContent: "space-between",
                        padding: "9px 12px", borderRadius: 8, cursor: "pointer", textAlign: "left",
                        fontSize: 13, fontWeight: 500, transition: "all 0.15s",
                      } }
                    >
                      { issue.label }
                      { issue.sub.length > 0 && <span style={ { fontSize: 11, opacity: 0.6 } }>▾</span> }
                    </button>
                  ) ) }
                </div>

                {/* Sub-issue chips (shown when category has sub-items) */ }
                { feedbackIssue && FEEDBACK_ISSUES.find( i => i.label === feedbackIssue )?.sub?.length > 0 && (
                  <div style={ { display: "flex", flexWrap: "wrap", gap: 6, paddingTop: 4 } }>
                    { FEEDBACK_ISSUES.find( i => i.label === feedbackIssue ).sub.map( sub => (
                      <button
                        key={ sub }
                        onClick={ () => setFeedbackSubIssue( feedbackSubIssue === sub ? "" : sub ) }
                        className={ `${ feedbackSubIssue === sub ? 'brand-grad hover:opacity-70 text-white' : 'hover:bg-gray-50 text-gray-500 border border-gray-100' } outline-none` }
                        style={ {
                          padding: "5px 10px", borderRadius: 20, fontSize: 11, cursor: "pointer",
                        } }
                      >
                        { sub }
                      </button>
                    ) ) }
                  </div>
                ) }

                {/* Comment box */ }
                <textarea
                  value={ feedbackComment }
                  onChange={ e => setFeedbackComment( e.target.value ) }
                  placeholder="Additional comments (optional)"
                  rows={ 3 }
                  autoFocus
                  className="w-full border border-gray-200 rounded-lg text-gray-900 text-sm px-3 py-2.5 resize-none box-border font-inherit focus:border-indigo-300 focus:outline-none"
                  style={ {
                    borderWidth: "1.5px",
                  } }
                />

                <button
                  onClick={ submitFeedback }
                  disabled={ !feedbackIssue }
                  className={ `${ feedbackIssue ? 'brand-grad hover:opacity-70 text-white' : 'border border-gray-100 !text-gray-500 hover:!bg-gray-50' } outline-none` }
                  style={ {
                    padding: "10px",
                    borderRadius: 8,
                    fontSize: 13,
                    fontWeight: 600,
                    cursor: feedbackIssue ? "pointer" : "default",
                  } }
                >
                  Submit Feedback
                </button>
              </div>
            </div>
          </div>
        ) }

        {/* Voice Mode Overlay — KbChat keeps voice mode for hands-free
            knowledge lookup. */}
        {voiceModeActive && (
          <VoiceMode
            onClose={() => setVoiceModeActive(false)}
            onSendVoice={sendMessageForVoice}
            micLang={micLang}
            ttsApi={async (text) => {
              const r = await authFetch(`${API}/voice/tts`, {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ text, voice: "nova", speed: 0.92 }),
              });
              if (!r.ok) throw new Error("TTS failed");
              return r.blob();
            }}
          />
        )}

        {/* Combined Input Box — drag-drop / file-attach / image-attach
            are all removed in KbChat. KB chats are answered from the
            indexed corpus, not ad-hoc uploads. */}
        <div
          className="border-t border-gray-100 bg-white px-4 pb-4 pt-3 flex-shrink-0 relative transition-all"
        >
          {/* No file/image inputs, no drag overlay, no image previews. */}

          {/* Upload progress bar — visible only while uploading */}
          {uploading && (
            <div className="mb-2 flex items-center gap-2">
              <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
                <div
                  className="h-full bg-blue-500 rounded-full transition-all duration-200"
                  style={{ width: `${uploadProgress}%` }}
                />
              </div>
              <span className="text-xs text-blue-500 font-medium w-9 text-right shrink-0">
                {uploadProgress}%
              </span>
                            <button
                onClick={cancelUpload}
                title="Cancel upload"
                className="shrink-0 flex items-center justify-center w-6 h-6 text-red-500 rounded-full hover:bg-red-50 transition cursor-pointer"
              >
                <CircleX size={16} />
              </button>
            </div>
          )}

          {/* Budget exhausted banner — shown above input box when cloud budget is spent */}
          {budgetExhausted && (
            <div className="mb-2 px-3 py-2.5 bg-amber-50 border border-amber-300 rounded-xl flex items-start gap-2.5 text-sm">
              <span className="text-amber-500 mt-0.5 shrink-0">⚠️</span>
              <div className="flex-1 min-w-0">
                <p className="font-medium text-amber-800">Cloud API budget exhausted</p>
                <p className="text-xs text-amber-700 mt-0.5">
                  Your cloud API quota is used up. In-house models are still available — select one from the model picker or ask your admin to increase your budget.
                </p>
              </div>
              <button
                onClick={() => setBudgetExhausted(false)}
                className="shrink-0 px-2.5 py-1 bg-amber-600 text-white text-xs rounded-lg hover:bg-amber-700 whitespace-nowrap"
              >
                Dismiss
              </button>
            </div>
          )}

          {fileLimitError && (
            <div className="mb-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              <span className="mt-0.5 shrink-0">⚠️</span>
              <span className="flex-1">
                The number of files you are trying to add exceeds the maximum limit. Ainxt currently supports adding up to 3 files at a time.
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

          {/* Invalid file type error banner (drag-and-drop) */}
          {dropError && (
            <div className="mb-2 flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
              <div className="flex-shrink-0 mt-0.5">
                <X size={16} className="text-red-500" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-red-700">Invalid File Type</p>
                <p className="text-xs text-red-600 mt-0.5">{dropError}</p>
              </div>
              <button
                type="button"
                onClick={() => setDropError(null)}
                className="flex-shrink-0 px-3 py-1 text-xs font-medium text-red-600 bg-white border border-red-300 rounded hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-400 cursor-pointer transition-colors"
              >
                Close
              </button>
            </div>
          )}
          

          <div className={`border rounded-xl bg-gray-50 transition-colors shadow-md ${
            inputDisabled ? "border-gray-200" : "border-gray-300 focus-within:border-indigo-300 focus-within:bg-white"
          }`}>

            {/* Attachment chips — inside the box */}
            {attachments.length > 0 && (
              <div className="px-3 pt-2.5 flex flex-wrap gap-1.5">
                {attachments.map(a => (
                  <div
                    key={a.id}
                    className="group relative flex items-center gap-1 bg-blue-50 border border-blue-200 text-blue-700 text-xs px-2 py-0.5 rounded-full"
                  >
                    <FileText size={10} />
                    <span className="max-w-[110px] truncate">{a.file_name}</span>
                    <span className="text-blue-400">({Math.round((a.file_size || 0) / 1024)}KB)</span>
                    {a.parsed_preview && (
                      <div className="absolute bottom-full left-0 mb-1 hidden group-hover:block z-50 bg-white border border-gray-200 rounded-md shadow-lg p-2 w-64 text-xs text-gray-600 pointer-events-none">
                        <div className="font-medium text-gray-800 mb-1">{a.file_name}</div>
                        <div className="text-gray-500 leading-relaxed">{a.parsed_preview}</div>
                      </div>
                    )}
                    <button
                      onClick={() => removeAttachment(a.id)}
                      className="text-blue-400 hover:text-blue-600 ml-0.5 cursor-pointer"
                    >
                      <X size={10} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* "/" prompt-template menu */}
            {tplMenuOpen && templates.length > 0 && (
                <div className="absolute bottom-full mb-1 left-2 right-2 bg-white border border-gray-200 rounded-lg shadow-xl max-h-56 overflow-y-auto z-20">
                  <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
                    Saved prompts {tplFilter && `· "${tplFilter}"`}
                  </div>
                  {templates
                      .filter(t => !tplFilter || (t.name || "").toLowerCase().includes(tplFilter))
                      .slice(0, 8)
                      .map(t => (
                          <button
                              key={t.id}
                              type="button"
                              onClick={() => applyTemplate(t)}
                              className="w-full text-left px-3 py-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0"
                          >
                            <div className="text-xs font-medium text-gray-800 truncate">
                              {t.name}
                              {t.scope === "org" && (
                                  <span className="ml-1 text-[10px] text-gray-400">org</span>
                              )}
                            </div>
                            <div className="text-[11px] text-gray-500 line-clamp-1">
                              {(t.body || "").slice(0, 120)}
                            </div>
                          </button>
                      ))
                  }
                  {templates.filter(t => !tplFilter || (t.name || "").toLowerCase().includes(tplFilter)).length === 0 && (
                      <div className="px-3 py-2 text-xs text-gray-400">No matching templates.</div>
                  )}
                </div>
            )}

            {/* Edit-mode warning banner — shown when editing a previous prompt */}
            {editingMsgId && editDiscardCount > 0 && (
              <div className="flex items-center gap-2 px-3 py-2 bg-amber-50 border-b border-amber-200 rounded-t-xl">
                <Pencil size={13} className="text-amber-500 shrink-0" />
                <span className="text-xs text-amber-700 flex-1">
                  Editing earlier message — <strong>{editDiscardCount} message{editDiscardCount > 1 ? "s" : ""}</strong> after this will be removed on submit
                </span>
                <button
                  onClick={cancelEditMsg}
                  title="Cancel edit"
                  className="shrink-0 p-0.5 text-amber-400 hover:text-amber-600 rounded hover:bg-amber-100 transition cursor-pointer"
                >
                  <X size={14} />
                </button>
              </div>
            )}

            {/* Textarea */}
            <textarea
                id="chat-input"
              ref={textareaRef}
              value={input}
              disabled={inputDisabled}
              onChange={e => {
                handleInputChange(e);
                // Let the useEffect handle height adjustment, but for immediate response
                requestAnimationFrame(() => adjustTextareaHeight(e.target));
              }}
              onKeyDown={e => {
                // Handle Escape for template menu and edit mode
                if (e.key === "Escape") {
                  if (editingMsgId) {
                    cancelEditMsg();
                    return;
                  }
                  if (tplMenuOpen) {
                    setTplMenu(false);
                    return;
                  }
                }

                // Plain Enter (no Shift) sends the message — slash/at
                // command autocomplete navigation has been removed.
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  if (!uploading) sendMessage();
                  return;
                }
              }}
              onPaste={handlePaste}
              placeholder="Ask anything… (Shift+Enter for new line, paste image to attach)"
              rows={1}
              className="w-full resize-none bg-transparent px-3 py-3 outline-none text-sm text-gray-800 placeholder-gray-400 min-h-[60px] max-h-[200px] overflow-y-auto scrollbar-thin transition-all duration-200 ease-out"
            />

            {/* Toolbar row */}
            <div className="flex items-center gap-1 px-2 pb-2">

              {/* No attach-file / attach-image / image-gen buttons in
                  KbChat — answers must come from the indexed corpus. */}

              {/* Mic — Speech-to-Text */}
              <div className="flex items-center">
                <button
                  onClick={handleMicToggle}
                  disabled={inputDisabled}
                  title={isListening ? "Stop recording" : "Speak your prompt"}
                  className={`p-1.5 cursor-pointer rounded-l-lg transition disabled:opacity-40 ${
                    isListening
                      ? "text-red-500 animate-pulse hover:text-red-600"
                      : "text-gray-400 hover:text-gray-600 hover:bg-gray-100"
                  }`}
                >
                  {isListening ? <MicOff size={16} /> : <Mic size={16} />}
                </button>
                <select
                  value={micLang}
                  onChange={e => setMicLang(e.target.value)}
                  disabled={isListening}
                  title="Speech language"
                  className="text-[10px] border-l border-gray-200 hover:bg-gray-100 p-1 rounded bg-transparent text-gray-400 outline-none cursor-pointer pr-1 disabled:opacity-40"
                >
                  <option value="en-IN">EN-IN</option>
                  <option value="hi-IN">हिन्दी</option>
                  <option value="ta-IN">தமிழ்</option>
                  <option value="te-IN">తెలుగు</option>
                  <option value="kn-IN">ಕನ್ನಡ</option>
                  <option value="ml-IN">മലയാളം</option>
                  <option value="bn-IN">বাংলা</option>
                  <option value="mr-IN">मराठी</option>
                  <option value="gu-IN">ગુજરાતી</option>
                  <option value="pa-IN">ਪੰਜਾਬੀ</option>
                  <option value="en-US">EN-US</option>
                </select>
              </div>

              <div className="flex-1" />

              {/* Generate image (Imagen / DALL-E via LLM proxy) */}
              <button
                  type="button"
                  onClick={() => {
                    const p = (input || "").trim();
                    if (p) {
                      setInput("");
                      handleImageGenerate(p);
                    } else {
                      setInput("/image ");
                      // Use requestAnimationFrame to position cursor AFTER React has
                      // flushed the state update and the DOM reflects the new value.
                      requestAnimationFrame(() => {
                        const ta = textareaRef.current;
                        if (ta) {
                          ta.focus();
                          const end = ta.value.length;
                          ta.selectionStart = end;
                          ta.selectionEnd = end;
                        }
                      });
                    }
                  }}
                  disabled={inputDisabled}
                  title="Generate image (Imagen / DALL-E)"
                  className="p-1.5 cursor-pointer text-gray-500 hover:text-pink-500 transition disabled:opacity-40"
              >
                <Wand2 size={16} />
              </button>

              {/* RAG toggle removed — KB-scoped chats are now started from the
                  Knowledge Base → Chat tab (KbChatPanel). Server-side scope
                  read + ragMode handling remain intact; chats created with
                  rag_mode='on' from KbChatPanel still flow through the same
                  /ask scope-injection path. */}

              {/* Model selector */}
              <select
                value={imageGenerating ? "auto" : selectedModel}
                onChange={e => {
                  const v = e.target.value;
                  if (!v.startsWith("__div_")) setSelectedModel(v);
                }}
                onFocus={refreshModelLists}
                disabled={imageGenerating}
                className={`text-xs border border-gray-200 rounded-lg px-2 py-1.5 outline-none bg-white text-gray-500 hover:border-gray-300 focus:border-indigo-300 ${
                  imageGenerating ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
                }`}
                title={imageGenerating ? "Model selection disabled during image generation" : "Select model"}
              >
                {MODEL_OPTIONS.map(o => (
                  <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>
                ))}
              </select>

              {/* Send / Stop */}
              <button
                id="chat-send-btn"
                onClick={loading && !imageGenerating ? stopGeneration : sendMessage}
                disabled={ imageGenerating || (!loading && !input.trim()) || uploading || docGenerating }
                className="p-1.5 cursor-pointer text-gray-500 hover:text-gray-400 transition disabled:opacity-30 rounded-full"
              >
                {loading ? <CirclePauseIcon size={20} /> : <SendHorizontal size={20} />}
              </button>

            </div>

            {/* KB scope picker removed from Chat — scope is picked once
                in Knowledge Base → Chat (KbDrillGraph) and persisted on the
                Chat row via PATCH /chats/{id}/scope. Chat.jsx still hydrates
                product_id/domain/spec_version/kb_doc_id on load so the KB
                grounding indicator and source citations work correctly. */}
          </div>
        </div>

      </div>

      {/* Document Preview Modal — opens when user clicks Eye icon on attachment chip */}
      {previewAttachment && (
        <DocumentPreviewModal
          attachmentId={previewAttachment.id}
          fileName={previewAttachment.fileName}
          fileType={previewAttachment.fileType}
          parsedText={previewAttachment.parsedText}
          onClose={() => setPreviewAttachment(null)}
        />
      )}

      {/* No PPTWizard in KbChat — presentation generation is not a
          KB chat capability. */}
    </div>
  );
}
