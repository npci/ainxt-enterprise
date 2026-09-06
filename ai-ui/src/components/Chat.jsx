// SPDX-License-Identifier: MIT
import { useState, useRef, useEffect, useCallback } from "react";
import {
  CirclePauseIcon,
  SendHorizontal,
  SquarePen,
  MessageSquare,
  Trash2,
  Pencil,
  Pin,
  PinOff,
  Paperclip,
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
  ImageIcon,
  Share2,
  RotateCcw,
  Download,
  Sparkles,
  CircleX,
  BookOpen,
  Brain,
  Users,
  Eye,
  AlertTriangle,
  ArrowDown,
} from "lucide-react";
import MemoryPanel from "./MemoryPanel";
import ArtifactsPanel from "./ArtifactsPanel";
import MessageMeta from "./MessageMeta";
import { motion } from "framer-motion";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { mdComponents, mdUrlTransform, parseDocMarkers, DocDownloadButton, PPTDownloadButton, ExpandableMessageBody, buildDocJobMarker, DownloadableImage, stripDocMarkersForExport } from "./Message";
import VoiceMode from "./VoiceMode";
import AiNxtSpinner from "./AiNxtSpinner";
import { ChatListSkeleton, ChatMessageSkeleton, StreamingMessageSkeleton } from "./Skeleton";
// ScopePicker import removed — scope selection moved to Knowledge Base → Chat (KbDrillGraph).

import { API_BASE as API, authFetch, MODEL_IMAGE } from '../config';
import { toIST } from '../utils/time';
import { useConfirm, useToast } from './ui/DialogProvider.jsx';
import { useFileDrop } from '../hooks/useFileDrop';
import { isDesktop, readFileSpreadsheet } from '../hooks/useDesktop.js';
import PPTWizard from './PPTWizard.jsx';
import { usePPTChat } from '../hooks/usePPTChat.js';
import { usePPTConversation } from '../hooks/usePPTConversation.js';
import PPTChatMessageRenderer from './PPTChatMessageRenderer.jsx';
import DocumentPreviewModal from './DocumentPreviewModal.jsx';
import { cacheStore, cachePurgeExpired, cachedGet, cachedGetOrFetch } from '../utils/previewCache';
import { isKbChat } from '../utils/kbChat.js';
import { stripMemoryTag, parseMemoryTag, stripSystemPrefix, detectTone, stripAttachmentContext } from '../utils/messageContent.js';
import { generateImage, IMAGE_ARTIFACT_TITLE } from '../utils/imageGenerate';
import { validateIdentifier, validateFreeText } from '../utils/securityValidation';

// ── extractDurationFromPrompt: parse a desired video duration from natural
// language. Returns a clamped integer in [min, max], or `fallback` if no
// duration phrase is present. Recognised forms (case-insensitive):
//   "10 second video", "10-second clip", "10s", "10 sec", "10 secs",
//   "10 seconds", "for 10 seconds", "duration 10", "duration: 10",
//   "duration of 10 seconds", "lasting 10 seconds"
// Numeric words 1–20 ("ten seconds") are also supported.
// The default [min, max] is the 4–8 s product window enforced by the backend
// (cil/intent.py _VID_MIN_DURATION/_VID_MAX_DURATION → /chat/video-generate).
// An explicit out-of-range ask ("30 second video") is clamped, not rejected,
// so the request still succeeds at the nearest allowed duration.
export function extractDurationFromPrompt(prompt, fallback = 8, min = 4, max = 8) {
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
    // Cache-first, then authenticated server fallback so previews survive
    // re-login / browser restart / cross-device (server is source of truth).
    cachedGetOrFetch(attachment.id).then(res => {
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

// ── ImageChip: cache-aware thumbnail for sent/reloaded image messages ─────
// Image bytes live in the 30-day browser preview cache (same store used for
// documents) keyed by the attachment id. On mount we pull the blob from the
// cache and mint a fresh object URL — this is what makes an image survive a
// page refresh (the original in-memory blob URL is revoked after send).
// If the cache entry is missing/expired → a small "preview expired" chip.
function ImageChip({ attachment }) {
  const [url, setUrl] = useState(null);
  const [status, setStatus] = useState("checking"); // "checking" | "available" | "expired"

  useEffect(() => {
    let cancelled = false;
    let objUrl = null;
    // Cache-first, then authenticated server fallback so image thumbnails
    // survive re-login / browser restart / cross-device.
    cachedGetOrFetch(attachment.id)
      .then(async res => {
        if (cancelled) return;
        if (!res) { setStatus("expired"); return; }
        const blob = await res.blob();
        if (cancelled) return;
        objUrl = URL.createObjectURL(blob);
        setUrl(objUrl);
        setStatus("available");
      })
      .catch(() => { if (!cancelled) setStatus("expired"); });
    return () => {
      cancelled = true;
      if (objUrl) URL.revokeObjectURL(objUrl);
    };
  }, [attachment.id]);

  if (status === "available" && url) {
    return (
      <img
        src={url}
        alt={attachment.file_name || "Attached image"}
        className="max-h-48 max-w-xs rounded-md object-contain border border-gray-200"
      />
    );
  }

  if (status === "checking") {
    return (
      <span className="flex items-center gap-1 bg-gray-50 border border-gray-200 text-gray-400 text-xs px-2 py-0.5 rounded-full">
        <Loader2 size={10} className="animate-spin" />
        <span className="max-w-[120px] truncate">{attachment.file_name || "image"}</span>
      </span>
    );
  }

  // expired / missing from cache
  return (
    <span
      title="Image preview expired — re-upload to preview again"
      className="flex items-center gap-1 bg-gray-50 border border-gray-200 text-gray-400 text-xs px-2 py-0.5 rounded-full"
    >
      <ImageIcon size={10} />
      <span className="max-w-[120px] truncate">{attachment.file_name || "image"}</span>
      <span className="text-[9px] text-amber-500 ml-0.5">preview expired</span>
    </span>
  );
}

// ── ToolCard: a single expandable tool-call card ──────────────────────────
// One structured tool_event (name, status, args, output). Extracted so it can
// render both standalone and inside a collapsed ToolGroup (Phase 1.3).
function ToolCard({ te }) {
  const dot =
    te.status === "success"
      ? "bg-green-500"
      : te.status === "error"
      ? "bg-red-500"
      : "bg-amber-500 animate-pulse";
  return (
    <details className="text-xs border border-gray-200 rounded-md group" open={te.status === "error"}>
      <summary className="px-2 py-1.5 cursor-pointer select-none flex items-center gap-2 text-gray-600 hover:bg-gray-50">
        <span className={`inline-block w-1.5 h-1.5 rounded-full ${dot}`} />
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
  );
}

// ── ToolGroup: collapses multiple tool calls into one summary (Phase 1.3) ──
// Mirrors Buddy's tool grouping. When ≥2 tools ran and none errored, they
// collapse under "Used N tools" so a multi-tool turn doesn't push the answer
// off-screen. A single tool, or any errored/running tool, renders expanded.
function ToolGroup({ toolEvents }) {
  if (!Array.isArray(toolEvents) || toolEvents.length === 0) return null;

  const anyActive = toolEvents.some(t => t.status !== "success" && t.status !== "error");
  const anyError  = toolEvents.some(t => t.status === "error");

  // Few tools, or something needs attention → show cards directly.
  if (toolEvents.length < 2 || anyError || anyActive) {
    return (
      <div className="mb-2 space-y-1">
        {toolEvents.map((te, i) => <ToolCard key={i} te={te} />)}
      </div>
    );
  }

  // ≥2 completed read-only tools → collapse behind one summary.
  return (
    <details className="mb-2 text-xs border border-gray-200 rounded-md group">
      <summary className="px-2 py-1.5 cursor-pointer select-none flex items-center gap-2 text-gray-600 hover:bg-gray-50">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-500" />
        <span className="font-medium">Used {toolEvents.length} tools</span>
        <span className="text-gray-400">
          — {toolEvents.map(t => t.name || "tool").slice(0, 3).join(", ")}
          {toolEvents.length > 3 ? "…" : ""}
        </span>
        <span className="ml-auto text-gray-400 group-open:rotate-90 transition">›</span>
      </summary>
      <div className="px-2 py-2 border-t border-gray-100 space-y-1">
        {toolEvents.map((te, i) => <ToolCard key={i} te={te} />)}
      </div>
    </details>
  );
}

// ── ErrorCard: styled error + Retry (Phase 1.6) ───────────────────────────
// Replaces the raw "Error: …" text bubble. Shows a clean card with the
// message and a Retry button that re-runs the last user prompt.
function ErrorCard({ message, onRetry }) {
  return (
    <div className="my-2 flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50/70 px-3 py-2.5 text-sm">
      <AlertTriangle size={16} className="text-red-500 mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="font-medium text-red-700">Something went wrong</div>
        <div className="text-red-600/90 text-xs mt-0.5 break-words">
          {message || "Failed to get a response. Please try again."}
        </div>
        {onRetry && (
          <button
            onClick={onRetry}
            className="mt-2 inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-red-300 text-red-600 hover:bg-red-100 transition-colors text-xs cursor-pointer"
          >
            <RotateCcw size={12} />
            Retry
          </button>
        )}
      </div>
    </div>
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

// Phase 5.3: context-window badge for the model picker. Native <select> options
// can only hold plain text, so we append a compact "· 200K" tag to the label
// (matching the backend _MODEL_CONTEXT_WINDOW map in gateway.py). Keyed by
// case-insensitive substring; first match wins; no badge for Auto/dividers.
const MODEL_CONTEXT_BADGE = [
  ["gemini", "1M"],
  ["gpt-5",  "256K"],
  ["gpt",    "128K"],
  ["claude", "200K"],
  ["sonnet", "200K"],
  ["opus",   "200K"],
  ["haiku",  "200K"],
  ["kimi",   "128K"],
  ["local",  "128K"],
];
function _modelContextBadge(value = "", label = "") {
  const hay = `${value} ${label}`.toLowerCase();
  for (const [key, tag] of MODEL_CONTEXT_BADGE) {
    if (hay.includes(key)) return tag;
  }
  return null;
}

// Price-tier tag shown after the context badge, e.g.
// "Claude Sonnet 4.6 · 200K · Paid". The tier is authoritative from the
// backend (/all-models returns tier: "paid" | "free" per model) — the UI does
// NOT hardcode any provider→tier mapping. This helper only maps the backend
// value to a display word.
function _modelTierTag(tier) {
  if (tier === "paid") return "Paid";
  if (tier === "free") return "Free";
  return null;  // Auto / unknown → no tag
}

// Status banner shown while a doc job is queued (keyed by format).
const DOC_STATUS_MAP = {
  pdf:  "📄 PDF document generation started",
  docx: "📝 Word document generation started",
  xlsx: "📈 Excel spreadsheet generation started",
  pptx: "📊 Presentation generation started via AI skillset…",
  md:   "📝 Markdown document generation started",
  txt:  "📃 Text document generation started",
  csv:  "📑 CSV data file generation started",
};

// Doc-job statuses that count as "still in progress" and therefore lock the
// composer. Terminal states (ready/error/timeout) and the clarify pause do NOT
// lock it. Module-scope so it's a single stable reference (see docGenerating).
const DOC_ACTIVE_STATES = ["checking", "polling"];

// NOTE: the local-model intent-classifier JSON schema prompt
// (DOC_CLASSIFIER_SYS_PROMPT) and its brace-depth JSON scanner
// (_tryExtractJSON) were REMOVED along with classifyIntent() — doc and
// image generation intent are now classified entirely by the backend CIL
// inside /ask (see the routing block in sendMessage()).

const ACCEPT_TYPES = [
  ".pdf", ".docx", ".pptx", ".ppt", ".xlsx", ".xls", ".csv",
  ".html", ".htm", ".rtf", ".txt", ".json", ".md", ".xml",
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
].join(",");

const IMAGE_ACCEPTED   = "image/jpeg,image/png,image/gif,image/webp";
const IMAGE_MAX_BYTES  = 10 * 1024 * 1024; // 10 MB
const IMAGE_MIME_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];

export default function Chat({
  chats, setChats, activeChatId, setActiveChatId, user,
  chatsLoading = false, pendingPrompt, onPendingPromptConsumed,
  // Embed-mode flags — used when Chat is hosted inside another surface
  // (currently: the Knowledge Base → Chat tab). When true:
  //   - hideSidebar: skip rendering the internal chat list panel so the
  //     parent (KbChatList) owns the left rail.
  //   - embedded:   switch the outer container from h-screen to h-full
  //     so it fits the parent's flex box instead of stretching to the
  //     full viewport height.
  hideSidebar = false,
  embedded    = false,
  // KB-embedded mode only. When the chat surface is hosted inside the
  // Knowledge Base "Chat" tab, the parent (KbChatPanel) passes the
  // resolved scope so we can render a scope-summary welcome line in
  // place of the generic "Hey {firstName}!" greeting.
  //   { domain, product_name, spec_version, kb_doc_name }
  kbScope     = null,
}) {
  const { toast } = useToast();

  // ── Chat list state ────────────────────────────────────────
  const [search, setSearch]             = useState("");
  const [editingId, setEditingId]       = useState(null);
  const [editingTitle, setEditingTitle] = useState("");

  // ── Message state ──────────────────────────────────────────
  const [input, setInput]           = useState("");
  const [loadingMap, setLoadingMap] = useState({});   // per-chat loading state keyed by chatId
  const [historyLoading, setHistoryLoading] = useState(false);

  // ── Multimodal state ───────────────────────────────────────
  const [selectedModel, setSelectedModel] = useState("auto");
  const [attachments, setAttachments]     = useState([]);
  const [uploading, setUploading]         = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadPhase, setUploadPhase]     = useState("uploading"); // "uploading" | "processing"
  const [fileLimitError, setFileLimitError] = useState(false);
  const [previewAttachment, setPreviewAttachment] = useState(null);


  // ── Prompt Enhancer ────────────────────────────────────────
  const [enhancing, setEnhancing]             = useState(false);
  const [enhancerModal, setEnhancerModal]     = useState(false);
  const [enhancerEdited, setEnhancerEdited]   = useState("");
  const [followupQs, setFollowupQs]           = useState([]);
  const [followupAnswers, setFollowupAnswers] = useState({});

  // ── PPT Wizard ────────────────────────────────────────────
  const [pptWizardOpen, setPptWizardOpen]   = useState(false);
  const [pptWizardPrompt, setPptWizardPrompt] = useState("");
  const [pptWizardChatId, setPptWizardChatId] = useState(null);

  // ── Inline PPT Chat State ─────────────────────────────────
  const [pptChatStates, setPPTChatStates] = useState({}); // Map messageId -> pptState
  
  // ── PPT Chat Hook ─────────────────────────────────────────
  const {
    pptState,
    updateParams,
    reset: resetPPTChat,
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

  // Note: PPT state is NOT reset when switching chats to allow background
  // generation. The PPT conversation persists even when the user switches to
  // other chats. Likewise, doc-generation state (docJobStatus) is NOT reset on
  // switch — a document generating in chat A must keep tracking (and finish +
  // become downloadable) while the user works in chat B. docGenerating is
  // derived + chat-scoped, so background jobs in other chats can't lock this
  // chat's composer.
  useEffect(() => {
    pptConversation.reset();
    setEditingMsgId(null);
    setContextInfo(null);  // Phase 2: drop stale context meter on chat switch
    // Clear the input box on chat switch so a draft typed in one chat
    // does not bleed into another chat when the user switches.
    setInput("");
  }, [activeChatId]);

  // ── Budget exhausted banner ────────────────────────────────
  const [budgetExhausted, setBudgetExhausted] = useState(false);

  // ── Context-window telemetry (Phase 2) ────────────────────
  // Populated from the backend `context` SSE event: { tokens_used,
  // context_window, pct_used, recent_turns, compacted }. Rendered as a
  // live meter in the composer footer, matching Buddy's context bar.
  const [contextInfo, setContextInfo] = useState(null);

  // ── Jump-to-latest button (Phase 6.1) ─────────────────────
  // Reactive mirror of the userScrolledUp ref so a floating "scroll to
  // bottom" button can render when the user has scrolled up in a long chat.
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);

  // ── Document generation tracking (per-job) ─────────────────
  // Separate from `loading` because doc jobs are background — loading is
  // cleared immediately once /ask returns {route:"doc", job_id}.
  // a doc still generating in chat A
  // must not lock the input while the user is typing in chat B (doc jobs run in
  // the background across chat switches, like PPT).
  const [docJobStatus, setDocJobStatus] = useState(() => {
    // Restore startedAt for any in-flight doc jobs that survived a page refresh.
    // Only the startedAt timestamp is persisted — status is reset to "checking"
    // so the component re-probes the backend on mount.
    const restored = {};
    try {
      for (let i = 0; i < sessionStorage.length; i++) {
        const key = sessionStorage.key(i);
        if (key?.startsWith("docjob:")) {
          const jobId = key.slice(7);
          const val = JSON.parse(sessionStorage.getItem(key));
          if (val?.startedAt) {
            restored[jobId] = { status: "checking", chatId: val.chatId ?? null, startedAt: val.startedAt };
          }
        }
      }
    } catch (_) {}
    return restored;
  }); // { [jobId]: { status, chatId, startedAt } }

  const docGenerating = Object.values(docJobStatus).some(
    j => j && j.chatId === activeChatId && DOC_ACTIVE_STATES.includes(j.status)
  );

  // Record a single job's status. Centralised so every write is logged.
  // `startedAt` (epoch ms) is stamped ONCE, the first time a job is seen, and preserved on every subsequent status write.
  // sessionStorage is used to survive page refreshes: startedAt is written on first
  // sight and removed when the job reaches a terminal state.
  const _DOC_TERMINAL_STATES = ["ready", "error", "cancelled", "timeout"];
  const setDocJobState = useCallback((jobId, status, chatId) => {
    if (!jobId) return;
    setDocJobStatus(prev => {
      const existing = prev[jobId];
      if (existing && existing.status === status) return prev;  // no-op render guard
      console.info(
        `[docgen] client job=${jobId} chat=${chatId || existing?.chatId || "?"} ` +
        `status=${existing?.status || "(new)"} → ${status}`
      );
      const startedAt = existing?.startedAt ?? Date.now();
      const resolvedChatId = chatId ?? existing?.chatId ?? null;

      // Persist startedAt on first sight so it survives a page refresh.
      if (!existing) {
        try {
          sessionStorage.setItem(`docjob:${jobId}`, JSON.stringify({ startedAt, chatId: resolvedChatId }));
        } catch (_) {}
      }
      // Clean up once the job reaches a terminal state.
      if (_DOC_TERMINAL_STATES.includes(status)) {
        try { sessionStorage.removeItem(`docjob:${jobId}`); } catch (_) {}
      }

      return {
        ...prev,
        [jobId]: {
          status,
          chatId:    resolvedChatId,
          startedAt,
        },
      };
    });
  }, []);

  // True when a message embeds ≥1 [DOCJOB:…] marker AND at least one of those
  // jobs has not yet reached "ready". Used to hide the feedback/copy/share
  // action bar while any embedded doc is still generating. Parses the marker
  // ids straight from the message content so it correctly handles a single
  // message that requested MULTIPLE documents (the old per-message status map
  // could only remember one job's status).
  const messageHasPendingDoc = useCallback((content) => {
    if (!content || !content.includes("[DOCJOB:")) return false;
    const re = /\[DOCJOB:([^:]+):[^:]+:[^\]]+\]/g;
    let m;
    while ((m = re.exec(content)) !== null) {
      const jid = m[1];
      if (docJobStatus[jid]?.status !== "ready") return true;  // any not-ready → pending
    }
    return false;
  }, [docJobStatus]);

  // ── Image generation in-progress flag ─────────────────────
  // Used to disable the model selector during image generation
  // (Generate Image toolbar button or classifier-routed image intent).
  const [imageGenerating, setImageGenerating] = useState(false);

  // ── Memories panel toggle ──────────────────────────────────
  const [memoryOpen, setMemoryOpen] = useState(false);

  // ── Saved prompt templates ("/" slash-command menu) ──────────────
  const [templates, setTemplates]   = useState([]);
  const [tplMenuOpen, setTplMenu]   = useState(false);
  const [tplFilter, setTplFilter]   = useState("");
  const [tplActiveIdx, setTplActiveIdx] = useState(0);  // Phase 5.2: keyboard-nav highlight

  // Templates matching the current "/" filter, capped like the render list.
  // Shared by the render and the keyboard-nav handler so Enter selects exactly
  // what the user sees highlighted.
  const tplMatches = (() => {
    const f = tplFilter;
    return templates
      .filter(t => !f || (t.name || "").toLowerCase().includes(f))
      .slice(0, 8);
  })();

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
      setTplActiveIdx(0);   // reset highlight to first match on each keystroke
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

    // Client-side pre-check — mirrors validate_prompt_template_request() in
    // core/security_validation.py: `name` is an identifier, `body` is free
    // text. The backend (POST /prompt-templates) remains the authoritative
    // enforcer.
    const nameCheck = validateIdentifier(name);
    if (!nameCheck.isValid) { toast.error(nameCheck.errors[0]?.message || "Invalid template name"); return; }
    const bodyCheck = validateFreeText(body);
    if (!bodyCheck.isValid) { toast.error(bodyCheck.errors[0]?.message || "Invalid template content"); return; }

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
  const [governanceLoaded, setGovernanceLoaded] = useState(false); // true once /my-models responds

  // Flattened MODEL_OPTIONS filtered to only models the user is permitted to use
  const MODEL_OPTIONS = (() => {
    const raw = allModelProviders.length > 0
      ? allModelProviders.flatMap((group, gi) => [
          ...(gi > 0 ? [{ value: `__div_${gi}__`, label: `── ${group.provider} ──`, disabled: true }] : []),
          // modelId = full concrete model ID (e.g. "claude-sonnet-4-6") used for governance matching
          // value   = short alias sent as the model hint in POST /ask (e.g. "claude")
          ...group.models.map(m => ({ value: m.id, modelId: m.modelId || m.id, label: m.label, tier: m.tier, modality: m.modality })),
        ])
      : BASE_MODEL_OPTIONS;

    // Only show all models if governance hasn't loaded yet (network pending / error).
    // Once loaded, an empty allowedModels means ALL models are blocked — show only Auto.
    if (!governanceLoaded) return raw;

    // Keep "auto" always, dividers always; filter real model entries by allowedModels.
    // Match against modelId (full concrete ID like "claude-sonnet-4-6") because
    // /model-governance/my-models returns full IDs, not short aliases like "claude".
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
  // chatId → true when the user hit Stop during this turn. Set in
  // stopGeneration(), cleared at the top of sendMessage()/
  // handleImageGenerate() for each new turn.
  const cancelledChatsRef = useRef({});
  const fileInputRef = useRef(null);
  const imageInputRef = useRef(null);
  const textareaRef = useRef(null);
  const uploadXhrRef = useRef(null);
  const { confirm } = useConfirm();

  // ── Derived ────────────────────────────────────────────────
  const activeChat = chats.find(c => c.id === activeChatId);
  const messages   = activeChat?.messages || [];

  // id of the most-recent assistant message — used to show the Regenerate
  // button only on the latest reply (standard AI chat UX).
  const lastAssistantId = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant") return messages[i].id;
    }
    return null;
  })();

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
    const scrolledUp = (el.scrollHeight - el.scrollTop - el.clientHeight) > 120;
    userScrolledUp.current = scrolledUp;
    // Reactive mirror for the Jump-to-latest button (Phase 6.1). Only flip
    // state on change to avoid a setState on every scroll frame.
    setShowJumpToLatest(prev => (prev !== scrolledUp ? scrolledUp : prev));
  }, []);

  // Smooth-scroll to the newest message and hide the jump button.
  const jumpToLatest = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    userScrolledUp.current = false;
    setShowJumpToLatest(false);
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // Recompute the Jump-to-latest visibility on every content change (not just
    // on scroll events). Otherwise the button gets stuck "on" from a previous
    // long conversation when content shrinks, or shows during "Understanding…"
    // before any text exists. It must ONLY show when the content actually
    // OVERFLOWS the viewport AND the user has scrolled up.
    const _overflow  = el.scrollHeight - el.clientHeight;
    const _distance  = el.scrollHeight - el.scrollTop - el.clientHeight;
    const _scrolledUp = _overflow > 120 && _distance > 120;
    userScrolledUp.current = _scrolledUp;
    setShowJumpToLatest(prev => (prev !== _scrolledUp ? _scrolledUp : prev));

    // If the user has manually scrolled up, don't hijack their position.
    // This prevents doc-generation polling re-renders from snapping back to bottom.
    if (_scrolledUp) return;
    if (_isStreaming || _distance < 120) {
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
    // Client-side pre-check — mirrors validate_chat_scope_fields() in
    // core/security_validation.py (identifier allow-list for domain/
    // spec_version). The backend (PATCH /chats/{id}/scope) remains the
    // authoritative enforcer; silently drop an invalid value here rather
    // than block the whole scope-change UX over what's normally a
    // dropdown-driven field.
    if (body.domain) {
      const domainCheck = validateIdentifier(body.domain);
      if (!domainCheck.isValid) body.domain = null;
    }
    if (body.spec_version) {
      const specCheck = validateIdentifier(body.spec_version);
      if (!specCheck.isValid) body.spec_version = null;
    }
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

  // NOTE: client-side document-intent regex detection was REMOVED. Doc-vs-chat
  // routing is now decided entirely by the BACKEND small-LLM classifier in the
  // /ask handler (no regex on the client). Image intent still uses classifyIntent().

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
      const response = await authFetch(`${API}/ask`, {
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
  // Extracted so it can be called both on mount AND right before the model
  // dropdown opens (see the <select>'s onFocus below) — otherwise a model an
  // admin adds/syncs via the "LLM Providers" screen while this Chat tab is
  // already open never appears until a full page reload.
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

    // Fetch user-specific allowed models from governance rules.
    // governance_loaded=true in the response means the backend successfully
    // evaluated rules — even if models=[] (all blocked). We must set
    // governanceLoaded=true in both cases so the picker hides blocked models
    // instead of falling back to showing everything.
    authFetch(`${API}/model-governance/my-models`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d?.governance_loaded) {
          setAllowedModels(d.models || []);  // [] = all blocked, still apply filter
          setGovernanceLoaded(true);
        }
      })
      .catch(() => {});  // network error → governanceLoaded stays false → fail-open
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
        const loaded = (data.messages || []).map(m => {
          const _hasAttach = !!(m.attachment_ids && m.attachment_ids.length);
          // Per-attachment kind now comes from the server (ChatAttachment.kind),
          // resolved in _attKind() below. The old approach tested m.content for
          // a 🖼 marker, but that marker only ever existed in the UI's local
          // bubble text — /ask receives the plain `question` and the gateway
          // persists its own safe_question, so it NEVER round-tripped through
          // the DB. The test therefore always failed on reload and every
          // uploaded image came back as a nameless "doc" chip instead of a
          // thumbnail. The marker test is kept ONLY as a fallback for legacy
          // rows whose ChatAttachment record is gone (kind unavailable).
          const _legacyIsImage = _hasAttach && /🖼/.test(m.content || "");
          const _attKind = (a) => {
            const k = String(a?.kind || "").toLowerCase();
            // Only kind="image" is an image. Generated IMAGES are also stored
            // with kind="image" (routers/chat_router.py chat_generate_image),
            // whereas kind="generated" is a generated DOCUMENT re-ingested as an
            // attachment (services/doc_context.py mirror_generated_doc_as_attachment,
            // parsed_text = the markdown source) — that one is a doc, not an image.
            if (k === "image") return "image";
            if (k) return "doc";
            return _legacyIsImage ? "image" : "doc";
          };
          // For attachment turns the stored content includes injected file
          // context (docs) or a marker line — show only the user's question;
          // the chip/thumbnail below represents the file.
          // Rehydrate video player on reload — the backend persists a
          // [VIDEO:{id}:{filename}] marker in the assistant message content
          // (chat_router.py video-generate handler). Extract the first match so
          // msg.videoUrl is set and the rich player renders on reload.
          // Strip the marker from content so parseDocMarkers never sees it and
          // renders a duplicate <video> element alongside the rich player.
          const _vidMatch = /\[VIDEO:([A-Za-z0-9_\-]{8,64}):([^\]]+)\]/.exec(m.content || "");
          const _rawContent = _vidMatch
            ? (m.content || "").replace(/\[VIDEO:[^\]]+\]/g, "").trim()
            : m.content;
          const _content = _hasAttach
            ? stripAttachmentContext(_rawContent)
            : stripSystemPrefix(_rawContent);
          return {
            id:         m.id,
            role:       m.role,
            content:    _content,
            // videoUrl/videoMime are set only for assistant messages that
            // contain a [VIDEO:...] marker; undefined for all other messages.
            videoUrl:   _vidMatch ? `/ainxt/v1/api/chat/video/${_vidMatch[1]}` : undefined,
            videoMime:  _vidMatch ? "video/mp4" : undefined,
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
            // Artifacts (image / code / html Canvas blocks) — restores the
            // "Open in Canvas" chip after page reload.
            artifacts: m.artifacts && m.artifacts.length ? m.artifacts : undefined,
            // Rehydrate attachments from server-resolved metadata (name/type
            // from the ChatAttachment table for docs; id-only for images which
            // live in the browser cache). imageUrls is omitted — live blob URLs
            // are gone; ImageChip pulls bytes from the cache by id.
            attachments: _hasAttach
              ? (m.attachments && m.attachments.length
                  ? m.attachments
                  : m.attachment_ids.map(id => ({ id }))
                ).map(a => ({
                  id:         a.id,
                  file_name:  a.file_name || "",
                  file_type:  a.file_type || "",
                  file_size:  a.file_size || 0,
                  kind:       _attKind(a),
                }))
              : undefined,
          };
        });
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
    // A freshly-opened chat should start pinned to the bottom.
    userScrolledUp.current = false;
    setShowJumpToLatest(false);

    let raf1 = 0, raf2 = 0;
    const scrollDown = () => {
      const el = containerRef.current;
      if (el) el.scrollTo({ top: el.scrollHeight, behavior: "instant" });
    };
    scrollDown();
    raf1 = requestAnimationFrame(() => {
      scrollDown();
      raf2 = requestAnimationFrame(scrollDown);
    });
    const t = setTimeout(scrollDown, 250);
    return () => {
      cancelAnimationFrame(raf1);
      cancelAnimationFrame(raf2);
      clearTimeout(t);
    };
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

  // KB chats live exclusively in Knowledge Base → Chat tab — keep them
  // out of the main Chat sidebar. App.jsx hands us a single mixed
  // `chats` array (regular + KB) and `isKbChat()` discriminates.
  const filteredChats = chats
    .filter(c => c && !isKbChat(c))
    .filter(c => (c.title || "").toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));

  // ── Chat list helpers ──────────────────────────────────────

  function createEmptyChat() {
    return {
      id:        crypto.randomUUID(),
      title:     "New Chat",
      messages:  [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
  }

  function createNewChat() {
    const newChat = createEmptyChat();
    setChats(prev => [newChat, ...prev]);
    setActiveChatId(newChat.id);
    setAttachments([]);
  }

  function togglePin(chatId) {
    setChats(prev => prev.map(c => c.id === chatId ? { ...c, pinned: !c.pinned } : c));
    authFetch(`${API}/chats/${chatId}/pin`, { method: "PATCH" }).catch(() => {});
  }

  async function deleteChat(chat) {

    const {id,title} = chat;

 const ok = await confirm({ title: "Delete chat", message: `Delete this "${title || 'Chat'}"? This cannot be undone.`, confirmLabel: "Delete" });
    if (!ok) return;
   try {
    // Remove from backend; 404 is expected for locally-created chats with no messages
   const res =  await authFetch(`${API}/chats/${id}`, { method: "DELETE" });
   
   // Handle successful responses - 200, 204 are success codes, 404 is acceptable for local chats
   if (!res.ok && res.status !== 404) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Delete failed");
      }
      
    const updated = chats.filter(c => c.id !== id);
    if (updated.length === 0) {
      const newChat = createEmptyChat();
      setChats([newChat]);
      setActiveChatId(newChat.id);
    } else {
      setChats(updated);
      if (id === activeChatId) setActiveChatId(updated[0].id);
    }
   } catch (error) {
    toast.error(error.message || "Failed to delete chat");
   }


    
  }

  function startRename(chat) {
    setEditingId(chat.id);
    setEditingTitle(chat.title);
  }

  function saveRename(chatId) {
    let title = editingTitle.trim() || "New Chat";
    // Client-side pre-check — mirrors validate_chat_title() in
    // core/security_validation.py (XSS-only via validate_free_text()). The
    // backend (PATCH /chats/{id}/title) remains the authoritative enforcer.
    const titleCheck = validateFreeText(title);
    if (!titleCheck.isValid) {
      toast.error(titleCheck.errors[0]?.message || "Invalid title");
      setEditingId(null);
      return;
    }
    setChats(prev => prev.map(c => c.id === chatId ? { ...c, title, titleEditedByUser: true, updatedAt: Date.now() } : c));
    setEditingId(null);
    authFetch(`${API}/chats/${chatId}/title`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).catch(() => {});
  }

  // ── Message helpers ────────────────────────────────────────

  function updateMessages(newMessages) {
    setChats(prev =>
      prev.map(chat =>
        chat.id === activeChatId
          ? { ...chat, messages: newMessages, updatedAt: Date.now() }
          : chat
      )
    );
  }

  async function attachCoachHits(chatId, messageId, requestId) {
    if (!chatId || !messageId || !requestId) return;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (attempt > 0) await new Promise(resolve => setTimeout(resolve, 750));
      try {
        const res = await authFetch(`${API}/coach/events/by-request/${encodeURIComponent(requestId)}/hits`);
        if (!res.ok) continue;
        const data = await res.json();
        const hits = Array.isArray(data?.rule_hits) ? data.rule_hits : [];
        if (!hits.length) {
          if (data?.evaluated) return;
          continue;
        }
        setChats(prev => prev.map(chat =>
          chat.id === chatId
            ? {
              ...chat,
              messages: chat.messages.map(m =>
                m.id === messageId ? { ...m, requestId, coachHits: hits } : m
              ),
              updatedAt: Date.now(),
            }
            : chat
        ));
        return;
      } catch (_) { /* Coach inline hints are best-effort */ }
    }
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
        body: JSON.stringify({ rating, rag_mode: "off" }),
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
          rag_mode:           "off",
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


  // ── Retry after an error (Phase 1.6) ────────────────────────────────────
  // Drops the failed assistant message and re-sends the last user prompt.
  // Same mechanic as regenerate, but triggered from the ErrorCard.
  async function handleRetry() {
    if (loading) return;
    const msgs = activeChat?.messages || [];
    // Find the last user message; drop everything after it (the failed reply).
    let lastUserIdx = -1;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "user") { lastUserIdx = i; break; }
    }
    if (lastUserIdx === -1) return;
    const lastUserMsg = msgs[lastUserIdx];
    updateMessages(msgs.slice(0, lastUserIdx));   // keep everything before the prompt
    setInput(lastUserMsg.content);
    setTimeout(() => document.getElementById("chat-send-btn")?.click(), 80);
  }

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

  // Generate an image inline via gemini-3.1-flash-image (routed through
  // the LLM proxy /llm/imagen). This is the explicit "make me an image"
  // toolbar shortcut — it ALWAYS uses the gemini image model regardless
  // of the chat-model picker, because there's no other image-capable
  // model on the platform. For typed prompts the classifier in
  // sendMessage() decides between this path (Auto / gemini-3.1-flash-
  // image picked) and a normal /ask call to the user's chosen model
  // (which will respond in text — Claude/GPT/local can't make images).
  // Appends a user-bubble (the prompt) and an assistant-bubble (the image).
  async function handleImageGenerate(prompt) {
    const trimmed = (prompt || "").trim();
    if (!trimmed) return;

    const imgChatId = activeChatId;   // snapshot for async safety
    cancelledChatsRef.current[imgChatId] = false;   // fresh turn
    const userMsgId = crypto.randomUUID();
    const astMsgId  = crypto.randomUUID();
    const baseMsgs = (activeChat?.messages || []);

    // Optimistic placeholder — show the bare prompt (no slash-command prefix).
    // imageStage drives the image-specific AiNxtSpinner; we start at
    // "submitting" because the toolbar shortcut bypasses the classifier hop.
    setChats(prev => prev.map(c =>
        c.id === activeChatId
            ? {
              ...c,
              messages: [
                ...baseMsgs,
                { id: userMsgId, role: "user",      content: trimmed },
                { id: astMsgId,  role: "assistant", content: "", streaming: true,
                  imageStage: "submitting" },
              ],
            }
            : c
    ));

    setLoading(true, imgChatId);
    setImageGenerating(true);
    const _imgT0 = performance.now();
    // Bump to "rendering" stage shortly after submit so the user sees
    // progress while the gemini image call runs (single round-trip, no SSE).
    const _imgRenderTimer = setTimeout(() => {
      setChats(prev => prev.map(c => {
        if (c.id !== activeChatId) return c;
        let changed = false;
        const nextMessages = c.messages.map(m => {
          if (m.id === astMsgId && m.imageStage === "submitting") {
            changed = true;
            return { ...m, imageStage: "rendering" };
          }
          return m;
        });
        return changed ? { ...c, messages: nextMessages } : c;
      }));
    }, 2500);
    try {
      const { md, artifacts, modelLabel, costUsd, inTok, outTok, tokenUsage, latencySec } = await generateImage({
        api: API, authFetch,
        prompt:    trimmed,
        chatId:    activeChatId,
        messageId: astMsgId,
        // Always uses gemini-3.1-flash-image server-side — no model
        // selection needed (the only image-capable model on the platform).
      });
      // Prefer the server-measured latency (X-Latency-Sec) so the live chip
      // shows the SAME value that gets persisted to the ChatMessage row and
      // reappears after a refresh. Fall back to a client stopwatch only for
      // older backends that don't send the header.
      const _latencySecs = latencySec != null
        ? latencySec
        : (performance.now() - _imgT0) / 1000;
      setChats(prev => prev.map(c =>
          c.id === activeChatId
              ? {
                ...c,
                messages: c.messages.map(m =>
                    m.id === astMsgId
                        ? {
                            ...m,
                            content:    md,
                            streaming:  false,
                            artifacts,
                            // Real image-model id from X-Model-Label
                            // (e.g. "gemini-3.1-flash-image") so the chip
                            // mirrors text/doc footers.
                            modelLabel: modelLabel,
                            latency:    _latencySecs,
                            costUsd:    costUsd ?? null,
                            inTok:      inTok ?? null,
                            outTok:     outTok ?? null,
                            tokenUsage: tokenUsage ?? null,
                            timestamp:  Date.now(),
                            imageStage: undefined,
                          }
                        : m
                ),
              }
              : c
      ));
    } catch (e) {
      // 503 from the backend = both gemini-3.1-flash-image AND OpenAI
      // (gpt-image-1 / dall-e-3) were unavailable. Render as a clean
      // chat reply (no scary "Error:" prefix) — matches the user spec.
      const friendly = e?.unavailable
        ? (e.message || "Image generation model not available — please try again later.")
        : `Error: ${e?.message || "image generation failed"}`;
      setChats(prev => prev.map(c =>
          c.id === activeChatId
              ? {
                ...c,
                messages: c.messages.map(m =>
                    m.id === astMsgId
                        ? { ...m, content: friendly, streaming: false, imageStage: undefined }
                        : m
                ),
              }
              : c
      ));
    } finally {
      clearTimeout(_imgRenderTimer);
      setLoading(false, imgChatId);
      setImageGenerating(false);
    }
  }

  // Continue a truncated/stopped assistant response. Backend endpoint
  // POST /ask/continue/{message_id} re-streams from the cut point.
  async function handleContinue(messageId) {
    if (loading) return;

    // Re-run case: the turn was cancelled DURING classification / doc-submit,
    // before any answer was generated or persisted. /ask/continue has nothing
    // to resume (the assistant message doesn't exist server-side), so instead
    // we re-run the original prompt from scratch. We reuse the regenerate
    // mechanic: drop the cancelled placeholder, keep the user bubble, re-send.
    const _msgs = activeChat?.messages || [];
    const _target = _msgs.find(m => m.id === messageId);
    if (_target?.retryPrompt) {
      const idx = _msgs.findIndex(m => m.id === messageId);
      // Drop the cancelled assistant placeholder AND its preceding user bubble
      // (and anything after) — sendMessage() re-adds a fresh user bubble for the
      // same prompt, so keeping the old one here would duplicate it.
      let cut = idx >= 0 ? idx : _msgs.length;
      if (cut > 0 && _msgs[cut - 1]?.role === "user") cut -= 1;
      updateMessages(_msgs.slice(0, cut));
      setInput(_target.retryPrompt);
      setTimeout(() => document.getElementById("chat-send-btn")?.click(), 80);
      return;
    }

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
                                // Clear the cancelled flag on first resumed token
                                // so the "stopped generating" banner is replaced
                                // by the continued answer.
                                ? { ...m, content: (m.content || "") + o.t, cancelled: false }
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

  // ── Export chat ────────────────────────────────────────────────────────
  function handleExport() {
    const msgs = activeChat?.messages || [];
    if (!msgs.length) return;
    const title = activeChat?.title || "Chat";
    const lines = [`# ${title}`, `Exported: ${toIST(new Date())}`, ""];
    msgs.forEach(m => {
      lines.push(`**${m.role === "user" ? "You" : "AiNxt"}**`);
      lines.push(stripDocMarkersForExport(m.content || ""));
      lines.push("");
    });
    const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href = url;
    a.download = `${title.replace(/[^a-z0-9]/gi, "_")}.md`;
    a.click();
    // Defer revocation so the browser can finish reading the blob 
    setTimeout(() => URL.revokeObjectURL(url), 1000);
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
  // Aborts the in-flight POST /voice/tts so rapid clicks don't queue
  // multiple network requests that each spawn a new <audio> on resolve.
  const ttsAbortRef = useRef(null);
  // Monotonic counter — each handleSpeak invocation captures the current
  // value, and stale awaits compare against this ref to discard themselves.
  // Prevents a slow /voice/tts response from auto-playing after the user
  // has already toggled it off or switched to another message.
  const ttsRequestIdRef = useRef(0);
  // Blob URL currently held by ttsAudioRef.current — tracked separately so
  // we can revokeObjectURL on cancellation paths that don't fire onended.
  const ttsBlobUrlRef = useRef(null);

  // Hard-stop every TTS pathway: backend audio, blob URL, in-flight fetch,
  // and the Web Speech fallback. Safe to call multiple times. Used by
  // toggle-off, message-switch, chat-switch, and unmount.
  const stopAllTts = useCallback(() => {
    // Invalidate any pending awaits in handleSpeak.
    ttsRequestIdRef.current += 1;
    // Abort the in-flight /voice/tts fetch (if any).
    try { ttsAbortRef.current?.abort(); } catch { /* ignore */ }
    ttsAbortRef.current = null;
    // Pause and release the backend audio element.
    if (ttsAudioRef.current) {
      try { ttsAudioRef.current.pause(); } catch { /* ignore */ }
      try { ttsAudioRef.current.src = ""; } catch { /* ignore */ }
      ttsAudioRef.current = null;
    }
    // Revoke the blob URL to avoid memory leaks.
    if (ttsBlobUrlRef.current) {
      try { URL.revokeObjectURL(ttsBlobUrlRef.current); } catch { /* ignore */ }
      ttsBlobUrlRef.current = null;
    }
    // Cancel the Web Speech fallback.
    try { window.speechSynthesis?.cancel(); } catch { /* ignore */ }
  }, []);

  async function handleSpeak(msgId, content) {
    // Toggle-off: clicking the icon of the currently-active message stops it.
    // Also covers the "currently loading" case because speakingId is set
    // synchronously at the start of every invocation below.
    if (speakingId === msgId) {
      stopAllTts();
      setSpeakingId(null);
      return;
    }

    // Switching messages OR starting fresh: tear down anything in flight
    // before we begin so only one playback can ever be active.
    stopAllTts();

    // Mark this message as the intended speaker BEFORE any await so a
    // second click on the same icon (while loading) hits the toggle-off
    // branch above and cancels cleanly.
    setSpeakingId(msgId);

    // Capture the generation for this call. Any await that resolves after
    // the user has clicked again will see a mismatch and bail out without
    // creating a new <audio> element.
    const myRequestId = ++ttsRequestIdRef.current;
    const controller  = new AbortController();
    ttsAbortRef.current = controller;

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
        signal: controller.signal,
      });
      // Stale response — user clicked again (or toggled off) while we were
      // waiting. Discard silently; the newer invocation owns the UI now.
      if (myRequestId !== ttsRequestIdRef.current) return;
      if (resp.ok) {
        const blob = await resp.blob();
        if (myRequestId !== ttsRequestIdRef.current) return;
        const url   = URL.createObjectURL(blob);
        const audio = new Audio(url);
        ttsAudioRef.current  = audio;
        ttsBlobUrlRef.current = url;
        audio.onended = () => {
          // Only clear if this audio is still the active one.
          if (ttsAudioRef.current === audio) {
            ttsAudioRef.current = null;
            ttsBlobUrlRef.current = null;
            setSpeakingId(null);
          }
          URL.revokeObjectURL(url);
        };
        audio.onerror = () => {
          if (ttsAudioRef.current === audio) {
            ttsAudioRef.current = null;
            ttsBlobUrlRef.current = null;
            setSpeakingId(null);
          }
          URL.revokeObjectURL(url);
        };
        try {
          await audio.play();
        } catch (_playErr) {
          // play() rejects on autoplay restrictions or if pause() was
          // called mid-play. Clean up only if still current.
          if (ttsAudioRef.current === audio) {
            ttsAudioRef.current = null;
            ttsBlobUrlRef.current = null;
            URL.revokeObjectURL(url);
            setSpeakingId(null);
          }
        }
        return;
      }
      // Backend not available → fall through to browser speech
    } catch (e) {
      // AbortError = expected (user cancelled). Anything else falls through
      // to the Web Speech fallback below.
      if (e?.name === "AbortError") return;
      /* fall through */
    } finally {
      if (ttsAbortRef.current === controller) ttsAbortRef.current = null;
    }

    // If a newer click superseded us between fetch failure and fallback,
    // don't start the Web Speech utterance.
    if (myRequestId !== ttsRequestIdRef.current) return;

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
    utterance.onend   = () => {
      if (myRequestId === ttsRequestIdRef.current) setSpeakingId(null);
    };
    utterance.onerror = () => {
      if (myRequestId === ttsRequestIdRef.current) setSpeakingId(null);
    };

    // Defensive: cancel anything already queued in the synthesis queue
    // (handles the case where stopAllTts ran but the browser hadn't yet
    // flushed a previous utterance from a different code path).
    try { window.speechSynthesis.cancel(); } catch { /* ignore */ }
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

  // Stop TTS when the chat changes or component unmounts. Uses stopAllTts
  // (not just speechSynthesis.cancel) so we also abort the in-flight
  // /voice/tts fetch, pause the backend <audio>, and revoke its blob URL —
  // otherwise switching chats mid-playback would leak audio + memory.
  useEffect(() => {
    return () => {
      stopAllTts();
      setSpeakingId(null);
    };
  }, [activeChatId, stopAllTts]);

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
    setUploadPhase("uploading");
    toast.info("Upload cancelled.");
  }

  // ── Desktop spreadsheet pre-parse ──────────────────────────────────────────
  // On the desktop (Electron) the gateway's /chat/upload endpoint receives the
  // raw binary .xlsx bytes but the server-side parse_excel() path is identical
  // to the web Chat path — so the server DOES parse it correctly.  The bug is
  // that the desktop's local MCP read_file tool was reading xlsx as UTF-8 text.
  // That is fixed in main.js.  Additionally, we pre-parse xlsx/xls/xlsm files
  // HERE so the attachment's parsed_text is populated client-side immediately
  // (no round-trip needed) and the model sees the same tabular content as web.
  //
  // For non-desktop (web) the server already handles xlsx via parse_excel().
  // This function returns a map of { filename → parsedText } for xlsx files.
  async function _preParseSpreadsheets(files) {
    if (!isDesktop) return {};
    const _XLSX_EXTS = new Set(["xlsx", "xls", "xlsm"]);
    const result = {};
    await Promise.all(files.map(async (file) => {
      const ext = (file.name.split(".").pop() || "").toLowerCase();
      if (!_XLSX_EXTS.has(ext)) return;
      // File objects in Electron have a `path` property (absolute local path).
      const localPath = file.path;
      if (!localPath) return;
      try {
        const parsed = await readFileSpreadsheet(localPath);
        if (!parsed.error && parsed.text) {
          result[file.name] = parsed.text;
        } else if (parsed.error) {
          console.warn(`Chat: desktop spreadsheet pre-parse failed for ${file.name}:`, parsed.error);
        }
      } catch (e) {
        console.warn(`Chat: desktop spreadsheet pre-parse threw for ${file.name}:`, e);
      }
    }));
    return result;
  }

  async function handleFileUpload(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;

    const imageFilesToAdd = files.filter(f => IMAGE_MIME_TYPES.includes(f.type));
    const docFiles        = files.filter(f => !IMAGE_MIME_TYPES.includes(f.type));
    if (imageFilesToAdd.length > 0) addImageFiles(imageFilesToAdd);

    if (e.target?.value !== undefined) e.target.value = "";
    if (!docFiles.length) return;

    const MAX_FILES = 3;
    if (attachments.length + docFiles.length > MAX_FILES) {
      setFileLimitError(true);
      return;
    }
    setUploading(true);
    setUploadProgress(0);
    setUploadPhase("uploading");

    // On desktop, pre-parse any Excel files so parsed_text is available
    // immediately — identical to the server-side parse_excel() output.
    const desktopParsedMap = await _preParseSpreadsheets(files);

    const fd = new FormData();
    fd.append("chat_id", activeChatId);
    docFiles.forEach(f => fd.append("files", f));

    try {
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        uploadXhrRef.current = xhr;
        xhr.open("POST", `${API}/chat/upload`);
        xhr.withCredentials = true; // sends httpOnly auth_token cookie
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
      let uploaded = result.uploaded || [];

      // Merge desktop-parsed spreadsheet text into the upload response entries.
      // If the server already returned parsed_text (e.g. gateway is reachable and
      // ran parse_excel), prefer the server's version — it may be richer (tabulate
      // Markdown tables).  If the server returned an empty/error parsed_text, fill
      // in the desktop-parsed version so the model always gets tabular content.
      if (Object.keys(desktopParsedMap).length > 0) {
        uploaded = uploaded.map(entry => {
          const desktopText = desktopParsedMap[entry.file_name];
          if (!desktopText) return entry;
          // Use desktop text only when the server didn't produce usable content
          const serverText = entry.parsed_text || "";
          const useDesktopText = !serverText || serverText.startsWith("[") || serverText.length < 10;
          if (useDesktopText) {
            return {
              ...entry,
              parsed_text:    desktopText,
              parsed_length:  desktopText.length,
              parsed_preview: desktopText.slice(0, 200).trim(),
            };
          }
          return entry;
        });
      }

      setAttachments(prev => [...prev, ...uploaded.filter(u => !u.blocked)]);

      // ── Cache file bytes in browser for client-side preview ──
      // Match uploaded entries to original File objects by filename
      for (const entry of uploaded) {
        if (entry.blocked) continue;
        const originalFile = docFiles.find(f => f.name === entry.file_name);
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
      setUploadPhase("uploading");
    }
  }

  function removeAttachment(id) {
    setAttachments(prev => prev.filter(a => a.id !== id));
  }

  // ── Image attachment helpers ────────────────────────────────

  const addImageFiles = useCallback((files) => {
    const validFiles = (files || []).filter(file => {
      if (!IMAGE_MIME_TYPES.includes(file.type)) {
        toast.error(`Unsupported format for "${file.name}". Use JPEG, PNG, GIF, or WebP.`);
        return false;
      }
      if (file.size > IMAGE_MAX_BYTES) {
        toast.error(`"${file.name}" is too large. Maximum size is 10 MB.`);
        return false;
      }
      return true;
    });
    if (!validFiles.length) return 0;

    let added = 0;
    setImageFiles(prev => {
      const remaining = MAX_IMAGES - prev.length;
      if (remaining <= 0) {
        toast.error(`You can attach up to ${MAX_IMAGES} images.`);
        return prev;
      }
      const toAdd = validFiles.slice(0, remaining);
      added = toAdd.length;
      if (validFiles.length > remaining) {
        toast.error(`You can attach up to ${MAX_IMAGES} images. Only ${remaining} more allowed.`);
      }
      return [...prev, ...toAdd.map(file => ({ id: crypto.randomUUID(), file, previewUrl: URL.createObjectURL(file) }))];
    });
    return added;
  }, [toast]);

  function handleImageSelect(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    addImageFiles(files);
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
     addImageFiles([file]);
   }, [addImageFiles]);

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
    // Word / OpenDocument Text
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
    // Excel / OpenDocument Spreadsheet (xlsx, xlsm, xls, ods)
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/vnd.oasis.opendocument.spreadsheet",
    // PowerPoint (pptx, ppt)
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    // Text / data / config formats
    "text/csv", "application/csv",
    "text/tab-separated-values",
    "text/html", "text/plain", "application/json",
    "text/rtf", "application/rtf",
    "application/xml", "text/xml",
    "text/markdown",
    "image/svg+xml",
    // Images
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
  ];

  const { isDragging, dropRef } = useFileDrop({
    accept: ACCEPTED_DROP_MIME_TYPES,
    onFiles: (validFiles, invalidFiles) => {
      if (invalidFiles && invalidFiles.length > 0) {
        const names = invalidFiles.map(f => f.name).join(", ");
        setDropError(`Unsupported file type. Accepted: PDF, DOCX, XLSX, CSV, TXT, HTML, JSON, XML, images. Skipped: ${names}`);
        if (validFiles.length === 0) return;
      }

      const images = validFiles.filter(f => IMAGE_MIME_TYPES.includes(f.type));
      const docs   = validFiles.filter(f => !IMAGE_MIME_TYPES.includes(f.type));

      if (images.length > 0) addImageFiles(images);

      // Handle documents via existing upload flow
      if (docs.length > 0) {
        handleFileUpload({ target: { files: docs } });
      }
    },
    disabled: loading,
  });

  // ── @AgentName mention detection ───────────────────────────
  // Messages starting with @AiNxt or @AgentName are routed
  // to the agent runner instead of the /ask orchestrator.

  function parseMention(text) {
    const m = text.match(/^@([\w\-]+)\s+([\s\S]+)/i);
    if (!m) return null;
    return { agentName: m[1].toLowerCase().replace(/-/g, "_"), message: m[2].trim() };
  }

  // NOTE: the legacy client-side classifyIntent() local-model pre-classifier
  // (doc AND image intent) was REMOVED. Both document and image generation
  // requests now route entirely through the backend CIL inside /ask — see
  // the routing block in sendMessage() below. Removing this closes the gap
  // where an attached image's Vision description/caption (persisted via the
  // /chat/upload pre-upload) was never read because this classifier's
  // is_image=true branch called /chat/image-generate directly and returned
  // before /ask (and therefore the backend CIL) ever ran.
  //
  // NOTE: the client-side submitDocJob()/`/docs/generate` path was REMOVED.
  // Document requests now route through the normal /ask call: the backend
  // classifies intent on the small local model and returns {route:"doc", …},
  // which the streaming handler intercepts to mount the DOCJOB marker. The
  // /docs/generate REST endpoint still exists for other callers.

  // ── Send message ───────────────────────────────────────────
  // SYNC WITH KbChat.jsx sendMessage — any change here must also be
  // applied to KbChat.jsx::sendMessage. Both files duplicate this ~700
  // line streaming pipeline verbatim. Until the duplication is
  // extracted into a useChatSend hook, treat the two implementations
  // as a single source — diffs between them are bugs.
  async function sendMessage() {
    if (!input.trim() || inputDisabled) return;

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

    // Fresh turn → clear any leftover cancellation flag from a previous Stop
    // so this new request isn't spuriously aborted mid-flight.
    cancelledChatsRef.current[chatId] = false;

    const question = input;
    // Note: the legacy `/image <prompt>` slash command has been removed.
    // Image-generation requests are now detected by the same local-LLM
    // intent classifier that routes document-generation requests
    // (see classifyIntent() and the routing block further down).

    const assistantId = crypto.randomUUID();
    // Pre-generated so the optimistic bubble and the final one share an id.
    const userMsgId   = crypto.randomUUID();
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

    // Content markers so the message loader can classify persisted
    // attachment_ids after a refresh: 📎 for documents, 🖼 for images. The
    // marker text is stripped from the displayed bubble (chips/thumbnails
    // replace it) — it exists purely to survive round-tripping through the DB.
    //
    // Computed HERE (before the pre-upload below) rather than after it: this
    // depends only on pendingAttachments/pendingImages, both already
    // snapshotted, so there is nothing to wait for.
    let userContent = question;
    if (pendingAttachments.length > 0) {
      userContent = `${userContent}\n\n📎 ${pendingAttachments.map(a => a.file_name).join(", ")}`;
    } else if (pendingImages.length > 0) {
      const n = pendingImages.length;
      userContent = `${userContent}\n\n🖼 ${n} image${n !== 1 ? "s" : ""}`;
    }

    // ── Optimistic turn render (BEFORE the upload round-trip) ──────────────
    // setImageFiles([]) above already cleared the composer thumbnail, but the
    // user bubble used to be appended only AFTER `await /chat/upload` resolved
    // further down. For the whole upload that left the image nowhere on screen:
    // gone from the composer, not yet in the transcript — so a large image on a
    // slow link looked like the app had swallowed it.
    //
    // Render the turn immediately using the local blob previewUrls (already
    // available synchronously from addImageFiles) and client-side attachment
    // ids. The existing `updateMessages(newMessages)` below then replaces this
    // array with the final one carrying SERVER-assigned attachment ids. Both
    // use the same userMsgId/assistantId, so React reconciles in place and the
    // swap is invisible — no flicker, no duplicate bubble (baseMessages is a
    // pre-send snapshot, so the final array cannot re-append these).
    updateMessages([
      ...baseMessages,
      {
        id: userMsgId, role: "user", content: userContent, streaming: false,
        // undefined (never []) when there is nothing attached — an empty array
        // is truthy in JS, so `m.attachments && ...` render guards would emit an
        // empty chip row. Matches the final message's shape below exactly.
        attachments: (pendingAttachments.length + pendingImages.length) > 0
          ? [
              ...pendingAttachments.map(a => ({
                id: a.id, file_name: a.file_name, file_type: a.file_type,
                file_size: a.file_size, kind: "doc",
              })),
              ...pendingImages.map(i => ({
                id: i.id, file_name: i.file.name, file_type: i.file.type,
                file_size: i.file.size, kind: "image",
              })),
            ]
          : undefined,
        imageUrls: pendingImages.map(i => i.previewUrl),
      },
      { id: assistantId, role: "assistant", content: "", streaming: true,
        spinnerStage: ragMode === "off" ? 0 : 1,
        streamStartAt: Date.now(), liveOutTok: 0,
        // Images are uploaded before the turn starts, so name the phase the
        // user is actually waiting on instead of a generic "Thinking…".
        statusLine: pendingImages.length > 0 ? "Uploading image…" : "Thinking…",
        tokenUsage: null, costUsd: null, modelLabel: null, latency: null,
        inTok: null, outTok: null },
    ]);

    // ── Pre-upload image attachments via /chat/upload ─────────────────────
    // Images are uploaded to the server before the chat turn so that:
    //   1. /ask receives attachment_ids (not raw bytes) — same path as docs.
    //   2. The backend CIL can fetch the vision description from parsed_text
    //      and inject it into the prompt context.
    //   3. Image-intent routing in /ask works correctly (generate vs. analyse).
    // Previously images went to /ask/image (multipart) which only ran vision
    // analysis and had no CIL routing — causing "improve this UI" to get stuck.
    let uploadedImageMeta = []; // [{id, file_name, file_type, file_size}] from server
    if (pendingImages.length > 0) {
      try {
        const imgFd = new FormData();
        imgFd.append("chat_id", activeChatId);
        pendingImages.forEach(({ file, id: clientId }) => {
          imgFd.append("files", file);
          // Cache bytes in the browser preview store keyed by the client UUID.
          // ImageChip uses this cache to show thumbnails without a server round-trip.
          // We'll remap to the server-assigned ID below after the upload response.
          cacheStore(clientId, file, file.type || "image/png");
        });
        const uploadResp = await authFetch(`${API}/chat/upload`, {
          method: "POST",
          body:   imgFd,
        });
        if (uploadResp.ok) {
          const uploadData = await uploadResp.json();
          const serverUploaded = (uploadData.uploaded || []).filter(u => !u.blocked);
          // Re-cache bytes under the server-assigned ID so ImageChip can look
          // them up by the ID that will be stored in ChatMessage.attachment_ids.
          serverUploaded.forEach((u, idx) => {
            const orig = pendingImages[idx];
            if (orig) cacheStore(u.id, orig.file, orig.file.type || "image/png");
          });
          uploadedImageMeta = serverUploaded.map(u => ({
            id:        u.id,
            file_name: u.file_name,
            file_type: u.file_type || "image",
            file_size: u.file_size || 0,
          }));
        }
      } catch (_imgUploadErr) {
        // Non-fatal: proceed without server IDs. The turn will still work —
        // /ask just won't have the vision description in the prompt context.
        console.warn("[Chat] image pre-upload failed (non-fatal):", _imgUploadErr);
      }
    }

    // (userContent was computed above, before the pre-upload, so the optimistic
    // turn could render immediately.)

    // Attachment metadata (kind:"image"/"doc") — the stable ids round-trip
    // via attachment_ids so chips/thumbnails rehydrate from cache on refresh.
    // Use server-assigned IDs for images (from the pre-upload above) so the
    // IDs match what /ask stores in ChatMessage.attachment_ids.
    const imageAttachments = uploadedImageMeta.length > 0
      ? uploadedImageMeta.map(u => ({ ...u, kind: "image" }))
      : pendingImages.map(i => ({
          id:        i.id,
          file_name: i.file.name,
          file_type: i.file.type,
          file_size: i.file.size,
          kind:      "image",
        }));
    const docAttachments = pendingAttachments.map(a => ({
      id:         a.id,
      file_name:  a.file_name,
      file_type:  a.file_type,
      file_size:  a.file_size,
      parsed_text: a.parsed_text || "",
      kind:       "doc",
    }));
    const allAttachments = [...docAttachments, ...imageAttachments];

    const newMessages = [
      ...baseMessages,
      {
        // Same id as the optimistic bubble above — React updates it in place.
        id: userMsgId, role: "user", content: userContent, streaming: false,
        attachments: allAttachments.length > 0 ? allAttachments : undefined,
        // Live-turn thumbnails (blob URLs revoked in finally; refresh uses ImageChip).
        imageUrls: pendingImages.map(i => i.previewUrl),
      },
      { id: assistantId,         role: "assistant", content: "",          streaming: true,
        // spinnerStage 0=Understanding, 1=Searching (RAG), 2=Tools, 3=Generating
        spinnerStage: ragMode === "off" ? 0 : 1,
        // Live status-line clock anchor + running output-token estimate.
        // Seed statusLine so we show "Thinking…" immediately instead of the
        // generic timer-driven "Understanding" flash before the first SSE.
        streamStartAt: Date.now(), liveOutTok: 0, statusLine: "Thinking…",
        tokenUsage: null, costUsd: null, modelLabel: null, latency: null,
        inTok: null, outTok: null,
        tokensToday: null, maxTokensToday: null,
        requestsToday: null, maxRequestsToday: null },
    ];

    // Auto-title: use first user question as chat name, but ONLY when the
    // user has not manually set a title. titleEditedByUser is set to true
    // by saveRename() whenever the user explicitly renames the chat.
    if (baseMessages.length === 0) {
      const _curChat = chats.find(c => c.id === activeChatId);
      if (!_curChat?.titleEditedByUser) {
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
        // User set a custom title — preserve it; just append messages
        setChats(prev =>
          prev.map(chat =>
            chat.id === activeChatId
              ? { ...chat, messages: newMessages, updatedAt: Date.now() }
              : chat
          )
        );
      }
    } else {
      updateMessages(newMessages);
    }

    setTimeout(() => {
      containerRef.current?.scrollTo({ top: containerRef.current.scrollHeight, behavior: "smooth" });
    }, 100);

    // ── DOC + IMAGE ROUTING ARE NOW BOTH BACKEND-AUTHORITATIVE ───────────────
    // We NO LONGER classify document OR image intent on the client (no regex,
    // no client classifier gate, no early return before /ask). EVERY prompt —
    // including "improve this image and generate a new image" with an
    // attached image — is now sent straight to /ask with attachment_ids. The
    // backend CIL is the ONLY code path that (a) has attachment_ids to
    // resolve the just-uploaded image's Vision description/caption/
    // parsed_text off the ChatAttachment row and (b) can decide whether the
    // user wants a NEW image (img_intent="generate") vs. just wants text
    // analysis (img_intent="none"). It returns {route:"doc", …} or
    // {route:"image", prompt, …} as JSON, which the streaming path below
    // intercepts and, for images, forwards to /chat/image-generate.
    //
    // Previously a client-side classifyIntent() shortcut ran here BEFORE
    // /ask: it matched image-flavoured questions via a keyword regex
    // (_maybeImage) and, when the classifier said is_image=true, called
    // /chat/image-generate directly with a bare text prompt and then
    // `return`ed — completely bypassing /ask and the enriched, attachment-
    // aware prompt the backend CIL builds. That meant the
    // image_description/image_caption/parsed_text already persisted on the
    // ChatAttachment row (from the /chat/upload pre-upload above) was
    // uploaded but never read anywhere: the image was generated from
    // generic text with zero grounding in the original — the "generated
    // but a bit irrelevant" symptom. Removing this shortcut and always
    // routing through /ask closes that gap; the "selected model can't
    // generate images" guard now lives in the {route:"image"} handler
    // below instead.

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
      // Backend clamps to the 4–8 s product window (routers/chat_router.py
      // _VEO_MIN_DURATION/_VEO_MAX_DURATION, sourced from cil/intent.py);
      // mirror the same range here so the clamp happens client-side too.
      const VEO_DEFAULT_DURATION = 8;
      const VEO_MIN_DURATION = 4;
      const VEO_MAX_DURATION = 8;
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
      {
        // All turns (text-only, doc-attachment, image-attachment) now go through
        // /ask with JSON. Images were pre-uploaded above; their server-assigned
        // IDs are in uploadedImageMeta. /ask injects the vision description from
        // parsed_text into the prompt and runs the full CIL pipeline.
        const body = {
          question,
          chat_id:        activeChatId,
          attachment_ids: [
                      ...pendingAttachments.map(a => a.id),
                      ...uploadedImageMeta.map(u => u.id),
                    ],
          rag_mode:       "off",
        };
        if (selectedModel !== "auto") {
          // local:model-name → send as model_hint="local" + local_model=name
          if (selectedModel.startsWith("local:")) {
            body.model       = "local";
            body.local_model = selectedModel.slice(6);
          } else {
            // Send the FULL concrete model ID (e.g. "claude-sonnet-4-6"), not the
            // short alias (e.g. "claude"). The dropdown `value` is the alias
            // (m.id) but each option also carries `modelId` (the full ID). The
            // gateway uses this verbatim as the CLI session model so the user's
            // exact pick answers — the alias would map to a generic default.
            const _opt = MODEL_OPTIONS.find(o => o.value === selectedModel);
            body.model = (_opt && _opt.modelId) ? _opt.modelId : selectedModel;
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

        response = await authFetch(`${API}/ask`, {
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

      // ── Backend doc-intent routing signal ────────────────────────────────
      // The small local model on the backend classified this prompt as a
      // DOCUMENT request and already enqueued the doc-skills job.
      // The response is JSON ({route:"doc", job_id, format, filename_hint})
      // instead of an SSE token stream — mount the DOCJOB marker so the live
      // generation updates + download button render (no prose in chat).
      const _ct = response.headers.get("Content-Type") || "";
      if (_ct.includes("application/json")) {
        let _routed = null;
        try { _routed = await response.json(); } catch { /* not JSON after all */ }
        if (_routed && _routed.route === "doc" && _routed.job_id) {
          clearTimeout(streamTimeout);
          const _docJobs = Array.isArray(_routed.jobs) && _routed.jobs.length
            ? _routed.jobs
            : [{
                job_id:        _routed.job_id,
                format:        _routed.format,
                filename_hint: _routed.filename_hint,
              }];
          console.info(
            `[docgen] client enqueued ${_docJobs.length} doc job(s) chat=${chatId} ` +
            `jobs=[${_docJobs.map(j => `job=${j.job_id}:fmt=${j.format}`).join(", ")}]`
          );
          _docJobs.forEach(j => setDocJobState(j.job_id, "checking", chatId));
          const _combinedContent = _docJobs
            .map(j => buildDocJobMarker(
              j.job_id, j.format,
              j.filename_hint || `document.${j.format}`))
            .join("");
          updateMessages(newMessages.map(m =>
            m.id === assistantId
              ? {
                  ...m,
                  content: _combinedContent,
                  streaming: false,
                  docStage: undefined, docFormat: undefined, spinnerStage: undefined,
                  timestamp: Date.now(),
                }
              : m
          ));
          setLoading(false, chatId);
          return;
        }

        // ── Backend image-intent routing signal ────────────────────────────
        // The CIL on the backend classified this prompt as an image-generation
        // request and built an enriched prompt (with Vision description /
        // caption / parsed_text of any uploaded image, pulled from the
        // ChatAttachment row created by the /chat/upload pre-upload above,
        // and/or recent chat context). Call /chat/image-generate with that
        // enriched prompt — same underlying call as the toolbar shortcut,
        // so the image renders identically once generated.
        if (_routed && _routed.route === "image" && _routed.prompt) {
          clearTimeout(streamTimeout);
          // Image generation has exactly ONE backing model on this platform:
          // gemini-3.1-flash-image (see ai-ui/src/utils/imageGenerate.js).
          // If the user has explicitly forced a different chat model, honour
          // that choice with a clear message instead of silently generating
          // via gemini anyway. Mirrors the guard the old client-side
          // classifyIntent() shortcut used to apply before it was removed.
          const _selRouted = String(selectedModel || "").trim().toLowerCase();
          const _canMakeImageRouted = (_selRouted === "auto" || _selRouted === MODEL_IMAGE.toLowerCase());
          if (!_canMakeImageRouted) {
            updateMessages(newMessages.map(m =>
              m.id === assistantId
                ? {
                    ...m,
                    content:    `The selected model cannot generate images. Please switch to **Auto** or the image model (${MODEL_IMAGE}) to use image generation.`,
                    streaming:  false,
                    timestamp:  Date.now(),
                    docStage:   undefined,
                    docFormat:  undefined,
                    spinnerStage: undefined,
                  }
                : m
            ));
            setLoading(false, chatId);
            return;
          }
          updateMessages(newMessages.map(m =>
            m.id === assistantId
              ? { ...m, content: "", streaming: true,
                  imageStage: "submitting",
                  docStage: undefined, docFormat: undefined, spinnerStage: null }
              : m
          ));
          setImageGenerating(true);
          const _imgRouteTimer = setTimeout(() => {
            setChats(prev => prev.map(c => {
              if (c.id !== chatId) return c;
              let changed = false;
              const nextMsgs = c.messages.map(m => {
                if (m.id === assistantId && m.imageStage === "submitting") {
                  changed = true;
                  return { ...m, imageStage: "rendering" };
                }
                return m;
              });
              return changed ? { ...c, messages: nextMsgs } : c;
            }));
          }, 2500);
          try {
            const { md, artifacts, modelLabel: imgModel, costUsd: imgCost,
                    inTok: imgIn, outTok: imgOut, tokenUsage: imgTok,
                    latencySec: imgLatency } = await generateImage({
              api: API, authFetch,
              prompt:        _routed.prompt,
              chatId:        _routed.chat_id || chatId,
              messageId:     assistantId,
              // Forward the original uploaded-image attachment_ids returned by
              // /ask so /chat/image-generate can persist them on the user
              // ChatMessage row. This lets the L2-img block in gateway.py inject
              // the image caption on follow-up turns ("explain the image I attached").
              attachmentIds: Array.isArray(_routed.attachment_ids) ? _routed.attachment_ids : [],
              // The user's original question before backend enrichment (e.g.
              // "improve this image"). /chat/image-generate stores this as the
              // user message content so history shows the original phrasing,
              // not the long "Reference image description: …" enriched prompt.
              originalQuestion: _routed.original_question || question,
            });
            clearTimeout(_imgRouteTimer);
            updateMessages(newMessages.map(m =>
              m.id === assistantId
                ? {
                    ...m,
                    content:      md,
                    streaming:    false,
                    imageStage:   undefined,
                    artifacts:    artifacts || [],
                    modelLabel:   imgModel || MODEL_IMAGE,
                    costUsd:      imgCost  ?? null,
                    inTok:        imgIn    ?? null,
                    outTok:       imgOut   ?? null,
                    tokenUsage:   imgTok   ?? null,
                    latency:      imgLatency != null ? imgLatency * 1000 : null,
                    timestamp:    Date.now(),
                  }
                : m
            ));
          } catch (imgErr) {
            clearTimeout(_imgRouteTimer);
            const friendly = imgErr?.unavailable
              ? "Image generation model not available right now — please try again later."
              : `Image generation failed: ${imgErr?.message || imgErr}`;
            updateMessages(newMessages.map(m =>
              m.id === assistantId
                ? { ...m, content: friendly, streaming: false, imageStage: undefined }
                : m
            ));
          } finally {
            setImageGenerating(false);
            setLoading(false, chatId);
          }
          return;
        }

        // ── Backend video-intent routing signal ────────────────────────────
        // The CIL on the backend classified this prompt as a video-generation
        // request and returned {route:"video", prompt, aspect_ratio,
        // duration_secs, chat_id} as JSON instead of an SSE token stream.
        // Mirror the same Veo flow used when selectedModel === "veo-3.1-generate-preview"
        // so the video renders identically regardless of how it was triggered.
        //
        // Root cause of the "ReadableStream locked" error: without this block
        // the code fell through to `response.body.getReader()` after
        // `response.json()` had already consumed and locked the stream.
        if (_routed && _routed.route === "video" && _routed.prompt) {
          clearTimeout(streamTimeout);
          const VEO_RENDER_STAGE_DELAY_MS = 4000;
          updateMessages(newMessages.map(m =>
            m.id === assistantId
              ? { ...m, content: "", streaming: true,
                  videoStage: "submitting",
                  docStage: undefined, docFormat: undefined, spinnerStage: null }
              : m
          ));
          const _vidRouteStageTimer = setTimeout(() => {
            setChats(prev => prev.map(c => {
              if (c.id !== chatId) return c;
              let changed = false;
              const nextMsgs = c.messages.map(m => {
                if (m.id === assistantId && m.videoStage === "submitting") {
                  changed = true;
                  return { ...m, videoStage: "rendering" };
                }
                return m;
              });
              return changed ? { ...c, messages: nextMsgs } : c;
            }));
          }, VEO_RENDER_STAGE_DELAY_MS);
          try {
            const vbody = {
              prompt:            _routed.prompt,
              chat_id:           _routed.chat_id || chatId,
              aspect_ratio:      _routed.aspect_ratio  || "16:9",
              duration_secs:     _routed.duration_secs || 8,
              // The user's original question before backend enrichment (e.g.
              // "generate a video from this image"). /chat/video-generate stores
              // this as the user message content so history shows the original
              // phrasing, not the long "Reference image description: …" prompt.
              original_question: _routed.original_question || question,
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
            const vdata = await vres.json();
            const vurl  = `${API}${vdata.url}`;
            const vidMsg = {
              id:         assistantId,
              role:       "assistant",
              content:    `🎬 Video generated (${vdata.duration}s, ${vdata.mime}). Click play to preview below.`,
              videoUrl:   vurl,
              videoMime:  vdata.mime || "video/mp4",
              streaming:  false,
              timestamp:  Date.now(),
              modelLabel: vdata.model || null,
              tokenUsage: typeof vdata.total_tokens === "number" ? vdata.total_tokens : 0,
              inTok:      typeof vdata.input_tokens  === "number" ? vdata.input_tokens  : 0,
              outTok:     typeof vdata.output_tokens === "number" ? vdata.output_tokens : 0,
              costUsd:    typeof vdata.cost_usd      === "number" ? vdata.cost_usd      : null,
              latency:    typeof vdata.latency_ms    === "number" ? vdata.latency_ms / 1000 : null,
              meta: {
                model:    vdata.model,
                cost:     vdata.cost_usd,
                duration: vdata.duration,
                endpoint: vdata.endpoint || "/chat/video-generate",
              },
            };
            updateMessages(newMessages.map(m => m.id === assistantId ? vidMsg : m));
          } catch (vidErr) {
            updateMessages(newMessages.map(m =>
              m.id === assistantId
                ? { ...m, content: `⚠ Video generation failed: ${vidErr.message}`,
                    streaming: false, videoStage: undefined }
                : m
            ));
          } finally {
            clearTimeout(_vidRouteStageTimer);
            setLoading(false, chatId);
          }
          return;
        }

        // Any other JSON here is unexpected for /ask — fall through to error.
      }

      if (!response.body) throw new Error("No response body");

      // Capture the server-assigned request_id so stopGeneration() can signal
      // the backend to cancel the in-progress generation cooperatively.
      const responseRequestId = response.headers.get("X-Request-ID") || null;
      requestIdMapRef.current[chatId] = responseRequestId;

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
      let accumulated = "";
      let sseBuffer   = "";

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
              // Live status line (Phase 1.4). Backend narrates the current
              // phase ("Thinking…", "Reading sources…", "Generating
              // response…"). Store it on the message so the clean spinner
              // shows a meaningful label instead of timer-driven guesses.
              const _statusText = obj.status;
              updateMessages(
                  newMessages.map(msg =>
                      msg.id === assistantId ? { ...msg, statusLine: _statusText } : msg
                  )
              );
            } else if (obj.context !== undefined) {
              // Live context-window telemetry (Phase 2). Drives the meter in
              // the composer footer. Backward-compatible: only new backends
              // emit this key.
              setContextInfo(obj.context);
            } else if (obj.compaction !== undefined) {
              // History was summarized to stay within the model window. Pin an
              // inline notice on the current assistant message so the user
              // understands why older turns may be paraphrased.
              const _cmsg = obj.compaction?.message
                || "Earlier messages were summarized to keep the conversation within context.";
              updateMessages(
                  newMessages.map(msg =>
                      msg.id === assistantId ? { ...msg, compactionNotice: _cmsg } : msg
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
              // Live output-token estimate (~4 chars/token, matching backend)
              // so the status line can show "· out Nt" while streaming.
              const _liveOutTok = Math.ceil(stripMemoryTag(accumulated).length / 4);
              updateMessages(
                  newMessages.map(msg =>
                      msg.id === assistantId ? { ...msg, content: stripMemoryTag(accumulated), spinnerStage: 3, liveOutTok: _liveOutTok } : msg
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
            }
          } catch { /* ignore malformed events */ }
        }
      }

      // Final strip of any <!--MEMORY:{...}--> footer
      const cleanAccumulated = stripMemoryTag(accumulated);
      // Phase 3.2: if the model chose to persist a memory this turn, surface a
      // subtle "Memory updated" chip on the message.
      const _memStored = parseMemoryTag(accumulated);

      updateMessages(
        newMessages.map(msg =>
          msg.id === assistantId
            ? {
                ...msg,
                content:          cleanAccumulated,
                streaming:        false,
                statusLine:       null,
                memoryStored:     _memStored,
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
                requestId: responseRequestId,
                // Replace the client-temp id with the persisted server id
                // (used by Continue / Edit / Regenerate endpoints).
                id: serverMessageId || msg.id,
              }
            : msg
        )
      );
      if (responseRequestId) {
        attachCoachHits(chatId, serverMessageId || assistantId, responseRequestId);
      }
      fetchBudget();   // refresh remaining budget after reply

      // ── Metadata backfill from /chats/{id}/messages (fire-and-forget) ──
      // Primary source is __meta__ SSE event. This is the fallback for when
      // __meta__ is dropped (e.g. Kafka blocks the SSE connection in UAT for
      // 20+ seconds, by which time the browser has closed the stream).
      //
      // Matching strategy — most reliable to least:
      //   1. serverMessageId from __meta__ (exact DB id match)
      //   2. User question text → find user msg in DB → take next assistant msg
      //   3. Last assistant message in DB (safe only after strategies 1+2 fail)
      //
      // Retry schedule: 3s → 8s → 20s → 40s
      //   - Local: message persists in ~1s, first attempt at 3s always succeeds
      //   - UAT:   Kafka takes ~20s, retries at 8s and 20s cover it
      try {
        // Snapshot values now — closures capture stale state after re-renders
        const _snapServerMsgId = serverMessageId;   // from __meta__ (may be null)
        const _snapAssistantId = assistantId;        // client-side UUID
        const _snapQuestion    = question;           // user's question text
        const _snapChatId      = chatId;
        const _backfillDelays  = [3000, 8000, 20000, 40000];
        let _backfillDone = false;

        const _doBackfill = (attempt = 0) => {
          if (_backfillDone) return;
          console.log(`[backfill] attempt ${attempt + 1}/${_backfillDelays.length} for chat`, _snapChatId);
          authFetch(`${API}/chats/${_snapChatId}/messages`)
            .then(r => r.ok ? r.json() : null)
            .then(data => {
              const _msgs = data?.messages;
              console.log(`[backfill] API returned ${_msgs?.length ?? 0} messages`, _msgs?.map(m => `${m.role}:${m.id?.slice(0,8)} model=${m.model_used} in_tok=${m.in_tok}`));
              if (!_msgs?.length) {
                if (attempt + 1 < _backfillDelays.length)
                  setTimeout(() => _doBackfill(attempt + 1), _backfillDelays[attempt + 1]);
                return;
              }

              let _serverMsg = null;

              // Strategy 1: exact match by server-assigned message ID
              if (_snapServerMsgId) {
                _serverMsg = _msgs.find(m => m.id === _snapServerMsgId) || null;
                console.log("[backfill] strategy1 (serverMsgId):", _snapServerMsgId, "→", _serverMsg?.id);
              }

              // Strategy 2: find user message by question text → take the
              // assistant message immediately after it in the ordered list.
              // The user message is persisted synchronously (no Kafka needed)
              // so it's always in the DB even when the assistant row isn't yet.
              if (!_serverMsg && _snapQuestion) {
                const _qSnap = _snapQuestion.trim().slice(0, 60);
                const _userIdx = _msgs.findIndex(
                  m => m.role === "user" && (m.content || "").includes(_qSnap)
                );
                console.log("[backfill] strategy2 (question match):", JSON.stringify(_qSnap), "→ userIdx:", _userIdx);
                if (_userIdx !== -1) {
                  _serverMsg = _msgs.slice(_userIdx + 1).find(m => m.role === "assistant") || null;
                  console.log("[backfill] strategy2 assistant after user:", _serverMsg?.id);
                }
              }

              // Strategy 3: last assistant message in DB
              if (!_serverMsg) {
                _serverMsg = [..._msgs].reverse().find(m => m.role === "assistant") || null;
                console.log("[backfill] strategy3 (last assistant):", _serverMsg?.id);
              }

              if (!_serverMsg) {
                console.log("[backfill] no assistant message found, retrying...");
                if (attempt + 1 < _backfillDelays.length)
                  setTimeout(() => _doBackfill(attempt + 1), _backfillDelays[attempt + 1]);
                return;
              }

              // Row found but metadata not written yet (Kafka still in flight) — retry
              console.log("[backfill] server msg metadata check: model_used=", _serverMsg.model_used, "in_tok=", _serverMsg.in_tok, "latency=", _serverMsg.latency);
              if (!_serverMsg.model_used && _serverMsg.in_tok == null && !_serverMsg.latency) {
                console.log("[backfill] metadata not yet written, retrying...");
                if (attempt + 1 < _backfillDelays.length)
                  setTimeout(() => _doBackfill(attempt + 1), _backfillDelays[attempt + 1]);
                return;
              }

              _backfillDone = true;
              console.log("[backfill] found server msg:", _serverMsg.id, "model:", _serverMsg.model_used, "in_tok:", _serverMsg.in_tok, "latency:", _serverMsg.latency);

              // Patch the client-side message — only fill fields still null/undefined
              // so __meta__ values (if received) are never overwritten
              setChats(prev => prev.map(c => {
                if (c.id !== _snapChatId) return c;
                console.log("[backfill] scanning", c.messages.length, "client messages. looking for assistantId=", _snapAssistantId, "serverMsgId=", _snapServerMsgId);
                return {
                  ...c,
                  messages: c.messages.map(m => {
                    // Match by client assistantId OR server-assigned ID
                    if (m.id !== _snapAssistantId && m.id !== _snapServerMsgId) return m;
                    if (m.role !== "assistant") return m;
                    console.log("[backfill] ✅ matched message id=", m.id, "patching metadata");
                    return {
                      ...m,
                      modelLabel: m.modelLabel || _serverMsg.model_used  || null,
                      inTok:      m.inTok      ?? _serverMsg.in_tok      ?? null,
                      outTok:     m.outTok     ?? _serverMsg.out_tok     ?? null,
                      tokenUsage: m.tokenUsage ?? _serverMsg.tokens_used ?? null,
                      costUsd:    m.costUsd    ?? _serverMsg.cost_usd    ?? null,
                      latency:    m.latency    ?? _serverMsg.latency     ?? null,
                      coverageTrace: m.coverageTrace ?? _serverMsg.coverage_trace ?? null,
                    };
                  }),
                };
              }));
            })
            .catch(() => {
              if (attempt + 1 < _backfillDelays.length)
                setTimeout(() => _doBackfill(attempt + 1), _backfillDelays[attempt + 1]);
            });
        };

        // Only run backfill if __meta__ didn't populate the metadata.
        // If modelLabel is already set (from __meta__), skip entirely.
        const _needsBackfill = !modelLabel && !inTok && !latency;
        if (_needsBackfill) {
          console.log("[backfill] __meta__ missing metadata, starting backfill for chat", _snapChatId);
          setTimeout(() => _doBackfill(0), _backfillDelays[0]);
        }
      } catch { /* ignore */ }

      // ── Artifact extraction (fire-and-forget) ───────────────────
      try {
        const _aid = serverMessageId || assistantId;
        maybeExtractArtifacts(_aid, accumulated);
      } catch { /* ignore */ }

      // ── Auto follow-up chips (fire-and-forget) ───────────────────
      // After every assistant turn, ask the backend for 2-3 short
      // follow-ups and attach them to the message. Failure is silent.
      try {
        if (accumulated && accumulated.length > 30) {
          authFetch(`${API}/chat/followups`, {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ question, answer: accumulated }),
          })
              .then(r => r.ok ? r.json() : { followups: [] })
              .then(data => {
                const fus = Array.isArray(data?.followups) ? data.followups : [];
                if (!fus.length) return;
                setChats(prev => prev.map(c =>
                    c.id === activeChatId
                        ? {
                          ...c,
                          messages: c.messages.map(m =>
                              m.id === assistantId ? { ...m, followups: fus } : m
                          ),
                        }
                        : c
                ));
              })
              .catch(() => {});
        }
      } catch { /* ignore */ }

      // ── LLM-based auto-title (first turn only) ─────────────────
      // Runs once per chat after the first Q+A completes, but only when the
      // user has NOT manually renamed the chat (titleEditedByUser flag).
      // This is the generic guard — no hardcoded placeholder strings needed.
      try {
        const cur = chats.find(c => c.id === activeChatId);
        if (!cur?.titleEditedByUser && accumulated && accumulated.length > 20) {
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
        // Replace placeholder assistant message with a styled error card
        // (Phase 1.6). The `error` flag drives ErrorCard + Retry in the
        // message render; content stays empty so no raw "Error:" text shows.
        updateMessages(
          newMessages.map(msg =>
            msg.id === assistantId
              ? { ...msg, content: "", streaming: false, statusLine: null,
                  error: (err?.message || "Failed to get response") }
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

  async function handleEnhance() {
    if (!input.trim() || enhancing || loading) return;
    setEnhancing(true);
    try {
      const res = await authFetch(`${API}/enhance`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: input.trim() }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setEnhancerEdited(data.enhanced || input);
      setFollowupQs(data.followups || []);
      setFollowupAnswers({});
      setEnhancerModal(true);
    } catch (e) {
      toast.error("Enhance failed");
      // toast({ title: "Enhance failed", description: e.message, variant: "destructive" });
    } finally {
      setEnhancing(false);
    }
  }

  function applyEnhancement() {
     let final = enhancerEdited.trim();
     const contextLines = Object.entries(followupAnswers)
       .filter(([, v]) => v.trim())
       .map(([q, a]) => `- ${q}: ${a.trim()}`);
     if (contextLines.length > 0) final = `${final}\n\n## Context\n${contextLines.join("\n")}`;
     setInput(final);
     setEnhancerModal(false);
   }

  function stopGeneration() {
    // Only stop the stream that belongs to the *currently visible* chat.
    const cid = activeChatId;

    // 0. Mark this chat as cancelled for the remainder of the current turn.
    //    sendMessage() checks this flag after every await (classifier hop,
    //    doc-job submit, etc.) and bails before firing the *second* API call.
    //    Without this, a Stop clicked while the classifier (first /ask call)
    //    is in flight would still fall through and start response generation.
    cancelledChatsRef.current[cid] = true;

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

    setLoading(false, cid);
    setImageGenerating(false);
    // Mark any doc jobs still in-progress FOR THIS CHAT as timed-out (a
    // terminal state) so the composer unlocks immediately on Stop. Jobs in
    // OTHER chats are left untouched so background generation there continues.
    // Note: this only flips the local tracker; the DocDownloadButton's own
    // Cancel control tells the backend to abort the RQ job.
    setDocJobStatus(prev => {
      let changed = false;
      const next = {};
      for (const [jid, j] of Object.entries(prev)) {
        if (j && j.chatId === cid && DOC_ACTIVE_STATES.includes(j.status)) {
          console.info(`[docgen] client job=${jid} chat=${cid} status=${j.status} → timeout (user stop)`);
          next[jid] = { ...j, status: "timeout" };
          changed = true;
        } else {
          next[jid] = j;
        }
      }
      return changed ? next : prev;
    });
    // Functional update keyed by the stopped chat id (NOT activeChatId /
    // the stale `messages` closure) so this can't be clobbered by, and can't
    // clobber, the cancellation gate in sendMessage() that may fire on the
    // next tick when the aborted classifier promise settles. Also clears the
    // classify-phase spinner fields (docStage/imageStage/spinnerStage) so the
    // "Understanding" spinner is replaced by the cancelled banner immediately.
    setChats(prev => prev.map(chat =>
      chat.id === cid
        ? {
            ...chat,
            messages: chat.messages.map(msg =>
              msg.streaming
                ? { ...msg, streaming: false, cancelled: true,
                    docStage: undefined, docFormat: undefined,
                    imageStage: undefined, spinnerStage: undefined }
                : msg
            ),
            updatedAt: Date.now(),
          }
        : chat
    ));
  }

  // ── UI ─────────────────────────────────────────────────────

  return (
    <div className={`flex ${embedded ? "h-full" : "h-screen"} bg-white overflow-hidden`}>

      {/* ── LEFT PANEL: Chat list ── */}
      {!hideSidebar && (
      <div className="w-64 bg-gray-50 border-r border-gray-200 flex flex-col flex-shrink-0">

        {/* Header */}
        <div className="px-3 py-3 border-b border-gray-200 flex items-center justify-between">
          <span className="text-sm font-semibold  text-indigo-700">
            Chats
          </span>
          <button
            onClick={createNewChat}
            title="New chat"
            className="p-1.5 hover:bg-indigo-50 rounded-md text-indigo-700 hover:text-indigo-600 transition cursor-pointer"
          >
            <SquarePen size={14} />
          </button>
        </div>

        {/* Search */}
        <div className="px-3 py-2 border-b border-gray-100">
          <input
            placeholder="Search chats..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-md outline-none bg-white focus:border-indigo-300 shadow-sm"
          />
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto py-1">
          {/* Modern Skeleton: loading initial chat list */}
          {chatsLoading && <ChatListSkeleton />}

          {!chatsLoading && filteredChats.length === 0 && (
            <div className="px-4 py-6 text-xs text-gray-400 text-center">
              No chats found
            </div>
          )}

          {/* Pinned section label */}
          {!chatsLoading && filteredChats.some(c => c.pinned) && (
            <div className="px-3 pt-2 pb-1">
              <span className="text-[10px] font-semibold text-amber-400 uppercase tracking-wider flex items-center gap-1">
                <Pin size={9} /> Pinned
              </span>
            </div>
          )}

          {!chatsLoading && filteredChats.map((chat, idx) => {
            const prevPinned = idx > 0 && filteredChats[idx - 1].pinned;
            const showOtherLabel = !chat.pinned && prevPinned;
            return (
            <div key={chat.id}>
              {showOtherLabel && (
                <div className="px-3 pt-3 pb-1">
                  <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">Recent</span>
                </div>
              )}
              <div
                onClick={() => setActiveChatId(chat.id)}
                className={`
                  group relative flex items-center gap-2
                  px-3 py-2 mx-1 rounded-md cursor-pointer mb-0.5 transition
                  ${chat.id === activeChatId
                    ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-l-indigo-500"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-800"}
                `}
              >
                {editingId === chat.id ? (
                  <input
                    value={editingTitle}
                    autoFocus
                    onChange={e => setEditingTitle(e.target.value)}
                    onBlur={() => saveRename(chat.id)}
                    onKeyDown={e => { if (e.key === "Enter") saveRename(chat.id); }}
                    onClick={e => e.stopPropagation()}
                    className="bg-white border border-gray-300 rounded px-1 outline-none text-sm flex-1 min-w-0"
                  />
                ) : (
                  <>
                    <MessageSquare size={13} className="flex-shrink-0 text-gray-400" />
                    {/* Title takes full width — action buttons overlay on hover via absolute positioning */}
                    {/* color: "#f59e0b", */}
                    <span className="text-sm truncate flex-1 min-w-0 pr-1">
                      {chat.title}
                      {chat.pinned && (
                        <span title="Pinned" className="text-amber-400" style={{  marginLeft: 4, display: "inline-flex", verticalAlign: "middle" }}>
                          <Pin size={9} />
                        </span>
                      )}
                    </span>
                    {/* Action buttons — absolutely positioned over the right edge on hover */}
                    <div
                      className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition"
                      style={{ background: "inherit" }}
                    >
                      <button
                        onClick={e => { e.stopPropagation(); togglePin(chat.id); }}
                        title={chat.pinned ? "Unpin" : "Pin chat"}
                        className={`p-1 rounded transition  cursor-pointer ${chat.pinned ? "text-amber-600 hover:bg-amber-100" : "text-amber-600 hover:bg-amber-100"}`}
                      >
                        {chat.pinned ? <PinOff size={11} /> : <Pin size={11} />}
                      </button>
                      <button
                        onClick={e => { e.stopPropagation(); startRename(chat); }}
                        className="p-1 rounded text-indigo-700 hover:text-indigo-500 hover:bg-indigo-200 cursor-pointer"
                      >
                        <Pencil size={11} />
                      </button>
                      <button
                        onClick={e => { e.stopPropagation(); deleteChat(chat); }}
                        className="p-1 rounded text-red-500 hover:bg-red-100 cursor-pointer"
                      >
                        <Trash2 size={11} />
                      </button>
                    </div>
                  </>
                )}
              </div>
            </div>
            );
          })}
        </div>
      </div>
      )}

      {/* ── RIGHT PANEL: Active chat ── */}
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
              <h2 className="text-sm font-medium text-gray-800 truncate">
                {activeChat?.title || "New Chat"}
              </h2>
              <div className="flex items-center gap-2">
                {/* Memory + Voice are hidden in embedded (KB) mode — those
                    surfaces are scope-bound and the global memory/voice
                    affordances don't belong there. Export stays. */}
                {!embedded && (
                  <button
                      onClick={() => setMemoryOpen(true)}
                      title="Memories — what AiNxt remembers about you"
                      className="invisible cursor-pointer flex items-center gap-1.5 px-3 py-1.5 text-xs text-gray-500 hover:text-purple-600 hover:bg-purple-50 rounded-lg transition-colors border border-gray-200 hover:border-purple-200"
                  >
                    <Brain size={14} />
                    Memory
                  </button>
                )}
                {!embedded && (
                  <button
                    onClick={() => setVoiceModeActive(true)}
                    title="Voice conversation mode"
                    className="cursor-pointer flex items-center gap-1.5 px-3 py-1.5 text-xs brand-grad hover:opacity-70 rounded-sm text-white transition-colors "
                  >
                    <Headphones size={13} />
                    Voice
                  </button>
                )}
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

        {memoryOpen && !embedded && <MemoryPanel onClose={() => setMemoryOpen(false)} />}
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

          {/* ── Welcome screen: only when fully loaded and no messages ──
              In embedded (KB) mode we drop the generic greeting + suggestion
              chips in favour of a single muted scope-summary line so the
              user knows exactly what context they're chatting within. */}
          {!chatsLoading && !historyLoading && messages.length === 0 && embedded && (() => {
            const scope = {
              domain:       activeChat?.domain               || kbScope?.domain,
              product_name: kbScope?.product_name
                            || activeChat?._kb_scope_labels?.productName
                            || activeChat?.product_name,
              spec_version: activeChat?.spec_version         || kbScope?.spec_version,
              kb_doc_name:  kbScope?.kb_doc_name
                            || activeChat?._kb_scope_labels?.documentName,
            };
            const dash = "\u2014";
            const dom  = scope?.domain       || dash;
            const prod = scope?.product_name || dash;
            const ver  = scope?.spec_version || dash;
            const doc  = scope?.kb_doc_name;
            const path = doc
              ? `${dom} / ${prod} / ${ver} / ${doc}`
              : `${dom} / ${prod} / ${ver}`;
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
                    {path}
                  </p>
                </div>
              </div>
            );
          })()}

          {!chatsLoading && !historyLoading && messages.length === 0 && !embedded && (
            <div className="flex flex-col items-center justify-center h-full gap-4">
              <div className="w-16 h-16 rounded-full brand-grad-vivid flex items-center justify-center shadow-lg">
                <MessageSquare size={28} className="text-white" />
              </div>
              <div className="text-center">
                <p className="text-lg font-semibold text-gray-800">
                  Hey {getFirstName(user)}! 👋
                </p>
                <p className="text-sm text-gray-400 mt-1">
                  Ask me anything — I&apos;m here to help.
                </p>
              </div>
              <div className="flex flex-wrap gap-2 justify-center max-w-sm mt-2">
                {["What can you help me with?", "Explain this codebase", "Review my code"].map(q => (
                  <button
                    key={q}
                    onClick={() => setInput(q)}
                    className="px-3 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-full border border-gray-200 transition cursor-pointer"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {(Array.isArray(messages) ? messages : []).map(msg => {
            const Wrapper = msg.streaming ? "div" : motion.div;
            const wrapperProps = msg.streaming
              ? {}
              : { initial: { opacity: 0, y: 8 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.2 } };

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
                  {/* Inline PPT Chat Messages */}
                  <PPTChatMessageRenderer
                    msg={msg}
                    activeChatId={activeChatId}
                    setChats={setChats}
                    generateOutline={generateOutline}
                    confirmAndGenerate={confirmAndGenerate}
                    downloadPresentation={downloadPresentation}
                    pptState={pptState}
                    pptConversation={pptConversation}
                  />

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

                  {/* ── Structured tool-call cards (above the answer text) ──
                      Grouped into a collapsible ToolGroup (Phase 1.3) so a
                      multi-tool turn doesn't push the answer off-screen. */}
                  {msg.role === "assistant" && Array.isArray(msg.toolEvents) && msg.toolEvents.length > 0 && (
                      <ToolGroup toolEvents={msg.toolEvents} />
                  )}

                  {/* ── Context-compaction notice (Phase 2) ────────────── */}
                  {msg.role === "assistant" && msg.compactionNotice && (
                      <div className="flex items-start gap-2 my-2 px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-[11px] text-amber-700">
                        <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                        <span>{msg.compactionNotice}</span>
                      </div>
                  )}

                  {/* ── Error card + Retry (Phase 1.6) ─────────────────── */}
                  {msg.role === "assistant" && msg.error && !msg.streaming && (
                      <ErrorCard message={msg.error} onRetry={handleRetry} />
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
                              // Defer revocation so the browser can finish reading
                              // the (large) video blob
                              setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
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
                      const _parts = parseDocMarkers(_cleanContent);
                      const _hasDoc = _parts.some(p => p.type === "docjob" || p.type === "ppt" || p.type === "image");
                      const _body   = _hasDoc ? (
                        <div>
                          {_parts.map((part, i) => {
                            if (part.type === "docjob")
                              // two concurrent doc
                              // jobs no longer clobber a shared flag
                              return <DocDownloadButton
                                key={part.jobId || i}
                                jobId={part.jobId}
                                format={part.format}
                                filename={part.filename}
                                startedAt={docJobStatus[part.jobId]?.startedAt}
                                onStatusChange={s => setDocJobState(part.jobId, s, activeChatId)}
                              />;
                            if (part.type === "ppt")
                              return <PPTDownloadButton key={i} presentationId={part.id} format={part.format} title={part.title} />;
                            if (part.type === "image")
                              return (
                                <DownloadableImage
                                  key={i}
                                  src={`/ainxt/v1/api/chat/image/${part.imageId}`}
                                  filename={part.filename}
                                />
                              );
                            if (!part.value?.trim()) return null;
                            return (
                                <ReactMarkdown key={i} remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeHighlight, rehypeKatex]} urlTransform={mdUrlTransform} components={mdComponents}>
                                {part.value}
                              </ReactMarkdown>
                            );
                          })}
                        </div>
                      ) : (
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
                      {/* Strip the "📎 file1, file2" / "🖼 N images" marker line
                          from displayed text when attachment metadata is present —
                          the chips/thumbnails below replace it */}
                      <div className="whitespace-pre-wrap">{
                        msg.attachments?.length > 0
                          ? stripSystemPrefix(msg.content)?.replace(/\n\n(?:📎|🖼)\s*.+$/, "").trimEnd()
                          : stripSystemPrefix(msg.content)
                      }</div>
                      {/* Image attachments: thumbnails rehydrated from the browser
                          preview cache (survive refresh). Only shown when we don't
                          already have live imageUrls for this turn. */}
                      {!(msg.imageUrls?.length > 0) && msg.attachments?.some(a => a.kind === "image") && (
                        <div className="mb-2 mt-2 flex flex-wrap gap-2">
                          {msg.attachments.filter(a => a.kind === "image").map(a => (
                            <ImageChip key={a.id} attachment={a} />
                          ))}
                        </div>
                      )}
                      {/* Document attachment chips: cache-aware preview button or expired notice */}
                      {msg.attachments?.some(a => a.kind !== "image") && (
                        <div className="flex flex-wrap gap-1.5 mt-2">
                          {msg.attachments.filter(a => a.kind !== "image").map(a => (
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

                  {/* ── Auto follow-up suggestion chips ── */}
                  {msg.role === "assistant" && !msg.streaming && msg.followups && msg.followups.length > 0 && !msg.followupsUsed && (
                    <div className="flex flex-wrap gap-1.5 mt-3">
                      {msg.followups.map(fu => (
                        <button
                          key={fu}
                          onClick={() => {
                            // Mark chips as used so they gray out after click
                            setChats(prev => prev.map(c =>
                              c.id === activeChatId
                                ? { ...c, messages: c.messages.map(m => m.id === msg.id ? { ...m, followupsUsed: true } : m) }
                                : c
                            ));
                            setInput(fu);
                            setTimeout(() => textareaRef.current?.focus(), 50);
                          }}
                          className="px-3 py-1 text-xs bg-blue-50 hover:bg-blue-100 text-blue-700 rounded-full border border-blue-200 transition"
                        >
                          {fu}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* Grayed-out followup chips after one is selected */}
                  {msg.role === "assistant" && msg.followups && msg.followupsUsed && (
                    <div className="flex flex-wrap gap-1.5 mt-3 opacity-40">
                      {msg.followups.map(fu => (
                        <span key={fu} className="px-3 py-1 text-xs bg-gray-100 text-gray-400 rounded-full border border-gray-200">
                          {fu}
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
                    ) : msg.imageStage ? (
                      <AiNxtSpinner
                        steps={[
                          { id: 1, label: "Understanding prompt" },
                          { id: 2, label: `Routing to ${MODEL_IMAGE}` },
                          { id: 3, label: "Rendering image" },
                          { id: 4, label: "Finalizing PNG" },
                        ]}
                        stage={
                          msg.imageStage === "classify"   ? 0 :
                          msg.imageStage === "submitting" ? 1 :
                          msg.imageStage === "rendering"  ? 2 : 3
                        }
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
                  {/* Once tokens are flowing, keep the same live status line
                      (spinner + elapsed timer + running token count) below the
                      streaming answer — claude-code style — so the clock never
                      resets and the user always sees progress. */}
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
                  {msg.role === "assistant" && !msg.streaming && Array.isArray(msg.artifacts) && msg.artifacts.length > 0 && (() => {
                      // Generated-image messages persist the raw prompt as the
                      // artifact title server-side (older rows). Detect them from
                      // the message content — live: ![generated image](data:…),
                      // persisted: [IMAGE:{id}:{file}] — and force the friendly
                      // "Generated image" chip label so the chip reads the same
                      // before and after a page refresh.
                      const isImageMsg = /\[IMAGE:[^\]]+\]/.test(msg.content || "")
                        || /!\[generated image\]\(data:image/i.test(msg.content || "");
                      return (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {msg.artifacts.map(a => (
                            <button
                                key={a.id}
                                type="button"
                                onClick={() => setOpenArtifactId(a.id)}
                                className="text-xs px-2.5 py-1 rounded-md border border-purple-200 text-purple-600 bg-purple-50 hover:bg-purple-100 transition"
                                title="Open in Canvas"
                            >
                              ◳ {isImageMsg ? IMAGE_ARTIFACT_TITLE : (a.title || a.type)}
                            </button>
                        ))}
                      </div>
                      );
                  })()}

                  {/* ── KB grounding indicator + expandable Sources panel ─────── */}
                  {msg.role === "assistant" && !msg.streaming && Array.isArray(msg.sources) && msg.sources.length > 0 && (
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
                          {msg.sources.map((s, i) => (
                              <div key={i} className="text-gray-700">
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
                                <div className="text-gray-500 line-clamp-3">
                                  {s.snippet || ""}
                                </div>
                                {/* ── Part U11 — section + page footer + "Open original" link ── */}
                                {(s.section_name || s.page_number != null || s.original_url || s.namespace) && (
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
                                    {s.original_url && (
                                      <a
                                        href={s.original_url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="ml-auto text-indigo-600 hover:text-indigo-800 hover:underline"
                                        title="Open the retained original file (PDF / DOCX / etc.)"
                                      >
                                        📎 Open original
                                      </a>
                                    )}
                                  </div>
                                )}
                              </div>
                          ))}
                        </div>
                      </details>
                  )}

                  {/* ── Memory-updated chip (Phase 3.2) ─────────────────
                      Shown when the model persisted a cross-chat memory this
                      turn. Click opens the Memory panel for review/removal. */}
                  {msg.role === "assistant" && !msg.streaming && msg.memoryStored?.summary && !embedded && (
                    <button
                      onClick={() => setMemoryOpen(true)}
                      title={`Saved to memory: ${msg.memoryStored.summary}`}
                      className="inline-flex items-center gap-1 mt-2 px-2 py-0.5 rounded-full bg-purple-50 border border-purple-100 text-[10px] text-purple-600 hover:bg-purple-100 transition"
                    >
                      <Brain size={11} />
                      Memory updated
                    </button>
                  )}

                  {/* ── Feedback + Copy + TTS action bar (assistant only, not streaming, not cancelled, has content, NOT PPT message, NOT a doc job still generating) ── */}
                  {msg.role === "assistant" && !msg.streaming && !msg.cancelled && (msg.content?.trim() || '') && !msg.pptType && !msg.pptConversation
                    && !messageHasPendingDoc(msg.content) && (
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
                      {/* Regenerate — only on the latest assistant reply, standard
                          AI chat UX. Calls handleRegenerate(), which re-sends
                          the preceding user prompt in place. */}
                      {msg.id === lastAssistantId && !loading && (
                        <button
                          onClick={handleRegenerate}
                          title="Regenerate response"
                          className="p-1 rounded cursor-pointer text-gray-600 hover:text-indigo-500 transition-colors ml-0.5"
                        >
                          <RotateCcw size={14} />
                        </button>
                      )}
                    </div>
                  )}

                  {/* ── Cancelled indicator + Continue (assistant only, cancelled) ── */}
                  {msg.role === "assistant" && msg.cancelled && (
                    <div className="flex items-center gap-3 mt-2 rounded-sm text-sm text-gray-600">
                      <span className="flex items-center gap-1.5">
                        <CircleX size={14} />
                        <span>OK, I've stopped generating the response.</span>
                      </span>
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

        {/* Voice Mode Overlay — disabled in embedded (KB) mode. */}
        {voiceModeActive && !embedded && (
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

        {/* Combined Input Box — supports drag-and-drop files */}
        <div
          ref={dropRef}
          className={`border-t border-gray-100 bg-white px-4 pb-4 pt-3 flex-shrink-0 relative transition-all ${
            isDragging ? 'bg-blue-50' : ''
          }`}
        >
          {/* ── Jump-to-latest button (Phase 6.1) ─────────────────────
              Circular icon-only button (matches the Copilot style), floating
              just above the chat section (composer). Anchored to the composer's
              top edge so it never overlaps the input area regardless of
              composer height. Smooth-scrolls back to the newest message. */}
          {showJumpToLatest && messages.length > 0 && (
            <button
              onClick={jumpToLatest}
              title="Jump to latest"
              aria-label="Jump to latest"
              className="absolute right-4 bottom-full mb-3 z-20 flex items-center justify-center w-9 h-9 rounded-full bg-white border border-gray-300 shadow-md text-gray-600 hover:text-indigo-600 hover:border-indigo-200 hover:bg-gray-50 transition"
            >
              <ArrowDown size={18} />
            </button>
          )}

          {/* Drag-over overlay */}
          {isDragging && (
            <div className="absolute inset-0 z-10 flex items-center justify-center rounded-b-xl border-2 border-dashed border-blue-400 bg-blue-50/80 pointer-events-none">
              <div className="flex flex-col items-center gap-1 text-blue-500">
                <Paperclip size={28} />
                <span className="text-sm font-medium">Drop files to attach</span>
                <span className="text-xs text-blue-400">PDF, DOCX, images and more</span>
              </div>
            </div>
          )}

          {/* Hidden file input (docs/attachments) */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPT_TYPES}
            onChange={handleFileUpload}
            className="hidden"
          />

          {/* Hidden image file input */}
          <input
            ref={imageInputRef}
            type="file"
            multiple
            accept={IMAGE_ACCEPTED}
            onChange={handleImageSelect}
            className="hidden"
          />

          {/* Image preview thumbnail */}
          {imageFiles.length > 0 && (
            <div className="mb-2 flex items-start gap-2 flex-wrap">
              {imageFiles.map(({ previewUrl, file }, idx) => (
                <div key={previewUrl} className="relative inline-block">
                  <img
                    src={previewUrl}
                    alt={`Attached image ${idx + 1}`}
                    className="h-20 max-w-[160px] rounded-md object-contain border border-gray-200 bg-gray-50"
                  />
                  <button
                    onClick={() => removeImage(idx)}
                    title={`Remove ${file.name}`}
                    className="absolute -top-1.5 -right-1.5 bg-white border border-gray-300 rounded-full p-0.5 text-gray-500 hover:text-red-500 hover:border-red-300 transition"
                  >
                    <X size={10} />
                  </button>
                </div>
              ))}
              <span className="text-xs text-gray-400 mt-1 self-center">
                {imageFiles.length}/{MAX_IMAGES} image{imageFiles.length > 1 ? "s" : ""} attached
              </span>
            </div>
          )}

          {uploading && (
            <div className="mb-2 flex items-center gap-2">
              {uploadPhase === "processing" ? (
                <>
                  <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
                    <div className="h-full w-2/5 bg-indigo-500 rounded-full animate-upload-indeterminate" />
                  </div>
                  <span className="text-xs text-indigo-600 font-medium flex items-center gap-1 shrink-0">
                    <ShieldOff size={12} className="animate-pulse" />
                    Scanning for PCI &amp; parsing document…
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
                {attachments.map(a => {
                  const ext = (a.file_name || "").split(".").pop()?.toLowerCase() || "";
                  return (
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
                        onClick={() => setPreviewAttachment({ id: a.id, fileName: a.file_name, fileType: ext, parsedText: a.parsed_preview || "" })}
                        title="Preview file"
                        className="text-blue-400 hover:text-indigo-600 ml-0.5 cursor-pointer"
                      >
                        <Eye size={10} />
                      </button>
                      <button
                        onClick={() => removeAttachment(a.id)}
                        title="Remove attachment"
                        className="text-blue-400 hover:text-blue-600 cursor-pointer"
                      >
                        <X size={10} />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}

            {/* "/" prompt-template menu */}
            {tplMenuOpen && templates.length > 0 && (
                <div className="absolute bottom-full mb-1 left-2 right-2 bg-white border border-gray-200 rounded-lg shadow-xl max-h-56 overflow-y-auto z-20">
                  <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
                    Saved prompts {tplFilter && `· "${tplFilter}"`}
                  </div>
                  {tplMatches.map((t, idx) => (
                          <button
                              key={t.id}
                              type="button"
                              onClick={() => applyTemplate(t)}
                              onMouseEnter={() => setTplActiveIdx(idx)}
                              className={`w-full text-left px-3 py-2 border-b border-gray-100 last:border-b-0 ${
                                idx === tplActiveIdx ? "bg-indigo-50" : "hover:bg-gray-50"
                              }`}
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
                  {tplMatches.length === 0 && (
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
                  if (tplMenuOpen) {
                    setTplMenu(false);
                    return;
                  }
                  if (editingMsgId) {
                    cancelEditMsg();
                    return;
                  }
                }

                // Phase 5.2: keyboard navigation for the "/" template menu.
                // ↑/↓ move the highlight; Enter applies the highlighted
                // template instead of sending. Only active while the menu
                // is open and has matches.
                if (tplMenuOpen && tplMatches.length > 0) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setTplActiveIdx(i => (i + 1) % tplMatches.length);
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setTplActiveIdx(i => (i - 1 + tplMatches.length) % tplMatches.length);
                    return;
                  }
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    applyTemplate(tplMatches[Math.min(tplActiveIdx, tplMatches.length - 1)]);
                    return;
                  }
                }

                // Plain Enter (no Shift) sends the message.
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

              {/* Attach files */}
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={inputDisabled || uploading}
                title={uploading ? `Uploading… ${uploadProgress}%` : "Attach files"}
                className="cursor-pointer p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition disabled:opacity-40"
              >
                {uploading
                  ? <Loader2 size={16} className="animate-spin text-blue-500" />
                  : <Paperclip size={16} />
                }
              </button>

              {/* Image attach — vision queries via Gemini */}
              <button
                onClick={() => imageInputRef.current?.click()}
                disabled={inputDisabled || imageFiles.length >= MAX_IMAGES}
                title={
                  imageFiles.length >= MAX_IMAGES
                    ? `Maximum ${MAX_IMAGES} images attached`
                    : imageFiles.length > 0
                    ? `${imageFiles.length}/${MAX_IMAGES} image(s) attached — click to add more`
                    : "Attach image"
                }
                className={`p-1.5 cursor-pointer rounded-lg transition disabled:opacity-40 disabled:cursor-not-allowed ${
                  imageFiles.length > 0
                    ? "text-blue-500 bg-blue-50 hover:bg-blue-100"
                    : "text-gray-400 hover:text-gray-600 hover:bg-gray-100"
                }`}
              >
                <ImageIcon size={16} />
              </button>

              {/* Prompt Enhancer */}
              <button
                onClick={handleEnhance}
                disabled={!input.trim() || enhancing || inputDisabled}
                title="Enhance prompt with AI"
                className={`p-1.5 cursor-pointer rounded-lg hover:bg-blue-100 transition disabled:opacity-40 ${
                  enhancing
                    ? "text-purple-500 animate-pulse"
                    : "text-gray-400 hover:text-gray-600 hover:bg-gray-200"
                }`}
              >
                {enhancing
                  ? <Loader2 size={16} className="animate-spin text-purple-500" />
                  : <Sparkles size={16} />
                }
              </button>

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
                {MODEL_OPTIONS.map(o => {
                  // Phase 5.3: append a context-window badge to real model
                  // entries (skip Auto + section dividers).
                  const skip = o.disabled || o.value === "auto";
                  const badge = skip ? null : _modelContextBadge(o.value, o.label);
                  // Price tier (Paid / Free) comes from the backend /all-models
                  // response — NOT hardcoded in the UI. Appended after the badge.
                  const tier = skip ? null : _modelTierTag(o.tier);
                  const suffix = [badge, tier].filter(Boolean).join(" · ");
                  return (
                    <option key={o.value} value={o.value} disabled={o.disabled}>
                      {suffix ? `${o.label} · ${suffix}` : o.label}
                    </option>
                  );
                })}
              </select>

              {/* Send / Stop
                  Stop is intentionally NOT offered for image generation: while
                  imageGenerating is true the button reverts to a disabled Send
                  icon (single-shot render, no cooperative cancel). Stop stays
                  available for chat, document, and file-based text workflows. */}
              <button
                id="chat-send-btn"
                onClick={loading && !imageGenerating ? stopGeneration : sendMessage}
                disabled={ imageGenerating || (!loading && !input.trim()) || enhancing || uploading || docGenerating }
                title={imageGenerating ? "Stop is unavailable during image generation" : (loading ? "Stop generating" : "Send")}
                className="p-1.5 cursor-pointer text-gray-500 hover:text-gray-400 transition disabled:opacity-30 rounded-full"
              >
                {loading && !imageGenerating ? <CirclePauseIcon size={20} /> : <SendHorizontal size={20} />}
              </button>

            </div>

            {/* ── Live context-window meter (Phase 2) ──────────────────
                Mirrors Buddy's context bar. Only shown once the backend has
                reported context usage for this chat. Turns amber >80% so the
                user knows a summary/compaction is imminent. */}
            {contextInfo && contextInfo.context_window > 0 && (
              <div className="flex items-center gap-2 px-1 pt-1.5 text-[10px] text-gray-400 select-none">
                <div className="flex-1 h-1 rounded-full bg-gray-100 overflow-hidden max-w-[160px]">
                  <div
                    className={`h-full rounded-full transition-all ${
                      contextInfo.pct_used >= 80 ? "bg-amber-400" : "bg-indigo-400"
                    }`}
                    style={{ width: `${Math.min(100, contextInfo.pct_used || 0)}%` }}
                  />
                </div>
                <span title="Context window used">
                  {Math.round((contextInfo.tokens_used || 0) / 1000)}K / {Math.round(contextInfo.context_window / 1000)}K
                  {" "}({contextInfo.pct_used}%)
                </span>
                {contextInfo.compacted && (
                  <span className="text-amber-500" title="Older messages were summarized">· summarized</span>
                )}
              </div>
            )}

            {/* KB scope picker removed from Chat — scope is picked once
                in Knowledge Base → Chat (KbDrillGraph) and persisted on the
                Chat row via PATCH /chats/{id}/scope. Chat.jsx still hydrates
                product_id/domain/spec_version/kb_doc_id on load so the KB
                grounding indicator and source citations work correctly. */}
          </div>
        </div>

      </div>

      {/* ── Prompt Enhancer Modal ─────────────────────────────── */}
      {enhancerModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg mx-4 flex flex-col max-h-[80vh]">

            {/* Header */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
              <div className="flex items-center gap-2 text-purple-600 font-semibold text-sm">
                <Sparkles size={16} />
                Enhanced Prompt
              </div>
              <button
                onClick={() => setEnhancerModal(false)}
                className="p-1 text-gray-400 hover:text-gray-600 rounded-lg hover:bg-gray-100 transition"
              >
                <X size={16} />
              </button>
            </div>

            {/* Scrollable body */}
            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">

              <div>
                <label className="text-xs font-medium text-gray-500 uppercase tracking-wide block mb-1.5">
                  Enhanced Question
                </label>
                <textarea
                  value={enhancerEdited}
                  onChange={e => setEnhancerEdited(e.target.value)}
                  rows={5}
                  className="w-full text-sm text-gray-800 border border-gray-200 rounded-xl px-3 py-2.5 resize-none outline-none focus:ring-2 focus:ring-purple-200 focus:border-purple-400 transition"
                />
              </div>

              {followupQs.length > 0 && (
                <div>
                  <label className="text-xs font-medium text-gray-500 uppercase tracking-wide block mb-2">
                    Add context <span className="normal-case font-normal text-gray-400">(optional — answer any that help)</span>
                  </label>
                  <div className="space-y-2.5">
                    {followupQs.map((q, i) => (
                      <div key={i}>
                        <p className="text-xs text-gray-600 mb-1">{q}</p>
                        <input
                          type="text"
                          placeholder="Your answer…"
                          value={followupAnswers[q] || ""}
                          onChange={e => setFollowupAnswers(prev => ({ ...prev, [q]: e.target.value }))}
                          className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-purple-200 focus:border-purple-400 transition"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between px-5 py-4 border-t border-gray-100 gap-3">
              <button
                onClick={() => setEnhancerModal(false)}
                className="text-sm text-gray-500 hover:text-gray-700 px-4 py-2 rounded-xl hover:bg-gray-100 transition"
              >
                Keep original
              </button>
              <button
                onClick={applyEnhancement}
                className="text-sm bg-purple-600 hover:bg-purple-700 text-white font-medium px-5 py-2 rounded-xl transition"
              >
                Use enhanced prompt
              </button>
            </div>

          </div>
        </div>
      )}

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

      {/* PPT Wizard — full-screen portal, opened when PPT intent detected */}
      {pptWizardOpen && (
        <PPTWizard
          prompt={pptWizardPrompt}
          chatId={pptWizardChatId || activeChatId}
          onClose={() => setPptWizardOpen(false)}
          onComplete={(data) => {
            // Add presentation message to the chat that opened the wizard.
            const targetChatId = pptWizardChatId || activeChatId;
            const title = data.title || "Presentation";
            const safeTitle = encodeURIComponent(title);
            const marker = `[PPT:${data.id}:${data.format}:${safeTitle}]`;
            const newMsg = {
              id: crypto.randomUUID(),
              role: "assistant",
              content: `Your presentation "${title}" is ready!\n\n${marker}`,
              streaming: false
            };
            setChats(prev => prev.map(chat =>
              chat.id === targetChatId
                ? { ...chat, messages: [...(chat.messages || []), newMsg], updatedAt: Date.now() }
                : chat
            ));
          }}
        />
      )}
    </div>
  );
}
