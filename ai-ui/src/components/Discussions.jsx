// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useCallback, useRef } from "react";
import {
  Plus, ArrowLeft, ThumbsUp, ThumbsDown, CheckCircle2,
  Tags, Users, Award, HelpCircle, Image as ImageIcon, Eye, EyeOff, Loader2, ChevronDown, Check, X, Search,
  MessageCircleQuestion, AlertTriangle, MessagesSquare, Inbox, Sparkles, Trash2, Pencil, LayoutGrid,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { authFetch } from "../config";
import { useToast, useConfirm } from "./ui/DialogProvider.jsx";
import { toIST, toISTRelative } from "../utils/time";
import { cacheStore, cachedGet } from "../utils/previewCache";
import { validateFreeText, validateIdentifier } from "../utils/securityValidation";

// Native Discussions — same pattern as Threads.jsx/Chat.jsx: calls AiNxt's
// own gateway (routers/discussions_router.py). Phase 1 visual redesign: the
// underlying question/answer/vote/accept data model is unchanged — this
// pass is about information hierarchy (cards, sidebar filters, previews,
// stat chips) so the feed reads like a community, not a spreadsheet.
// Apache Answer runs headless behind the gateway (services/discussions_engine/)
// — this component never talks to it directly.

// Plain inline spinner for detail/tag/contributor/badge loads — the feed's
// own initial load uses CardSkeleton instead (better perceived performance
// for a list); this is for single-item loads where a skeleton doesn't fit.
function Spinner() {
  return (
    <div className="flex justify-center py-10 text-gray-300">
      <Loader2 size={20} className="animate-spin" />
    </div>
  );
}

const SECTIONS = [
  { key: "discussions", label: "Discussions", icon: HelpCircle },
  { key: "tags", label: "Tags", icon: Tags },
  { key: "contributors", label: "Contributors", icon: Users },
  { key: "badges", label: "Badges", icon: Award },
];

const SORTS = [
  { key: "newest", label: "Newest" },
  { key: "active", label: "Active" },
  { key: "votes", label: "Votes" },
];

const QUICK_FILTERS = [
  { key: "all", label: "All discussions", icon: MessagesSquare },
  { key: "unanswered", label: "Unanswered", icon: Inbox },
  { key: "mine", label: "My discussions", icon: Users },
];

// Not backed by a separate schema field — the engine auto-creates any tag
// it hasn't seen before on first use (tag_common.go::ObjectChangeTag), so
// these double as a "what kind of post is this" selector without forking
// the vendored engine.
const TYPE_TAGS = ["question", "feedback", "issue"];
const TYPE_ICONS = { question: MessageCircleQuestion, feedback: MessagesSquare, issue: AlertTriangle };

// Distinct solid color per type so the badge is distinguishable at a glance
// — topic tags stay the light indigo-50 pill, type badges stay solid, just
// no longer all the same gray-900 regardless of type.
const TYPE_COLORS = { question: "bg-indigo-600", feedback: "bg-green-600", issue: "bg-rose-600" };
function typeBadgeClass(t) {
  return `${TYPE_COLORS[t] || "bg-gray-900"} text-white`;
}

const TITLE_MIN = 6, TITLE_MAX = 150;       // question_schema.go: QuestionAdd.Title
const ANSWER_MIN = 6;                        // answer_schema.go: AnswerAddReq.Content
const COMMENT_MIN = 2, COMMENT_MAX = 600;    // comment_schema.go: AddCommentReq.OriginalText

function stripHtml(html) {
  if (!html) return "";
  const doc = new DOMParser().parseFromString(html, "text/html");
  return doc.body.textContent || "";
}

async function errorDetail(resp, fallback) {
  try {
    const body = await resp.json();
    return body?.detail || fallback;
  } catch {
    return fallback;
  }
}

// Same avatar shape as Threads.jsx's message-author initials circle
// (rounded-full, white bold initial) — Discussions has many distinct
// authors rather than a fixed user/assistant pair, so the color is a stable
// hash of the name instead of a role-based constant.
const AVATAR_COLORS = [
  "bg-indigo-500", "bg-rose-500", "bg-amber-500", "bg-emerald-500",
  "bg-sky-500", "bg-violet-500", "bg-pink-500", "bg-teal-500",
];
function avatarColor(name) {
  if (!name) return "bg-gray-400";
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}
function Avatar({ name, size = "w-6 h-6" }) {
  const initial = (name || "?").trim().charAt(0).toUpperCase() || "?";
  const isBot = name === "AiNxt";
  return (
    <div
      className={`${size} rounded-full flex items-center justify-center text-white text-xs font-bold flex-shrink-0 ${
        isBot ? "bg-indigo-600" : avatarColor(name)
      }`}
      title={name}
    >
      {initial}
    </div>
  );
}

// "By <name> · <department> · created <IST> · edited <IST>" — audit line
// reused across the card feed, question header, and each reply.
function AuthorLine({ name, department, createdAt, updatedAt, compact, hideDepartment }) {
  const edited = updatedAt && createdAt && new Date(updatedAt) - new Date(createdAt) > 60000;
  return (
    <div className={`flex items-center gap-1.5 ${compact ? "text-[11px]" : "text-xs"} text-gray-500`}>
      <Avatar name={name} size={compact ? "w-6 h-6" : "w-7 h-7"} />
      <span className="font-medium text-gray-700">{name}</span>
      {department && !hideDepartment && <span className="text-gray-400">· {department}</span>}
      <span className="text-gray-400">· {compact ? toISTRelative(createdAt) : toIST(createdAt)}</span>
      {edited && <span className="text-gray-400">· edited {toISTRelative(updatedAt)}</span>}
    </div>
  );
}

// ── Composer: bordered textarea + toolbar row, same shape as Chat.jsx's
// message box (textarea on top, icon-button toolbar below). The textarea
// auto-grows with content instead of scrolling inside a fixed box — minHeight
// is a floor (min-h-*), not a cap. ──
function Composer({ value, onChange, placeholder, minHeight = "min-h-28" }) {
  const [preview, setPreview] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef(null);
  const textareaRef = useRef(null);
  const { toast } = useToast();

  function autoGrow(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }

  // Belt-and-suspenders: grow synchronously inside the textarea's own
  // onChange (so typing never lags a render cycle behind), AND on every
  // value/preview change (covers programmatic edits — image insert, the
  // @AiNxt button, switching back from preview — that don't go through
  // the textarea's native change event at all).
  useEffect(() => { autoGrow(textareaRef.current); }, [value, preview]);

  async function handleImagePick(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const resp = await authFetch("/discussions/upload", { method: "POST", body: fd });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Image upload failed"));
      const { url } = await resp.json();
      // Seed the browser preview cache with the bytes we already have in hand,
      // keyed by the returned url. This lets the preview (and later the posted
      // feed) render cache-first instead of re-fetching from the server.
      await cacheStore(url, file, file.type || "application/octet-stream");
      onChange(`${value}\n![${file.name}](${url})\n`);
    } catch (e) {
      toast.error(e.message || "Image upload failed");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  // Insert "@AiNxt" at the cursor instead of requiring it to be typed —
  // falls back to appending at the end if the textarea isn't focused/mounted.
  function insertMention() {
    const el = textareaRef.current;
    const mention = "@AiNxt";
    if (!el) { onChange(`${value}${value && !value.endsWith(" ") ? " " : ""}${mention} `); return; }
    const start = el.selectionStart ?? value.length;
    const end = el.selectionEnd ?? value.length;
    const needsSpaceBefore = start > 0 && !/\s$/.test(value.slice(0, start));
    const insert = `${needsSpaceBefore ? " " : ""}${mention} `;
    const next = value.slice(0, start) + insert + value.slice(end);
    onChange(next);
    requestAnimationFrame(() => {
      const pos = start + insert.length;
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  }

  return (
    <div className="border border-gray-200 rounded-xl overflow-hidden bg-white focus-within:ring-2 focus-within:ring-indigo-100 transition-shadow">
      {preview ? (
        <div className={`px-3 pt-3 pb-1 prose prose-sm max-w-none ${minHeight} overflow-y-auto`}>
          {value ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={MD_COMPONENTS}>{value}</ReactMarkdown>
          ) : (
            <span className="text-gray-400">Nothing to preview yet.</span>
          )}
        </div>
      ) : (
        <textarea
          ref={textareaRef}
          className={`w-full ${minHeight} resize-none bg-transparent px-3 pt-3 pb-1 outline-none text-sm text-gray-800 placeholder-gray-400 overflow-hidden`}
          placeholder={placeholder} value={value}
          onChange={(e) => { onChange(e.target.value); autoGrow(e.target); }}
        />
      )}
      <div className="flex items-center gap-1 px-2 pb-2">
        <button
          type="button" onClick={() => fileRef.current?.click()} disabled={uploading}
          title={uploading ? "Uploading…" : "Insert image"}
          className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <ImageIcon size={16} />
        </button>
        <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={handleImagePick} />
        <button
          type="button" onClick={() => setPreview((p) => !p)}
          title={preview ? "Back to writing" : "Preview"}
          className={`p-1.5 rounded-lg transition cursor-pointer ${preview ? "text-indigo-600 bg-indigo-50" : "text-gray-400 hover:text-gray-600 hover:bg-gray-100"}`}
        >
          {preview ? <EyeOff size={16} /> : <Eye size={16} />}
        </button>
        <div className="w-px h-4 bg-gray-200 mx-0.5" />
        <button
          type="button" onClick={insertMention} disabled={preview}
          title="Mention @AiNxt for a bot reply"
          className="flex items-center gap-1 px-2 py-1 text-xs font-medium text-indigo-600 hover:bg-indigo-50 rounded-lg transition cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Sparkles size={13} /> @AiNxt
        </button>
      </div>
    </div>
  );
}

// Cache-first image. The upload url (e.g. "/discussions/uploads/…") is the
// cache key, so on render we:
//   1. look in the browser preview cache (cachedGet) — the fast local path,
//   2. on a miss, fetch the same url from the server and re-populate the cache
//      so the next render is local,
//   3. on a total miss / error, fall back to the raw src so behaviour is never
//      worse than a plain <img>.
// We fetch the src url directly (not cachedGetOrFetch, whose fallback targets
// the chat attachment endpoint) because discussion images are served straight
// off "/discussions/uploads/{path}". Blob URLs are revoked on unmount / src
// change to avoid leaks.
function CachedImg({ src, alt, ...rest }) {
  const [resolvedSrc, setResolvedSrc] = useState(src);
  // react-markdown passes a `node` prop that must not reach the DOM <img>.
  const { node, ...imgProps } = rest;
  void node;

  useEffect(() => {
    let cancelled = false;
    let objectUrl = null;

    // No src → nothing to resolve; initial state already mirrors src.
    if (!src) return;

    const showBlob = (blob) => {
      if (cancelled) return true;
      objectUrl = URL.createObjectURL(blob);
      setResolvedSrc(objectUrl);
      return true;
    };

    (async () => {
      try {
        // 1) Cache first.
        const cached = await cachedGet(src);
        if (cancelled) return;
        if (cached) { showBlob(await cached.blob()); return; }

        // 2) Cache miss → server, then re-populate the cache.
        const res = await fetch(src);
        if (cancelled) return;
        if (res && res.ok) {
          const contentType = res.headers.get("Content-Type") || "application/octet-stream";
          const blob = await res.blob();
          if (cancelled) return;
          await cacheStore(src, blob, contentType);
          showBlob(blob);
          return;
        }
      } catch {
        // fall through to raw src below
      }
      // 3) Total miss / error — fall back to the raw server URL.
      if (!cancelled) setResolvedSrc(src);
    })();

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  return <img src={resolvedSrc} alt={alt} {...imgProps} />;
}

const MD_COMPONENTS = {
  // Cache-first image rendering (see CachedImg): check the browser cache before
  // going to the server, exactly the order we want for uploaded previews.
  img: (props) => <CachedImg {...props} />,
  // Wrap tables so wide ones scroll horizontally instead of overflowing the thread.
  table: ({ node, ...props }) => (
    <div className="my-3 w-full overflow-x-auto rounded-lg border border-gray-200">
      <table className="w-full border-collapse text-sm" {...props} />
    </div>
  ),
  thead: ({ node, ...props }) => <thead className="bg-gray-50" {...props} />,
  th: ({ node, ...props }) => (
    <th
      className="border-b border-gray-200 px-3 py-2 text-left font-semibold text-gray-700"
      {...props}
    />
  ),
  td: ({ node, ...props }) => (
    <td className="border-b border-gray-100 px-3 py-2 align-top text-gray-800" {...props} />
  ),
  tr: ({ node, ...props }) => <tr className="even:bg-gray-50/50" {...props} />,
};

function MarkdownView({ content }) {
  return (
    <div className="prose prose-sm max-w-none text-gray-800">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={MD_COMPONENTS}
      >
        {content || ""}
      </ReactMarkdown>
    </div>
  );
}

function VoteButtons({ voteCount, myVote = 0, onVote, disabled }) {
  return (
    <div className="flex flex-col items-center gap-0.5 shrink-0 w-10">
      <button
        disabled={disabled} onClick={() => onVote(1)} title="Upvote"
        className={`w-8 h-8 flex items-center justify-center rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed active:scale-90 transition-all ${
          myVote > 0 ? "text-indigo-600 bg-indigo-100 ring-1 ring-indigo-300" : "text-gray-400 hover:bg-indigo-50 hover:text-indigo-600"
        }`}
      >
        <ThumbsUp size={15} fill={myVote > 0 ? "currentColor" : "none"} />
      </button>
      <span className={`text-sm font-bold tabular-nums ${
        (voteCount ?? 0) > 0 ? "text-indigo-600" : (voteCount ?? 0) < 0 ? "text-red-500" : "text-gray-500"
      }`}>{voteCount ?? 0}</span>
      <button
        disabled={disabled} onClick={() => onVote(-1)} title="Downvote"
        className={`w-8 h-8 flex items-center justify-center rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed active:scale-90 transition-all ${
          myVote < 0 ? "text-red-600 bg-red-100 ring-1 ring-red-300" : "text-gray-400 hover:bg-red-50 hover:text-red-600"
        }`}
      >
        <ThumbsDown size={15} fill={myVote < 0 ? "currentColor" : "none"} />
      </button>
    </div>
  );
}

// ── Comments (shared by discussion topics and replies) ──
function CommentsBlock({ targetType, targetId, count = 0, user }) {
  const [comments, setComments] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [localCount, setLocalCount] = useState(count);
  const { toast } = useToast();
  const { confirm } = useConfirm();

  async function deleteComment(commentId) {
    const ok = await confirm({
      title: "Delete comment?",
      message: "This permanently removes your comment.",
      confirmLabel: "Delete",
    });
    if (!ok) return;
    try {
      const resp = await authFetch(`/discussions/${targetType}/${targetId}/comments/${commentId}`, { method: "DELETE" });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to delete comment"));
      setComments((cs) => cs.filter((c) => c.id !== commentId));
      setLocalCount((c) => Math.max(c - 1, 0));
    } catch (e) {
      toast.error(e.message || "Failed to delete comment");
    }
  }

  // Which comment is currently being edited, plus the buffered text. Only
  // one edit-in-flight per CommentsBlock — clicking the pencil on another
  // comment swaps focus, matching how a Slack thread edits inline.
  const [editingId, setEditingId] = useState(null);
  const [editingText, setEditingText] = useState("");

  function startEditing(c) {
    setEditingId(c.id);
    setEditingText(c.content);
  }

  function cancelEditing() {
    setEditingId(null);
    setEditingText("");
  }

  async function saveEditing(commentId) {
    const next = editingText.trim();
    if (!next) return;
    if (next.length < COMMENT_MIN || next.length > COMMENT_MAX) {
      toast.warn(`Comments must be ${COMMENT_MIN}–${COMMENT_MAX} characters.`);
      return;
    }
    // Mirrors validate_free_text(body.content) in edit_comment() (routers/discussions_router.py).
    const contentCheck = validateFreeText(next);
    if (!contentCheck.isValid) {
      toast.warn(contentCheck.errors[0]?.message || "Invalid comment");
      return;
    }
    try {
      const resp = await authFetch(`/discussions/${targetType}/${targetId}/comments/${commentId}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: next }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to update comment"));
      setComments((cs) => cs.map((c) => (
        c.id === commentId ? { ...c, content: next, updated_at: new Date().toISOString() } : c
      )));
      cancelEditing();
    } catch (e) {
      toast.error(e.message || "Failed to update comment");
    }
  }

  const load = useCallback(async () => {
    try {
      const resp = await authFetch(`/discussions/${targetType}/${targetId}/comments`);
      if (resp.ok) setComments(await resp.json());
    } finally {
      setLoaded(true);
    }
  }, [targetType, targetId]);

  useEffect(() => { if (open && !loaded) load(); }, [open, loaded, load]);

  const tooShort = text.trim().length > 0 && text.trim().length < COMMENT_MIN;
  const tooLong = text.length > COMMENT_MAX;

  async function submit() {
    if (!text.trim() || tooShort || tooLong) return;
    // Mirrors validate_free_text(body.content) in post_comment() (routers/discussions_router.py).
    const contentCheck = validateFreeText(text);
    if (!contentCheck.isValid) {
      toast.warn(contentCheck.errors[0]?.message || "Invalid comment");
      return;
    }
    try {
      const resp = await authFetch(`/discussions/${targetType}/${targetId}/comments`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to post comment"));
      setText("");
      setLocalCount((c) => c + 1);
      toast.success(text.includes("@AiNxt") ? "Comment posted — @AiNxt will reply shortly" : "Comment posted");
      load();
    } catch (e) {
      toast.error(e.message || "Failed to post comment");
    }
  }

  return (
    <div className="mt-2">
      <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-1 text-[12px] text-gray-400 hover:text-indigo-600 transition-colors cursor-pointer mt-2">
        <ChevronDown size={12} className={`transition-transform ${open ? "rotate-180" : ""}`} />
        {open ? "Hide comments" : localCount > 0 ? `${localCount} comment${localCount === 1 ? "" : "s"}` : "Add a comment"}
      </button>
      {open && (
        <div className="mt-2 pl-3 border-l-2 border-indigo-100 space-y-2">
          {!loaded ? (
            <div className="text-[12px] text-gray-400">Loading…</div>
          ) : comments.length === 0 ? (
            <div className="text-[12px] text-gray-400">No comments yet.</div>
          ) : (
            comments.map((c) => {
              const isMine = user?.userId === c.author_user_id;
              const edited = c.updated_at && c.created_at
                && new Date(c.updated_at) - new Date(c.created_at) > 60000;
              const isEditing = editingId === c.id;
              return (
                <div key={c.id} className="group text-[12px] text-gray-600 flex items-start gap-2 py-0.5 rounded hover:bg-gray-50 px-1 -mx-1 transition-colors">
                  <Avatar name={c.author_name} size="w-4 h-4" />
                  <div className="flex-1">
                    <span className="font-medium text-gray-700">{c.author_name}</span>{" "}
                    {isEditing ? (
                      <div className="mt-1 flex gap-1.5">
                        <input
                          className="inp flex-1 text-[13px] py-1"
                          value={editingText} autoFocus
                          onChange={(e) => setEditingText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") saveEditing(c.id);
                            if (e.key === "Escape") cancelEditing();
                          }}
                        />
                        <button
                          onClick={() => saveEditing(c.id)}
                          className="text-[12px] px-2 py-1 rounded bg-indigo-600 text-white hover:bg-indigo-700 cursor-pointer"
                        >Save</button>
                        <button
                          onClick={cancelEditing}
                          className="text-[12px] px-2 py-1 rounded bg-gray-100 text-gray-600 hover:bg-gray-200 cursor-pointer"
                        >Cancel</button>
                      </div>
                    ) : (
                      <>
                        <span className="text-gray-800">{c.content}</span>
                        <span className="text-gray-400 ml-1.5">— {toISTRelative(c.created_at)}</span>
                        {edited && <span className="text-gray-400"> · edited</span>}
                      </>
                    )}
                  </div>
                  {isMine && !isEditing && (
                    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition">
                      <button
                        onClick={() => startEditing(c)} title="Edit comment"
                        className="p-0.5 text-gray-300 hover:text-indigo-600 cursor-pointer"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        onClick={() => deleteComment(c.id)} title="Delete comment"
                        className="p-0.5 text-gray-300 hover:text-red-600 cursor-pointer"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  )}
                </div>
              );
            })
          )}
          <div className="flex gap-1.5 mt-1 items-start">
            <input
              className="inp flex-1 text-[13px] py-1" placeholder="Add a comment — mention @AiNxt for a bot reply"
              value={text} onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
            />
            <button onClick={submit} disabled={tooShort || tooLong} className="text-[12px] px-2 py-1 rounded bg-gray-100 hover:bg-gray-200 text-gray-600 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed">Post</button>
          </div>
          {(tooShort || tooLong) && (
            <div className="text-[11px] text-amber-600">Comments must be {COMMENT_MIN}–{COMMENT_MAX} characters.</div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Reusable topic dropdown: multi-select existing + create new ────────────
// Props:
//   selected      string[]   — currently selected topic slugs
//   onChange      fn(string[]) — called with the new full array
//   availableTags string[]   — tags fetched from the API
function TopicDropdown({ selected, onChange, availableTags }) {
  const [open, setOpen]         = useState(false);
  const [search, setSearch]     = useState("");
  const [newTopic, setNewTopic] = useState("");
  const ref                     = useRef(null);

  // Close on outside click
  useEffect(() => {
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const q = search.toLowerCase();
  const filtered = availableTags.filter((t) => t.toLowerCase().includes(q));
  const hasResults = filtered.length > 0;

  const newSlug  = newTopic.trim().toLowerCase().replace(/\s+/g, "-");
  const canCreate = newSlug.length > 0 && !availableTags.includes(newSlug) && !selected.includes(newSlug);

  function toggle(slug) {
    onChange(selected.includes(slug) ? selected.filter((s) => s !== slug) : [...selected, slug]);
  }

  function createAndSelect() {
    if (!canCreate) return;
    onChange([...selected, newSlug]);
    setNewTopic("");
    setSearch("");
  }

  function OptionRow({ slug }) {
    const checked = selected.includes(slug);
    return (
      <button
        key={slug}
        type="button"
        onClick={() => toggle(slug)}
        className={`w-full flex items-center gap-2.5 px-3 py-1.5 text-xs transition cursor-pointer hover:bg-indigo-50/60 ${
          checked ? "bg-indigo-50 text-indigo-700" : "text-gray-700"
        }`}
      >
        <span className={`flex-shrink-0 w-3.5 h-3.5 rounded border flex items-center justify-center transition-colors ${
          checked ? "bg-indigo-600 border-indigo-600" : "border-gray-300 bg-white"
        }`}>
          {checked && <Check size={9} className="text-white" strokeWidth={3} />}
        </span>
        <span className="truncate flex-1">{slug}</span>
      </button>
    );
  }

  return (
    <div ref={ref} className="relative">
      {/* Trigger button */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 text-sm border border-gray-200 rounded-xl bg-white hover:border-indigo-300 focus:outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 transition cursor-pointer"
      >
        <span className="flex flex-wrap gap-1 flex-1 min-w-0">
          {selected.length === 0 ? (
            <span className="text-gray-400 text-xs">Select or create topics…</span>
          ) : (
            selected.map((s) => (
              <span
                key={s}
                className="inline-flex items-center gap-1 pl-2 pr-1 py-0.5 text-xs rounded-full bg-indigo-600 text-white"
              >
                {s}
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); toggle(s); }}
                  className="hover:opacity-70 cursor-pointer p-0.5"
                >
                  <X size={10} />
                </button>
              </span>
            ))
          )}
        </span>
        <ChevronDown size={14} className={`text-gray-400 flex-shrink-0 transition-transform duration-150 ${open ? "rotate-180" : ""}`} />
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="absolute z-50 mt-1 w-full bg-white border border-gray-200 rounded-xl shadow-lg overflow-hidden">
          {/* Search */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-100">
            <Search size={13} className="text-gray-400 flex-shrink-0" />
            <input
              autoFocus
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search topics…"
              className="flex-1 text-xs outline-none bg-transparent placeholder-gray-400 text-gray-700"
            />
            {search && (
              <button type="button" onClick={() => setSearch("")} className="text-gray-400 hover:text-gray-600">
                <X size={11} />
              </button>
            )}
          </div>

          {/* Options list */}
          <div className="max-h-52 overflow-y-auto">
            {!hasResults ? (
              <div className="px-3 py-2 text-xs text-gray-400">
                {availableTags.length === 0 ? "No topics yet — create one below." : "No matches."}
              </div>
            ) : (
              filtered.map((t) => <OptionRow key={t} slug={t} />)
            )}
          </div>

          {/* Create new topic */}
          <div className="border-t border-gray-100 px-3 py-2">
            <div className="flex items-center gap-2">
              <Plus size={12} className="text-gray-400 flex-shrink-0" />
              <input
                type="text"
                value={newTopic}
                onChange={(e) => setNewTopic(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); createAndSelect(); } }}
                placeholder="Create new topic…"
                className="flex-1 text-xs outline-none bg-transparent placeholder-gray-400 text-gray-700"
              />
            </div>
            {canCreate && (
              <button
                type="button"
                onClick={createAndSelect}
                className="mt-1.5 w-full text-left text-xs text-indigo-600 hover:text-indigo-800 font-medium flex items-center gap-1 transition-colors cursor-pointer"
              >
                <Plus size={11} /> Create &ldquo;{newSlug}&rdquo;
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Start a discussion: inline page state in the right panel, not a modal ──
function StartDiscussionPage({ onCancel, onCreated, initialType = "question" }) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [type, setType] = useState(initialType);
  const [availableTags, setAvailableTags] = useState([]);
  const [selectedTags, setSelectedTags] = useState([]);
  const [saving, setSaving] = useState(false);
  const { toast } = useToast();

  useEffect(() => {
    authFetch("/discussions/tags?page_size=50").then((r) => r.ok && r.json()).then((d) => {
      const apiTags = (d?.list || d?.tag_list || []).map((t) => t.slug_name).filter((t) => t && !TYPE_TAGS.includes(t));
      setAvailableTags(apiTags);
    }).catch(() => {});
  }, []);

  const titleLen = title.trim().length;
  const titleValid = titleLen >= TITLE_MIN && titleLen <= TITLE_MAX;

  async function submit() {
    if (!titleValid) {
      toast.warn(`Title must be ${TITLE_MIN}–${TITLE_MAX} characters`);
      return;
    }
    if (!content.trim()) {
      toast.warn("Add some content before posting");
      return;
    }
    // Client-side pre-check mirroring validate_discussion_title_and_tags() in
    // core/security_validation.py — title/content are free text, tags are
    // identifier-ish. Backend remains the authoritative enforcer.
    const titleCheck = validateFreeText(title);
    if (!titleCheck.isValid) {
      toast.warn(titleCheck.errors[0]?.message || "Invalid title");
      return;
    }
    const contentCheck = validateFreeText(content);
    if (!contentCheck.isValid) {
      toast.warn(contentCheck.errors[0]?.message || "Invalid content");
      return;
    }
    const allTags = [type, ...selectedTags];
    for (const tag of allTags) {
      const tagCheck = validateIdentifier(tag);
      if (!tagCheck.isValid) {
        toast.warn(tagCheck.errors[0]?.message || `Invalid tag: ${tag}`);
        return;
      }
    }
    setSaving(true);
    try {
      const resp = await authFetch("/discussions/questions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title, content, tags: allTags,
        }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to post discussion"));
      const data = await resp.json();
      toast.success("Discussion posted");
      onCreated(data.id);
    } catch (e) {
      toast.error(e.message || "Failed to post discussion");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="px-6 py-6 max-w-2xl">
      <input
        className="w-full text-base font-semibold outline-none border border-gray-200 rounded-xl focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 px-4 py-3 placeholder-gray-300 transition-shadow mb-1"
        placeholder="What do you want to discuss?" value={title} onChange={(e) => setTitle(e.target.value)}
      />
      <div className={`text-[11px] mb-3 ${title && !titleValid ? "text-amber-600" : "text-gray-400"}`}>
        {titleLen}/{TITLE_MAX} — {title && !titleValid ? `titles need at least ${TITLE_MIN} characters` : `min ${TITLE_MIN} characters`}
      </div>
      <Composer
        value={content} onChange={setContent}
        placeholder="Share details — a question, feedback, or an issue. Mention @AiNxt for a bot reply."
        minHeight="min-h-40"
      />

      <div className="mt-4">

        <div className="flex gap-2">
          {TYPE_TAGS.map((t) => {
            const TypeIcon = TYPE_ICONS[t];
            const isActive = type === t;
            const ringColors = {
              question: "ring-indigo-400 border-indigo-300 bg-indigo-50 text-indigo-700",
              feedback: "ring-green-400 border-green-300 bg-green-50 text-green-700",
              issue: "ring-rose-400 border-rose-300 bg-rose-50 text-rose-700",
            };
            return (
              <button
                key={t} onClick={() => setType(t)}
                className={`flex items-center gap-1.5 px-3 py-2 rounded-xl border text-xs font-medium capitalize transition cursor-pointer ${
                  isActive ? `${ringColors[t]} ring-2` : "border-gray-200 bg-white text-gray-500 hover:border-gray-300 hover:bg-gray-50"
                }`}
                aria-pressed={isActive}
              >
                <TypeIcon size={13} /> {t}
              </button>
            );
          })}
        </div>
      </div>

      <div className="mt-4">
        <div className="text-[11px] text-gray-500 mb-1.5">Topic (optional)</div>
        <TopicDropdown
          selected={selectedTags}
          onChange={setSelectedTags}
          availableTags={availableTags}
        />
      </div>

      <div className="flex justify-end gap-2 mt-6 pt-4 border-t border-gray-100">
        <button onClick={onCancel} className="px-4 py-2 text-sm rounded-xl text-gray-600 hover:bg-gray-100 transition cursor-pointer">Cancel</button>
        <button
          onClick={submit} disabled={saving || !titleValid || !content.trim()}
          className="flex items-center gap-1.5 px-4 py-2 text-sm rounded-xl bg-indigo-600 text-white hover:bg-indigo-700 transition cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed font-medium shadow-sm"
        >
          {saving ? <><Loader2 size={14} className="animate-spin" /> Posting…</> : <><Plus size={14} /> Post discussion</>}
        </button>
      </div>
    </div>
  );
}

// ── Tag filter toolbar: removable chips for active filters + an "Add" dropdown
// to pick more — sits in the horizontal toolbar above the feed.
function TagFilterBar({ selected, onChange }) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState([]);
  const ref = useRef(null);

  useEffect(() => {
    authFetch("/discussions/tags?page_size=100").then((r) => r.ok && r.json()).then((d) => {
      setOptions((d?.list || d?.tag_list || []).map((t) => t.slug_name).filter(Boolean));
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!open) return;
    function onOutside(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, [open]);

  function toggle(t) {
    onChange(selected.includes(t) ? selected.filter((x) => x !== t) : [...selected, t]);
  }

  const remaining = options.filter((t) => !selected.includes(t));

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      {selected.map((t) => (
        <span key={t} className="flex items-center gap-1 pl-2 pr-1 py-1 text-xs rounded-md bg-indigo-50 text-indigo-700">
          {t}
          <button onClick={() => toggle(t)} className="hover:text-indigo-900 p-0.5 cursor-pointer" title={`Remove ${t}`}>
            <X size={11} />
          </button>
        </span>
      ))}
      <div className="relative" ref={ref}>
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-gray-200 text-gray-500 hover:bg-gray-50 hover:text-gray-700 cursor-pointer"
        >
          <Plus size={12} /> Filter tags <ChevronDown size={11} className={`transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
        {open && (
          <div className="absolute left-0 top-full mt-1 z-20 w-48 bg-white border border-gray-200 rounded-lg shadow-lg max-h-56 overflow-y-auto py-1">
            {remaining.length === 0 ? (
              <div className="px-2.5 py-1.5 text-xs text-gray-400">{options.length === 0 ? "No tags yet." : "All tags selected."}</div>
            ) : (
              remaining.map((t) => (
                <button
                  key={t} onClick={() => { toggle(t); setOpen(false); }}
                  className="w-full flex items-center gap-2 px-2.5 py-1.5 text-xs text-left hover:bg-gray-50 text-gray-700 cursor-pointer"
                >
                  {t}
                </button>
              ))
            )}
          </div>
        )}
      </div>
      {selected.length > 0 && (
        <button onClick={() => onChange([])} className="text-xs text-gray-400 hover:text-gray-600 cursor-pointer">Clear</button>
      )}
    </div>
  );
}

function SortControl({ sort, onChange }) {
  return (
    <div className="flex items-center gap-1 select-none flex-shrink-0 bg-gray-100 rounded-xl p-0.5">
      {SORTS.map((s) => (
        <button
          key={s.key} onClick={() => onChange(s.key)}
          className={`px-2.5 py-1 text-xs rounded-lg transition cursor-pointer font-medium ${
            sort === s.key ? "bg-white text-indigo-700 shadow-sm" : "text-gray-500 hover:text-gray-700"
          }`}
          aria-pressed={sort === s.key}
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}

// ── Live search dropdown: matching discussions + tags as you type, instead
// of only re-filtering the feed in place ──
function SearchBox({ onOpenDiscussion, onSelectTag }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState([]);
  const [tagResults, setTagResults] = useState([]);
  const [allTags, setAllTags] = useState([]);
  const ref = useRef(null);

  useEffect(() => {
    authFetch("/discussions/tags?page_size=100").then((r) => r.ok && r.json()).then((d) => {
      setAllTags((d?.list || d?.tag_list || []).filter((t) => t.slug_name));
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!open) return;
    function onOutside(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, [open]);

  useEffect(() => {
    const q = query.trim();
    const t = setTimeout(async () => {
      if (!q) { setResults([]); setTagResults([]); return; }
      try {
        const resp = await authFetch(`/discussions/questions?q=${encodeURIComponent(q)}&limit=5`);
        if (resp.ok) setResults(await resp.json());
      } catch { /* best-effort */ }
      setTagResults(allTags.filter((tg) => tg.slug_name.includes(q.toLowerCase())).slice(0, 5));
    }, 300);
    return () => clearTimeout(t);
  }, [query, allTags]);

  const hasResults = results.length > 0 || tagResults.length > 0;

  return (
    <div className="relative w-full max-w-md" ref={ref}>
      <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
      <input
        value={query} onFocus={() => setOpen(true)}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        placeholder="Search discussions…"
        className={`w-full pl-8 pr-7 py-1.5 text-xs rounded-md border outline-none placeholder-gray-400 transition-colors ${
          query ? "border-indigo-300 bg-indigo-50/30" : "border-gray-200 focus:border-indigo-300"
        }`}
      />
      {query && (
        <button
          onClick={() => { setQuery(""); setOpen(false); }} title="Clear search"
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 cursor-pointer"
        >
          <X size={13} />
        </button>
      )}
      {open && query && (
        <div className="absolute left-0 top-full mt-1 z-30 w-96 bg-white border border-gray-200 rounded-lg shadow-lg max-h-80 overflow-y-auto py-1">
          {!hasResults ? (
            <div className="px-3 py-3 text-xs text-gray-400">No matches for "{query}".</div>
          ) : (
            <>
              {results.length > 0 && (
                <div className="pb-1">
                  <div className="px-3 pt-1.5 pb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Discussions</div>
                  {results.map((r) => (
                    <button
                      key={r.id} onClick={() => { onOpenDiscussion(r.id); setOpen(false); setQuery(""); }}
                      className="w-full text-left px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 truncate cursor-pointer"
                    >
                      {r.title}
                    </button>
                  ))}
                </div>
              )}
              {tagResults.length > 0 && (
                <div className="border-t border-gray-100 pt-1">
                  <div className="px-3 pt-1 pb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">Tags</div>
                  {tagResults.map((t) => (
                    <button
                      key={t.slug_name} onClick={() => { onSelectTag(t.slug_name); setOpen(false); setQuery(""); }}
                      className="w-full text-left px-3 py-1.5 text-xs text-indigo-600 hover:bg-gray-50 cursor-pointer"
                    >
                      {t.slug_name} <span className="text-gray-400">· {t.question_count ?? 0} discussions</span>
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function EmptyState({ icon: Icon, children }) {
  return (
    <div className="py-16 text-center">
      <Icon size={28} className="mx-auto text-gray-200 mb-2" />
      <div className="text-sm text-gray-400">{children}</div>
    </div>
  );
}

function DiscussionEmptyState({ hasFilter, onStart }) {
  return (
    <div className="py-16 text-center">
      <MessagesSquare size={28} className="mx-auto text-gray-200 mb-2" />
      <div className="text-sm text-gray-400 mb-4">
        {hasFilter ? "Nothing here yet." : "No discussions yet — be the first."}
      </div>
      <div className="flex items-center justify-center gap-2">
        {TYPE_TAGS.map((t) => {
          const TypeIcon = TYPE_ICONS[t];
          const label = t === "question" ? "Ask a question" : t === "issue" ? "Report an issue" : "Share feedback";
          return (
            <button
              key={t} onClick={() => onStart(t)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-gray-600 border border-gray-200 rounded-lg hover:border-indigo-200 hover:text-indigo-600 hover:bg-indigo-50 transition cursor-pointer"
            >
              <TypeIcon size={13} /> {label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function CardSkeleton() {
  return (
    <div className="rounded-xl border border-gray-100 p-4 animate-pulse">
      <div className="h-3 w-16 bg-gray-100 rounded mb-3" />
      <div className="h-4 w-2/3 bg-gray-100 rounded mb-2" />
      <div className="h-3 w-full bg-gray-100 rounded mb-1.5" />
      <div className="h-3 w-5/6 bg-gray-100 rounded mb-3" />
      <div className="h-3 w-1/3 bg-gray-100 rounded" />
    </div>
  );
}

function DiscussionCard({ q, onOpen }) {
  const typeTag = (q.tags || []).find((t) => TYPE_TAGS.includes(t));
  const topicTags = (q.tags || []).filter((t) => t !== typeTag);
  const TypeIcon = typeTag ? TYPE_ICONS[typeTag] : HelpCircle;
  return (
    <button
      onClick={() => onOpen(q.id)}
      className="group w-full text-left rounded-xl border border-gray-200 bg-white hover:border-indigo-200 hover:bg-indigo-50/20 shadow-sm hover:shadow-md transition-all duration-150 cursor-pointer p-4"
    >
      <div className="flex items-center gap-1.5 mb-2 flex-wrap">
        {typeTag && (
          <span className={`flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded capitalize ${typeBadgeClass(typeTag)}`}>
            <TypeIcon size={11} /> {typeTag}
          </span>
        )}
        {topicTags.map((t) => (
          <span key={t} className="text-[11px] px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600">{t}</span>
        ))}
      </div>
      <div className="text-sm font-semibold text-gray-900 leading-snug group-hover:text-indigo-900 transition-colors">{q.title}</div>
      {q.content_preview && (
        <div className="text-[13px] text-gray-500 mt-1 line-clamp-2">{q.content_preview}</div>
      )}
      <div className="flex items-center justify-between mt-3 flex-wrap gap-2">
        <AuthorLine
          name={q.author_name} department={q.author_department}
          createdAt={q.created_at} updatedAt={q.updated_at} compact hideDepartment
        />
        <div className="flex items-center gap-1.5">
          <span className={`text-[11px] px-2 py-1 rounded-md font-medium tabular-nums ${
            (q.vote_count ?? 0) > 0 ? "bg-indigo-50 text-indigo-700" : "bg-gray-100 text-gray-500"
          }`}>▲ {q.vote_count ?? 0}</span>
          <span className={`flex items-center gap-1 text-[11px] px-2 py-1 rounded-md font-medium tabular-nums ${
            q.accepted_answer_id ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200" : "bg-gray-100 text-gray-500"
          }`}>
            {q.accepted_answer_id && <CheckCircle2 size={11} />}
            {q.answer_count ?? 0} {q.answer_count === 1 ? "reply" : "replies"}
          </span>
          {q.comment_count > 0 && (
            <span className="text-[11px] px-2 py-1 rounded-md bg-gray-100 text-gray-500 font-medium tabular-nums">{q.comment_count} comments</span>
          )}
        </div>
      </div>
    </button>
  );
}

function DiscussionList({ discussions, loading, onOpen, hasFilter, onStart }) {
  if (loading) {
    return (
      <div className="space-y-3">
        {Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} />)}
      </div>
    );
  }
  if (!discussions.length) {
    return <DiscussionEmptyState hasFilter={hasFilter} onStart={onStart} />;
  }
  return (
    <div className="space-y-3">
      {discussions.map((q) => <DiscussionCard key={q.id} q={q} onOpen={onOpen} />)}
    </div>
  );
}

function ReplyCard({ answer, questionId, onVoted, onAccepted, onDeleted, onEdited, canAccept, accentAccepted, user }) {
  const { toast } = useToast();
  const { confirm } = useConfirm();
  const isBot = answer.author_user_id === "ainxt-system-bot";
  // Same ownership rule as delete — author-only, no bot edits.
  const canEdit = !isBot && user?.userId === answer.author_user_id;
  const canDelete = canEdit;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(answer.content);

  async function saveEdit() {
    const next = (draft || "").trim();
    if (!next || next.length < ANSWER_MIN) {
      toast.warn(`Replies need at least ${ANSWER_MIN} characters.`);
      return;
    }
    // Mirrors validate_free_text(body.content) in edit_answer() (routers/discussions_router.py).
    const contentCheck = validateFreeText(next);
    if (!contentCheck.isValid) {
      toast.warn(contentCheck.errors[0]?.message || "Invalid reply");
      return;
    }
    try {
      const resp = await authFetch(`/discussions/answers/${answer.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: next }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to update reply"));
      onEdited?.(answer.id, { content: next, updated_at: new Date().toISOString() });
      setEditing(false);
    } catch (e) {
      toast.error(e.message || "Failed to update reply");
    }
  }

  async function deleteReply() {
    const ok = await confirm({
      title: "Delete reply?",
      message: "This permanently removes your reply.",
      confirmLabel: "Delete",
    });
    if (!ok) return;
    try {
      const resp = await authFetch(`/discussions/answers/${answer.id}`, { method: "DELETE" });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to delete reply"));
      onDeleted?.(answer.id);
    } catch (e) {
      toast.error(e.message || "Failed to delete reply");
    }
  }

  async function vote(direction) {
    try {
      const resp = await authFetch(`/discussions/answers/${answer.id}/vote`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ direction }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Vote failed"));
      const data = await resp.json();
      onVoted(answer.id, data.vote_count, data.my_vote ?? direction);
    } catch (e) {
      toast.error(e.message || "Vote failed");
    }
  }

  async function accept() {
    try {
      const resp = await authFetch(`/discussions/questions/${questionId}/accept`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answer_id: answer.id }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Couldn't mark as resolved"));
      onAccepted(answer.id);
    } catch (e) {
      toast.error(e.message || "Couldn't mark as resolved");
    }
  }

  return (
    <div className={`flex gap-3 p-3 rounded-lg ${accentAccepted ? "bg-green-50/60" : ""}`}>
      <VoteButtons voteCount={answer.vote_count} myVote={answer.my_vote} onVote={vote} />
      <div className="min-w-0 flex-1">
        {editing ? (
          <>
            <Composer value={draft} onChange={setDraft} placeholder="Edit your reply…" minHeight="min-h-24" />
            <div className="flex justify-end gap-2 mt-2">
              <button
                onClick={() => { setEditing(false); setDraft(answer.content); }}
                className="px-2.5 py-1 text-xs rounded-lg text-gray-600 hover:bg-gray-100 cursor-pointer"
              >Cancel</button>
              <button
                onClick={saveEdit}
                className="px-2.5 py-1 text-xs rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 cursor-pointer"
              >Save</button>
            </div>
          </>
        ) : (
          <MarkdownView content={answer.content} />
        )}
        <div className="flex items-center gap-2 mt-2 flex-wrap">
          <AuthorLine
            name={answer.author_name} department={answer.author_department}
            createdAt={answer.created_at} updatedAt={answer.updated_at} compact
          />
          {isBot && <span className="px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 font-medium text-[11px]">bot reply</span>}
          {canAccept && !answer.is_accepted && !editing && (
            <button
              onClick={accept}
              className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-md bg-emerald-50 text-emerald-700 hover:bg-emerald-100 border border-emerald-200 transition cursor-pointer font-medium"
            >
              <CheckCircle2 size={11} /> Mark as resolved
            </button>
          )}
          {canEdit && !editing && (
            <button
              onClick={() => { setDraft(answer.content); setEditing(true); }} title="Edit reply"
              className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-indigo-600 cursor-pointer"
            >
              <Pencil size={12} /> Edit
            </button>
          )}
          {canDelete && !editing && (
            <button
              onClick={deleteReply} title="Delete reply"
              className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-red-600 cursor-pointer"
            >
              <Trash2 size={12} /> Delete
            </button>
          )}
        </div>
        <CommentsBlock targetType="answers" targetId={answer.id} count={answer.comment_count} user={user} />
      </div>
    </div>
  );
}

function DiscussionDetail({ questionId, user, onDeleted }) {
  const [question, setQuestion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [replyText, setReplyText] = useState("");
  const [posting, setPosting] = useState(false);
  // Question edit state — kept alongside the other detail-page state so
  // switching between view and edit doesn't require its own component.
  const [editingQuestion, setEditingQuestion] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editTags, setEditTags] = useState([]);
  const [editSaving, setEditSaving] = useState(false);
  const [editAvailableTags, setEditAvailableTags] = useState([]);

  const { toast } = useToast();
  const { confirm } = useConfirm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch(`/discussions/questions/${questionId}`);
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to load discussion"));
      setQuestion(await resp.json());
    } catch (e) {
      toast.error(e.message || "Failed to load discussion");
    } finally {
      setLoading(false);
    }
  }, [questionId]);

  useEffect(() => { load(); }, [load]);

  async function voteQuestion(direction) {
    try {
      const resp = await authFetch(`/discussions/questions/${questionId}/vote`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ direction }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Vote failed"));
      const data = await resp.json();
      setQuestion((q) => ({ ...q, vote_count: data.vote_count, my_vote: data.my_vote ?? direction }));
    } catch (e) {
      toast.error(e.message || "Vote failed");
    }
  }

  const replyLen = replyText.trim().length;
  const replyTooShort = replyLen > 0 && replyLen < ANSWER_MIN;

  async function postReply() {
    if (!replyText.trim() || replyTooShort) return;
    // Mirrors validate_free_text(body.content) in post_answer() (routers/discussions_router.py).
    const contentCheck = validateFreeText(replyText);
    if (!contentCheck.isValid) {
      toast.warn(contentCheck.errors[0]?.message || "Invalid reply");
      return;
    }
    setPosting(true);
    try {
      const resp = await authFetch(`/discussions/questions/${questionId}/answers`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: replyText }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to post reply"));
      setReplyText("");
      toast.success(replyText.includes("@AiNxt") ? "Posted — @AiNxt will reply shortly" : "Reply posted");
      load();
    } catch (e) {
      toast.error(e.message || "Failed to post reply");
    } finally {
      setPosting(false);
    }
  }

  function updateAnswer(id, patch) {
    setQuestion((q) => ({ ...q, answers: q.answers.map((x) => (x.id === id ? { ...x, ...patch } : x)) }));
  }

  function removeAnswer(id) {
    setQuestion((q) => ({
      ...q,
      answers: q.answers.filter((x) => x.id !== id),
      answer_count: Math.max((q.answer_count || 1) - 1, 0),
      accepted_answer_id: q.accepted_answer_id === id ? null : q.accepted_answer_id,
    }));
  }

  async function deleteQuestion() {
    const ok = await confirm({
      title: "Delete discussion?",
      message: "This permanently removes your discussion and all its replies and comments.",
      confirmLabel: "Delete",
    });
    if (!ok) return;
    try {
      const resp = await authFetch(`/discussions/questions/${questionId}`, { method: "DELETE" });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to delete discussion"));
      toast.success("Discussion deleted");
      onDeleted?.();
    } catch (e) {
      toast.error(e.message || "Failed to delete discussion");
    }
  }

  function beginQuestionEdit() {
    setEditTitle(question.title || "");
    setEditContent(question.content || "");
    setEditTags(question.tags || []);
    setEditingQuestion(true);
    setEditAvailableTags([]);
    authFetch("/discussions/tags?page_size=50")
      .then((r) => r.ok && r.json())
      .then((d) => {
        const apiTags = (d?.list || d?.tag_list || [])
          .map((t) => t.slug_name)
          .filter((t) => t && !TYPE_TAGS.includes(t));
        setEditAvailableTags(apiTags);
      })
      .catch(() => {});
  }

  function cancelQuestionEdit() {
    setEditingQuestion(false);
  }

  async function saveQuestionEdit() {
    const title = (editTitle || "").trim();
    if (title.length < TITLE_MIN || title.length > TITLE_MAX) {
      toast.warn(`Title must be ${TITLE_MIN}–${TITLE_MAX} characters`);
      return;
    }
    if (!editContent.trim()) {
      toast.warn("Add some content before saving");
      return;
    }
    // Client-side pre-check mirroring validate_discussion_title_and_tags() in
    // core/security_validation.py — same rules as the create-question handler.
    // Backend remains the authoritative enforcer.
    const titleCheck = validateFreeText(title);
    if (!titleCheck.isValid) {
      toast.warn(titleCheck.errors[0]?.message || "Invalid title");
      return;
    }
    const contentCheck = validateFreeText(editContent);
    if (!contentCheck.isValid) {
      toast.warn(contentCheck.errors[0]?.message || "Invalid content");
      return;
    }
    for (const tag of editTags) {
      const tagCheck = validateIdentifier(tag);
      if (!tagCheck.isValid) {
        toast.warn(tagCheck.errors[0]?.message || `Invalid tag: ${tag}`);
        return;
      }
    }
    setEditSaving(true);
    try {
      const resp = await authFetch(`/discussions/questions/${questionId}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, content: editContent, tags: editTags }),
      });
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to update discussion"));
      setQuestion((q) => ({
        ...q, title, content: editContent, tags: editTags,
        updated_at: new Date().toISOString(),
      }));
      setEditingQuestion(false);
      toast.success("Discussion updated");
    } catch (e) {
      toast.error(e.message || "Failed to update discussion");
    } finally {
      setEditSaving(false);
    }
  }

  if (loading) return <Spinner />;
  if (!question) return null;

  const canAccept = user?.userId === question.author_user_id;
  const canEditQuestion = user?.userId === question.author_user_id;
  const canDeleteQuestion = user?.userId === question.author_user_id;
  const accepted = question.answers.find((a) => a.is_accepted);
  const others = question.answers.filter((a) => !a.is_accepted);

  return (
    <div className="px-6 py-6 max-w-2xl">
      <div className="flex gap-3">
        <VoteButtons voteCount={question.vote_count} myVote={question.my_vote} onVote={voteQuestion} />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            {editingQuestion ? (
              <input
                className="flex-1 text-lg font-medium outline-none border-b border-gray-200 focus:border-indigo-300 pb-1 mr-2"
                value={editTitle} onChange={(e) => setEditTitle(e.target.value)}
                placeholder="Discussion title"
              />
            ) : (
              <h2 className="text-xl font-bold text-gray-900 leading-snug">{question.title}</h2>
            )}
            <div className="flex items-center gap-2 shrink-0">
              {canEditQuestion && !editingQuestion && (
                <button
                  onClick={beginQuestionEdit} title="Edit discussion"
                  className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-indigo-600 cursor-pointer"
                >
                  <Pencil size={13} /> Edit
                </button>
              )}
              {canDeleteQuestion && !editingQuestion && (
                <button
                  onClick={deleteQuestion} title="Delete discussion"
                  className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-red-600 cursor-pointer"
                >
                  <Trash2 size={13} /> Delete
                </button>
              )}
            </div>
          </div>
          <div className="mt-1.5 mb-3">
            <AuthorLine
              name={question.author_name} department={question.author_department}
              createdAt={question.created_at} updatedAt={question.updated_at}
            />
          </div>
          {editingQuestion ? (
            <>
              <Composer
                value={editContent} onChange={setEditContent}
                placeholder="Update the discussion body…" minHeight="min-h-32"
              />
              {/* Tag editor: dropdown multi-select with search + create new */}
              <div className="mt-3">
                <div className="text-[11px] text-gray-500 mb-1.5">Topic tags</div>
                <TopicDropdown
                  selected={editTags.filter((t) => !TYPE_TAGS.includes(t))}
                  onChange={(newTopics) => {
                    const typeTag = editTags.find((t) => TYPE_TAGS.includes(t));
                    setEditTags(typeTag ? [typeTag, ...newTopics] : newTopics);
                  }}
                  availableTags={editAvailableTags}
                />
              </div>
              <div className="flex justify-end gap-2 mt-3">
                <button
                  onClick={cancelQuestionEdit}
                  className="px-3 py-1.5 text-sm rounded-lg text-gray-600 hover:bg-gray-100 cursor-pointer"
                >Cancel</button>
                <button
                  onClick={saveQuestionEdit} disabled={editSaving}
                  className="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {editSaving ? "Saving…" : "Save changes"}
                </button>
              </div>
            </>
          ) : (
            <>
              <MarkdownView content={question.content} />
              <div className="flex gap-1.5 mt-2 flex-wrap">
                {(question.tags || []).map((t) => (
                  <span
                    key={t}
                    className={`text-[11px] px-1.5 py-0.5 rounded ${
                      TYPE_TAGS.includes(t) ? `capitalize ${typeBadgeClass(t)}` : "bg-indigo-50 text-indigo-600"
                    }`}
                  >
                    {t}
                  </span>
                ))}
              </div>
            </>
          )}
          <CommentsBlock targetType="questions" targetId={question.id} count={question.comment_count} user={user} />
        </div>
      </div>

      {accepted && (
        <div className="mt-6">
          <div className="rounded-xl border-2 border-emerald-300 overflow-hidden shadow-sm shadow-emerald-100">
            <div className="flex items-center gap-2 px-4 py-2 bg-emerald-600 text-white text-xs font-semibold tracking-wide">
              <CheckCircle2 size={14} className="shrink-0" /> Accepted Solution
            </div>
            <ReplyCard
              answer={accepted} questionId={questionId} canAccept={canAccept} accentAccepted
              user={user} onDeleted={removeAnswer}
              onEdited={(id, patch) => updateAnswer(id, patch)}
              onVoted={(id, count, direction) => updateAnswer(id, { vote_count: count, my_vote: direction })}
              onAccepted={(id) => setQuestion((q) => ({
                ...q, accepted_answer_id: id, answers: q.answers.map((x) => ({ ...x, is_accepted: x.id === id })),
              }))}
            />
          </div>
        </div>
      )}

      <div className="mt-6">
        <h3 className="text-sm font-semibold text-gray-600 mb-2">
          {others.length} {accepted ? "Other replies" : "Replies"}
        </h3>
        <div className="divide-y divide-gray-100">
          {others.map((a) => (
            <ReplyCard
              key={a.id} answer={a} questionId={questionId} canAccept={canAccept}
              user={user} onDeleted={removeAnswer}
              onEdited={(id, patch) => updateAnswer(id, patch)}
              onVoted={(id, count, direction) => updateAnswer(id, { vote_count: count, my_vote: direction })}
              onAccepted={(id) => setQuestion((q) => ({
                ...q, accepted_answer_id: id, answers: q.answers.map((x) => ({ ...x, is_accepted: x.id === id })),
              }))}
            />
          ))}
        </div>
      </div>

      <div className="mt-4">
        <Composer value={replyText} onChange={setReplyText} placeholder="Write a reply — mention @AiNxt for a bot reply" minHeight="min-h-24" />
        {replyTooShort && <div className="text-[11px] text-amber-600 mt-1">Replies need at least {ANSWER_MIN} characters.</div>}
        <div className="flex justify-end mt-2">
          <button
            onClick={postReply} disabled={posting || replyTooShort}
            className="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {posting ? "Posting…" : "Post reply"}
          </button>
        </div>
      </div>
    </div>
  );
}

function TagsPage({ onSelectTag }) {
  const [tags, setTags] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    authFetch("/discussions/tags?page_size=100").then((r) => r.ok && r.json()).then((d) => {
      setTags(d?.list || d?.tag_list || []);
    }).finally(() => setLoading(false));
  }, []);

  if (loading) return <Spinner />;

  return (
    <div className="px-6 py-5">
      {/* Page header */}
      <div className="mb-5">
        <h2 className="text-base font-semibold text-gray-900">Tags</h2>
        <p className="text-xs text-gray-400 mt-0.5">
          Topics used across discussions — click one to filter the feed. New tags are created automatically when you use them.
        </p>
      </div>
      {!tags.length ? (
        <EmptyState icon={Tags}>No tags yet — start a discussion and add one.</EmptyState>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {tags.map((t) => (
            <button
              key={t.slug_name || t.tag_id} onClick={() => onSelectTag(t.slug_name)}
              className="group text-left p-4 rounded-xl border border-gray-200 bg-white hover:border-indigo-300 hover:bg-indigo-50/30 shadow-sm hover:shadow-md transition-all duration-150 cursor-pointer"
            >
              {/* Tag name row */}
              <div className="flex items-center gap-1.5 mb-2">
                <span className="text-indigo-400 font-bold text-base leading-none">#</span>
                <span className="font-semibold text-gray-900 text-sm group-hover:text-indigo-700 transition-colors truncate">
                  {t.display_name || t.slug_name}
                </span>
              </div>
              {/* Excerpt */}
              {t.excerpt
                ? <p className="text-xs text-gray-500 line-clamp-2 mb-2">{stripHtml(t.excerpt)}</p>
                : <p className="text-xs text-gray-300 italic mb-2">No description yet.</p>
              }
              {/* Count pill */}
              <span className="inline-flex items-center gap-1 text-[11px] font-medium px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-600 tabular-nums">
                <MessagesSquare size={10} />
                {t.question_count ?? 0} {(t.question_count ?? 0) === 1 ? "discussion" : "discussions"}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ContributorsPage() {
  const [ranking, setRanking] = useState(null);
  const [experts, setExperts] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      authFetch("/discussions/users").then((r) => r.ok && r.json()),
      authFetch("/discussions/experts").then((r) => r.ok && r.json()),
    ]).then(([u, e]) => { setRanking(u); setExperts(e); }).finally(() => setLoading(false));
  }, []);

  if (loading) return <Spinner />;

  const expertTags = Object.keys(experts || {}).filter((t) => !TYPE_TAGS.includes(t) && experts[t]?.length);
  const globalGroups = [
    { key: "users_with_the_most_reputation", label: "Most active overall" },
    //{ key: "staffs", label: "Staff" },
  ];
  const hasAny = expertTags.length > 0 || globalGroups.some((g) => (ranking?.[g.key] || []).length);

  // Medal colors for top-3 positions
  const medalColors = ["text-amber-500", "text-gray-400", "text-amber-700"];
  const medalBg    = ["bg-amber-50 border-amber-200", "bg-gray-50 border-gray-200", "bg-amber-50/60 border-amber-100"];

  return (
    <div className="px-6 py-5 space-y-8">
      {/* Page header */}
      <div>
        <h2 className="text-base font-semibold text-gray-900">Contributors</h2>
        <p className="text-xs text-gray-400 mt-0.5">
          Who knows about what — ranked by real replies and votes per topic, not just overall activity.
        </p>
      </div>

      {/* Top experts by topic */}
      {expertTags.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-1.5">
            <Sparkles size={14} className="text-indigo-400" /> Top experts by topic
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {expertTags.map((tag) => (
              <div key={tag} className="rounded-xl border border-gray-200 bg-white shadow-sm p-4">
                {/* Topic header */}
                <div className="flex items-center gap-1.5 mb-3 pb-2 border-b border-gray-100">
                  <span className="text-indigo-400 font-bold">#</span>
                  <span className="text-sm font-semibold text-indigo-700">{tag}</span>
                </div>
                <div className="space-y-2">
                  {experts[tag].map((e, idx) => (
                    <div key={e.author_user_id} className="flex items-center justify-between">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className={`text-xs font-bold w-4 text-center flex-shrink-0 ${medalColors[idx] ?? "text-gray-300"}`}>
                          {idx === 0 ? "🥇" : idx === 1 ? "🥈" : idx === 2 ? "🥉" : `${idx + 1}`}
                        </span>
                        <Avatar name={e.author_name} size="w-6 h-6" />
                        <span className="text-sm text-gray-800 truncate font-medium">{e.author_name}</span>
                      </div>
                      <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-600 flex-shrink-0 tabular-nums">
                        {e.answer_count} {e.answer_count === 1 ? "reply" : "replies"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Global leaderboard groups */}
      {globalGroups.map((g) => {
        const list = ranking?.[g.key] || [];
        if (!list.length) return null;
        return (
          <div key={g.key}>
            <h3 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-1.5">
              <Users size={14} className="text-indigo-400" /> {g.label}
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {list.map((u, idx) => (
                <div
                  key={u.username}
                  className={`flex items-center gap-3 p-3 rounded-xl border ${idx < 3 ? medalBg[idx] : "border-gray-200 bg-white"} shadow-sm`}
                >
                  {/* Rank badge */}
                  <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${
                    idx === 0 ? "bg-amber-100 text-amber-600" :
                    idx === 1 ? "bg-gray-100 text-gray-500" :
                    idx === 2 ? "bg-amber-50 text-amber-700" :
                    "bg-gray-50 text-gray-400"
                  }`}>
                    {idx + 1}
                  </div>
                  <Avatar name={u.display_name || u.username} size="w-7 h-7" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-gray-900 truncate">{u.display_name || u.username}</div>
                    {u.department && <div className="text-[11px] text-gray-400 truncate">{u.department}</div>}
                  </div>
                  <span className="text-xs font-bold text-indigo-600 flex-shrink-0 tabular-nums">{u.rank ?? u.vote_count ?? 0}</span>
                </div>
              ))}
            </div>
          </div>
        );
      })}
      {!hasAny && <EmptyState icon={Users}>No activity yet.</EmptyState>}
    </div>
  );
}

function BadgesPage() {
  const [mine, setMine] = useState([]);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      authFetch("/discussions/badges/mine").then((r) => r.ok && r.json()).catch(() => []),
      authFetch("/discussions/badges").then((r) => r.ok && r.json()),
    ]).then(([m, g]) => { setMine(m || []); setGroups(g || []); }).finally(() => setLoading(false));
  }, []);

  if (loading) return <Spinner />;

  return (
    <div className="px-6 py-5 space-y-8">
      {/* Page header */}
      <div>
        <h2 className="text-base font-semibold text-gray-900">Badges</h2>
        <p className="text-xs text-gray-400 mt-0.5">
          Automatic milestones awarded by the system for asking, answering, and getting upvoted. Nothing to set up.
        </p>
      </div>

      {/* Your badges */}
      <div>
        <h3 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-1.5">
          <Award size={14} className="text-amber-500" /> Your badges
        </h3>
        {mine.length === 0 ? (
          <div className="flex items-center gap-3 p-4 rounded-xl border border-dashed border-gray-200 bg-gray-50">
            <Award size={20} className="text-gray-300 shrink-0" />
            <div>
              <div className="text-sm font-medium text-gray-500">No badges yet</div>
              <div className="text-xs text-gray-400 mt-0.5">Ask or answer a discussion to earn your first badge.</div>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {mine.map((b) => (
              <div key={b.id} className="flex flex-col items-center gap-2 p-4 rounded-xl border border-amber-200 bg-gradient-to-b from-amber-50 to-white shadow-sm text-center">
                <div className="w-10 h-10 rounded-full bg-amber-100 flex items-center justify-center">
                  <Award size={20} className="text-amber-500" />
                </div>
                <div className="text-sm font-semibold text-gray-800">{b.name}</div>
                {b.earned_count > 1 && (
                  <span className="text-[11px] font-bold px-2 py-0.5 rounded-full bg-amber-500 text-white tabular-nums">
                    ×{b.earned_count}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* All badges catalog */}
      {!groups.length ? (
        <EmptyState icon={Award}>No badges awarded yet.</EmptyState>
      ) : (
        <div>
          <h3 className="text-sm font-semibold text-gray-700 mb-4 flex items-center gap-1.5">
            <LayoutGrid size={14} className="text-indigo-400" /> Badge catalog
          </h3>
          <div className="space-y-6">
            {groups.map((g) => (
              <div key={g.group_name}>
                {/* Group header */}
                <div className="flex items-center gap-2 mb-3">
                  <div className="text-xs font-semibold uppercase tracking-widest text-gray-400">{g.group_name}</div>
                  <div className="flex-1 h-px bg-gray-100" />
                  <div className="text-[11px] text-gray-300">{(g.badges || []).length} badges</div>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
                  {(g.badges || []).map((b) => {
                    const earned = mine.some((m) => m.id === b.id);
                    return (
                      <div
                        key={b.id}
                        className={`flex items-center gap-2.5 p-3 rounded-xl border transition-all ${
                          earned
                            ? "border-amber-200 bg-amber-50/60 shadow-sm"
                            : "border-gray-200 bg-white"
                        }`}
                      >
                        <div className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
                          earned ? "bg-amber-100" : "bg-gray-100"
                        }`}>
                          <Award size={15} className={earned ? "text-amber-500" : "text-gray-400"} />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className={`text-xs font-semibold truncate ${earned ? "text-gray-900" : "text-gray-600"}`}>{b.name}</div>
                          {(b.award_count ?? 0) > 0 && (
                            <div className="text-[10px] text-gray-400 tabular-nums">{b.award_count} awarded</div>
                          )}
                        </div>
                        {earned && (
                          <CheckCircle2 size={13} className="text-amber-500 flex-shrink-0" />
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Left rail: quick filters + tag shortcuts, Discussions-section only ──
const STATUS_FILTERS = [
  { key: null, label: "Raised", icon: MessagesSquare },
  { key: "replied", label: "Replied", icon: MessageCircleQuestion },
  { key: "closed", label: "Closed", icon: CheckCircle2 },
];

const TOPIC_VISIBLE_DEFAULT = 5;

function DiscussionsSidebar({ quickFilter, onQuickFilter, tagFilters, onSelectTag,
                              statusFilter, onSelectStatus, isAdmin }) {
  const [tags, setTags]             = useState([]);
  const [topicsExpanded, setTopicsExpanded] = useState(false);

  useEffect(() => {
    authFetch("/discussions/tags?page_size=100").then((r) => r.ok && r.json()).then((d) => {
      const apiTags = (d?.list || d?.tag_list || []).filter((t) => t.slug_name && !TYPE_TAGS.includes(t.slug_name));
      setTags(apiTags);
    }).catch(() => {});
  }, []);

  return (
    <div className="w-56 bg-gray-50 border-r border-gray-200 flex-shrink-0 px-3 py-4 space-y-5 overflow-y-auto">
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-widest text-gray-400 px-2 mb-2">Views</div>
        <div className="space-y-0.5">
          {QUICK_FILTERS.map((f) => (
            <button
              key={f.key} onClick={() => onQuickFilter(f.key)}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-sm transition cursor-pointer ${
                quickFilter === f.key && tagFilters.length === 0
                  ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-indigo-500 pl-[6px]" : "text-gray-600 hover:bg-gray-200/60"
              }`}
            >
              <f.icon size={14} /> {f.label}
            </button>
          ))}
        </div>
      </div>
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-widest text-gray-400 px-2 mb-2">Type</div>
        <div className="space-y-0.5">
          {TYPE_TAGS.map((t) => {
            const TypeIcon = TYPE_ICONS[t];
            const active = tagFilters.length === 1 && tagFilters[0] === t;
            return (
              <button
                key={t} onClick={() => onSelectTag(active ? null : t)}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-sm capitalize transition cursor-pointer ${
                  active ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-indigo-500 pl-[6px]" : "text-gray-600 hover:bg-gray-200/60"
                }`}
              >
                <TypeIcon size={14} /> {t}
              </button>
            );
          })}
        </div>
      </div>
      {isAdmin && (
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-widest text-gray-400 px-2 mb-2">Status</div>
          <div className="space-y-0.5">
            {STATUS_FILTERS.map((s) => {
              const active = (statusFilter ?? null) === s.key;
              return (
                <button
                  key={s.label} onClick={() => onSelectStatus(s.key)}
                  className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-sm transition cursor-pointer ${
                    active ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-indigo-500 pl-[6px]" : "text-gray-600 hover:bg-gray-200/60"
                  }`}
                >
                  <s.icon size={14} /> {s.label}
                </button>
              );
            })}
          </div>
        </div>
      )}
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-widest text-gray-400 px-2 mb-2">Topic</div>
        <div className="space-y-0.5">
          {tags.length === 0 ? (
            <div className="text-xs text-gray-400 px-2">No topics yet.</div>
          ) : (
            <>
              {(topicsExpanded ? tags : tags.slice(0, TOPIC_VISIBLE_DEFAULT)).map((t) => (
                <button
                  key={t.slug_name} onClick={() => onSelectTag(t.slug_name)}
                  className={`w-full flex items-center justify-between px-2 py-1.5 rounded-lg text-sm transition cursor-pointer ${
                    tagFilters.includes(t.slug_name) ? "bg-indigo-50 text-indigo-700 font-semibold border-l-2 border-indigo-500 pl-[6px]" : "text-gray-600 hover:bg-gray-200/60"
                  }`}
                >
                  <span className="truncate">{t.slug_name}</span>
                  <span className="text-[10px] font-medium text-gray-400 bg-gray-200/70 px-1.5 py-0.5 rounded-full flex-shrink-0 tabular-nums">{t.question_count ?? 0}</span>
                </button>
              ))}
              {tags.length > TOPIC_VISIBLE_DEFAULT && (
                <button
                  onClick={() => setTopicsExpanded((e) => !e)}
                  className="w-full flex items-center gap-1 px-2 py-1.5 text-xs text-indigo-500 hover:text-indigo-700 transition cursor-pointer"
                >
                  <ChevronDown
                    size={13}
                    className={`transition-transform duration-200 ${topicsExpanded ? "rotate-180" : ""}`}
                  />
                  {topicsExpanded
                    ? "Show less"
                    : `+ ${tags.length - TOPIC_VISIBLE_DEFAULT} more`}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Discussions({ user }) {
  const [section, setSection] = useState("discussions");
  const [openId, setOpenId] = useState(null);
  const [composing, setComposing] = useState(false);
  const [composeType, setComposeType] = useState("question");
  const [tagFilters, setTagFilters] = useState([]);
  const [quickFilter, setQuickFilter] = useState("all");
  // Status drill-down from the Overview modal. null = "raised" (all of type),
  // "replied" = answer_count>0, "closed" = has accepted answer. Matches /stats.
  const [statusFilter, setStatusFilter] = useState(null);
  const [sort, setSort] = useState("newest");
  const [discussions, setDiscussions] = useState([]);
  const [loadingDiscussions, setLoadingDiscussions] = useState(true);
  const { toast } = useToast();

  // Admin stats modal
  const isAdmin = user?.role === "admin";
  const [statsOpen, setStatsOpen] = useState(false);
  const [statsData, setStatsData] = useState(null);
  const [statsLoading, setStatsLoading] = useState(false);

  async function loadStats() {
    setStatsLoading(true);
    setStatsData(null);
    try {
      const res = await authFetch("/discussions/stats");
      if (res.ok) setStatsData(await res.json());
    } finally {
      setStatsLoading(false);
    }
  }

  const loadDiscussions = useCallback(async () => {
    setLoadingDiscussions(true);
    try {
      const params = new URLSearchParams({ sort });
      tagFilters.forEach((t) => params.append("tag", t));
      if (quickFilter === "unanswered") params.set("unanswered", "true");
      if (quickFilter === "mine") params.set("mine", "true");
      if (statusFilter) params.set("status", statusFilter);
      const resp = await authFetch(`/discussions/questions?${params.toString()}`);
      if (!resp.ok) throw new Error(await errorDetail(resp, "Failed to load Discussions"));
      setDiscussions(await resp.json());
    } catch (e) {
      toast.error(e.message || "Failed to load Discussions");
    } finally {
      setLoadingDiscussions(false);
    }
  }, [sort, tagFilters, quickFilter, statusFilter]);

  useEffect(() => { if (section === "discussions") loadDiscussions(); }, [section, loadDiscussions]);

  function switchSection(key) {
    setSection(key);
    setOpenId(null);
    setComposing(false);
    setStatusFilter(null);
    if (key !== "discussions") setTagFilters([]);
  }

  function startDiscussion(type = "question") {
    setSection("discussions");
    setOpenId(null);
    setComposeType(type);
    setComposing(true);
  }

  function backToList() {
    setOpenId(null);
    setComposing(false);
  }

  function selectTag(tag) {
    setTagFilters(tag ? [tag] : []);
    setSection("discussions");
    setOpenId(null);
    setComposing(false);
    setStatusFilter(null);
  }

  function selectQuickFilter(key) {
    setQuickFilter(key);
    setTagFilters([]);
    setOpenId(null);
    setComposing(false);
    setStatusFilter(null);
  }

  function onDiscussionCreated(id) {
    setComposing(false);
    loadDiscussions();
    setOpenId(id);
  }

  // Overview drill-down: close the modal and land on the Discussions feed
  // filtered to the picked category (Type) + status. loadDiscussions refires
  // via its useEffect when these states change — no extra user action needed.
  function openOverviewSelection(category, status) {
    setStatsOpen(false);
    setSection("discussions");
    setComposing(false);
    setOpenId(null);
    setTagFilters([category]);   // "question" | "feedback" | "issue"
    setQuickFilter("all");       // don't let a view filter fight the status one
    setStatusFilter(status);     // null (raised) | "replied" | "closed"
  }

  const showingList = section === "discussions" && !composing && !openId;

  return (
    <div className="flex flex-col h-full bg-white">
      {/* ── TOP TAB BAR ── */}
      <div className="border-b border-gray-200 px-6 flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-6">
          {SECTIONS.map((s) => (
            <button
              key={s.key} onClick={() => switchSection(s.key)}
              className={`flex items-center gap-1.5 py-3 text-sm transition border-b-2 -mb-px cursor-pointer ${
                section === s.key ? "text-indigo-700 border-indigo-600 font-medium" : "text-gray-500 border-transparent hover:text-gray-700 hover:border-gray-300"
              }`}
            >
              <s.icon size={14} /> {s.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          {isAdmin && (
            <button
              onClick={() => { setStatsOpen(true); loadStats(); }}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-indigo-700 bg-indigo-50 hover:bg-indigo-100 border border-indigo-200 rounded-lg transition-colors cursor-pointer"
            >
              <LayoutGrid size={14} /> Overview
            </button>
          )}
          <button
            onClick={() => startDiscussion()}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition-colors cursor-pointer"
          >
            <Plus size={14} /> Start a discussion
          </button>
        </div>
      </div>

      <div className="flex flex-1 min-h-0">
        {section === "discussions" && (
          <DiscussionsSidebar
            quickFilter={quickFilter} onQuickFilter={selectQuickFilter}
            tagFilters={tagFilters} onSelectTag={selectTag}
            statusFilter={statusFilter} onSelectStatus={setStatusFilter}
            isAdmin={isAdmin}
          />
        )}

        <div className="flex flex-col flex-1 min-w-0">
          {/* ── FILTER / SORT TOOLBAR (Discussions list only) ── */}
          {showingList && (
            <div className="border-b border-gray-100 px-6 py-3 flex items-center gap-3 flex-shrink-0">
              <SearchBox onOpenDiscussion={(id) => setOpenId(id)} onSelectTag={selectTag} />
              <div className="flex items-center gap-2 flex-wrap flex-1 justify-end">
                <TagFilterBar selected={tagFilters} onChange={setTagFilters} />
                <SortControl sort={sort} onChange={setSort} />
              </div>
            </div>
          )}

          {/* ── MAIN CONTENT ── */}
          <div className="flex-1 overflow-y-auto">
            {composing ? (
              <div className="max-w-3xl mx-auto">
                <nav className="px-6 pt-4 pb-1 flex items-center gap-1.5 text-xs min-w-0">
                  <button onClick={backToList} className="flex items-center gap-1 font-medium text-gray-500 hover:text-indigo-600 transition-colors cursor-pointer shrink-0">
                    <ArrowLeft size={13} /> Discussions
                  </button>
                  <span className="text-gray-300 shrink-0">/</span>
                  <span className="text-gray-700 font-medium">New discussion</span>
                </nav>
                <StartDiscussionPage onCancel={backToList} onCreated={onDiscussionCreated} initialType={composeType} />
              </div>
            ) : section === "discussions" ? (
              openId ? (
                <div className="max-w-3xl mx-auto">
                  <nav className="px-6 pt-4 pb-1 flex items-center gap-1.5 text-xs min-w-0">
                    <button onClick={backToList} className="flex items-center gap-1 font-medium text-gray-500 hover:text-indigo-600 transition-colors cursor-pointer shrink-0">
                      <ArrowLeft size={13} /> Discussions
                    </button>
                    <span className="text-gray-300 shrink-0">/</span>
                    <span className="text-gray-700 font-medium truncate">
                      {discussions.find((d) => d.id === openId)?.title ?? "…"}
                    </span>
                  </nav>
                  <DiscussionDetail
                    questionId={openId} user={user}
                    onDeleted={() => { backToList(); loadDiscussions(); }}
                  />
                </div>
              ) : (
                <div className="px-6 py-4">
                  <DiscussionList
                    discussions={discussions} loading={loadingDiscussions}
                    onOpen={(id) => setOpenId(id)}
                    hasFilter={tagFilters.length > 0 || quickFilter !== "all"}
                    onStart={startDiscussion}
                  />
                </div>
              )
            ) : section === "tags" ? (
              <TagsPage onSelectTag={selectTag} />
            ) : section === "contributors" ? (
              <ContributorsPage />
            ) : (
              <BadgesPage />
            )}
          </div>
        </div>
      </div>

      {/* ── ADMIN STATS MODAL ── */}
      {statsOpen && (
        <div
          className="fixed inset-0 bg-black/40 flex items-center justify-center z-50"
          onClick={() => setStatsOpen(false)}
        >
          <div
            className="bg-white rounded-2xl shadow-2xl p-6 w-full max-w-lg mx-4"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center justify-between mb-5">
              <div>
                <h2 className="text-base font-semibold text-slate-900">Discussion Overview</h2>
                <p className="text-xs text-slate-500 mt-0.5">Stats across all discussion types</p>
              </div>
              <button
                onClick={() => setStatsOpen(false)}
                className="text-slate-400 hover:text-slate-600 transition-colors cursor-pointer"
              >
                <X size={18} />
              </button>
            </div>

            {/* Content */}
            {statsLoading ? (
              <div className="space-y-3">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="animate-pulse bg-gradient-to-r from-slate-100 to-slate-50 rounded-xl h-28"
                  />
                ))}
              </div>
            ) : statsData ? (
              <div className="space-y-3">
                {[
                  { key: "question", label: "Questions", Icon: MessageCircleQuestion, accent: "indigo" },
                  { key: "feedback", label: "Feedback",  Icon: MessagesSquare,        accent: "emerald" },
                  { key: "issue",    label: "Issues",    Icon: AlertTriangle,          accent: "rose" },
                ].map(({ key, label, Icon, accent }) => {
                  const d = statsData[key] ?? { total: 0, replied: 0, closed: 0 };
                  const unreplied = d.total - d.replied;
                  const repliedPct = d.total > 0 ? Math.round((d.replied / d.total) * 100) : 0;
                  return (
                    <div
                      key={key}
                      className="border border-slate-200 rounded-xl p-4 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
                    >
                      {/* Type header */}
                      <div className="flex items-center gap-2 mb-3">
                        <Icon size={15} className={`text-${accent}-600`} />
                        <span className="text-sm font-semibold text-slate-800">{label}</span>
                        <span
                          className={`ml-auto text-xs font-bold px-2 py-0.5 rounded-full bg-${accent}-50 text-${accent}-700`}
                        >
                          {d.total} raised
                        </span>
                      </div>

                      {/* 3 stat chips — click to open the feed filtered to this
                          category + status (no extra action needed). */}
                      <div className="grid grid-cols-3 gap-2">
                        <button
                          type="button"
                          onClick={() => openOverviewSelection(key, null)}
                          title={`View all ${label} (Raised)`}
                          className="text-center p-2 bg-slate-50 rounded-lg cursor-pointer transition hover:bg-slate-100 hover:ring-1 hover:ring-slate-300"
                        >
                          <div className="text-xl font-bold text-slate-900">{d.total}</div>
                          <div className="text-[10px] text-slate-500 uppercase tracking-wide mt-0.5">Raised</div>
                        </button>
                        <button
                          type="button"
                          onClick={() => openOverviewSelection(key, "replied")}
                          title={`View replied ${label}`}
                          className="text-center p-2 bg-blue-50 rounded-lg cursor-pointer transition hover:bg-blue-100 hover:ring-1 hover:ring-blue-300"
                        >
                          <div className="text-xl font-bold text-blue-700">{d.replied}</div>
                          <div className="text-[10px] text-blue-500 uppercase tracking-wide mt-0.5">Replied</div>
                        </button>
                        <button
                          type="button"
                          onClick={() => openOverviewSelection(key, "closed")}
                          title={`View closed ${label}`}
                          className="text-center p-2 bg-emerald-50 rounded-lg cursor-pointer transition hover:bg-emerald-100 hover:ring-1 hover:ring-emerald-300"
                        >
                          <div className="text-xl font-bold text-emerald-700">{d.closed}</div>
                          <div className="text-[10px] text-emerald-500 uppercase tracking-wide mt-0.5">Closed</div>
                        </button>
                      </div>

                      {/* Progress bar */}
                      {d.total > 0 && (
                        <div className="mt-3">
                          <div className="flex justify-between text-[10px] text-slate-400 mb-1">
                            <span>{unreplied} awaiting reply</span>
                            <span>{repliedPct}% replied</span>
                          </div>
                          <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
                            <div
                              className="h-full bg-blue-400 rounded-full transition-all"
                              style={{ width: `${repliedPct}%` }}
                            />
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-slate-500 text-center py-8">Failed to load stats.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
