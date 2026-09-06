// SPDX-License-Identifier: MIT
import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import {
  FolderKanban, Plus, Trash2, Edit2, SendHorizontal, CirclePauseIcon,
  Sparkles, Loader2, X, Copy, Check, Pencil, ImageIcon,
  Paperclip, FileText, ShieldOff, GitBranch,
} from "lucide-react";
import MessageMeta from "./MessageMeta";
import { motion } from "framer-motion";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { mdComponents } from "./Message";

import { API_BASE as API, authFetch } from '../config';
import AiNxtSpinner from "./AiNxtSpinner";
import { toISTDate } from "../utils/time";
import { useConfirm, useToast } from './ui/DialogProvider';
import {
  validateProductName,
  validateDescription,
  validateRepoName,
  validateProductCode,
  validateSecurity,
  getErrorMessage,
} from "../utils/securityValidation";
import { useFileDrop } from '../hooks/useFileDrop';
import { setLocalData, getLocalData, removeLocalData } from '../utils/storageUtils';

// Strip <!--MEMORY:{...}--> tags injected by the backend before rendering
function stripMemoryTag(text) {
  return typeof text === "string"
    ? text.replace(/<!--MEMORY:\{.*?\}-->/gs, "").trimEnd()
    : text;
}

// Strip [STYLE INSTRUCTION:...] / [CONTEXT:...] prefixes injected by the backend
function stripSystemPrefix(text) {
  return typeof text === "string"
    ? text.replace(/^\[(?:STYLE INSTRUCTION|CONTEXT):[^\]]*\]\s*/i, "")
    : text;
}

export default function Projects({
  user,
  projectMessages,
  setProjectMessages,
  projectLoading,
  setProjectLoading,
  activeProjectId,
  setActiveProjectId,
  projectAbortRef,
}) {
  // ── Active workspace — must be declared first so the derived aliases below can read it ──
  const [selected, setSelected]   = useState(null);

  // ── Per-workspace derived aliases ─────────────────────────────────────────
  // projectMessages and projectLoading are dicts keyed by project ID in App.jsx.
  // These aliases scope all reads/writes to the currently selected workspace so
  // the rest of the component can use `messages`, `loading`, etc. naturally.
  const messages = useMemo(
    () => (selected?.id ? (projectMessages[selected.id] ?? []) : []),
    [selected?.id, projectMessages] // eslint-disable-line react-hooks/exhaustive-deps -- selected.id is the stable key
  );

  const setMessages = useCallback((updater) => {
    if (!selected?.id) return;
    const id = selected.id;
    setProjectMessages(prev => ({
      ...prev,
      [id]: typeof updater === 'function' ? updater(prev[id] ?? []) : updater,
    }));
  }, [selected?.id, setProjectMessages]); // eslint-disable-line react-hooks/exhaustive-deps -- selected.id is the stable key

  const loading = selected?.id ? (projectLoading[selected.id] ?? false) : false;

  const abortRef      = projectAbortRef;
  const requestIdRef  = useRef(null);   // X-Request-ID for cooperative backend stop

  const [projects, setProjects]   = useState([]);
  const [repos, setRepos]         = useState([]);
  // Embed-svc reachability — only matters once a project actually has a
  // codebase attached (routers/projects_router.py's /projects/{id}/ask
  // always injects repo context + forces RAG retrieval once repo_name is
  // set, per "the orchestrator never classifies a project-scoped question
  // as 'general' and skips RAG retrieval"). Checked once on mount, same
  // check index_router.py already exposes for the Codebase panel.
  const [embedSvcReachable, setEmbedSvcReachable] = useState(true);
  useEffect(() => {
    authFetch(`${API}/index/embed-svc-status`)
      .then(r => r.json())
      .then(d => setEmbedSvcReachable(d.embed_svc_reachable !== false))
      .catch(() => {});
  }, []);
  const [products, setProducts]   = useState([]);
  const [showForm, setShowForm]   = useState(false);
  const [isEdit, setIsEdit]       = useState(false);
  const [formErrors, setFormErrors] = useState({
    name: "", description: "", repo_name: "", team: "", custom_instructions: "", tags: "", product_id: "",
  });
  const [saving, setSaving]       = useState(false);
  const [formError, setFormError] = useState("");
  const [searchQ, setSearchQ]     = useState("");
  const [form, setForm]           = useState({
    name: "", description: "", repo_name: "", team: "", custom_instructions: "", tags: "", product_id: "",
  });

  const [input, setInput]         = useState("");
  const { confirm } = useConfirm();
  const { toast }   = useToast();

  // ── User message actions (edit / copy) ──────
  const [copiedMsgId, setCopiedMsgId]       = useState(null);
  // Track which message is being edited — messages are only removed on submit, not on click
  const [editingMsgId, setEditingMsgId]     = useState(null);

  // How many messages will be discarded when the edit is submitted (includes the edited msg itself)
  const editDiscardCount = (() => {
    if (!editingMsgId) return 0;
    const idx = messages.findIndex(m => m.id === editingMsgId);
    return idx === -1 ? 0 : messages.length - idx;
  })();

  // ── Prompt Enhancer ────────────────────────────────────────
  const [enhancing, setEnhancing]             = useState(false);
  const [enhancerModal, setEnhancerModal]     = useState(false);
  const [enhancerEdited, setEnhancerEdited]   = useState("");
  const [followupQs, setFollowupQs]           = useState([]);
  const [followupAnswers, setFollowupAnswers] = useState({});

  // ── Server-side message fetch — server is the only source of truth ──────────
  // No localStorage fallback for display: if server returns empty, show empty.
  async function _fetchProjectMessages(projectId) {
    try {
      const r = await authFetch(`${API}/projects/${projectId}/messages?limit=60`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      return d.messages || [];
    } catch {
      return [];
    }
  }

  function _saveSelectedProjectId(projectId) {
    if (projectId) {
      setLocalData("proj_last_selected", String(projectId).replace(/[^a-zA-Z0-9_\-]/g, ''));
    } else {
      removeLocalData("proj_last_selected");
    }
  }

  function _loadSelectedProjectId() {
    return getLocalData("proj_last_selected") || null;
  }

  // ── Image upload state ──────────────────────────────────────
  const [imageFile, setImageFile]         = useState(null);
  const [imagePreviewUrl, setImagePreviewUrl] = useState(null);
  const imageInputRef = useRef(null);

  const IMAGE_ACCEPTED  = "image/jpeg,image/png,image/gif,image/webp";
  const IMAGE_MAX_BYTES = 10 * 1024 * 1024;

  // ── Document upload state ───────────────────────────────────
  const [attachments, setAttachments]         = useState([]);
  const [uploading, setUploading]             = useState(false);
  const [uploadProgress, setUploadProgress]   = useState(0);
  const fileInputRef = useRef(null);

  const ACCEPT_TYPES = [
    ".pdf", ".docx", ".xlsx", ".xls", ".csv",
    ".html", ".htm", ".rtf", ".txt", ".json",
  ].join(",");

  const [budget, setBudget] = useState(null);
  const containerRef = useRef(null);
  const textareaRef = useRef(null);

  // Ref to track selected project — used in async guards to detect project switches
  const selectedRef = useRef(null);
  useEffect(() => { selectedRef.current = selected; }, [selected]);

  // ── Auto-grow textarea ───────────────────────────────────────────────────
  const adjustTextareaHeight = useCallback((el) => {
    if (!el) return;
    // Reset height to auto to get the actual scroll height
    el.style.height = 'auto';
    // Calculate new height: min 60px, max 200px
    const newHeight = Math.min(Math.max(el.scrollHeight, 60), 200);
    el.style.height = `${newHeight}px`;
  }, []);

  // Update textarea height when input changes
  useEffect(() => {
    if (textareaRef.current) {
      adjustTextareaHeight(textareaRef.current);
    }
  }, [input, adjustTextareaHeight]);

  const fetchBudget = () => {
    const uid = user?.userId || "";
    authFetch(`${API}/budget/me`, { headers: { "X-User-Id": uid } })
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setBudget(d))
      .catch(() => {});
  };

  useEffect(() => { loadProjects(); loadRepos(); loadProducts(); fetchBudget(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    containerRef.current?.scrollTo({
      top: containerRef.current.scrollHeight,
      behavior: 'smooth',
    });
  }, [messages]);

  async function loadProjects() {
    const r = await authFetch(`${API}/projects`);
    const d = await r.json();
    const list = d.projects || [];
    setProjects(list);

    // Restore the last-selected project on mount.
    // Prefer the hoisted activeProjectId (survives navigation); fall back to localStorage.
    const lastId = activeProjectId || _loadSelectedProjectId();
    if (lastId) {
      const match = list.find(p => p.id === lastId);
      if (match) {
        setSelected(match);
        setShowForm(false);
        // If a stream is already running for this project, don't overwrite live messages.
        if (projectLoading[match.id]) return;
        // Fetch from server — no localStorage flash, server is source of truth.
        const serverMsgs = await _fetchProjectMessages(match.id);
        if (selectedRef.current?.id === match.id && !projectLoading[match.id]) {
          setProjectMessages(prev => ({ ...prev, [match.id]: serverMsgs }));
        }
      }
    }
  }

  async function loadRepos() {
    const r = await authFetch(`${API}/index/repos`);
    const d = await r.json();
    setRepos(d.repos || []);
  }

  async function loadProducts() {
    try {
      const r = await authFetch(`${API}/products`);
      const d = await r.json();
      setProducts(d.products || []);
    } catch { setProducts([]); }
  }

  // Field validation helper
  function validateField(fieldName, value) {
    if (fieldName === "name" && (!value || !value.trim())) return "Project name is required";
    switch (fieldName) {
      case "name": {
        if (!/[a-zA-Z0-9]/.test(value)) {
          return "Project name must contain at least one letter or number";
        }
        const result = validateProductName(value);
        return result.isValid ? "" : result.errors[0]?.message || "";
      }
      case "description": {
        const result = validateDescription(value);
        return result.isValid ? "" : result.errors[0]?.message || "";
      }
      case "custom_instructions": {
        const result = validateDescription(value);
        return result.isValid ? "" : result.errors[0]?.message || "";
      }
      case "team": {
        if (!value || !value.trim()) return "";
        const emails = value.split(",").map(e => e.trim()).filter(Boolean);
        for (const email of emails) {
          if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            return "Invalid email format in team field";
          }
          // Security check on each email value
          const result = validateSecurity(email, { checkSQL: false });
          if (!result.isValid) return result.errors[0]?.message || "Invalid characters in team field";
        }
        return "";
      }
      case "tags": {
        if (!value || !value.trim()) return "";
        const tags = value.split(",").map(t => t.trim()).filter(Boolean);
        for (const tag of tags) {
          const result = validateSecurity(tag, { checkSQL: false });
          if (!result.isValid) return result.errors[0]?.message || "Tags contain invalid characters";
        }
        return "";
      }
      case "product_id":
        return !value ? "Product is required" : "";
      case "repo_name":
        return !value ? "Codebase is required" : "";
      default:
        return "";
    }
  }

  function handleBlur(fieldName) {
    const error = validateField(fieldName, form[fieldName]);
    setFormErrors(prev => ({ ...prev, [fieldName]: error }));
  }

  function handleChange(fieldName, value) {
    setForm(prev => ({ ...prev, [fieldName]: value }));
    // Clear error when user starts typing
    if (formErrors[fieldName]) {
      setFormErrors(prev => ({ ...prev, [fieldName]: "" }));
    }
  }

  async function saveProject() {
    // Validate all required fields
    const errors = {
      name: validateField("name", form.name),
      description: validateField("description", form.description),
      team: validateField("team", form.team),
      tags: validateField("tags", form.tags),
      custom_instructions: validateField("custom_instructions", form.custom_instructions),
      product_id: validateField("product_id", form.product_id),
      repo_name: validateField("repo_name", form.repo_name),
    };

    // Check if any errors exist
    const hasErrors = Object.values(errors).some(e => e !== "");
    if (hasErrors) {
      setFormErrors(errors);
      return;
    }

    if (!form.name.trim()) { setFormError("Project Name is mandatory."); return; }
    if (!form.product_id)  { setFormError("Product is mandatory."); return; }
    if (!form.repo_name)   { setFormError("Codebase is mandatory."); return; }
    setFormError("");
    setSaving(true);
    const body = {
      name: form.name,
      description: form.description,
      repo_name: form.repo_name,
      product_id: form.product_id || null,
      team: form.team.split(",").map(t => t.trim()).filter(Boolean),
      custom_instructions: form.custom_instructions,
      tags: form.tags.split(",").map(t => t.trim()).filter(Boolean),
    };
    const method = isEdit ? "PUT" : "POST";
    const url    = isEdit ? `${API}/projects/${selected.id}` : `${API}/projects`;
    try {
      // The response was never checked — a failed create/update (duplicate
      // name, validation error, or a 403/404 from the visibility/ownership
      // check on PUT) still closed the form and reloaded the list as if it
      // had succeeded, with no error shown at all.
      const res = await authFetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setFormError(d.detail || "Save failed");
        return;
      }
      setShowForm(false);
      await loadProjects();
    } catch (err) {
      setFormError(err.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function deleteProject(p) {
    const ok = await confirm({ title: "Delete Project", message: `Delete ${p.name} This cannot be undone.`, confirmLabel: "Delete" });
    if (!ok) return;
    try {
      const res = await authFetch(`${API}/projects/${p.id}`, { method: "DELETE" });
      if (!res.ok && res.status !== 204) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Delete failed");
      }
      setProjects(prev => prev.filter(proj => proj.id !== p.id));
      // Remove the deleted project's messages and loading state from the dicts
      setProjectMessages(prev => { const next = { ...prev }; delete next[p.id]; return next; });
      setProjectLoading(prev => { const next = { ...prev }; delete next[p.id]; return next; });
      if (selected?.id === p.id) {
        setSelected(null);
        setActiveProjectId(null);
        _saveSelectedProjectId(null);
      }
    } catch (err) {
      // Was silently swallowed — a failed delete (e.g. 403 not-owner, 404
      // not-visible) showed no feedback, looking identical to success.
      toast.error(err.message || "Delete failed");
    }
  }

  async function openProject(p) {
    setSelected(p);
    setActiveProjectId(p.id);
    setShowForm(false);
    _saveSelectedProjectId(p.id);
    // Clear any pending attachments and edit state from the previous project
    setAttachments([]);
    setUploading(false);
    setUploadProgress(0);
    setEditingMsgId(null);
    setInput("");

    // If a stream is actively running for THIS exact project, the messages in the
    // dict are already live and correct — do not overwrite them.
    // Use the per-workspace loading flag so switching to a DIFFERENT workspace
    // while Workspace A streams is never blocked.
    if (projectLoading[p.id]) return;

    // Fetch from server — no localStorage flash, server is source of truth.
    const serverMsgs = await _fetchProjectMessages(p.id);
    // Guard: only update if the user hasn't switched away during the async fetch.
    // No loading check here — a different workspace may be streaming and that
    // must not prevent this workspace's messages from being displayed.
    if (selectedRef.current?.id === p.id) {
      setProjectMessages(prev => ({ ...prev, [p.id]: serverMsgs }));
    }
  }

  function openEdit(p) {
    setForm({
      name: p.name, description: p.description, repo_name: p.repo_name,
      product_id: p.product_id || "",
      team: (p.team || []).join(", "),
      custom_instructions: p.custom_instructions || "",
      tags: (p.tags || []).join(", "),
    });
    setFormErrors({
      name: "", description: "", repo_name: "", team: "", custom_instructions: "", tags: "", product_id: "",
    });
    setSelected(p); setIsEdit(true); setShowForm(true);
  }

  function openNew() {
    setForm({ name: "", description: "", repo_name: "", product_id: "", team: "", custom_instructions: "", tags: "" });
    setFormErrors({
      name: "", description: "", repo_name: "", team: "", custom_instructions: "", tags: "", product_id: "",
    });
    setIsEdit(false); setShowForm(true); setSelected(null);
    _saveSelectedProjectId(null);
  }

  // ── Image upload handlers ────────────────────────────────────
  function handleImageSelect(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!["image/jpeg", "image/png", "image/gif", "image/webp"].includes(file.type)) {
      setDropError("Unsupported image format. Please use JPEG, PNG, GIF, or WebP.");
      return;
    }
    if (file.size > IMAGE_MAX_BYTES) {
      setDropError("Image is too large. Maximum size is 10 MB.");
      return;
    }
    if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
    setImageFile(file);
    setImagePreviewUrl(URL.createObjectURL(file));
  }

  function removeImage() {
    if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
    setImageFile(null);
    setImagePreviewUrl(null);
  }

  // ── Document upload handlers ────────────────────────────────
  async function handleFileUpload(e) {
    const files = Array.from(e.target?.files || e);
    if (!files.length) return;
    setUploading(true);
    setUploadProgress(0);
    if (e.target?.value !== undefined) e.target.value = "";

    const fd = new FormData();
    // Use the project id as the chat_id so the backend associates uploads correctly
    fd.append("chat_id", selected?.id || "project");
    files.forEach(f => fd.append("files", f));

    try {
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", `${API}/chat/upload`);
        xhr.withCredentials = true;
        xhr.upload.onprogress = (ev) => {
          if (ev.lengthComputable) setUploadProgress(Math.round((ev.loaded / ev.total) * 100));
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            try { resolve(JSON.parse(xhr.responseText)); }
            catch { reject(new Error("Invalid response")); }
          } else {
            reject(new Error(`Upload failed: ${xhr.status}`));
          }
        };
        xhr.onerror = () => reject(new Error("Network error"));
        xhr.send(fd);
      });

      const uploaded = result.uploaded || [];
      setAttachments(prev => [...prev, ...uploaded.filter(u => !u.blocked)]);

      const blocked = uploaded.filter(u => u.blocked);
      if (blocked.length > 0) {
        const complianceMsgs = blocked.map(b => ({
          id:           crypto.randomUUID(),
          role:         "compliance_block",
          filename:     b.file_name,
          block_reason: b.block_reason || null,
          reasons:      b.compliance_reasons || [],
          streaming:    false,
        }));
        setMessages(prev => [...prev, ...complianceMsgs]);
      }
    } catch (err) {
      setMessages(prev => [...prev, {
        id: crypto.randomUUID(), role: "assistant",
        content: `Upload error: ${err.message}`, streaming: false,
      }]);
    } finally {
      setUploading(false);
      setUploadProgress(0);
    }
  }

  function removeAttachment(id) {
    setAttachments(prev => prev.filter(a => a.id !== id));
  }

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
    "image/jpeg", "image/png", "image/gif", "image/webp",
  ];

  const { isDragging, dropRef } = useFileDrop({
    accept: ACCEPTED_DROP_MIME_TYPES,
    onFiles: (validFiles, invalidFiles) => {
      if (invalidFiles && invalidFiles.length > 0) {
        const names = invalidFiles.map(f => f.name).join(", ");
        setDropError(`Unsupported file type. Accepted: PDF, DOCX, XLSX, CSV, TXT, HTML, JSON, images. Skipped: ${names}`);
        if (validFiles.length === 0) return;
      }

      const imageTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
      const images = validFiles.filter(f => imageTypes.includes(f.type));
      const docs   = validFiles.filter(f => !imageTypes.includes(f.type));

      if (images.length > 0) {
        const img = images[0];
        if (img.size > IMAGE_MAX_BYTES) {
          setDropError("Image is too large. Maximum size is 10 MB.");
          return;
        }
        if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
        setImageFile(img);
        setImagePreviewUrl(URL.createObjectURL(img));
      }

      if (docs.length > 0) {
        handleFileUpload(docs);
      }
    },
    disabled: loading,
  });

  // ── Streaming send ──────────────────────────────────────────

  async function sendMessage() {
    if (!input.trim() && !imageFile || !selected || loading) return;

    // Capture the project ID at call time so the entire async function — including
    // every token update — always targets THIS workspace's slot in the dict,
    // even if the user switches to a different workspace mid-stream.
    const projectId          = selected.id;

    const question          = input;
    const pendingImage      = imageFile;
    const pendingImgUrl     = imagePreviewUrl;
    const pendingAttachments = [...attachments];
    const assistantId       = crypto.randomUUID();
    setInput("");
    setImageFile(null);
    setImagePreviewUrl(null);
    setAttachments([]);
    // Mark THIS workspace as loading — other workspaces are unaffected
    setProjectLoading(prev => ({ ...prev, [projectId]: true }));

    // Helper: update only this workspace's message list in the dict
    function updateMsgs(updater) {
      setProjectMessages(prev => ({
        ...prev,
        [projectId]: typeof updater === 'function' ? updater(prev[projectId] ?? []) : updater,
      }));
    }

    // If editing a previous message, truncate history to before that message (deferred from click)
    let baseMessages = messages;
    if (editingMsgId) {
      const editIdx = messages.findIndex(m => m.id === editingMsgId);
      if (editIdx !== -1) baseMessages = messages.slice(0, editIdx);
      setEditingMsgId(null);
    }

    // Build user message content — append attachment filenames if any
    const userContent = pendingAttachments.length > 0
      ? `${question}\n\n📎 ${pendingAttachments.map(a => a.file_name).join(", ")}`
      : question;

    updateMsgs(_ => [
      ...baseMessages,
      {
        id: crypto.randomUUID(), role: "user", content: userContent, streaming: false,
        imageUrl: pendingImgUrl || null,
      },
      { id: assistantId, role: "assistant", content: "", streaming: true,
        spinnerStage: 0,   // 0=Understanding, 1=Searching, 2=Tools, 3=Generating
        modelLabel: null, costUsd: null, latency: null, inTok: null, outTok: null },
    ]);

    // Hoist accumulated outside try so the catch block can reference it
    let accumulated = "";

    try {
      const controller = new AbortController();
      abortRef.current = controller;

      let r;
      if (pendingImage) {
        const fd = new FormData();
        fd.append("question", question || "What is in this image?");
        fd.append("image", pendingImage);
        fd.append("session_id", projectId);
        r = await authFetch(`${API}/ask/image`, {
          method: "POST",
          body:   fd,
          signal: controller.signal,
        });
      } else {
        r = await authFetch(`${API}/projects/${projectId}/ask`, {
          method:  "POST",
          headers: { "Content-Type": "application/json" },
          body:    JSON.stringify({
            question,
            session_id:     projectId,
            chat_id:        crypto.randomUUID(),
            attachment_ids: pendingAttachments.map(a => a.id),
          }),
          signal:  controller.signal,
        });
      }

      if (!r.ok) {
        const txt = await r.text().catch(() => "");
        throw new Error(`Server error ${r.status}: ${txt.slice(0, 200)}`);
      }
      if (!r.body) throw new Error("No response body");

      // Capture request ID for cooperative backend stop
      requestIdRef.current = r.headers.get("X-Request-ID") || null;

      const reader  = r.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: false });
      let sseBuffer   = "";
      let modelLabel = null, costUsd = null, latency = null, inTok = null, outTok = null;
      let tokensToday = null, maxTokensToday = null, requestsToday = null, maxRequestsToday = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value) continue;
        sseBuffer += decoder.decode(value, { stream: true });

        const parts = sseBuffer.split("\n\n");
        sseBuffer = parts.pop() ?? "";

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data: ")) continue;
          try {
            const obj = JSON.parse(line.slice(6));
            if (obj.tool_event) {
              // Advance spinner to "Tools" stage
              updateMsgs(prev => prev.map(m =>
                m.id === assistantId ? { ...m, spinnerStage: 2 } : m
              ));
            } else if (obj.tool_call !== undefined) {
              // Legacy string tool call — append as inline note
              if (obj.tool_call) accumulated += `\n\`Tool: ${obj.tool_call}\`\n`;
              updateMsgs(prev => prev.map(m =>
                m.id === assistantId ? { ...m, content: stripMemoryTag(accumulated), spinnerStage: 2 } : m
              ));
            } else if (obj.t !== undefined) {
              accumulated += obj.t;
              updateMsgs(prev => prev.map(m =>
                m.id === assistantId ? { ...m, content: stripMemoryTag(accumulated), spinnerStage: 3 } : m
              ));
            } else if (obj.__meta__) {
              const meta = obj.__meta__;
              if (meta.model              != null) modelLabel       = meta.model;
              if (meta.cost               != null) costUsd          = meta.cost;
              if (meta.latency            != null) latency          = meta.latency;
              if (meta.in_tok             != null) inTok            = meta.in_tok;
              if (meta.out_tok            != null) outTok           = meta.out_tok;
              if (meta.tokens_today       != null) tokensToday      = meta.tokens_today;
              if (meta.max_tokens_today   != null) maxTokensToday   = meta.max_tokens_today;
              if (meta.requests_today     != null) requestsToday    = meta.requests_today;
              if (meta.max_requests_today != null) maxRequestsToday = meta.max_requests_today;
            }
          } catch { /* ignore malformed events */ }
        }
      }

      updateMsgs(prev => prev.map(m =>
        m.id === assistantId
          ? { ...m, content: stripMemoryTag(accumulated), streaming: false,
              modelLabel, costUsd, latency, inTok, outTok,
              tokensToday, maxTokensToday, requestsToday, maxRequestsToday }
          : m
      ));
      fetchBudget();

    } catch (err) {
      if (err?.name === "AbortError") {
        // Manual stop — keep whatever was accumulated so far
        updateMsgs(prev => prev.map(m =>
          m.id === assistantId
            ? { ...m, content: accumulated || m.content, streaming: false }
            : m
        ));
      } else {
        updateMsgs(prev => prev.map(m =>
          m.id === assistantId
            ? { ...m, content: `Error: ${err.message}`, streaming: false }
            : m
        ));
      }
    } finally {
      // Clear loading only for THIS workspace — other workspaces are unaffected
      setProjectLoading(prev => ({ ...prev, [projectId]: false }));
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
    } catch {
      /* silently fail — user keeps their original */
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
    abortRef.current?.abort();
    // Cooperative backend stop — fire-and-forget so the LLM call is cancelled server-side too
    if (requestIdRef.current) {
      authFetch(`${API}/chat/stop`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ request_id: requestIdRef.current }),
      }).catch(() => {});
      requestIdRef.current = null;
    }
    if (!selected?.id) return;
    const id = selected.id;
    // Scope both the loading flag and the streaming message update to THIS workspace only
    setProjectLoading(prev => ({ ...prev, [id]: false }));
    setProjectMessages(prev => ({
      ...prev,
      [id]: (prev[id] ?? []).map(m => m.streaming ? { ...m, streaming: false } : m),
    }));
  }

  // ── User message action handlers ───────────────────────────
  function handleUserCopy(msgId, content) {
    navigator.clipboard.writeText(content).then(() => {
      setCopiedMsgId(msgId);
      setTimeout(() => setCopiedMsgId(null), 1500);
    }).catch(() => {});
  }

  function startEditUserMsg(msgId, content) {
    // Mark which message is being edited — actual removal is deferred until submit
    setEditingMsgId(msgId);
    setInput(stripSystemPrefix(content));
    setTimeout(() => textareaRef.current?.focus(), 50);
  }

  function cancelEditMsg() {
    setEditingMsgId(null);
    setInput("");
  }

  const filteredProjects = projects.filter(p =>
    !searchQ ||
    p.name.toLowerCase().includes(searchQ.toLowerCase()) ||
    (p.description || "").toLowerCase().includes(searchQ.toLowerCase())
  );

  // ── Render ──────────────────────────────────────────────────

  return (
    <div className="flex h-full bg-white overflow-hidden">

      {/* ── LEFT: Project list ── */}
      <div className="w-72 bg-gray-50 border-r border-gray-200 flex flex-col flex-shrink-0">

        <div className="px-3 py-3 border-b border-gray-200 flex items-center justify-between">
          <span className="text-sm font-semibold  text-indigo-700">My Workspace</span>
          <button
            onClick={openNew}
            title="New project"
            className="p-1.5 hover:bg-indigo-50 rounded-md text-indigo-700 hover:text-indigo-600 transition cursor-pointer"
          >
            <Plus size={14} />
          </button>
        </div>

        <div className="px-3 py-2 border-b border-gray-100">
          <input
            value={searchQ}
            onChange={e => setSearchQ(e.target.value)}
            placeholder="Search workspaces..."
            className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-md outline-none bg-white focus:border-indigo-300 shadow-sm"
          />
        </div>

        <div className="flex-1 overflow-y-auto py-1">
          {filteredProjects.length === 0 && (
            <div className='px-4 py-6 text-xs text-gray-400 text-center'>
              No workspaces found
            </div>
          )}
          {filteredProjects.map((p) => (
            <div
              key={p.id}
              onClick={() => openProject(p)}
              className={`group flex flex-col px-3 py-2 m-1 border-b-1 border-b-gray-100 rounded cursor-pointer mb-0.5 transition ${
                selected?.id === p.id && !showForm
                  ? 'bg-indigo-50 !text-indigo-700 font-semibold border-l-2 border-l-indigo-500'
                  : 'text-gray-600 hover:bg-gray-100'
              }`}>
              <div className='flex items-center justify-between gap-2'>
                <span className='text-sm truncate flex-1 font-medium'>{p.name}</span>
                <div className='flex gap-1 opacity-0 group-hover:opacity-100 flex-shrink-0'>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      openEdit(p);
                    }}
                    className='p-1 hover:bg-indigo-100 rounded-md text-indigo-700 hover:text-indigo-600 cursor-pointer'>
                    <Edit2 size={12} />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteProject(p);
                    }}
                    className='p-1 hover:bg-red-100 rounded text-red-500 hover:text-red-400 cursor-pointer'>
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
              {p.repo_name && (
                <div className='text-xs text-gray-400 mt-0.5 truncate'>
                  📁 {p.repo_name}
                </div>
              )}
              {p.created_at > 0 && (
                <div className='text-[10px] text-gray-400 mt-0.5 truncate'>
                  {toISTDate(new Date(p.created_at * 1000))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
      {/* ── RIGHT ── */}
      {showForm ? (
        /* Project form */
        <div className='flex-1 overflow-y-auto p-6'>
          <div className='max-w-2xl mx-auto px-8 py-8'>
            <h2 className='font-semibold text-gray-800 mb-4'>
              {isEdit ? 'Edit Project' : 'New Project'}
            </h2>
            <div className='space-y-3'>
              {[
                { key: 'name', label: 'Name', disabled: isEdit, required: true },
                { key: 'description', label: 'Description' },
                { key: 'team', label: 'Team (comma-separated emails)' },
                { key: 'tags', label: 'Tags (comma-separated)' },
              ].map((f) => (
                <div key={f.key}>
                  <label className='text-xs text-gray-500 relative'>
                    {f.label}
                    {f.required && (
                      <span className='text-red-500 text-[10px] font-bold absolute -top-1 -right-2.5'>*</span>
                    )}
                  </label>
                  <input
                    value={form[f.key]}
                    disabled={f.disabled}
                    onChange={(e) => handleChange(f.key, e.target.value)}
                    onBlur={() => handleBlur(f.key)}
                    className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none disabled:bg-gray-50 focus:border-indigo-300 ${formErrors[f.key] ? "border-red-500" : "border-gray-200"}`}
                  />
                  {formErrors[f.key] && (
                    <p className='mt-1 text-xs text-red-600'>{formErrors[f.key]}</p>
                  )}
                </div>
              ))}
              <div>
                <label className='text-xs text-gray-500 relative'>
                  Product
                  <span className='text-red-500 text-[10px] font-bold absolute -top-1 -right-2.5'>*</span>
                </label>
                <select
                  value={form.product_id}
                  onChange={(e) => handleChange("product_id", e.target.value)}
                  onBlur={() => handleBlur("product_id")}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 ${formErrors.product_id ? "border-red-500" : "border-gray-200"}`}
                >
                  <option value=''>— Select product —</option>
                  {products?.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
                {formErrors.product_id && (
                  <p className='mt-1 text-xs text-red-600'>{formErrors.product_id}</p>
                )}
              </div>
              <div>
                <label className='text-xs text-gray-500 relative'>
                  Codebase
                  <span className='text-red-500 text-[10px] font-bold absolute -top-1 -right-2.5'>*</span>
                </label>
                <select
                  value={form.repo_name}
                  onChange={e => {
                    const repoName = e.target.value;
                    handleChange("repo_name", repoName);
                  }}
                  onBlur={() => handleBlur("repo_name")}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none focus:border-indigo-300 ${formErrors.repo_name ? "border-red-500" : "border-gray-200"}`}
                >
                  <option value="">— Select codebase —</option>
                  {repos
                    .filter(
                      (r) =>
                        r.status === 'ready' &&
                        (!form.product_id ||
                          !r.product_id ||
                          r.product_id === form.product_id)
                    )
                    .map((r) => (
                      <option key={`${r.name}:${r.branch || 'main'}`} value={r.name}>
                        {r.name} [{r.branch || 'main'}] ({r.vector_count} vectors)
                      </option>
                    ))}
                </select>
                {formErrors.repo_name && (
                  <p className='mt-1 text-xs text-red-600'>{formErrors.repo_name}</p>
                )}
                {form.product_id &&
                  repos.filter(
                    (r) =>
                      r.status === 'ready' &&
                      r.product_id &&
                      r.product_id !== form.product_id
                  ).length > 0 && (
                    <p className='text-xs text-gray-400 mt-1'>
                      Only showing repos indexed under the selected product.
                    </p>
                  )}
              </div>
              <div>
                <label className='text-xs text-gray-500'>Custom Instructions</label>
                <textarea
                  rows={3}
                  value={form.custom_instructions}
                  onChange={(e) =>
                    handleChange("custom_instructions", e.target.value)
                  }
                  onBlur={() => handleBlur("custom_instructions")}
                  className={`w-full mt-1 px-3 py-2 border rounded text-sm outline-none resize-none focus:border-indigo-300 ${formErrors.custom_instructions ? "border-red-500" : "border-gray-200"}`}
                  placeholder='Always respond in the context of our payment system...'
                />
                {formErrors.custom_instructions && (
                  <p className='mt-1 text-xs text-red-600'>{formErrors.custom_instructions}</p>
                )}
              </div>
              {formError && (
                <p className='text-xs text-red-500 font-medium'>{formError}</p>
              )}
              <div className='flex gap-2 pt-1'>
                <button
                  onClick={saveProject}
                  disabled={saving}
                  className='px-4 py-2 text-white rounded text-sm brand-grad hover:opacity-90  cursor-pointer'>
                  {saving ? 'Saving...' : isEdit ? 'Save Changes' : 'Create Project'}
                </button>
                <button
                  onClick={() => {
                    setShowForm(false);
                    setFormErrors({
                      name: "", description: "", repo_name: "", team: "", custom_instructions: "", tags: "", product_id: "",
                    });
                  }}
                  className='px-4 py-2 border border-gray-200 rounded text-sm hover:bg-gray-100 cursor-pointer'>
                  Cancel
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : selected ? (
        /* Chat panel */
        <div className='flex flex-col flex-1 min-w-0 bg-white'>
          {/* Header */}
          <div className='border-b border-gray-200 px-6 py-3 flex-shrink-0 flex items-center justify-between'>
            <div className='flex items-center gap-2 min-w-0'>
              <h2 className='text-sm font-medium text-gray-800 truncate'>
                {selected.name}
              </h2>
              {selected.repo_name && (
                <span className='flex-shrink-0 text-xs bg-blue-50 text-blue-600 px-2 py-0.5 rounded'>
                  📁 {selected.repo_name}
                </span>
              )}
              {selected.repo_name && (() => {
                const repoMeta = repos.find(r => r.name === selected.repo_name);
                return repoMeta?.branch ? (
                  <span className='flex-shrink-0 inline-flex items-center gap-1 text-xs bg-blue-50 text-blue-600 px-2 py-0.5 rounded'>
                    <GitBranch size={11} />
                    {repoMeta.branch}
                  </span>
                ) : null;
              })()}
            </div>
            <button
              onClick={() => openEdit(selected)}
              className='flex-shrink-0 flex items-center cursor-pointer gap-1 text-xs text-indigo-700 hover:bg-indigo-50 rounded px-1.5 py-1'>
              <Edit2 size={12} /> Edit
            </button>
          </div>

          {/* Messages */}
          <div
            ref={containerRef}
            className='flex-1 overflow-y-auto overflow-x-hidden px-6 py-8 leading-5'>
            {messages.length === 0 && (
              <div className='flex flex-col items-center justify-center h-full text-gray-300 gap-3'>
                <FolderKanban size={40} strokeWidth={1} />
                <p className='text-sm'>Ask anything about {selected.name}</p>
              </div>
            )}

            {messages.map((msg) => {
              const Wrapper = msg.streaming ? 'div' : motion.div;
              const wrapperProps = msg.streaming
                ? {}
                : {
                    initial: { opacity: 0, y: 8 },
                    animate: { opacity: 1, y: 0 },
                    transition: { duration: 0.2 },
                  };

              // ── Compliance block card ─────────────────────────────────
              if (msg.role === 'compliance_block') {
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
                  className={
                    msg.role === 'user'
                      ? 'flex justify-end mb-6'
                      : 'flex justify-start mb-6'
                  }>
                  <div
                    className={
                      msg.role === 'user'
                        ? 'group/usermsg relative bg-gray-100 px-4 py-3 rounded-md text-sm max-w-2xl break-words min-w-0'
                        : 'px-4 py-3 rounded-md text-sm break-words min-w-0 overflow-hidden'
                    }>
                    {/* ── Floating action buttons (user messages only) ── */}
                    {msg.role === 'user' && !msg.streaming && (
                      <div className="absolute -bottom-7 right-0 flex items-center gap-0.5 opacity-0 group-hover/usermsg:opacity-100 transition-opacity duration-150">
                        <button
                          onClick={() => startEditUserMsg(msg.id, msg.content)}
                          title="Edit message"
                          className="p-1.5 rounded cursor-pointer text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
                        >
                          <Pencil size={13} />
                        </button>
                        <button
                          onClick={() => handleUserCopy(msg.id, msg.content)}
                          title="Copy message"
                          className="p-1.5 rounded cursor-pointer text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors"
                        >
                          {copiedMsgId === msg.id ? <Check size={13} className="text-green-500" /> : <Copy size={13} />}
                        </button>
                      </div>
                    )}
                    {msg.role === 'assistant' ? (
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        rehypePlugins={[rehypeHighlight]}
                        components={mdComponents}>
                        {msg.content}
                      </ReactMarkdown>
                    ) : (
                      <div>
                        {msg.imageUrl && (
                          <img
                            src={msg.imageUrl}
                            alt="Attached"
                            className="mb-2 max-h-48 max-w-xs rounded-md object-contain border border-gray-200"
                          />
                        )}
                        <div className='whitespace-pre-wrap'>{msg.content}</div>
                      </div>
                    )}

                    {msg.streaming && !msg.content && <AiNxtSpinner stage={msg.spinnerStage ?? 0} />}
                    {msg.streaming && msg.content && (
                      <span className='inline-flex gap-0.5 ml-1 align-middle'>
                        <span
                          className='w-1 h-1 bg-blue-400 rounded-full animate-bounce'
                          style={{ animationDelay: '0ms' }}
                        />
                        <span
                          className='w-1 h-1 bg-blue-400 rounded-full animate-bounce'
                          style={{ animationDelay: '150ms' }}
                        />
                        <span
                          className='w-1 h-1 bg-blue-400 rounded-full animate-bounce'
                          style={{ animationDelay: '300ms' }}
                        />
                      </span>
                    )}

                    <MessageMeta
                      msg={msg}
                      budget={budget}
                      isLast={
                        msg.id ===
                        [...messages].filter((m) => m.role === 'assistant').pop()?.id
                      }
                    />
                  </div>
                </Wrapper>
              );
            })}
          </div>

          {/* Input — supports drag-and-drop files */}
          <div
            ref={dropRef}
            className={`border-t border-gray-100 bg-white px-4 pb-4 pt-3 flex-shrink-0 relative transition-all ${
              isDragging ? 'bg-blue-50' : ''
            }`}
          >
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

            {/* Hidden file input for document uploads */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPT_TYPES}
              onChange={handleFileUpload}
              className="hidden"
            />

            {/* Hidden file input for image selection */}
            <input
              ref={imageInputRef}
              type="file"
              accept={IMAGE_ACCEPTED}
              onChange={handleImageSelect}
              className="hidden"
            />

            {/* Image preview strip */}
            {imagePreviewUrl && (
              <div className="mb-2 flex items-start gap-2">
                <div className="relative inline-block">
                  <img
                    src={imagePreviewUrl}
                    alt="To send"
                    className="h-16 max-w-[130px] rounded-md object-contain border border-gray-200 bg-gray-50"
                  />
                  <button
                    onClick={removeImage}
                    className="absolute -top-1.5 -right-1.5 bg-white border border-gray-300 rounded-full p-0.5 text-gray-500 hover:text-red-500 hover:border-red-300 transition"
                  >
                    <X size={10} />
                  </button>
                </div>
                <span className="text-xs text-gray-400 mt-1">Image attached</span>
              </div>
            )}

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

            <div
              className={`border rounded-xl bg-gray-50 transition-colors shadow-md ${
                loading
                  ? 'border-gray-200'
                  : 'border-gray-300 focus-within:border-indigo-300 focus-within:bg-white'
              }`}>

              {/* Edit-mode warning banner — shown when editing a previous prompt */}
              {editingMsgId && editDiscardCount > 0 && (
                <div className="flex items-center gap-2 px-3 py-2 bg-amber-50 border-b border-amber-200 rounded-t-xl">
                  <Pencil size={13} className="text-amber-500 shrink-0" />
                  <span className="text-xs text-amber-700 flex-1">
                    Editing earlier message —{" "}
                    <strong>{editDiscardCount} message{editDiscardCount > 1 ? "s" : ""}</strong>{" "}
                    after this will be removed on submit
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

              {/* Attachment chips — inside the box, above textarea */}
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
                        className="text-blue-400 hover:text-blue-600 ml-0.5"
                      >
                        <X size={10} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {/* Embed-svc unreachable — only matters once this project has a
                  codebase attached; a project with no repo_name never
                  triggers RAG retrieval in the first place. */}
              {selected?.repo_name && !embedSvcReachable && (
                <div className="flex items-center gap-2 px-3 py-2 bg-amber-50 border-b border-amber-200 rounded-t-xl">
                  <span className="text-xs text-amber-700 flex-1">
                    <strong>Heads up:</strong> Embedding service is not reachable — this
                    project's chat won't be able to retrieve relevant context from{" "}
                    <strong>{selected.repo_name}</strong>. Run{" "}
                    <code>docker compose --profile embed up -d embed-svc</code> in the
                    project directory.
                  </span>
                </div>
              )}

              {/* Textarea */}
              <textarea
                ref={textareaRef}
                value={input}
                disabled={loading}
                onChange={(e) => {
                  setInput(e.target.value);
                  // Let the useEffect handle height adjustment, but for immediate response
                  requestAnimationFrame(() => adjustTextareaHeight(e.target));
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Escape' && editingMsgId) {
                    e.preventDefault();
                    cancelEditMsg();
                    return;
                  }
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                  }
                }}
                placeholder={`Ask about ${selected.name}… (Shift+Enter for new line)`}
                rows={1}
                className='w-full resize-none bg-transparent px-3 py-3 outline-none text-sm text-gray-800 placeholder-gray-400 min-h-[60px] max-h-[200px] overflow-y-auto scrollbar-thin transition-all duration-200 ease-out'
              />
              <div className="flex items-center gap-1 px-2 pb-2">
                {/* Document attach button */}
                <button
                  onClick={() => fileInputRef.current?.click()}
                  disabled={loading || uploading}
                  title={uploading ? `Uploading… ${uploadProgress}%` : "Attach files (PDF, DOCX, XLSX, CSV…)"}
                  className="cursor-pointer p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition disabled:opacity-40"
                >
                  {uploading
                    ? <Loader2 size={16} className="animate-spin text-blue-500" />
                    : <Paperclip size={16} />
                  }
                </button>
                {/* Image upload button */}
                <button
                  onClick={() => imageInputRef.current?.click()}
                  disabled={loading}
                  title="Attach image"
                  className={`p-1.5 rounded-lg transition disabled:opacity-30 cursor-pointer ${
                    imageFile
                      ? "text-blue-500 bg-blue-50"
                      : "text-gray-400 hover:text-gray-600 hover:bg-gray-200"
                  }`}
                >
                  <ImageIcon size={16} />
                </button>
                {/* Prompt Enhancer */}
                <button
                  onClick={handleEnhance}
                  disabled={!input.trim() || enhancing || loading}
                  title="Enhance prompt with AI"
                  className={`p-1.5 cursor-pointer rounded-lg transition disabled:opacity-40 ${
                    enhancing
                      ? "text-indigo-600 animate-pulse"
                      : "text-gray-400 hover:text-gray-600 hover:bg-gray-200"
                  }`}
                >
                  {enhancing
                    ? <Loader2 size={16} className="animate-spin text-purple-500" />
                    : <Sparkles size={16} />
                  }
                </button>
                <div className="flex-1" />
                <button
                  onClick={loading ? stopGeneration : sendMessage}
                  disabled={!loading && !input.trim() && !imageFile && attachments.length === 0 || enhancing}
                  className='p-1.5 cursor-pointer text-gray-600 hover:text-gray-500 transition disabled:opacity-30'>
                  {loading ? <CirclePauseIcon size={20} /> : <SendHorizontal size={20} />}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : (
        /* Empty state */
        <div className='flex-1 flex items-center justify-center text-gray-300'>
          <div className='text-center'>
            <FolderKanban size={48} strokeWidth={1} />
          <p className='mt-3 text-sm'>Select or create a project</p>
          </div>
        </div>
      )}
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
    </div>
  );
}
