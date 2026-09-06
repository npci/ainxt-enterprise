// SPDX-License-Identifier: MIT
import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { findAndReplace } from "mdast-util-find-and-replace";
import { mdComponents } from "./Message.jsx";
import { API_BASE, authFetch } from "../config";
import { validateIdentifier } from "../utils/securityValidation";
import {
  Loader2,
  RefreshCw,
  RotateCw,
  FileText,
  FolderOpen,
  AlertCircle,
  CheckCircle2,
  BookMarked,
  Play,
  Search,
  ChevronLeft,
  Plus,
  Trash2,
} from "lucide-react";

const POLL_INTERVAL_MS = 3_000;

// Escape a plain string for safe use inside a RegExp -- equivalent to the
// `escape-string-regexp` package (not pulled in as a dependency here since
// this is the only place it's needed).
const escapeRegExp = (str) => str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// A remark plugin (requirement: highlight search-query matches when a
// content-search result is opened) that wraps every case-insensitive
// occurrence of `query` inside the document's plain text in a <mark>
// element, via mdast-util-find-and-replace. Runs on the mdast tree BEFORE
// the remark->rehype conversion, so it only ever touches genuine prose text
// nodes -- code blocks/inline code and link URLs are structurally different
// node types (`code`/`inlineCode`/the text *inside* a `link`'s children is
// still text, so `ignore: ["code", "inlineCode"]` is what actually matters;
// link label text intentionally stays highlightable, e.g. a doc-crossref
// link whose visible label contains the search term).
function remarkHighlightSearch(query) {
  return function attacher() {
    return function transformer(tree) {
      const trimmed = (query || "").trim();
      if (!trimmed) return;
      const re = new RegExp(escapeRegExp(trimmed), "gi");
      findAndReplace(
        tree,
        [
          [
            re,
            (value) => ({
              type: "text",
              value,
              data: {
                hName: "mark",
                hProperties: { className: ["codewiki-search-hit"] },
                hChildren: [{ type: "text", value }],
              },
            }),
          ],
        ],
        { ignore: ["code", "inlineCode"] }
      );
    };
  };
}

// ── Status badge ─────────────────────────────────────────────────────────

function StatusBadge({ status, error }) {
  const base =
    "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium";
  switch (status) {
    case "completed":
      return (
        <span className={`${base} bg-green-100 text-green-700`}>
          <CheckCircle2 className="w-3 h-3" />
          Completed
        </span>
      );
    case "failed":
      return (
        <span className={`${base} bg-red-100 text-red-700`} title={error || undefined}>
          <AlertCircle className="w-3 h-3" />
          Failed
        </span>
      );
    case "running":
      return (
        <span className={`${base} bg-blue-100 text-blue-700`}>
          <Loader2 className="w-3 h-3 animate-spin" />
          Running
        </span>
      );
    case "pending_approval":
      return (
        <span className={`${base} bg-orange-100 text-orange-700`}>
          <AlertCircle className="w-3 h-3" />
          Awaiting approval
        </span>
      );
    case "rejected":
      return (
        <span className={`${base} bg-red-100 text-red-700`} title={error || undefined}>
          <AlertCircle className="w-3 h-3" />
          Rejected
        </span>
      );
    default:
      return (
        <span className={`${base} bg-yellow-100 text-yellow-700`}>
          <Loader2 className="w-3 h-3 animate-spin" />
          Pending
        </span>
      );
  }
}

// ── Wiki grid cell ───────────────────────────────────────────────────────

function WikiCard({ job, onOpen, onRegenerate, onRetry, onDelete, onApprove, onReject, regenerating, retrying, deleting, approving, rejecting, canApprove }) {
  const busy = job.status === "pending" || job.status === "running";
  const failed = job.status === "failed";
  const pendingApproval = job.status === "pending_approval";
  return (
    <div
      onClick={() => { if (!pendingApproval) onOpen(job); }}
      title={pendingApproval ? "Awaiting approval — nothing has started yet" : undefined}
      className={`border border-gray-200 rounded-xl p-4 transition bg-white group flex flex-col h-full ${
        pendingApproval ? "cursor-default opacity-90" : "hover:bg-indigo-50 hover:shadow-sm cursor-pointer"
      }`}
    >
      <div className="flex items-start gap-3 mb-2">
        <div className="w-9 h-9 rounded-lg bg-indigo-50 flex items-center justify-center flex-shrink-0">
          <BookMarked size={18} className="text-indigo-500" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-gray-700 truncate" title={job.codebase_name}>
            {job.codebase_name}
          </p>
          <StatusBadge status={job.status} error={job.error_message} />
        </div>
      </div>

      <p className="text-xs text-gray-500 mb-1 truncate" title={job.repo_url}>
        {job.repo_url}
      </p>
      <p className="text-[10px] text-gray-400 mb-3">
        {job.branch} · Updated {job.updated_at ? new Date(job.updated_at).toLocaleString() : "—"}
      </p>

      <div className="flex-1" />

      <div
        className={`flex justify-end gap-1 transition ${
          failed || (pendingApproval && canApprove) ? "" : "opacity-0 group-hover:opacity-100"
        }`}
      >
        {pendingApproval && canApprove && (
          <>
            <button
              onClick={(e) => { e.stopPropagation(); onApprove(job); }}
              disabled={approving === job.id}
              title="Approve this CodeWiki request"
              className="p-1.5 rounded text-green-600 hover:bg-green-100 disabled:opacity-40 cursor-pointer"
            >
              {approving === job.id ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onReject(job); }}
              disabled={rejecting === job.id}
              title="Reject this CodeWiki request"
              className="p-1.5 rounded text-red-600 hover:bg-red-100 disabled:opacity-40 cursor-pointer"
            >
              {rejecting === job.id ? <Loader2 size={13} className="animate-spin" /> : <AlertCircle size={13} />}
            </button>
          </>
        )}
        {failed && (
          <button
            onClick={(e) => { e.stopPropagation(); onRetry(job.codebase_name); }}
            disabled={retrying === job.codebase_name}
            title="Retry generation"
            className="p-1.5 rounded text-amber-600 hover:bg-amber-100 disabled:opacity-40 cursor-pointer"
          >
            {retrying === job.codebase_name ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <RotateCw size={13} />
            )}
          </button>
        )}
        {job.status === "completed" && (
          <button
            onClick={(e) => { e.stopPropagation(); onRegenerate(job.codebase_name); }}
            disabled={regenerating === job.codebase_name}
            title="Regenerate — checks for changes since the last generated commit first"
            className="p-1.5 rounded text-indigo-600 hover:bg-indigo-100 disabled:opacity-40 cursor-pointer"
          >
            {regenerating === job.codebase_name ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <RefreshCw size={13} />
            )}
          </button>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onDelete(job.codebase_name); }}
          disabled={deleting === job.codebase_name}
          title="Delete"
          className="p-1.5 rounded text-red-600 hover:bg-red-100 disabled:opacity-40 cursor-pointer"
        >
          {deleting === job.codebase_name ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <Trash2 size={13} />
          )}
        </button>
      </div>
    </div>
  );
}

export default function CodeWikiDocs({ user }) {
  const canApprove = user?.role === "admin" || user?.can_approve === true;
  // "grid" — all wikis in a grid | "form" — generate a new wiki | "wiki" — read a single wiki
  const [view, setView] = useState("grid");

  // Generate form state
  const [codebaseName, setCodebaseName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("main");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  // List state
  const [codebases, setCodebases] = useState([]);
  const [loadingList, setLoadingList] = useState(false);
  const [listError, setListError] = useState(null);

  const [regenerating, setRegenerating] = useState(null);
  const [retrying, setRetrying] = useState(null);
  const [deleting, setDeleting] = useState(null);

  // Wiki viewer state
  const [selectedCodebase, setSelectedCodebase] = useState(null);
  const [selectedPage, setSelectedPage] = useState(null);
  const [pageContent, setPageContent] = useState("");
  const [loadingPage, setLoadingPage] = useState(false);
  const [pageError, setPageError] = useState(null);
  const [expandedNodes, setExpandedNodes] = useState(new Set());
  const [pageSearch, setPageSearch] = useState("");

  // The content-search query that led to the currently-open page, if any --
  // used to highlight matching text inside the rendered document (the
  // actual bug being fixed here: clicking a search result opened the page
  // but gave no visual indication of WHERE the match was). Cleared when a
  // page is opened from the normal tree (not search) or when the query
  // itself changes/clears, so stale highlights never linger.
  const [highlightQuery, setHighlightQuery] = useState("");

  // Scroll container for the rendered document -- used by the effect below
  // to jump straight to the first highlighted match instead of leaving the
  // reader to hunt for it in a long page.
  const docScrollRef = useRef(null);

  // Hierarchical module tree (folders/files) for the open codebase, from
  // GET .../page-tree — drives the sidebar's folder/file ordering
  // (Overview -> folders -> files -> orphans). Empty children = no tree
  // available (legacy job predating this endpoint, or module_tree.json
  // missing/unreadable) -- the sidebar then falls back to a flat file
  // listing so nothing regresses.
  const [pageTree, setPageTree] = useState(null);
  const [loadingPageTree, setLoadingPageTree] = useState(false);

  // Content search (requirement 1.3): searches INSIDE every page's raw
  // Markdown via GET .../search, not just page titles like the old
  // client-side filterPageTree() did. searchResults is null when no
  // search is active (renders the normal tree); an array (possibly
  // empty) once a search has resolved.
  const [searchResults, setSearchResults] = useState(null);
  const [searchLoading, setSearchLoading] = useState(false);

  // Live terminal logs for a wiki that is currently pending/running — this
  // is the exact stdout/stderr of the `codewiki generate --github-pages
  // --verbose --output <dir>` CLI subprocess the worker runs.
  const [liveLogs, setLiveLogs] = useState("");

  // Config/worker readiness — checked once up front so a user sees WHY
  // generation will fail/hang before submitting anything, instead of only
  // finding out from a stuck "pending" job or a buried error_message.
  const [wikiStatus, setWikiStatus] = useState(null);

  // ── Helpers ─────────────────────────────────────────────────────────────

  const fetchCodebases = async () => {
    setLoadingList(true);
    setListError(null);
    try {
      const res = await authFetch(`${API_BASE}/codewiki/codebases`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load codebases");
      setCodebases(data);
    } catch (err) {
      setListError(err.message);
    } finally {
      setLoadingList(false);
    }
  };

  // Fetch the hierarchical module tree for a just-opened/just-completed
  // codebase. Best-effort: on any failure, leave pageTree as an empty tree
  // rather than surfacing an error -- the flat `pages` list (already loaded
  // via fetchCodebases) is still fully usable on its own, so a broken/
  // missing module_tree.json should never block viewing documentation.
  const fetchPageTree = async (codebaseName) => {
    setLoadingPageTree(true);
    try {
      const res = await authFetch(
        `${API_BASE}/codewiki/codebases/${encodeURIComponent(codebaseName)}/page-tree`
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load page tree");
      setPageTree(data);
    } catch {
      setPageTree({ root_label: codebaseName, children: [] });
    } finally {
      setLoadingPageTree(false);
    }
  };

  // ── Effects ─────────────────────────────────────────────────────────────

  useEffect(() => {
    fetchCodebases();
  }, []);

  // Checked once on mount — deliberately not polled, since fixing either
  // condition requires a manual restart/config edit outside the UI anyway.
  useEffect(() => {
    authFetch(`${API_BASE}/codewiki/status`)
      .then((r) => r.json())
      .then(setWikiStatus)
      .catch(() => {});
  }, []);

  // Keep polling the list while any wiki is still generating so cells/status update live.
  useEffect(() => {
    const hasActive = codebases.some((c) => c.status === "pending" || c.status === "running");
    if (!hasActive) return;
    const timer = setInterval(fetchCodebases, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [codebases]);

  // Keep the open wiki in sync with the latest list data (status/pages updates).
  useEffect(() => {
    setSelectedCodebase((prev) => {
      if (!prev) return prev;
      const updated = codebases.find((c) => c.id === prev.id);
      return updated || prev;
    });
  }, [codebases]);

  // If the open codebase transitions into "completed" while already open
  // (e.g. it was pending/running at handleOpenWiki() time), fetch its page
  // tree now -- handleOpenWiki() only fetches it when the job is ALREADY
  // completed at open time.
  useEffect(() => {
    if (selectedCodebase?.status === "completed" && pageTree === null && !loadingPageTree) {
      fetchPageTree(selectedCodebase.codebase_name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCodebase?.status]);

  // While the currently-open wiki is pending/running, poll its live CLI
  // logs so the UI shows the same terminal output a manual
  // `codewiki generate --github-pages --verbose --output <dir>` run would.
  useEffect(() => {
    if (view !== "wiki" || !selectedCodebase) return;
    if (selectedCodebase.status !== "pending" && selectedCodebase.status !== "running") return;

    let cancelled = false;
    const fetchLogs = async () => {
      try {
        const res = await authFetch(
          `${API_BASE}/codewiki/codebases/${encodeURIComponent(selectedCodebase.codebase_name)}/logs`
        );
        const data = await res.json();
        if (!cancelled && res.ok) setLiveLogs(data.logs || "");
      } catch {
        // best-effort — a failed poll just retries on the next tick
      }
    };
    fetchLogs();
    const timer = setInterval(fetchLogs, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // Deliberately keyed on codebase_name/status, not the whole object --
    // selectedCodebase is a new object reference on every codebases poll
    // tick even when name/status haven't changed, which would otherwise
    // restart this log-polling effect every 3s for no reason.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, selectedCodebase?.codebase_name, selectedCodebase?.status]);

  // Debounced full-text content search (requirement 1.3) — searches INSIDE
  // every generated page's raw Markdown via the backend, not just titles.
  // Clearing the query (pageSearch === "") resets searchResults to null,
  // which switches the sidebar back to the normal folder/file tree.
  useEffect(() => {
    const query = pageSearch.trim();
    if (!query || !selectedCodebase || selectedCodebase.status !== "completed") {
      setSearchResults(null);
      setSearchLoading(false);
      return;
    }
    let cancelled = false;
    setSearchLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await authFetch(
          `${API_BASE}/codewiki/codebases/${encodeURIComponent(
            selectedCodebase.codebase_name
          )}/search?q=${encodeURIComponent(query)}`
        );
        const data = await res.json();
        if (!cancelled && res.ok) setSearchResults(data.results || []);
      } catch {
        // best-effort — leave the previous results/tree visible on failure
      } finally {
        if (!cancelled) setSearchLoading(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // Same rationale as the log-polling effect above -- keyed on the
    // specific fields that should actually retrigger a search, not the
    // whole (frequently-new-reference) selectedCodebase object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageSearch, selectedCodebase?.codebase_name, selectedCodebase?.status]);

  // After a page opened from a search result finishes loading and rendering
  // its highlighted matches, jump the scroll container to the first one --
  // otherwise the highlight could be scrolled far below the fold in a long
  // document and the user would have no way to tell it actually worked.
  // rAF (rather than a plain effect) waits one paint so ReactMarkdown has
  // definitely committed the <mark> elements to the DOM before querying.
  useEffect(() => {
    if (!highlightQuery || loadingPage || !pageContent) return;
    const raf = requestAnimationFrame(() => {
      const container = docScrollRef.current;
      const firstHit = container?.querySelector('[data-search-hit="true"]');
      firstHit?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(raf);
  }, [highlightQuery, loadingPage, pageContent]);

  // ── Handlers ────────────────────────────────────────────────────────────

  const resetForm = () => {
    setCodebaseName("");
    setRepoUrl("");
    setBranch("main");
    setSubmitError(null);
  };

  const handleGenerate = async (e) => {
    e.preventDefault();

    // Client-side pre-check — mirrors the server-side validate_identifier()
    // in core/security_validation.py. The backend remains the authoritative
    // enforcer; this just gives faster feedback and stops obviously-bad
    // input before it hits the network.
    const nameCheck = validateIdentifier(codebaseName);
    if (!nameCheck.isValid) {
      setSubmitError(nameCheck.errors[0]?.message || "Invalid codebase name");
      return;
    }
    const branchCheck = validateIdentifier(branch);
    if (!branchCheck.isValid) {
      setSubmitError(branchCheck.errors[0]?.message || "Invalid branch name");
      return;
    }

    setSubmitting(true);
    setSubmitError(null);

    try {
      const res = await authFetch(`${API_BASE}/codewiki/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          codebase_name: codebaseName.trim(),
          repo_url: repoUrl.trim(),
          branch: branch.trim() || "main",
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to start generation");

      // Optimistically add the new wiki as a cell in the grid, then refresh from server.
      setCodebases((prev) => [...prev, data]);
      resetForm();
      setView("grid");
      fetchCodebases();
    } catch (err) {
      setSubmitError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const [approving, setApproving] = useState(null);
  const [rejecting, setRejecting] = useState(null);

  const handleApprove = async (job) => {
    setApproving(job.id);
    try {
      const res = await authFetch(`${API_BASE}/codewiki/requests/${job.id}/approve`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Approve failed");
      fetchCodebases();
    } catch (err) {
      window.alert(err.message);
    } finally {
      setApproving(null);
    }
  };

  const handleReject = async (job) => {
    const note = window.prompt("Rejection reason (optional):") || "";
    setRejecting(job.id);
    try {
      const res = await authFetch(`${API_BASE}/codewiki/requests/${job.id}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Reject failed");
      fetchCodebases();
    } catch (err) {
      window.alert(err.message);
    } finally {
      setRejecting(null);
    }
  };

  const handleRegenerate = async (name) => {
    setSubmitError(null);

    try {
      const res = await authFetch(`${API_BASE}/codewiki/regenerate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ codebase_name: name, confirm: false }),
      });
      const dry = await res.json();
      if (!res.ok) throw new Error(dry.detail || "Failed to compute changes");

      // codewiki's own `--update --compare-to <sha>` computes the actual
      // file/module-level diff once a job runs — this dry-run only answers
      // "has anything changed since the last documented commit", via a
      // cheap remote check (no clone).
      if (!dry.latest_commit_sha && !dry.current_commit_sha) {
        window.alert(dry.note || "Documentation is already up to date.");
        return;
      }

      const confirmed = window.confirm(`${dry.note || "Regenerate this documentation?"}\n\nProceed?`);
      if (!confirmed) return;

      setRegenerating(name);

      const res2 = await authFetch(`${API_BASE}/codewiki/regenerate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ codebase_name: name, confirm: true }),
      });
      const data = await res2.json();
      if (!res2.ok) throw new Error(data.detail || "Failed to regenerate");

      setSelectedCodebase((prev) => {
        if (!prev || prev.codebase_name !== name) return prev;
        setSelectedPage(null);
        setPageContent("");
        return { ...prev, status: data.status || "pending" };
      });

      fetchCodebases();
    } catch (err) {
      setSubmitError(err.message);
      window.alert(err.message);
    } finally {
      setRegenerating(null);
    }
  };

  // Re-run a failed generation from scratch — no dry-run/confirmation step,
  // unlike Regenerate (which diffs against the last successful commit; not
  // meaningful for a job that never completed).
  const handleRetry = async (name) => {
    setSubmitError(null);
    setRetrying(name);

    try {
      const res = await authFetch(`${API_BASE}/codewiki/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ codebase_name: name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to retry generation");

      setSelectedCodebase((prev) => {
        if (!prev || prev.codebase_name !== name) return prev;
        setSelectedPage(null);
        setPageContent("");
        setLiveLogs("");
        return { ...prev, status: data.status || "pending", error_message: null };
      });

      fetchCodebases();
    } catch (err) {
      setSubmitError(err.message);
      window.alert(err.message);
    } finally {
      setRetrying(null);
    }
  };

  const handleDelete = async (name) => {
    const confirmed = window.confirm(`Delete documentation for "${name}"? This cannot be undone.`);
    if (!confirmed) return;
    setSubmitError(null);
    setDeleting(name);

    try {
      const res = await authFetch(`${API_BASE}/codewiki/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ codebase_name: name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to delete");

      setSelectedCodebase((prev) => {
        if (prev && prev.codebase_name === name) {
          setSelectedPage(null);
          setPageContent("");
          setView("grid");
          return null;
        }
        return prev;
      });
      fetchCodebases();
    } catch (err) {
      setSubmitError(err.message);
      window.alert(err.message);
    } finally {
      setDeleting(null);
    }
  };

  const handleOpenWiki = (job) => {
    // Cards are unclickable while pending_approval (see WikiCard) -- this is
    // a second guard against opening one anyway (e.g. a future caller other
    // than the card's own onClick), since nothing has actually started yet.
    if (job.status === "pending_approval") return;
    setSelectedCodebase(job);
    setSelectedPage(null);
    setPageContent("");
    setPageError(null);
    setLiveLogs("");
    setExpandedNodes(new Set([job.id]));
    setPageSearch("");
    setSearchResults(null);
    setPageTree(null);
    setHighlightQuery("");
    if (job.status === "completed") fetchPageTree(job.codebase_name);
    setView("wiki");
  };

  // `highlight` is the content-search query to highlight inside the
  // rendered document, passed only when navigating here FROM a search
  // result click. Any other caller (tree click, in-doc crossref link) omits
  // it, which clears a stale highlight from a previously-viewed search hit.
  const handleSelectPage = async (page, highlight = "") => {
    if (!selectedCodebase) return;
    setSelectedPage(page);
    setLoadingPage(true);
    setPageError(null);
    setHighlightQuery(highlight);

    try {
      const path = page.relative_path || page.path || page.name;
      const res = await authFetch(
        `${API_BASE}/codewiki/codebases/${encodeURIComponent(
          selectedCodebase.codebase_name
        )}/pages/${encodeURIComponent(path)}`
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to load page");
      setPageContent(data.content || "");
    } catch (err) {
      setPageError(err.message);
      setPageContent("");
    } finally {
      setLoadingPage(false);
    }
  };

  // Generated docs cross-reference each other with plain relative markdown
  // links, e.g. [desktop_app](desktop_app.md) -- correct as MARKDOWN, but
  // rendered as a literal <a href="desktop_app.md"> it makes the browser
  // navigate the whole page relative to the current URL, breaking out of
  // this SPA entirely (landing wherever that path happens to route to,
  // e.g. the chat page) instead of switching to that page within the wiki
  // viewer. Match the link target's filename against the codebase's own
  // flat page list, and switch pages in-app when it resolves to a real one.
  const resolveInternalDocPage = (href) => {
    if (!href || !selectedCodebase?.pages) return null;
    // Only ever treat a genuinely RELATIVE link as a candidate -- an
    // absolute URL (any scheme, or a protocol-relative "//host/...") is
    // never something CodeWiki itself generated as a cross-reference, even
    // if it happens to end in a filename that matches one of this
    // codebase's own pages (e.g. a GitHub blob URL).
    if (/^([a-z][a-z0-9+.-]*:)?\/\//i.test(href) || href.startsWith("mailto:")) return null;
    // Strip any query string/hash and leading ./ or ../ segments, then
    // compare basenames -- relative links may be written as "foo.md",
    // "./foo.md", or occasionally with a subfolder prefix.
    const clean = href.split(/[?#]/)[0];
    const base = clean.split("/").pop();
    if (!base || !base.toLowerCase().endsWith(".md")) return null;
    const findIn = (pages) => {
      for (const p of pages || []) {
        const pFile = (p.file || p.name || "").toLowerCase();
        const pPath = (p.relative_path || p.path || "").toLowerCase();
        if (pFile === base.toLowerCase() || pPath.endsWith("/" + base.toLowerCase()) || pPath === base.toLowerCase()) {
          return p;
        }
        if (p.children) {
          const found = findIn(p.children);
          if (found) return found;
        }
      }
      return null;
    };
    return findIn(selectedCodebase.pages);
  };

  // mdComponents.a is shared with chat/other viewers (opens external links
  // in a new tab) -- override only `a` here, for this component, so an
  // internal doc-to-doc link switches pages in-app instead of triggering a
  // real browser navigation, while anything that ISN'T a resolvable page
  // link (a genuine external URL) still falls back to the shared behavior.
  const docMdComponents = useMemo(() => ({
    ...mdComponents,
    a: ({ href, children }) => {
      const internalPage = resolveInternalDocPage(href);
      if (internalPage) {
        return (
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              handleSelectPage(internalPage);
            }}
            className="text-indigo-600 hover:text-indigo-800 underline
                       underline-offset-2 transition-colors cursor-pointer"
          >
            {children}
          </a>
        );
      }
      return mdComponents.a({ href, children });
    },
    // Renders the <mark> elements the remarkHighlightSearch plugin below
    // inserts around content-search matches. `data-search-hit` is a plain
    // marker (not read by anything) purely so the "scroll to first match"
    // effect can find the right node via a CSS selector.
    mark: ({ children }) => (
      <mark
        data-search-hit="true"
        className="bg-yellow-200 text-inherit rounded-sm px-0.5"
      >
        {children}
      </mark>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [selectedCodebase?.pages]);

  // Only recompute the remark plugin list when the highlight query actually
  // changes (not on every render) -- remarkHighlightSearch('') is a no-op
  // transformer, so an empty query safely renders the document with no
  // highlighting at all, same as before this feature existed.
  const docMdRemarkPlugins = useMemo(
    () => [remarkGfm, remarkHighlightSearch(highlightQuery)],
    [highlightQuery]
  );

  const toggleNode = (id) => {
    setExpandedNodes((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // ── Render helpers ──────────────────────────────────────────────────────

  // Highlight the matched substring within a label/snippet so a match is
  // easy to spot at a glance. Reused for both search-result snippets and
  // (defensively) any plain label text.
  const highlightMatch = (label, query) => {
    if (!query) return label;
    const idx = label.toLowerCase().indexOf(query.toLowerCase());
    if (idx === -1) return label;
    return (
      <>
        {label.slice(0, idx)}
        <mark className="bg-yellow-200 text-inherit rounded-sm">
          {label.slice(idx, idx + query.length)}
        </mark>
        {label.slice(idx + query.length)}
      </>
    );
  };

  // Build the sidebar's ordered page list (requirement 1.2): Overview
  // first, then every folder module (alphabetical, each expandable to its
  // own children via the existing toggle behavior), then every remaining
  // single file (alphabetical). Falls back to a flat, alphabetical (minus
  // Overview) listing of `selectedCodebase.pages` if no page-tree could be
  // built (legacy job, or module_tree.json missing/unreadable) -- this is
  // the exact previous "flat list" behavior, just still Overview-first.
  const buildOrderedPages = () => {
    if (!selectedCodebase) return [];
    const flatPages = selectedCodebase.pages || [];
    const overviewPage = flatPages.find((p) =>
      (p.file || p.name || "").toLowerCase().startsWith("overview")
    );

    const sortByTitle = (a, b) => (a.title || "").localeCompare(b.title || "");
    const bucketAndSort = (children) => {
      const folders = (children || []).filter((c) => c.type === "folder").sort(sortByTitle);
      const files = (children || []).filter((c) => c.type === "file").sort(sortByTitle);
      return [...folders, ...files].map((c) => ({
        title: c.title,
        path: c.path,
        file: c.path,
        // Recurse so nested folders (arbitrary depth) get the same
        // folders-then-files ordering at every level, not just the top.
        children: c.type === "folder" ? bucketAndSort(c.children) : undefined,
      }));
    };

    const hasTree = pageTree && Array.isArray(pageTree.children) && pageTree.children.length > 0;
    const bodyPages = hasTree
      ? bucketAndSort(pageTree.children)
      : flatPages
          .filter((p) => p !== overviewPage)
          .map((p) => ({ title: p.title, path: p.path || p.file, file: p.file }))
          .sort(sortByTitle);

    return overviewPage ? [overviewPage, ...bodyPages] : bodyPages;
  };

  const renderPageTree = (pages, parentId = "") => {
    if (!pages || pages.length === 0) return null;

    return (
      <ul className="space-y-1">
        {pages.map((page, idx) => {
          const nodeId = `${parentId}-${idx}`;
          const isDir = page.children && page.children.length > 0;
          const isExpanded = expandedNodes.has(nodeId);
          const label = page.title || page.name || "";

          return (
            <li key={nodeId}>
              <button
                onClick={() => (isDir ? toggleNode(nodeId) : handleSelectPage(page))}
                className={`flex items-center gap-2 w-full text-left px-2 py-1.5 rounded-md text-sm transition-colors ${
                  selectedPage === page
                    ? "bg-indigo-50 text-indigo-700 font-medium"
                    : "text-slate-700 hover:bg-slate-100"
                }`}
              >
                {isDir ? (
                  <FolderOpen className="w-4 h-4 text-amber-500" />
                ) : (
                  <FileText className="w-4 h-4 text-slate-400" />
                )}
                <span className="truncate">{label}</span>
              </button>
              {isDir && isExpanded && (
                <div className="ml-5 mt-1">
                  {renderPageTree(page.children, nodeId)}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  };

  // Flat results list for content search (requirement 1.3) — replaces the
  // tree entirely while a search query is active, since content matches
  // don't map cleanly onto folder structure the way a title match did.
  const renderSearchResults = () => {
    const query = pageSearch.trim();
    if (searchLoading && searchResults === null) {
      return (
        <div className="flex items-center gap-2 text-xs text-slate-400 px-2 py-1">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          Searching…
        </div>
      );
    }
    if (!searchResults || searchResults.length === 0) {
      return <p className="text-xs text-slate-400 px-2 py-1">No pages contain "{query}".</p>;
    }
    return (
      <ul className="space-y-1">
        {searchResults.map((r) => (
          <li key={r.path}>
            <button
              onClick={() =>
                handleSelectPage({ title: r.title, path: r.path, file: r.file }, query)
              }
              className={`w-full text-left px-2 py-1.5 rounded-md text-sm transition-colors ${
                selectedPage?.path === r.path
                  ? "bg-indigo-50 text-indigo-700"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              <div className="flex items-center gap-2">
                <FileText className="w-4 h-4 text-slate-400 flex-shrink-0" />
                <span className="truncate font-medium">{r.title}</span>
                <span className="text-[10px] text-slate-400 flex-shrink-0">
                  {r.match_count}×
                </span>
              </div>
              <p className="text-[11px] text-slate-500 pl-6 truncate">
                {highlightMatch(r.snippet, query)}
              </p>
            </button>
          </li>
        ))}
      </ul>
    );
  };

  // ── Render: dedicated wiki-reading page (maximum space, minimal chrome) ──

  if (view === "wiki" && selectedCodebase) {
    return (
      <div className="flex flex-col h-full bg-white">
        {/* py-3 (not py-2) so this bar's height matches the sidebar's own
            logo bar and the grid view's header below -- keeps the bottom
            border lines aligned across all three regardless of which view
            is showing. */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-200 flex-shrink-0">
          <button
            onClick={() => setView("grid")}
            className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition cursor-pointer"
            title="Back to all wikis"
          >
            <ChevronLeft size={18} />
          </button>
          <span className="text-sm font-semibold text-gray-700 truncate">
            {selectedCodebase.codebase_name}
          </span>
          <StatusBadge status={selectedCodebase.status} error={selectedCodebase.error_message} />
          {selectedCodebase.status === "failed" && (
            <button
              onClick={() => handleRetry(selectedCodebase.codebase_name)}
              disabled={retrying === selectedCodebase.codebase_name}
              className="ml-1 flex items-center gap-1 text-xs font-medium px-2 py-1 rounded text-amber-700 bg-amber-50 hover:bg-amber-100 disabled:opacity-40 cursor-pointer"
            >
              {retrying === selectedCodebase.codebase_name ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <RotateCw size={12} />
              )}
              Retry
            </button>
          )}
        </div>

        <div className="flex-1 flex overflow-hidden">
          {/* Page tree — ALWAYS visible regardless of whether the main
              content area (to the right) is showing the graph or a
              document; only the main content area swaps between the two. */}
          <div className="w-64 border-r border-slate-200 overflow-y-auto p-3 bg-slate-50 flex-shrink-0">
            <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">
              Pages
            </h4>
            {selectedCodebase.status === "completed" && (
              <div className="relative mb-3">
                <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none" />
                <input
                  type="text"
                  value={pageSearch}
                  onChange={(e) => setPageSearch(e.target.value)}
                  placeholder="Search page contents…"
                  className="w-full pl-7 pr-2 py-1.5 text-xs border border-slate-200 rounded-md bg-white focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
                />
              </div>
            )}
            {selectedCodebase.status === "completed" ? (
              pageSearch.trim() ? (
                renderSearchResults()
              ) : loadingPageTree ? (
                <div className="flex items-center gap-2 text-xs text-slate-400 px-2 py-1">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  Loading pages…
                </div>
              ) : (
                renderPageTree(buildOrderedPages())
              )
            ) : selectedCodebase.status === "failed" ? (
              <div className="p-3 bg-red-50 text-red-700 rounded-lg text-sm">
                <p>{selectedCodebase.error_message || "Documentation generation failed."}</p>
                <button
                  onClick={() => handleRetry(selectedCodebase.codebase_name)}
                  disabled={retrying === selectedCodebase.codebase_name}
                  className="mt-2 flex items-center gap-1.5 text-xs font-medium px-2.5 py-1.5 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-40 cursor-pointer"
                >
                  {retrying === selectedCodebase.codebase_name ? (
                    <Loader2 size={13} className="animate-spin" />
                  ) : (
                    <RotateCw size={13} />
                  )}
                  Retry generation
                </button>
              </div>
            ) : selectedCodebase.status === "pending_approval" ? (
              <div className="text-sm text-slate-500">Awaiting approval — generation hasn't started yet.</div>
            ) : (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="w-4 h-4 animate-spin" />
                Generating documentation…
              </div>
            )}
          </div>

          {/* Page content — given maximum space. pt-3 (not p-8's default top
              padding) so the first heading sits close to the top bar instead
              of leaving a large empty gap above it. */}
          <div ref={docScrollRef} className="flex-1 overflow-y-auto pt-3 px-8 pb-8 bg-white">
            {selectedCodebase.status === "pending" || selectedCodebase.status === "running" ? (
              <div className="h-full flex flex-col">
                <div className="flex items-center gap-2 text-sm text-slate-600 mb-3 flex-shrink-0">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Running <code className="bg-slate-100 px-1.5 py-0.5 rounded text-xs">
                    codewiki generate --github-pages --verbose --output &lt;dir&gt;
                  </code>
                </div>
                <pre className="flex-1 overflow-auto bg-slate-900 text-slate-100 text-xs leading-relaxed rounded-lg p-4 whitespace-pre-wrap break-words">
                  {liveLogs || "Waiting for output…"}
                </pre>
              </div>
            ) : selectedPage ? (
              <>
                {loadingPage ? (
                  <div className="flex items-center gap-2 text-slate-500">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Loading page…
                  </div>
                ) : pageError ? (
                  <div className="p-4 bg-red-50 text-red-700 rounded-lg">{pageError}</div>
                ) : (
                  <div className="prose prose-slate max-w-none">
                    <ReactMarkdown
                      remarkPlugins={docMdRemarkPlugins}
                      components={docMdComponents}
                      remarkRehypeOptions={{ allowDangerousHtml: true }}
                    >
                      {pageContent}
                    </ReactMarkdown>
                  </div>
                )}
              </>
            ) : (
              <div className="h-full flex flex-col items-center justify-center text-slate-400">
                <Search className="w-12 h-12 mb-3 opacity-40" />
                <p className="text-sm">Select a page from the sidebar to view it.</p>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ── Render: header + grid / form ─────────────────────────────────────────

  return (
    <div className="flex flex-col h-full bg-gray-50">
      {/* py-3 (not py-4) so this bar's height matches the sidebar's own logo
          bar and the wiki-detail view's header -- keeps the bottom border
          lines aligned across all three regardless of which view is showing. */}
      <div className="flex items-center gap-3 px-6 py-3 border-b border-gray-200 bg-white flex-shrink-0">
        {view === "form" && (
          <button
            onClick={() => setView("grid")}
            className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition cursor-pointer"
            title="Back"
          >
            <ChevronLeft size={18} />
          </button>
        )}
        <BookMarked size={18} className="text-indigo-700" />
        <h1 className="text-sm font-semibold text-indigo-700">
          {view === "form" ? "New Wiki" : "Wiki"}
        </h1>
        <div className="flex-1" />
        {view === "grid" && (
          <button
            onClick={() => { resetForm(); setView("form"); }}
            className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded text-white cursor-pointer brand-grad hover:opacity-70"
          >
            <Plus size={12} /> New Wiki
          </button>
        )}
      </div>

      {/* Config/worker readiness — shown regardless of which view (grid or
          form) is active, so it's visible the moment the section opens, not
          just after clicking "New Wiki". */}
      {wikiStatus?.missing_env?.length > 0 && (
        <div className="mx-6 mt-3 p-3 bg-amber-50 border border-amber-200 rounded text-sm text-amber-700 flex-shrink-0">
          <strong>Heads up:</strong> CodeWiki is not configured — the following required
          variable(s) are missing from <code>.env</code>:{" "}
          <strong>{wikiStatus.missing_env.join(", ")}</strong>. CodeWiki needs its own
          OpenAI-compatible LLM endpoint — it does not reuse the platform's chat provider key.
        </div>
      )}
      {wikiStatus && !wikiStatus.worker_running && (
        <div className="mx-6 mt-3 p-3 bg-amber-50 border border-amber-200 rounded text-sm text-amber-700 flex-shrink-0">
          <strong>Heads up:</strong> <code>codewiki-worker</code> is not running — generation
          requests will stay stuck indefinitely.
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {view === "form" ? (
          <div className="max-w-2xl mx-auto p-8 h-full overflow-y-auto">
            <div className="bg-white border border-slate-200 rounded-xl shadow-sm p-6">
              <h2 className="text-lg font-semibold text-slate-900 mb-1">Generate a wiki</h2>
              <p className="text-sm text-slate-500 mb-6">
                Clone a public GitHub repository and generate an LLM-powered wiki for it.
              </p>

              <form onSubmit={handleGenerate} className="space-y-5">
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1">
                    Codebase name
                  </label>
                  <input
                    type="text"
                    value={codebaseName}
                    onChange={(e) => setCodebaseName(e.target.value)}
                    placeholder="e.g., my-project"
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
                    required
                  />
                  <p className="text-xs text-slate-500 mt-1">
                    This must be unique. It will appear in the wiki grid.
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1">
                    GitHub repository URL
                  </label>
                  <input
                    type="url"
                    value={repoUrl}
                    onChange={(e) => setRepoUrl(e.target.value)}
                    placeholder="https://github.com/owner/repo"
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
                    required
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-1">Branch</label>
                  <input
                    type="text"
                    value={branch}
                    onChange={(e) => setBranch(e.target.value)}
                    placeholder="main"
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
                  />
                </div>

                {submitError && (
                  <div className="flex items-start gap-2 p-3 bg-red-50 text-red-700 rounded-lg text-sm">
                    <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                    {submitError}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={submitting}
                  className="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white font-medium py-2.5 rounded-lg transition-colors"
                >
                  {submitting ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Starting…
                    </>
                  ) : (
                    <>
                      <Play className="w-4 h-4" />
                      Generate Wiki
                    </>
                  )}
                </button>
              </form>
            </div>
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto px-6 py-5 h-full">
            {loadingList && codebases.length === 0 && (
              <div className="flex items-center justify-center h-32 gap-2 text-gray-400">
                <Loader2 size={18} className="animate-spin" />
                <span className="text-sm">Loading wikis…</span>
              </div>
            )}

            {listError && (
              <div className="p-3 bg-red-50 text-red-700 rounded-lg text-sm max-w-md">
                {listError}
              </div>
            )}

            {codebases.length === 0 && !loadingList && !listError && (
              <div className="h-full flex flex-col items-center justify-center text-slate-400">
                <BookMarked className="w-16 h-16 mb-4 opacity-40" />
                <p className="text-lg font-medium text-slate-600">No wikis yet</p>
                <p className="text-sm">Click "+ New Wiki" to generate one.</p>
              </div>
            )}

            {codebases.length > 0 && (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
                {codebases.map((job) => (
                  <WikiCard
                    key={job.id}
                    job={job}
                    onOpen={handleOpenWiki}
                    onRegenerate={handleRegenerate}
                    onRetry={handleRetry}
                    onDelete={handleDelete}
                    onApprove={handleApprove}
                    onReject={handleReject}
                    regenerating={regenerating}
                    retrying={retrying}
                    deleting={deleting}
                    approving={approving}
                    rejecting={rejecting}
                    canApprove={canApprove && job.requested_by !== user?.email}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
