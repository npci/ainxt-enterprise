# SPDX-License-Identifier: Apache-2.0
"""
sdlc_context.py — Context gathering for SDLC pipeline.

Searches the indexed codebase, reads real files, and builds rich RAG queries
so every SDLC agent (classifier, triager, coder) works from actual code
instead of file-tree guesses.

Key exports:
  normalize_repo_index_key(repo)       ainxt/ainxt-platform  →  repo_ainxt_platform
  run_exploration_phase(...)           searches + reads code before solutioning
  build_coder_rag_query(...)           richer RAG query for per-file code generation
"""
import logging
import re
from typing import Optional

logger = logging.getLogger("sdlc_context")

# Retrieval tuning
_MAX_EXPLORE_SEARCH_PASSES = 3    # number of distinct RAG queries to run during exploration
_MAX_EXPLORE_CHUNKS_PER_Q  = 12   # chunks per search pass
_MAX_EXPLORE_FILES         = 10   # max files to read from GitLab during exploration
_CONFIDENCE_THRESHOLD      = 0.55 # below this: warn and log; never block the pipeline


# ── Repo key normalization ──────────────────────────────────────────────────

def normalize_repo_index_key(repo: str) -> str:
    """
    Convert any repo reference to the document_embeddings repo-key format.

    MUST match index_router._extract_repo_name exactly:
      name.lower().replace("-", "_").replace(".", "_")

    GitLab format:    ainxt/ainxt-platform   →  repo_ainxt_platform
    Indexed format:   repo_ainxt_platform    →  repo_ainxt_platform  (pass-through)
    Plain name:       ainxt-platform         →  repo_ainxt_platform

    Prod DB has ALL repos indexed with underscores — hyphens and dots are
    converted to underscores at index time. Using hyphens here would silently
    return zero RAG results for every existing repo.
    """
    if not repo:
        return ""
    name = repo.split("/")[-1].strip().lower()
    name = name.replace("-", "_").replace(".", "_")
    if not name.startswith("repo_"):
        name = f"repo_{type(name).__name__}"
    return name


def normalize_repo_index_key_without_prefix(repo: str) -> str:
    """
    Convert any repo reference to the document_embeddings repo-key format.

    GitLab format:    ainxt/ainxt-platform   →  repo_ainxt_platform
    Indexed format:   repo_ainxt_platform    →  repo_ainxt_platform  (pass-through)
    Plain name:       ainxt-platform         →  repo_ainxt_platform

    Rules applied in order:
      1. Take only the project-name segment (everything after the last '/')
      2. Lowercase
      3. Replace hyphens, dots and spaces with underscores
      4. Prepend 'repo_' if not already present

    Dots MUST be normalized too so dotted repos ('upi-2.0' -> 'upi_2_0') match
    the key the indexer stored — see build_manifest_resolver._canonical_slug and
    build_metadata_extractor._canon_repo_slug, which collapse hyphens, dots and
    whitespace to underscores identically.
    """
    if not repo:
        return ""
    name = repo.split("/")[-1].strip().lower()
    name = re.sub(r"[-.\s]+", "_", name)
    return name


# ── Low-level helpers ───────────────────────────────────────────────────────

def _extract_keywords(issue: dict) -> str:
    """Pull key technical terms from a Jira issue dict for RAG search."""
    summary = issue.get("summary", issue.get("fields", {}).get("summary", ""))
    desc    = issue.get("description", issue.get("fields", {}).get("description", "") or "")
    if isinstance(desc, dict):
        desc = str(desc)
    combined = f"{summary}. {desc}"[:1000]
    return combined.strip()


def _extract_file_paths_from_chunks(chunks: list) -> list:
    """
    Extract file paths referenced inside RAG chunk headers/signatures.
    Chunks commonly start with '--- path/to/File.java ---' or
    '[Symbol: method `name`] in path/to/File.java'.
    Returns a deduplicated list of candidate paths.
    """
    paths = []
    # Match path-like strings: at least one '/', ends with a known extension
    _pat = re.compile(
        r"(?:\[Source:\s*|---\s+|in\s+|file[:\s]+)([a-zA-Z0-9_./@-]+\.[a-zA-Z]{1,10})",
        re.IGNORECASE,
    )
    for chunk in (chunks or []):
        for m in _pat.finditer(chunk[:400]):
            candidate = m.group(1).strip().lstrip("/")
            if (
                    "." in candidate
                    and "/" in candidate
                    and len(candidate) > 5
                    and not candidate.endswith(".md")
                    and candidate not in paths
            ):
                paths.append(candidate)
            if len(paths) >= 30:
                break
    return list(dict.fromkeys(paths))  # deduplicate, preserve order


def codebase_search(
        query: str,
        repo_index_key: str,
        top_k: int = 12,
        complexity: str = "complex",
) -> tuple:
    """
    Search the indexed codebase via hybrid retrieval.
    Returns (chunks: list[str], confidence: float).
    Never raises — falls back to ([], 0.0) on error.
    """
    try:
        from models.hybrid_retriever import hybrid_retrieve_context
        result = hybrid_retrieve_context(
            query,
            repo_filter=repo_index_key,
            max_chunks=top_k,
            return_confidence=True,
            complexity=complexity,
        )
        if isinstance(result, tuple) and len(result) == 2:
            chunks, confidence = result
        else:
            chunks, confidence = (result or []), 0.5
        return (chunks or []), float(confidence or 0.0)
    except Exception:
        logger.warning("[IDE] codebase_search error for '%s'", repo_index_key)
        return [], 0.0


def read_files_from_gitlab(repo: str, paths: list, max_files: int = None) -> dict:
    """
    Bulk-read files from GitLab. Returns {path: content}.
    Skips 404s and errors silently — exploration is best-effort.
    """
    if not repo or not paths:
        return {}
    try:
        from tools.gitlab_tools import gitlab_read_file, _detect_default_branch
        branch = _detect_default_branch(repo)
        result = {}
        for path in (paths[:max_files] if max_files else paths):
            if not path or not isinstance(path, str):
                continue
            try:
                content = gitlab_read_file(repo, path.strip(), branch)
                if content and not content.startswith("[Error"):
                    result[path.strip()] = content
                    logger.info(f"[IDE] Read {path} ({len(content)} chars)")
            except Exception:
                logger.debug("[IDE] Skip %s", path)
        return result
    except Exception:
        logger.warning("[IDE] read_files_from_gitlab failed for %s", repo)
        return {}


# ── Exploration phase ───────────────────────────────────────────────────────

def run_exploration_phase(
        issue: dict,
        repo: str,
        repo_ctx: dict,
        repo_index_key: str,
        issue_type: str = "feature",
) -> dict:
    """
    IDE-mode exploration: search indexed codebase + read real files BEFORE solutioning.

    Flow:
      1. Build 2-3 targeted search queries from the Jira issue text
      2. Run hybrid_retrieve_context for each query (pgvector + BM25 + reranking)
      3. Extract file paths referenced in returned chunks
      4. Read those files from GitLab to get actual source code
      5. LLM synthesises: entry points, existing patterns, relevant functions
      6. Return the full exploration result dict

    Returns:
        {
          "entry_points":     [file paths most relevant to this issue],
          "code_snippets":    {path: trimmed content},
          "rag_chunks":       [raw retrieval chunks, up to 20],
          "patterns_summary": "LLM narrative about code patterns found",
          "search_queries":   [queries used],
          "confidence":       float,
          "index_key":        str,
        }

    Never raises — returns empty dict on total failure so the pipeline continues.
    """
    empty = {
        "entry_points": [], "code_snippets": {}, "rag_chunks": [],
        "patterns_summary": "", "search_queries": [],
        "confidence": 0.0, "index_key": repo_index_key,
    }

    if not repo_index_key:
        logger.warning("[IDE] run_exploration_phase: no repo_index_key — skipping")
        return empty

    keyword_text = _extract_keywords(issue)
    if not keyword_text.strip():
        return empty

    summary   = issue.get("summary", issue.get("fields", {}).get("summary", ""))
    language  = repo_ctx.get("language", "")
    tech      = repo_ctx.get("tech_stack", "")
    all_paths = repo_ctx.get("all_paths", [])   # full filtered path list from _fetch_repo_context

    # ── Build search queries (broad → narrow) ───────────────────────────────
    queries: list = []

    # Q1: Full issue summary as-is (broadest signal)
    q1 = summary.strip()[:250]
    if q1:
        queries.append(q1)

    # Q2: Technical terms only — strip stopwords for code-term focused recall
    code_terms = re.sub(
        r"\b(the|a|an|is|are|was|were|have|has|with|from|for|that|this|"
        r"should|could|would|when|where|what|how|please|need|want|"
        r"fix|add|update|change|implement|create|remove|feature|ticket|jira|"
        r"ainxt|user|as|in|on|to|of|be|it|we|and|or|but|not)\b",
        " ",
        keyword_text,
        flags=re.IGNORECASE,
    )
    q2 = re.sub(r"\s+", " ", code_terms).strip()[:200]
    if q2 and q2 != q1:
        queries.append(q2)

    # Q3: Bug-specific — add error/exception terms to bias towards error-handling paths
    if issue_type == "bug":
        q3 = f"{summary} error exception null timeout failure stack trace"[:250]
        if q3 not in queries:
            queries.append(q3)

    queries = [q for q in queries if q.strip()][:_MAX_EXPLORE_SEARCH_PASSES]
    empty["search_queries"] = queries

    # ── Run searches ────────────────────────────────────────────────────────
    all_chunks: list = []
    best_confidence = 0.0

    for i, query in enumerate(queries):
        chunks, conf = codebase_search(
            query, repo_index_key, top_k=_MAX_EXPLORE_CHUNKS_PER_Q
        )
        logger.info(
            f"[IDE] Exploration pass {i+1}/{len(queries)}: "
            f"query='{query[:60]}…' → {len(chunks)} chunks, confidence={conf:.2f}"
        )
        if conf > best_confidence:
            best_confidence = conf
        for c in chunks:
            if c not in all_chunks:
                all_chunks.append(c)

    result = {**empty, "confidence": best_confidence, "rag_chunks": all_chunks}

    if best_confidence < _CONFIDENCE_THRESHOLD:
        logger.warning(
            f"[IDE] Exploration confidence {best_confidence:.2f} below threshold "
            f"for repo '{repo_index_key}'. "
            "Ensure the codebase is indexed via CodebaseManager before triggering SDLC."
        )

    if not all_chunks:
        logger.warning(
            f"[IDE] No RAG results for repo '{repo_index_key}' — "
            "codebase not indexed or index key mismatch."
        )
        return result

    # ── Extract candidate file paths from chunks and read them ──────────────
    chunk_paths = _extract_file_paths_from_chunks(all_chunks)
    logger.info(
        f"[IDE] Extracted {len(chunk_paths)} candidate paths from chunks: "
        f"{chunk_paths[:10]}"
    )

    file_contents = read_files_from_gitlab(repo, chunk_paths)
    result["entry_points"]  = list(file_contents.keys())
    result["code_snippets"] = dict(file_contents)

    # ── Reference pattern search (cross-file awareness) ──────────────────────
    # Finds WHERE ELSE the same pattern is already working in the codebase.
    # Example: "projects router missing request ID tagging" → finds gateway.py
    # which already tags request IDs, giving the LLM a concrete reference.
    ref_patterns = _find_reference_patterns(
        issue          = issue,
        repo           = repo,
        repo_index_key = repo_index_key,
        entry_points   = result["entry_points"],
        language       = language,
    )
    result["reference_patterns"] = ref_patterns
    if ref_patterns:
        # Merge into code_snippets so they're available to prompt builders
        for p, c in ref_patterns.items():
            if p not in result["code_snippets"]:
                result["code_snippets"][p] = c

    # ── Build relevant file tree from all_paths + entry points ───────────────
    # Replaces the random filtered[:500] tree with a relevance-ranked view.
    if all_paths:
        result["relevant_tree"] = build_relevant_file_tree(
            all_paths    = all_paths,
            entry_points = result["entry_points"],
        )
    else:
        result["relevant_tree"] = ""

    # ── LLM synthesis ───────────────────────────────────────────────────────
    if file_contents:
        try:
            from models.model_router import model_router as _mr
            from core.prompt_sanitizer import sanitize

            code_block = "\n\n".join(
                f"--- {p} ---\n```{language}\n{c}\n```"
                for p, c in file_contents.items()
            )
            chunk_sample = "\n\n".join(all_chunks)

            synth_prompt = sanitize(
                f"You are a senior engineer doing pre-implementation code exploration.\n\n"
                f"=== JIRA TICKET ===\n"
                f"Type: {issue_type} | Summary: {summary}\n"
                f"Tech Stack: {tech} | Language: {language}\n\n"
                f"=== ACTUAL CODE FOUND IN INDEXED REPO ===\n{code_block}\n\n"
                f"=== ADDITIONAL RETRIEVED CONTEXT ===\n{chunk_sample}\n\n"
                f"=== YOUR TASK ===\n"
                f"Based ONLY on the actual code above (not guesses), provide:\n"
                f"1. entry_points: 2-5 most relevant files/classes for this ticket\n"
                f"2. existing_patterns: Code conventions already used "
                f"(error handling style, service structure, naming)\n"
                f"3. relevant_functions: Specific functions/methods likely involved\n"
                f"4. caller_graph: What calls what (from what you can see)\n"
                f"5. cautions: Shared utilities, patterns to preserve, gotchas\n\n"
                f"Name ACTUAL class names, function names, import paths from the code. "
                f"Max 400 words. Narrative format (no JSON)."
            )
            summary_text = _mr.generate(synth_prompt, model_hint="solution")
            result["patterns_summary"] = summary_text or ""
            logger.info(
                f"[IDE] Exploration complete — {len(file_contents)} files read, "
                f"confidence={best_confidence:.2f}"
            )
        except Exception as _e:
            logger.warning(f"[IDE] Exploration synthesis failed (non-blocking): {_e}")

    return result


# ── Relevant file tree ──────────────────────────────────────────────────────

def build_relevant_file_tree(
        all_paths: list,
        entry_points: list,
        max_lines: int = 500,
) -> str:
    """
    Build a relevance-ranked file tree for SDLC classifier / triager prompts.

    The classic problem: `filtered[:500]` on a 100K-file repo shows the first
    500 alphabetically-sorted paths. The actually relevant files are almost
    certainly NOT in that slice.

    This function instead produces:
      Section 1 — Exploration-found files (confirmed relevant by indexed search)
      Section 2 — Sibling files (same dirs as entry points: module context)
      Section 3 — Directory structure (all dirs, entry-point dirs first)
      Section 4 — Remaining files (all, no cap)

    No internal sub-caps — every file and directory is shown. The caller
    controls total output size via max_lines (default 500). PATH CHECK ground
    truth uses file_tree (fully uncapped), so relevant_tree is a focused hint
    and silently dropping files is worse than a longer output.
    """
    if not all_paths:
        return ""

    output: list = []
    seen:   set  = set()

    # ── Section 1: exploration entry points (ALWAYS first — most important) ──
    if entry_points:
        output.append("=== RELEVANT FILES (found via indexed codebase search) ===")
        for ep in entry_points:
            output.append(f"{ep}  ← relevant")
            seen.add(ep)
        output.append("")

    # ── Section 2: sibling files (same directories as entry points) ─────────
    if entry_points:
        relevant_dirs = {
            ep.rsplit("/", 1)[0]
            for ep in entry_points
            if "/" in ep
        }
        siblings = [
            p for p in all_paths
            if (p.rsplit("/", 1)[0] if "/" in p else "") in relevant_dirs
               and p not in seen
        ]
        if siblings:
            output.append("=== SIBLING FILES (same modules as relevant files) ===")
            for p in siblings:
                output.append(p)
                seen.add(p)
            output.append("")

    # ── Section 3: directory shape (all dirs, entry-point dirs first) ────────
    dirs: set = set()
    for p in all_paths:
        parts = p.split("/")
        for depth in range(1, min(len(parts), 4)):
            dirs.add("/".join(parts[:depth]) + "/")

    ep_dirs: set = set()
    for ep in (entry_points or []):
        parts = ep.split("/")
        for depth in range(1, min(len(parts), 4)):
            ep_dirs.add("/".join(parts[:depth]) + "/")
    prioritized_dirs = sorted(ep_dirs) + sorted(dirs - ep_dirs)

    output.append("=== PROJECT DIRECTORY STRUCTURE ===")
    for d in prioritized_dirs:
        output.append(d)
    output.append("")

    # ── Section 4: remaining files (all, no internal cap) ───────────────────
    remaining = [p for p in all_paths if p not in seen]
    if remaining:
        output.append("=== OTHER FILES ===")
        output.extend(remaining)

    return "\n".join(output)


def _find_reference_patterns(
        issue: dict,
        repo: str,
        repo_index_key: str,
        entry_points: list,
        language: str = "",
) -> dict:
    """
    Cross-file pattern awareness: find WHERE ELSE in the codebase the same
    pattern is already implemented.

    Example: ticket says "projects router missing request ID logging."
    Entry point found: routers/projects_router.py
    Reference finder: search "set_request_id log request_id" → finds gateway.py
    which already HAS the pattern.  The LLM can now see both files and understand
    exactly what's missing and how to add it.

    Returns {path: content_snippet} for up to 3 reference files.
    Never raises.
    """
    if not repo_index_key or not entry_points:
        return {}

    try:
        summary = issue.get("summary", issue.get("fields", {}).get("summary", ""))

        # Build a "how is X done" search query — different from the issue keywords
        # Focus on the mechanism, not the symptom
        ref_query = f"{summary} existing implementation pattern {language}"[:250]

        ref_chunks, ref_conf = codebase_search(ref_query, repo_index_key, top_k=8)
        if not ref_chunks or ref_conf < 0.4:
            return {}

        # Extract paths, exclude files we already have from main exploration
        ref_paths = _extract_file_paths_from_chunks(ref_chunks)
        new_paths  = [p for p in ref_paths if p not in entry_points]

        if not new_paths:
            return {}

        ref_files = read_files_from_gitlab(repo, new_paths)
        logger.info(
            f"[IDE] Reference patterns found: {list(ref_files.keys())} "
            f"(confidence={ref_conf:.2f})"
        )
        return dict(ref_files)

    except Exception as e:
        logger.debug(f"[IDE] _find_reference_patterns failed (non-blocking): {e}")
        return {}


def build_exploration_block(exploration: dict) -> str:
    """
    Format the exploration result as a prompt block to inject into
    classifier / triage / analyst / solutioning prompts.

    Includes three sections:
      1. Entry points (files confirmed relevant by indexed search)
      2. Actual source code snippets from those files
      3. Reference patterns (WHERE ELSE the same pattern is already working)
      4. LLM pattern summary
    """
    if not exploration or not exploration.get("entry_points"):
        return ""

    conf = exploration.get("confidence", 0)
    lines = [
        "=== CODEBASE EXPLORATION — INDEXED SEARCH RESULTS ===",
        f"(Searched indexed repo before classification. Confidence: {conf:.0%})",
        "",
    ]

    # Section 1: entry point files
    ep = exploration.get("entry_points", [])
    if ep:
        lines.append("Relevant files identified in indexed repo:")
        for p in ep:
            lines.append(f"  • {p}")
        lines.append("")

    # Section 2: actual code from entry point files
    snippets     = exploration.get("code_snippets", {})
    ref_patterns = exploration.get("reference_patterns", {})
    target_files = {k: v for k, v in snippets.items() if k not in ref_patterns}

    for path, content in target_files.items():
        lines.append(f"--- {path} ---")
        lines.append(f"```\n{content}\n```")
        lines.append("")

    # Section 3: reference patterns (cross-file awareness)
    # Shows HOW the same pattern is already implemented elsewhere in the codebase.
    # This is what gives the LLM the "working example" to model the fix on.
    if ref_patterns:
        lines.append(
            "=== REFERENCE IMPLEMENTATIONS — the same pattern already works here ==="
        )
        lines.append(
            "(Use these as the MODEL for your fix — replicate the pattern, "
            "don't reinvent it)"
        )
        for path, content in ref_patterns.items():
            lines.append(f"--- {path} (reference) ---")
            lines.append(f"```\n{content}\n```")
            lines.append("")

    # Section 4: pattern summary
    patterns = exploration.get("patterns_summary", "")
    if patterns:
        lines.append("Entry-point analysis / code patterns:")
        lines.append(patterns)
        lines.append("")

    lines.append(
        "CRITICAL: Your affected_components / files_to_change MUST include files "
        "from the 'Relevant files' list above. Reference actual class names, "
        "method names, and import paths visible in the code snippets."
    )
    return "\n".join(lines)


# ── Better RAG query for the coder ─────────────────────────────────────────

def build_coder_rag_query(
        path: str,
        desc: str,
        design: Optional[dict] = None,
        language: str = "",
        exploration: Optional[dict] = None,
) -> str:
    """
    Build a rich, intent-focused RAG query for per-file code generation.

    Instead of just 'path + desc', this combines:
    - The specific change description (what exactly changes in this file)
    - Function/class names from the solution design
    - Language context for better BM25 matching
    - Exploration-derived patterns (if available)
    """
    parts: list = []

    if language:
        parts.append(language)

    fname = path.split("/")[-1] if path else ""
    if fname:
        parts.append(fname)

    if desc and desc.strip():
        parts.append(desc.strip()[:200])

    if design:
        cs = design.get("code_structure") or {}
        funcs = (
                [str(f) for f in (cs.get("modified_functions") or [])[:3]]
                + [str(f) for f in (cs.get("new_functions") or [])[:2]]
        )
        if funcs:
            parts.append(" ".join(funcs)[:150])

        # Pull the implementation plan step that mentions this file
        plan = design.get("implementation_plan") or []
        for step in plan:
            step_str = str(step)
            if fname and fname in step_str:
                # extract the action text (after the filename mention)
                parts.append(step_str[:120])
                break

    # Add exploration patterns hint if the path is an entry point
    if exploration and path in (exploration.get("entry_points") or []):
        patterns = exploration.get("patterns_summary", "")
        if patterns:
            parts.append(patterns[:150])

    query = " ".join(p for p in parts if p.strip())
    return query[:350]  # keep within reasonable embedding input size
