# SPDX-License-Identifier: Apache-2.0
"""
Code Knowledge Graph resolver.

Pipeline position:  Query → Graph Resolver → RAG → Reranker → LLM

Given a user question, extracts code identifiers (CamelCase class names,
snake_case functions, UPPER_CASE constants), looks them up in the code_graph
table, traverses 1 hop of relations (imports, extends, implements, calls),
and returns the structurally-related file paths AND the matched symbol names.

hybrid_retriever uses these to:
  1. Run a graph-scoped pgvector pass first (file_filter → precision)
  2. Run an additional symbol_search on matched names (symbol-level boost)
  3. Fall back to full-repo search immediately after (recall)

Graph vs RAG:
    RAG  answers "what is semantically similar?"
    Graph answers "what is structurally related?"
    Together they give Context-Aware Code Intelligence.

`build_dependency_slice` is the SDLC helper: given a repo + caller-provided
symbol list (extracted from a Jira ticket), returns a compact structured map
of the dependency sub-graph — much smaller than a flat RAG dump.
"""

import re
from core.logger import logger

# ── Default deny-list path prefixes ──────────────────────────────────────────
# Paths whose file_path starts with any of these prefixes are excluded from
# build_dependency_slice results. Overridable via the deny_paths parameter.
_DEFAULT_DENY_PREFIXES: tuple[str, ...] = (
    "bkp_",
    "old_scripts/",
    "backup/",
    "prod_catchup_",
    "archive/",
    "archived/",
)

_EMPTY_SLICE: dict = {"text": "", "files": [], "symbols": [], "char_count": 0}


def _is_denied(file_path: str, deny_prefixes: tuple[str, ...]) -> bool:
    """Return True if *file_path* matches any deny prefix (case-insensitive check on basename-or-full)."""
    if not file_path:
        return False
    fp_lower = file_path.lower()
    # Check against full path and also the last path component to catch
    # deny-listed directories regardless of leading path segments.
    for prefix in deny_prefixes:
        p = prefix.lower()
        if fp_lower.startswith(p):
            return True
        # also match if any path component starts with the prefix (e.g. "bkp_utils" nested)
        for segment in fp_lower.replace("\\", "/").split("/"):
            if segment.startswith(p):
                return True
    return False

# Extracts code-style identifiers from natural-language questions:
#   CamelCase  — PaymentService, RetryHandler  (class / interface names)
#   UPPER_SNAKE — MAX_RETRY, BASE_URL           (constants)
#   snake_case  — process_payment (4+ chars, avoids noise like "the_", "is_a")
_IDENTIFIER_RE = re.compile(
    r'\b([A-Z][a-zA-Z0-9]{2,}|[A-Z][A-Z0-9_]{2,}[A-Z0-9]|[a-z][a-z0-9_]{3,}[a-z0-9])\b'
)

# Stop-words that look like identifiers but are just English prose
_STOP_WORDS = frozenset({
    "The", "This", "That", "What", "When", "Where", "Which", "How", "Why",
    "And", "But", "For", "With", "From", "Into", "Then", "Also", "Does",
    "API", "SQL", "HTTP", "URL", "JSON", "XML", "REST", "UUID", "IDE",
})


def resolve_graph_context(
    question: str,
    repo: str,
    user_ctx=None,
    max_hops: int = None,           # P4: configurable hop depth (default from env)
    edge_types: list = None,        # P4: filter by edge type (None = all types)
) -> tuple[list[str], list[str]]:
    """
    Returns (file_paths, matched_names) structurally connected to entities in *question*.

    file_paths   — set of file paths reachable within max_hops from matched nodes.
                   Used by hybrid_retriever as a file-scope pre-filter on pgvector.

    matched_names — exact node names from the graph that matched the question.
                    Used by hybrid_retriever to run an additional symbol_search,
                    giving symbol-level (chunk-level) precision on top of file-level routing.

    Both lists are empty when:
    - graph has no data for this repo (not yet indexed)
    - no code identifiers extracted from the question
    - DB is unavailable (non-fatal — caller falls back to full-repo search)

    Two-tier lookup:
    1. Exact lower(name) = ANY(candidates) — fast, uses idx_code_graph_name
    2. Fuzzy fallback: lower(name) LIKE '%term%' — fires when exact returns nothing,
       handles natural-language queries like "why retry failing" → RetryHandler

    P4 additions:
    - max_hops: override SDLC_GRAPH_MAX_HOPS env var (default 2)
    - edge_types: restrict traversal to specific edge types (e.g. ["call", "import"])
    """
    import os as _os
    if max_hops is None:
        try:
            max_hops = int(_os.getenv("SDLC_GRAPH_MAX_HOPS", "2"))
        except (ValueError, TypeError):
            max_hops = 2
    max_hops = max(1, min(max_hops, 5))  # safety clamp: 1–5 hops
    if not question or not repo:
        return [], []

    # code_graph stores repo WITHOUT the "repo_" prefix (same convention as code_symbols)
    repo = repo[5:] if repo.lower().startswith("repo_") else repo

    candidates = [
        m.group(1) for m in _IDENTIFIER_RE.finditer(question)
        if m.group(1) not in _STOP_WORDS
    ]
    candidates = list(dict.fromkeys(candidates))  # deduplicate, preserve order
    if not candidates:
        return [], []

    # ── Unified knowledge graph (multi-hop) — preferred when it has been built ─
    try:
        _kg = _resolve_kg_context(candidates, repo, user_ctx, max_hops=max_hops)
        if _kg is not None and (_kg[0] or _kg[1]):
            # Cap file list to 20 to avoid query explosion (P4 safety)
            _files = _kg[0][:20]
            logger.info("graph_resolver(KG): repo=%s files=%d names=%s hops=%d",
                        repo, len(_files), _kg[1][:6], max_hops)
            return _files, _kg[1]
    except Exception as _kge:
        logger.debug("graph_resolver: KG multi-hop path failed (non-fatal): %s", _kge)

    # ── Fallback: existing 1-hop code_graph (zero regression) ─────────────────

    try:
        from db.database import engine
        from sqlalchemy import text as _sql
        import json as _json

        with engine.connect() as conn:

            # ── Tier 1: exact case-insensitive name match ─────────────────────
            rows = conn.execute(_sql("""
                SELECT name, file_path, relations
                FROM code_graph
                WHERE repo = :repo
                  AND lower(name) = ANY(:names)
                LIMIT 30
            """), {
                "repo":  repo,
                "names": [c.lower() for c in candidates],
            }).fetchall()

            # ── Tier 2: fuzzy partial match (fires only when exact fails) ──────
            # Handles queries like "why retry failing?" → matches "RetryHandler"
            # Uses ILIKE per-term; acceptable on table sizes < 500K rows.
            if not rows:
                like_clauses = " OR ".join(
                    f"lower(name) LIKE :p{i}" for i in range(len(candidates[:8]))
                )
                like_params  = {f"p{i}": f"%{c.lower()}%" for i, c in enumerate(candidates[:8])}
                rows = conn.execute(_sql(f"""
                    SELECT name, file_path, relations
                    FROM code_graph
                    WHERE repo = :repo
                      AND ({like_clauses})
                    LIMIT 20
                """), {"repo": repo, **like_params}).fetchall()

            if not rows:
                return [], []

            file_paths:    set[str] = set()
            hop1_names:    set[str] = set()
            matched_names: list[str] = []

            for row in rows:
                file_paths.add(row.file_path)
                matched_names.append(row.name)   # exact name from graph node

                relations = row.relations
                if isinstance(relations, str):
                    try:
                        relations = _json.loads(relations)
                    except Exception:
                        relations = []
                relations = relations or []

                for rel in relations:
                    if not isinstance(rel, dict):
                        continue
                    target_file = rel.get("target_file", "") or ""
                    target_name = rel.get("target_name", "") or ""
                    if target_file:
                        file_paths.add(target_file)
                    elif target_name and target_name not in _STOP_WORDS:
                        hop1_names.add(target_name.lower())

            # ── 1-hop: resolve target_names to their file paths ───────────────
            if hop1_names:
                hop2_rows = conn.execute(_sql("""
                    SELECT DISTINCT name, file_path
                    FROM code_graph
                    WHERE repo = :repo
                      AND lower(name) = ANY(:names)
                    LIMIT 20
                """), {
                    "repo":  repo,
                    "names": list(hop1_names)[:30],
                }).fetchall()
                for r in hop2_rows:
                    file_paths.add(r.file_path)
                    # Also include hop-1 names as matched (they're callee/supertype names)
                    matched_names.append(r.name)

        result_files = [fp for fp in file_paths if fp]
        # Deduplicate matched names, preserve order
        seen: set[str] = set()
        result_names = [n for n in matched_names if n and not (n in seen or seen.add(n))]

        logger.info(
            "graph_resolver: repo=%s  candidates=%s  files=%d  names=%s",
            repo, candidates[:6], len(result_files), result_names[:6],
        )
        return result_files, result_names

    except Exception as e:
        logger.debug("graph_resolver: lookup failed (non-fatal): %s", e)
        return [], []


def build_dependency_slice(
    repo: str,
    symbols: list[str],
    *,
    max_hops: int = 1,
    deny_paths: list[str] | None = None,
) -> dict:
    """
    Build a compact, structured dependency slice for *symbols* in *repo*.

    Keyed off caller-provided symbol names (extracted from ticket title/summary
    + affected-files analysis), NOT a natural-language query.  This keeps the
    result precise and deterministic — no NL extraction noise.

    Parameters
    ----------
    repo      : str   — repo identifier (same format as code_graph.repo; "repo_" prefix
                        is stripped automatically, matching the convention in resolve_graph_context).
    symbols   : list  — explicit symbol names to look up (class names, function names,
                        constants).  Empty list → empty slice immediately.
    max_hops  : int   — traversal depth cap (capped at 2 internally, default 1).
    deny_paths: list  — path prefix deny-list override.  ``None`` → use _DEFAULT_DENY_PREFIXES.

    Returns
    -------
    dict with keys:
        "text"       : str   — compact explicit map, e.g.
                               "PaymentService imports RetryHandler; PaymentService calls process_payment\\n
                                Impacted files: payment_service.py, retry_handler.py"
                               Empty string when graph missing or symbols list empty.
        "files"      : list[str]  — de-duplicated file paths (deny-filtered).
        "symbols"    : list[str]  — de-duplicated matched symbol names (deny-filtered).
        "char_count" : int        — len(text).

    Guarantees
    ----------
    - Never raises.  Any DB/IO error → empty slice logged at DEBUG.
    - Excludes deny-listed paths from both "files" and "text".
    - max_hops capped at 2.
    - No LLM calls — pure graph/DB read + formatting.
    - Single-repo: repo is a scalar str, NOT a list.
    """
    if not repo or not symbols:
        return _EMPTY_SLICE

    # Resolve deny prefixes
    deny_prefixes: tuple[str, ...] = (
        tuple(deny_paths) if deny_paths is not None else _DEFAULT_DENY_PREFIXES
    )

    # Cap hops
    hops = max(1, min(int(max_hops), 2))

    # Strip "repo_" prefix to match code_graph convention
    _repo = repo[5:] if repo.lower().startswith("repo_") else repo

    # Deduplicate caller-provided symbols, preserve order
    seen_sym: set[str] = set()
    deduped_symbols = [s for s in symbols if s and not (s in seen_sym or seen_sym.add(s))]
    if not deduped_symbols:
        return _EMPTY_SLICE

    try:
        from db.database import engine
        from sqlalchemy import text as _sql
        import json as _json

        with engine.connect() as conn:

            # ── Seed lookup: exact match on caller-provided symbol names ─────────
            # Always case-insensitive exact match first (fast, uses idx_code_graph_name).
            seed_rows = conn.execute(_sql("""
                SELECT name, file_path, relations
                FROM code_graph
                WHERE repo = :repo
                  AND lower(name) = ANY(:names)
                LIMIT 50
            """), {
                "repo":  _repo,
                "names": [s.lower() for s in deduped_symbols],
            }).fetchall()

            if not seed_rows:
                # No graph data for this repo / these symbols — empty slice
                logger.debug(
                    "build_dependency_slice: no seed rows for repo=%s symbols=%s",
                    _repo, deduped_symbols[:6],
                )
                return _EMPTY_SLICE

            # ── Parse seed nodes ─────────────────────────────────────────────────
            # relation_map: seed_name → list of {rel_type, target_name, target_file}
            relation_map: dict[str, list[dict]] = {}
            hop1_names:   set[str] = set()
            file_paths:   set[str] = set()
            matched_symbols: list[str] = []

            for row in seed_rows:
                name = row.name
                fp   = row.file_path or ""

                # Apply deny filter to seed file
                if fp and not _is_denied(fp, deny_prefixes):
                    file_paths.add(fp)

                matched_symbols.append(name)
                relation_map.setdefault(name, [])

                relations = row.relations
                if isinstance(relations, str):
                    try:
                        relations = _json.loads(relations)
                    except Exception:
                        relations = []
                relations = relations or []

                for rel in relations:
                    if not isinstance(rel, dict):
                        continue
                    rel_type    = rel.get("type", "") or rel.get("relation", "") or "references"
                    target_name = rel.get("target_name", "") or ""
                    target_file = rel.get("target_file", "") or ""

                    if target_file and not _is_denied(target_file, deny_prefixes):
                        file_paths.add(target_file)

                    if target_name and target_name not in _STOP_WORDS:
                        hop1_names.add(target_name.lower())
                        relation_map[name].append({
                            "type": rel_type,
                            "target": target_name,
                            "file":  target_file,
                        })

            # ── Hop-1 resolution: resolve target names → file paths ──────────────
            hop1_resolved: dict[str, str] = {}  # lower(name) → file_path
            if hop1_names:
                hop1_rows = conn.execute(_sql("""
                    SELECT name, file_path
                    FROM code_graph
                    WHERE repo = :repo
                      AND lower(name) = ANY(:names)
                    LIMIT 40
                """), {
                    "repo":  _repo,
                    "names": list(hop1_names)[:40],
                }).fetchall()
                for r in hop1_rows:
                    fp = r.file_path or ""
                    if fp and not _is_denied(fp, deny_prefixes):
                        file_paths.add(fp)
                        hop1_resolved[r.name.lower()] = fp
                    matched_symbols.append(r.name)

            # ── Hop-2 (only when max_hops == 2) ──────────────────────────────────
            if hops == 2 and hop1_resolved:
                # Resolve the relations of hop-1 nodes one level deeper
                hop2_names: set[str] = set()

                hop1_rel_rows = conn.execute(_sql("""
                    SELECT name, relations
                    FROM code_graph
                    WHERE repo = :repo
                      AND lower(name) = ANY(:names)
                    LIMIT 30
                """), {
                    "repo":  _repo,
                    "names": list(hop1_resolved.keys())[:30],
                }).fetchall()

                for row in hop1_rel_rows:
                    rels = row.relations
                    if isinstance(rels, str):
                        try:
                            rels = _json.loads(rels)
                        except Exception:
                            rels = []
                    for rel in (rels or []):
                        if not isinstance(rel, dict):
                            continue
                        tname = rel.get("target_name", "") or ""
                        tfile = rel.get("target_file", "") or ""
                        if tfile and not _is_denied(tfile, deny_prefixes):
                            file_paths.add(tfile)
                        if tname and tname not in _STOP_WORDS:
                            hop2_names.add(tname.lower())

                if hop2_names:
                    hop2_rows = conn.execute(_sql("""
                        SELECT DISTINCT name, file_path
                        FROM code_graph
                        WHERE repo = :repo
                          AND lower(name) = ANY(:names)
                        LIMIT 30
                    """), {
                        "repo":  _repo,
                        "names": list(hop2_names)[:30],
                    }).fetchall()
                    for r in hop2_rows:
                        fp = r.file_path or ""
                        if fp and not _is_denied(fp, deny_prefixes):
                            file_paths.add(fp)
                        matched_symbols.append(r.name)

        # ── Format compact text map ───────────────────────────────────────────
        lines: list[str] = []

        for seed_name, rels in relation_map.items():
            if not rels:
                continue
            for r in rels:
                rel_type = r["type"]
                target   = r["target"]
                # Skip if target resolves to a denied file
                t_lower = target.lower()
                t_file  = hop1_resolved.get(t_lower, r.get("file", ""))
                if t_file and _is_denied(t_file, deny_prefixes):
                    continue
                lines.append(f"{seed_name} {rel_type} {target}")

        # De-duplicate file list, preserve insertion order
        seen_files: set[str] = set()
        result_files = [fp for fp in file_paths if fp and not (fp in seen_files or seen_files.add(fp))]

        # De-duplicate matched symbols, preserve order
        seen_names: set[str] = set()
        result_symbols = [
            n for n in matched_symbols
            if n and not (n in seen_names or seen_names.add(n))
        ]

        if result_files:
            lines.append("Impacted files: " + ", ".join(sorted(result_files)))

        text = "\n".join(lines)

        logger.info(
            "build_dependency_slice: repo=%s seeds=%d hops=%d files=%d symbols=%d chars=%d",
            _repo, len(deduped_symbols), hops, len(result_files), len(result_symbols), len(text),
        )

        return {
            "text":       text,
            "files":      result_files,
            "symbols":    result_symbols,
            "char_count": len(text),
        }

    except Exception as e:
        logger.debug("build_dependency_slice: lookup failed (non-fatal): %s", e)
        return _EMPTY_SLICE


# ── Backward-compatible alias (callers that only need file paths) ─────────────
def resolve_graph_files(question: str, repo: str) -> list[str]:
    files, _ = resolve_graph_context(question, repo)
    return files

# ── Unified knowledge-graph multi-hop resolver (P6) ───────────────────────────
def _resolve_kg_context(candidates, repo, user_ctx=None, max_hops: int = 2):
    """
    Multi-hop traversal over the unified knowledge_graph_* tables (RBAC-filtered).

    Returns:
        None          — graph for this repo has not been built (caller falls back
                         to the legacy 1-hop code_graph path → zero regression)
        ([], [])      — graph exists but nothing matched
        (files, names)— code source_refs (file paths) + matched/reached symbol names

    RBAC: filtered in SQL on BOTH node and edge tables. When user_ctx is None the
    filter is permissive — the file list is only a recall hint; the downstream
    pgvector pass re-applies the authoritative ACL on the actual chunks.
    """
    from db.database import engine
    from sqlalchemy import text as _sql

    graph_id = f"repo:{repo}"

    if user_ctx:
        try:
            _ad = int(user_ctx.get("band_level", user_ctx.get("ad_level", 6)))
        except Exception:
            _ad = 6
        ctx = {
            "is_admin": bool(user_ctx.get("is_admin")
                             or user_ctx.get("user_role") == "admin"),
            "user_dept": user_ctx.get("department") or "",
            "user_clearance": max(0, 6 - _ad),
        }
    else:
        ctx = {"is_admin": True, "user_dept": "", "user_clearance": 6}

    node_acl = ("(:is_admin OR ((n.department IS NULL OR n.department='' "
                "OR n.department=:user_dept) AND n.min_band_level<=:user_clearance))")
    edge_acl = "(:is_admin OR e.min_band_level<=:user_clearance)"

    with engine.connect() as conn:
        # Graph built?  (cheap indexed scan)
        built = conn.execute(_sql(
            "SELECT 1 FROM knowledge_graph_nodes WHERE graph_id=:gid LIMIT 1"),
            {"gid": graph_id}).first()
        if not built:
            return None  # not built → caller falls back to legacy code_graph

        lower_names = [c.lower() for c in candidates]

        # ── Seed match: exact, then bounded fuzzy ─────────────────────────────
        seeds = conn.execute(_sql(
            f"SELECT node_id, name FROM knowledge_graph_nodes n "
            f"WHERE graph_id=:gid AND lower(name)=ANY(:names) AND {node_acl} LIMIT 30"),
            {"gid": graph_id, "names": lower_names, **ctx}).fetchall()

        if not seeds and candidates:
            like = " OR ".join(f"lower(n.name) LIKE :p{i}"
                               for i in range(len(candidates[:8])))
            lp = {f"p{i}": f"%{c.lower()}%" for i, c in enumerate(candidates[:8])}
            seeds = conn.execute(_sql(
                f"SELECT node_id, name FROM knowledge_graph_nodes n "
                f"WHERE graph_id=:gid AND ({like}) AND {node_acl} LIMIT 20"),
                {"gid": graph_id, **lp, **ctx}).fetchall()

        if not seeds:
            return [], []

        seed_ids = [s[0] for s in seeds]
        names: list[str] = [s[1] for s in seeds]

        # ── Multi-hop: RECURSIVE CTE over edges (RBAC on both tables) ──────────
        reached = conn.execute(_sql(f"""
            WITH RECURSIVE trav(node_id, depth) AS (
                SELECT node_id, 0
                FROM knowledge_graph_nodes
                WHERE graph_id=:gid AND node_id = ANY(:seeds)
              UNION
                SELECT e.dst_node_id, t.depth + 1
                FROM knowledge_graph_edges e
                JOIN trav t ON e.src_node_id = t.node_id
                WHERE e.graph_id=:gid AND t.depth < :hops AND {edge_acl}
            )
            SELECT DISTINCT n.source_ref, n.name
            FROM trav tr
            JOIN knowledge_graph_nodes n
              ON n.graph_id=:gid AND n.node_id = tr.node_id
            WHERE n.source_type = 'code' AND {node_acl}
            LIMIT 200
        """), {"gid": graph_id, "seeds": seed_ids, "hops": max_hops, **ctx}).fetchall()

    files: list[str] = []
    for src_ref, nm in reached:
        if src_ref:
            files.append(src_ref)
        if nm:
            names.append(nm)

    files = list(dict.fromkeys(f for f in files if f))
    _seen: set[str] = set()
    names = [n for n in names if n and not (n in _seen or _seen.add(n))]
    return files, names