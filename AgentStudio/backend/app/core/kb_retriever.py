# SPDX-License-Identifier: MIT
"""
Knowledge Base retriever for Build Studio agents.

Bridges Build Studio agents (both standalone via AgentRunner and workflow nodes
via WorkflowEngine) to the platform pgvector Knowledge Base — the
``document_embeddings`` table populated by ``store/docs_store.activate_doc``.

Design contract (why this module is thin)
------------------------------------------
This module deliberately contains **no retrieval orchestration of its own**. All
search, hybrid merge, BGE reranking, query expansion, coverage tiering and result
caching are delegated to the single platform entry point
``models.hybrid_retriever.hybrid_retrieve_context`` — the exact same function that
powers chat RAG, the IDE and the orchestrator. Namespace enumeration is delegated
to ``store.docs_store.list_namespaces`` (Redis → DB fallback, the same source the
chat ``Knowledge`` toggle uses).

Consequence: any future enhancement to KB indexing or retrieval in the platform
(new reranker, better recall, scope filters, caching …) is automatically inherited
by Build Studio agents with **zero** changes here.

Two entry points (signatures are stable — callers in ``pipeline.py`` and
``native_engine.py`` depend on them):
  * ``retrieve(...)`` — runs the platform hybrid pipeline and returns the
    concatenated chunk text (a single string).
  * ``build_context_section(...)`` — one-shot helper that returns the
    ready-to-inject ``## Reference Context`` string (or ``""``).

Constants
  ``KB_MODE_NONE / EXISTING / ADD`` — canonical values of
  ``agent.knowledge.mode``. Imported by callers to avoid stringly-typed checks.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

# Route through the shared gateway logger (core/logger.py) so KB retrieval
# records land in the same rotating agent.log as the rest of the platform,
# carry the structlog context (request_id, span_id, user_id, chat_id,
# agent_id, …), and respect LOG_LEVEL / LOG_DIR env vars centrally.
from core.logger import logger

# Build Studio's upload proxy (ABStudio/backend/app/api/kb.py) tags the
# stored ``safe_filename`` with ``ABS_FILENAME_PREFIX`` so citations rendered
# to the LLM can strip the wrapper. ``core.file_validator._sanitise_filename``
# already supplies uniqueness via its own ``uuid.hex[:8]_`` prefix; the marker
# here is purely for citation hygiene. Single source of truth for both the
# producing prefix (kb.py) and the stripping regex (below).
ABS_FILENAME_PREFIX = "_abs_"
_ABS_PREFIX_RE = re.compile(rf"^{re.escape(ABS_FILENAME_PREFIX)}")


def _display_name(file_path: Optional[str]) -> str:
    """Strip the Build Studio marker from a stored filename for citations.

    No-ops for sidebar KB uploads (their filenames never carry the marker).
    Retained for callers that render citations from a raw ``file_path``.
    """
    if not file_path:
        return ""
    return _ABS_PREFIX_RE.sub("", file_path)


# Canonical values of ``agent.knowledge.mode`` — keep the wire format stable
# so the frontend and backend never drift.
KB_MODE_NONE = "none"
KB_MODE_EXISTING = "existing_kb"
KB_MODE_ADD = "add_kb"
_RETRIEVAL_MODES = (KB_MODE_EXISTING, KB_MODE_ADD)

# ── Input validation for the user-controlled ``knowledge`` blob ──────────────
# The ``knowledge`` JSONB is user-controlled, so every value pulled from it is
# validated here BEFORE it reaches the (already parameterized) DB queries in
# ``_resolve_scope_doc_ids`` / ``_resolve_doc_file_paths``. This is
# defence-in-depth: the queries bind values via SQLAlchemy ``text()`` params
# (never string interpolation), so injection is not possible even without this
# — but validating up front rejects malformed/oversized input early and keeps
# the audit trail clean.
#
# Field-specific rules (a single character class would break real data —
# e.g. domain "Online & Settlement Systems App Dev" contains spaces and "&"):
#   * product_id / kb_doc_id : must be a UUID (the platform id format).
#   * doc ids (uploaded/selected) : must be a UUID.
#   * source_type : must be one of the DB CHECK enum values.
#   * domain / spec_version : free-text labels — bounded length + a safe
#     character allow-list that permits letters, digits, space and common
#     punctuation, but rejects control chars and SQL/quote metacharacters.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Letters, digits, space and a conservative punctuation set (& . , _ - ( ) :).
# Deliberately excludes quotes, semicolons, backslashes, angle brackets AND the
# forward slash: labels never legitimately contain "/", and disallowing it
# removes any latent path-traversal foothold should a label ever be used in a
# future path context (today it is only ever a bound SQL parameter).
_LABEL_RE = re.compile(r"^[\w\s&.,():-]{1,128}$", re.UNICODE)
# MUST mirror the knowledge_docs.source_type DB CHECK constraint exactly — no
# more, no less (see db/migrate.py, constraint ``chk_kdocs_source_type``:
# 'BRD','FSD','TPMC_DECISION','RBI_CIRCULAR','ARCHITECTURE','SPEC','OTHER').
# Kept as a literal (source_type is a plain String column — there is no shared
# ORM Enum to derive from). Update both together if the constraint changes.
_SOURCE_TYPES = frozenset({
    "BRD", "FSD", "TPMC_DECISION", "RBI_CIRCULAR", "ARCHITECTURE", "SPEC", "OTHER",
})

# Safety cap on rows returned per scope-resolution query (defence against a
# crafted broad scope forcing an unbounded fetch). Far above any realistic
# per-scope document count, so it never affects legitimate usage.
_SCOPE_RESOLVE_LIMIT = 1000


def _is_uuid(val: Any) -> bool:
    return isinstance(val, str) and bool(_UUID_RE.match(val))


def _is_label(val: Any) -> bool:
    return isinstance(val, str) and bool(_LABEL_RE.match(val))


def _validate_scope_entry(entry: Any) -> bool:
    """Allow-list validate a single graph-model scope entry.

    Returns True only when the entry is a dict with a UUID ``product_id`` and
    every present optional field passes its field-specific rule. Used to drop
    malformed / potentially malicious scope entries before DB resolution.
    """
    if not isinstance(entry, dict):
        return False
    if not _is_uuid(entry.get("product_id")):
        return False
    kb_doc_id = entry.get("kb_doc_id")
    if kb_doc_id is not None and not _is_uuid(kb_doc_id):
        return False
    st = entry.get("source_type")
    if st is not None and st not in _SOURCE_TYPES:
        return False
    for field in ("domain", "spec_version"):
        v = entry.get(field)
        if v is not None and not _is_label(v):
            return False
    return True

# Header injected before retrieved chunks. Kept in one place so both
# AgentRunner and WorkflowEngine produce identical prompt sections. The inline
# citation format matches the platform's native ``[Source: <file_path>]``
# markers that ``hybrid_retrieve_context`` already emits per chunk.
_CONTEXT_HEADING = (
    "## Reference Context\n\n"
    "Use the following retrieved knowledge to ground your answer. "
    "When you rely on a passage, cite it inline as [Source: <file_path>].\n\n"
)

# Matches the /ask fast-path probe width (gateway.py pgvector_search top_k=10)
# so agent chats recall the same candidate pool as the KB chat before rerank.
# Previously 5, which starved multi-chunk answers relative to /ask.
_DEFAULT_TOP_K = 10


def _parse_knowledge_config(
    knowledge: Any,
) -> Optional[tuple[str, list[str], Optional[list[str]], list[dict]]]:
    """Extract ``(mode, namespaces, doc_ids, scope_filters)`` from an agent
    ``knowledge`` blob.

    Returns ``None`` when the blob is not a retrieval config (missing, not a
    dict, or a non-retrieval mode). ``doc_ids`` is populated for:

      * ``KB_MODE_ADD`` from ``uploaded_doc_ids`` (strict per-workflow scoping)
      * ``KB_MODE_EXISTING`` from ``selected_doc_ids`` when the user narrowed a
        KB down to specific files; when empty the whole namespace corpus is used

    ``scope_filters`` is the NEW graph-model attachment: a list of
    ``{product_id, domain, spec_version?, source_type?, kb_doc_id?}`` entries
    written by the Agent Studio ScopePicker. Empty when the blob uses only
    the legacy ``namespaces`` field. Callers resolve these into concrete
    doc_ids via :func:`_resolve_scope_doc_ids` and merge the result into
    ``doc_ids`` before calling ``_resolve_scope``.

    Shared by both ``build_context_section*`` helpers.
    """
    if not isinstance(knowledge, dict):
        return None
    mode = (knowledge.get("mode") or KB_MODE_NONE).lower()
    if mode not in _RETRIEVAL_MODES:
        return None
    namespaces = knowledge.get("namespaces") or []
    if not isinstance(namespaces, list):
        namespaces = []
    # Doc-id allow-lists (uploaded/selected) are user-controlled — accept only
    # well-formed UUIDs. Non-UUID entries are dropped and logged rather than
    # trusted. The department ACL in ``_resolve_scope``/``hybrid_retrieve_context``
    # is still the authoritative access gate (see the note in _resolve_scope):
    # these ids only ever NARROW the already ACL-restricted corpus, they can
    # never widen it, so a crafted id cannot reach a doc outside the caller's
    # department.
    doc_ids: Optional[list[str]] = None
    if mode == KB_MODE_ADD:
        raw_ids = knowledge.get("uploaded_doc_ids") or []
        if isinstance(raw_ids, list):
            valid = [str(d) for d in raw_ids if _is_uuid(d)]
            dropped = len(raw_ids) - len(valid)
            if dropped:
                logger.warning(
                    '[AGENT] kb_retriever: dropped %d invalid uploaded_doc_ids '
                    '(non-UUID)', dropped
                )
            doc_ids = valid
    elif mode == KB_MODE_EXISTING:
        raw_ids = knowledge.get("selected_doc_ids") or []
        if isinstance(raw_ids, list):
            valid = [str(d) for d in raw_ids if _is_uuid(d)]
            dropped = len(raw_ids) - len(valid)
            if dropped:
                logger.warning(
                    '[AGENT] kb_retriever: dropped %d invalid selected_doc_ids '
                    '(non-UUID)', dropped
                )
            doc_ids = valid or None

    # NEW graph-model scopes. Each entry MUST carry a UUID product_id and pass
    # per-field allow-list validation (see _validate_scope_entry). Invalid /
    # malformed entries are dropped AND logged (never trusted, never silent) so
    # security-relevant probing leaves an audit trail.
    raw_scopes = knowledge.get("scopes") or []
    scope_filters: list[dict] = []
    if isinstance(raw_scopes, list):
        for s in raw_scopes:
            if not _validate_scope_entry(s):
                logger.warning(
                    '[AGENT] kb_retriever: dropped invalid scope_filter entry '
                    '(type=%s, has_product_id=%s)',
                    type(s).__name__,
                    bool(isinstance(s, dict) and s.get("product_id")),
                )
                continue
            scope_filters.append({
                "product_id":   str(s["product_id"]),
                "domain":       (s.get("domain") or None),
                "spec_version": (s.get("spec_version") or None),
                "source_type":  (s.get("source_type") or None),
                "kb_doc_id":    (s.get("kb_doc_id") or None),
            })
    return mode, namespaces, doc_ids, scope_filters


def _namespace_repos(namespaces: Optional[list[str]]) -> Optional[list[str]]:
    """Map namespace display names to ``docs_kb:<lower>`` repo keys.

    Mirrors ``docs_store.activate_doc``. ``None`` / empty means "no explicit
    filter" — the caller resolves it to the full corpus via ``_all_docs_kb_repos``.
    """
    if not namespaces:
        return None
    repos = [f"docs_kb:{str(n).strip().lower()}" for n in namespaces if str(n).strip()]
    return repos or None


async def _all_docs_kb_repos() -> list[str]:
    """Return every ``docs_kb:*`` repo by delegating to the platform enumerator.

    Uses ``store.docs_store.list_namespaces`` — the same Redis (``docs:namespaces``)
    → DB fallback source the chat ``Knowledge`` toggle uses — so Build Studio sees
    exactly the corpus the rest of the platform sees. Namespaces are returned
    WITHOUT the ``docs_kb:`` prefix, so we prepend it to form repo keys.

    Kept as a public coroutine because ``app/swarm/capability_manifest.py``
    enumerates the KB catalog through it.
    """
    try:
        from store.docs_store import list_namespaces  # type: ignore
        names = await asyncio.to_thread(list_namespaces)
    except Exception as exc:
        logger.warning(f'[AGENT] kb_retriever: docs_kb namespace enumeration failed: {exc}')
        return []
    return [f"docs_kb:{str(n).strip().lower()}" for n in (names or []) if str(n).strip()]


async def _resolve_doc_file_paths(doc_ids: list[str]) -> list[str]:
    """Resolve ``KnowledgeDocument.id`` values to their stored ``file_path``s.

    ``store/docs_store.activate_doc`` stamps every chunk with
    ``file_path = <safe_filename>`` and ``metadata.doc_id = <doc id>``. One
    document maps to exactly one ``file_path`` (the ABS-prefixed safe filename),
    so we read the distinct pairs straight from ``document_embeddings``. The
    returned paths are the raw stored values (ABS prefix intact) — they must be
    passed verbatim to ``file_filter`` (``AND file_path = ANY(:file_filter)``).
    """
    ids = [str(d) for d in doc_ids if d]
    if not ids:
        return []

    def _query() -> list[str]:
        from db.database import VectorReadSessionLocal  # type: ignore
        from sqlalchemy import text as _sql
        db = VectorReadSessionLocal()
        try:
            rows = db.execute(
                _sql(
                    "SELECT DISTINCT file_path FROM document_embeddings "
                    "WHERE metadata->>'doc_id' = ANY(:ids) AND file_path IS NOT NULL"
                ),
                {"ids": ids},
            ).fetchall()
            return [r[0] for r in rows if r and r[0]]
        finally:
            db.close()

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warning(f'[AGENT] kb_retriever: doc_id→file_path resolution failed (doc_ids={len(ids)}): {exc}')
        return []


async def _resolve_scope_doc_ids(scope_filters: list[dict]) -> list[str]:
    """Resolve graph-model scope entries to the set of ACTIVE ``KnowledgeDocument.id``s
    they cover.

    Each ``scope_filter`` is a dict written by Agent Studio's ScopePicker:
    ``{product_id, domain?, spec_version?, source_type?, kb_doc_id?}``.
    Semantics:

      * ``kb_doc_id`` short-circuits — the scope pins that one doc regardless
        of the other keys.
      * Otherwise every present key becomes a WHERE clause. Absent keys are
        wildcards (e.g. no ``spec_version`` → every version under the
        product+domain).

    Returned ids are unioned across scopes and deduped. When no filter is
    valid the return is empty and the caller falls back to the namespace
    path — which for legacy blobs is still the correct behaviour.
    """
    scopes = [s for s in (scope_filters or []) if isinstance(s, dict) and s.get("product_id")]
    if not scopes:
        return []

    # Split off pinned doc ids — they don't need a DB query.
    pinned: list[str] = []
    to_query: list[dict] = []
    for s in scopes:
        if s.get("kb_doc_id"):
            pinned.append(str(s["kb_doc_id"]))
        else:
            to_query.append(s)

    if not to_query:
        return list({d for d in pinned if d})

    def _query() -> list[str]:
        # Same hot-standby read session used by _resolve_namespace_scope so
        # scope resolution stays off the primary.
        from db.database import VectorReadSessionLocal  # type: ignore
        from sqlalchemy import text as _sql
        db = VectorReadSessionLocal()
        try:
            ids: set[str] = set()
            # One query per scope keeps the SQL simple (no OR-of-ANDs
            # gymnastics) and each scope is bounded to a single product.
            # Cost is negligible: agents rarely attach more than a handful.
            for s in to_query:
                params: dict[str, Any] = {"product_id": s["product_id"]}
                where = [
                    "status = 'ACTIVE'",
                    "product_id::text = :product_id",
                ]
                if s.get("domain"):
                    where.append("domain = :domain")
                    params["domain"] = s["domain"]
                if s.get("spec_version"):
                    where.append("spec_version = :spec_version")
                    params["spec_version"] = s["spec_version"]
                if s.get("source_type"):
                    where.append("source_type = :source_type")
                    params["source_type"] = s["source_type"]
                # Bounded result set: a single (product[, domain, version])
                # scope never legitimately maps to thousands of docs. The cap
                # is a safety valve against a crafted broad scope forcing an
                # unbounded fetch — it does not affect real usage.
                params["_scope_limit"] = _SCOPE_RESOLVE_LIMIT
                rows = db.execute(
                    _sql(
                        "SELECT id::text FROM knowledge_docs WHERE "
                        + " AND ".join(where)
                        + " LIMIT :_scope_limit"
                    ),
                    params,
                ).fetchall()
                for r in rows:
                    if r and r[0]:
                        ids.add(r[0])
            return list(ids)
        finally:
            db.close()

    try:
        resolved = await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warning(
            f'[AGENT] kb_retriever: scope→doc_ids resolution failed '
            f'(scopes={len(to_query)}): {exc}'
        )
        resolved = []

    merged = {d for d in pinned if d}
    merged.update(resolved)
    logger.info(
        f'[AGENT] kb_retriever: resolved scopes={len(scopes)} '
        f'(pinned={len(pinned)}, queried={len(to_query)}) → doc_ids={len(merged)}'
    )
    return list(merged)


async def _resolve_namespace_scope(
    namespaces: Optional[list[str]],
) -> Optional[dict[str, str]]:
    """Derive the platform KB scope ({product_id, domain, spec_version}) for an
    attached namespace so agent chats trigger the same Coverage / full-file tier
    as the KB chat's ``/ask`` path.

    Why this exists
    ---------------
    ``/ask`` builds ``user_ctx["scope_filter"]`` from the chat's product/domain/
    version (``gateway.py`` scope injection). The platform Coverage tier only
    fires when that scope carries ``product_id AND spec_version`` (``_has_kb_scope``
    in ``models/hybrid_retriever.py``) — otherwise retrieval stays on the Fast
    (top-k chunk) tier. ABStudio agents only store a *namespace*, so without this
    derivation they never carried scope and always fell back to Fast-only,
    producing worse answers than the KB chat for whole-doc / structured queries.

    We look up the ACTIVE docs in the attached namespace(s) and:
      * set ``product_id`` only when the namespace maps to exactly ONE product
        (ambiguous / mixed-product namespaces return ``None`` → Fast tier, the
        same as an unscoped ``/ask`` chat — no over-fetch, no cross-product mix).
      * set ``domain`` / ``spec_version`` only when they are UNIFORM across the
        scoped rows (a single distinct non-null value). This mirrors an ``/ask``
        chat scoped to product (+ optionally domain/version).

    Read-only SELECT against the ``knowledge_docs`` table on the hot-standby.
    Returns ``None`` on any error, empty namespace list, or ambiguous product —
    the caller then omits ``scope_filter`` entirely (unchanged Fast behaviour).
    """
    names = [str(n).strip() for n in (namespaces or []) if str(n).strip()]
    if not names:
        return None

    def _query() -> list[tuple[Optional[str], Optional[str], Optional[str]]]:
        # Hot-standby read session (SELECT-only) — same DB the /ask namespace
        # lookup uses (gateway.py uses SessionLocal; ReadSessionLocal is the
        # read-replica equivalent for SELECTs).
        from db.database import ReadSessionLocal  # type: ignore
        from sqlalchemy import text as _sql
        db = ReadSessionLocal()
        try:
            rows = db.execute(
                _sql(
                    "SELECT DISTINCT product_id::text, domain, spec_version "
                    "FROM knowledge_docs "
                    "WHERE namespace = ANY(:ns) AND status = 'ACTIVE'"
                ),
                {"ns": names},
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]
        finally:
            db.close()

    try:
        rows = await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warning(
            f'[AGENT] kb_retriever: namespace scope lookup failed '
            f'(namespaces={names}): {exc}'
        )
        return None

    if not rows:
        return None

    product_ids = {r[0] for r in rows if r[0]}
    if len(product_ids) != 1:
        # Zero → docs have no product scope (org-wide KB); >1 → mixed products.
        # Either way we cannot form a deterministic scope; stay Fast-tier.
        logger.info(
            f'[AGENT] kb_retriever: namespace scope ambiguous '
            f'(namespaces={names}, distinct_product_ids={len(product_ids)}) — '
            f'no scope_filter, Fast tier only'
        )
        return None

    scope: dict[str, str] = {"product_id": next(iter(product_ids))}

    domains = {r[1] for r in rows if r[1]}
    if len(domains) == 1:
        scope["domain"] = next(iter(domains))

    versions = {r[2] for r in rows if r[2]}
    if len(versions) == 1:
        scope["spec_version"] = next(iter(versions))

    logger.info(
        f'[AGENT] kb_retriever: derived namespace scope {scope} '
        f'(namespaces={names}) — Coverage tier eligible='
        f'{bool(scope.get("product_id") and scope.get("spec_version"))}'
    )
    return scope


async def _resolve_scope(
    namespaces: Optional[list[str]],
    doc_ids: Optional[list[str]],
    owner_email: Optional[str],
    owner_dept: Optional[str],
    is_admin: bool,
) -> Optional[tuple[list[str], Optional[list[str]], dict[str, Any]]]:
    """Resolve the repo scope, doc file_filter and user_ctx for a retrieval.

    Returns ``(repos, file_filter, user_ctx)`` ready to pass to
    ``hybrid_retrieve_context``, or ``None`` when retrieval must be skipped
    (no repos available, or explicit doc_ids that resolve to zero indexed
    files). Shared by :func:`retrieve` and :func:`retrieve_with_meta` so the
    ACL / scoping logic lives in exactly one place.
    """
    # An explicit namespace selection wins; otherwise search every docs_kb:*
    # repo (matching the chat Knowledge toggle).
    repos: Optional[list[str]] = _namespace_repos(namespaces)
    if not repos:
        repos = await _all_docs_kb_repos()
        if not repos:
            logger.info('[AGENT] kb_retriever: no docs_kb:* repos available')
            return None

    # Strict per-workflow scoping for KB_MODE_ADD — resolve the attached
    # documents to their stored file_path values and pass as a hard file_filter.
    #
    # SECURITY (scope-bypass defence): ``doc_ids`` is a UNION of user-supplied
    # ids (selected_doc_ids / uploaded_doc_ids) and scope-resolved ids. It is
    # NOT the access decision. ``file_filter`` can only ever NARROW the result
    # set; the authoritative access gate is the department ACL carried in
    # ``user_ctx`` below, which ``hybrid_retrieve_context`` enforces as a SQL
    # WHERE clause (PUBLIC docs, plus PRIVATE docs where department = owner_dept,
    # unless is_admin). So even a crafted ``selected_doc_ids`` pointing at a doc
    # outside the caller's department is filtered out at retrieval time — the
    # union can never widen access beyond what the ACL already permits.
    file_filter: Optional[list[str]] = None
    if doc_ids:
        file_filter = await _resolve_doc_file_paths(doc_ids)
        if not file_filter:
            # The user explicitly named documents but none resolved to indexed
            # chunks — return nothing rather than silently widening to the whole
            # namespace corpus.
            logger.info(f'[AGENT] kb_retriever: KB_MODE_ADD doc_ids={len(doc_ids)} resolved to 0 file paths — no retrieval')
            return None

    # user_ctx drives the department ACL (SQL WHERE) + classification/allowed_roles
    # post-filter inside pgvector_search / keyword_search, and the per-user cache
    # scoping in hybrid_retrieve_context. Identical shape to gateway chat RAG.
    user_ctx = {
        "user_id":    owner_email or "",
        "user_role":  "admin" if is_admin else "viewer",
        "is_admin":   bool(is_admin),
        "department": owner_dept or "",
        "org_id":     "",
        "session_id": "",
    }

    # Namespace-derived KB scope → parity with the /ask KB chat path.
    # hybrid_retrieve_context reads user_ctx["scope_filter"] to decide whether
    # the Coverage / full-file tier fires (needs product_id + spec_version).
    # Skip when doc_ids hard-scoping is in effect (KB_MODE_ADD, the agent's own
    # uploaded docs): those are file_filter-restricted and are NOT platform
    # product-scoped docs, so a derived product scope would be meaningless there.
    if not doc_ids:
        scope_filter = await _resolve_namespace_scope(namespaces)
        if scope_filter:
            user_ctx["scope_filter"] = scope_filter

    return repos, file_filter, user_ctx


async def retrieve(
    query: str,
    owner_email: Optional[str] = None,
    owner_dept: Optional[str] = None,
    namespaces: Optional[list[str]] = None,
    top_k: int = _DEFAULT_TOP_K,
    is_admin: bool = False,
    doc_ids: Optional[list[str]] = None,
    full_file_doc_ids: Optional[list[str]] = None,
) -> str:
    """
    Retrieve top-k relevant chunks from the platform pgvector KB by delegating
    to ``models.hybrid_retriever.hybrid_retrieve_context`` — the platform's
    single hybrid pipeline (pgvector + BM25 + BGE rerank + relevance gate +
    coverage + cache). No orchestration is done here.

    Parameters
    ----------
    query
        The user's latest message — used as the retrieval query.
    owner_email
        Invoking user's identity. Threaded into ``user_ctx`` for the ACL and
        per-user cache scoping.
    owner_dept
        Invoking user's department. PUBLIC docs (``department IS NULL`` / ``''``)
        are always visible. PRIVATE docs are visible only when
        ``department = :owner_dept``. When None, only PUBLIC docs return.
    namespaces
        Optional list of namespace display names (e.g. ["HR Policy"]).
        Empty / None means "all visible namespaces" — enumerated via
        ``_all_docs_kb_repos()`` so retrieval matches the chat ``Knowledge``
        toggle's cross-repo behaviour.
    top_k
        Maximum number of chunks to return (``max_chunks``). Defaults to 5.
    is_admin
        When True the platform pipeline bypasses the department ACL and all
        PRIVATE docs become visible — matches gateway chat semantics.
    doc_ids
        Optional allow-list of ``KnowledgeDocument.id`` values. When given, the
        docs are resolved to their ``file_path``s and passed as ``file_filter``
        so retrieval is hard-scoped to exactly those documents at SQL time. This
        is how Build Studio's ``KB_MODE_ADD`` enforces strict per-workflow scoping.
    full_file_doc_ids
        Optional list of ``KnowledgeDocument.id`` values for KBs that contain
        exactly one document. When provided, each doc is retrieved in
        ``full_file`` mode — the platform's Coverage tier reads the entire
        document verbatim instead of doing top-k chunk retrieval. This works by
        injecting ``kb_doc_id`` into ``user_ctx``, which
        ``hybrid_retrieve_context`` checks to force ``_RS_effective="full_file"``.
        Multi-doc KBs are unaffected and always use standard RAG.

    Returns the formatted chunk-text block, or ``""`` when retrieval is skipped
    due to a missing dependency, an empty query, or no matches.
    """
    query = (query or "").strip()
    if not query:
        return ""

    try:
        from models.hybrid_retriever import hybrid_retrieve_context  # type: ignore
    except Exception as exc:
        logger.error(f'[AGENT] kb_retriever: hybrid_retrieve_context IMPORT FAILED — KB retrieval disabled for this request. Ensure models.hybrid_retriever is on sys.path. Error: {exc}')
        return ""

    scope = await _resolve_scope(namespaces, doc_ids, owner_email, owner_dept, is_admin)
    if scope is None:
        return ""
    repos, file_filter, user_ctx = scope

    # Normalise full_file_doc_ids — doc IDs from single-doc KBs that should be
    # read in their entirety via the Coverage tier (full_file mode).
    ff_ids: list[str] = []
    if full_file_doc_ids:
        ff_ids = [str(d) for d in full_file_doc_ids if d]

    logger.info(f'[AGENT] KB_DEBUG retrieve: repos={repos} query={query[:120]!r} owner_dept={owner_dept!r} is_admin={is_admin!r} top_k={top_k} doc_ids={(len(doc_ids) if doc_ids else None)} file_filter={(len(file_filter) if file_filter else None)} full_file_doc_ids={len(ff_ids)}')

    # ── Retrieval dispatch ──────────────────────────────────────────────────
    # Single-doc KBs (in ff_ids) → full_file mode: directly call the platform
    #   Coverage tier via kb_doc_cache.get_or_warm() + coverage_retriever.run_coverage()
    #   with retrieval_scope="full_file" (force_include_all=True). This is the
    #   same direct path that kb chat's gateway.py fast-path uses — every section
    #   of the document is returned verbatim, no top-k chunk filtering.
    # Multi-doc KBs → standard RAG: top-k chunk retrieval via the Fast tier.
    # Both can happen in the same call when the user selected mixed KBs.
    #
    # Keyword args (not positional) so a future reordering of the platform
    # signature can never silently mis-bind our arguments. asyncio.to_thread
    # forwards **kwargs to the target, so no wrapper closure is needed.
    all_chunks: list[str] = []

    # (a) Full-file retrieval — direct Coverage tier, one call per doc.
    #     Mirrors gateway.py lines 5491–5550 (the kb chat fast-path).
    if ff_ids:
        _cov_available = False
        try:
            from store import kb_doc_cache as _kb_cache          # type: ignore
            from models.coverage_retriever import run_coverage   # type: ignore
            _cov_available = True
        except Exception as exc:
            logger.error(
                f'[AGENT] kb_retriever: Coverage tier import FAILED — '
                f'full_file retrieval disabled for this request. Error: {exc}'
            )

        if _cov_available:
            # Derive the cache key scope (product_id, spec_version) from the
            # attached namespace — same lookup _resolve_namespace_scope() does
            # for the Coverage tier eligibility check. Needed so get_or_warm()
            # can find the correct Redis key for the doc.
            _ns_scope = await _resolve_namespace_scope(namespaces) or {}
            _product_id = _ns_scope.get("product_id")
            _spec_version = _ns_scope.get("spec_version")

            for ff_doc_id in ff_ids:
                try:
                    _payload = _kb_cache.get_or_warm(
                        _product_id, _spec_version, ff_doc_id
                    )
                    if not _payload:
                        # Force re-warm on cache miss — mirrors gateway.py lines
                        # 5504–5521 so a first-request-after-approval doc is never
                        # silently skipped.
                        logger.warning(
                            f'[AGENT] kb_retriever: cache miss doc_id={ff_doc_id} '
                            f'— attempting force re-warm'
                        )
                        try:
                            _payload = _kb_cache.warm(ff_doc_id)
                        except Exception as _rewarm_err:
                            logger.error(
                                f'[AGENT] kb_retriever: re-warm failed '
                                f'doc_id={ff_doc_id}: {_rewarm_err}'
                            )
                    if not _payload:
                        logger.error(
                            f'[AGENT] kb_retriever: skipping doc_id={ff_doc_id} '
                            f'— payload unavailable after re-warm'
                        )
                        continue

                    # run_coverage with retrieval_scope="full_file" sets
                    # force_include_all=True → every section returned verbatim,
                    # no local relevance filter. fast_hits=[] because ABStudio
                    # agents do not run a pgvector fast-path probe before Coverage
                    # (the doc is read fully regardless of query terms).
                    _cov = run_coverage(
                        query, _payload, [], retrieval_scope="full_file"
                    )
                    _cov_texts = [
                        e.get("text", "")
                        for e in (_cov.evidence or [])
                        if e.get("text")
                    ]
                    if _cov_texts:
                        # Per-doc labeled block — mirrors gateway.py lines 5541–5550
                        # so the LLM can cite the correct source document.
                        _doc_label = (
                            _payload.get("doc_name")
                            or _payload.get("name")
                            or ff_doc_id
                        )
                        _labeled_block = (
                            f"=== Document: {_doc_label} ===\n\n"
                            + "\n\n".join(_cov_texts)
                        )
                        all_chunks.append(_labeled_block)
                        logger.info(
                            f'[AGENT] kb_retriever: full_file doc_id={ff_doc_id} '
                            f'sections={_cov.sections_included}/'
                            f'{_cov.sections_examined} badge={_cov.badge!r}'
                        )
                    else:
                        logger.warning(
                            f'[AGENT] kb_retriever: full_file doc_id={ff_doc_id} '
                            f'coverage returned no evidence (mode={_cov.mode})'
                        )
                except Exception as exc:
                    logger.error(
                        f'[AGENT] kb_retriever: full_file Coverage FAILED '
                        f'doc_id={ff_doc_id}: {exc}'
                    )
                    continue

    # (b) Standard RAG retrieval for multi-doc KBs. We always run RAG when
    # there are repos/file_filter — the full_file results above are separate
    # and concatenated. Skip RAG only when every selected namespace is a
    # single-doc KB already covered by ff_ids (avoids double-injecting the
    # same content).
    has_rag_scope = bool(repos) or bool(file_filter)
    run_rag = True
    if ff_ids and not doc_ids and namespaces and len(namespaces) == len(ff_ids):
        run_rag = False

    if run_rag and has_rag_scope:
        try:
            chunks = await asyncio.to_thread(
                hybrid_retrieve_context,
                question=query,
                repo_filter=repos,           # list of docs_kb:* keys
                user_ctx=user_ctx,
                max_chunks=top_k,
                file_filter=file_filter,     # hard doc allow-list, or None
            )
        except Exception as exc:
            logger.error(f'[AGENT] kb_retriever: hybrid_retrieve_context FAILED (repos={repos}, query={query[:80]!r}): {exc}')
            chunks = []
        # hybrid_retrieve_context returns a list[str] — each chunk already
        # formatted as "[Source: <file_path>]\n<text>" (we never set
        # return_confidence, so it never returns the (context, confidence) form).
        if chunks:
            all_chunks.extend(
                c for c in chunks if isinstance(c, str) and c.strip()
            )

    if not all_chunks:
        logger.info(f'[AGENT] kb_retriever: no matches (owner_dept={owner_dept}, is_admin={is_admin}, namespaces={namespaces}, repos={repos}, top_k={top_k}, doc_ids={len(doc_ids or [])}, full_file_doc_ids={len(ff_ids)})')
        return ""

    context = "\n\n".join(all_chunks)
    if not context:
        return ""

    # ── FR-T0-2 (PI3): scan retrieved KB content for prompt-injection ────
    # Retrieved chunks are UNTRUSTED data that flows straight into the model
    # prompt. Neutralize any injected instructions (sanitize policy by
    # default) before they reach the LLM. Fails OPEN on detector error.
    try:
        import os
        from core.prompt_injection import scan  # type: ignore

        result = await asyncio.to_thread(scan, context, "kb_chunk")
        if result.get("is_suspicious"):
            policy = os.getenv("ABS_INJECTION_POLICY_KB", "sanitize")
            logger.warning(
                f'[INJECTION] kb_retriever: suspicious KB content '
                f'score={result.get("score")} categories={result.get("categories")} policy={policy}'
            )
            if policy == "sanitize":
                context = result.get("sanitized_text") or context
    except Exception as exc:  # fail open — never break retrieval on a gate bug
        logger.warning(f'[INJECTION] kb_retriever: scan failed: {exc}')

    logger.info(f'[AGENT] kb_retriever: returned {len(all_chunks)} chunk(s), {len(context)} chars (owner_dept={owner_dept}, is_admin={is_admin}, namespaces={namespaces}, full_file_doc_ids={len(ff_ids)})')
    return context


# Regex to split a platform chunk string ("[Source: <file_path>]\n<text>")
# into its citation source and body. hybrid_retrieve_context emits every
# chunk in this exact shape (see models/hybrid_retriever.py — the
# ``f"[Source: {fp}]\n{text}"`` formatter). Coverage / lineage evidence use
# ``[Coverage source: …]`` / ``[Lineage source: …]`` headers instead, which
# this pattern also captures via the optional qualifier group.
_CHUNK_SOURCE_RE = re.compile(
    r"^\[(?:(Coverage|Lineage) )?[Ss]ource:\s*(?P<src>.*?)\]\n?(?P<body>.*)$",
    re.DOTALL,
)


def _parse_chunks(chunks: list[str]) -> list[dict[str, Any]]:
    """Split platform chunk strings into structured {source, text} dicts.

    The platform retriever returns each chunk as ``"[Source: <fp>]\\n<text>"``
    (or a Coverage/Lineage variant). Per-chunk similarity scores are computed
    inside ``hybrid_retrieve_context`` but are NOT carried in the returned
    strings, so ``score`` is left ``None`` here — the Debug Log renders it as
    "score: n/a (not exposed by retriever)" while still showing the full chunk
    text and its source. The run-level ``confidence`` (surfaced separately)
    is the retriever's only externally-visible relevance signal.
    """
    parsed: list[dict[str, Any]] = []
    for idx, raw in enumerate(chunks):
        if not isinstance(raw, str) or not raw.strip():
            continue
        m = _CHUNK_SOURCE_RE.match(raw.strip())
        if m:
            src = _display_name(m.group("src") or "") or (m.group("src") or "")
            body = (m.group("body") or "").strip()
        else:
            src, body = "", raw.strip()
        parsed.append({
            "index":     idx,
            "source":    src,
            "text":      body,          # FULL chunk text — never truncated.
            "score":     None,          # not exposed per-chunk by the platform.
            "qualified": True,          # every returned chunk passed the gate.
        })
    return parsed


async def retrieve_with_meta(
    query: str,
    owner_email: Optional[str] = None,
    owner_dept: Optional[str] = None,
    namespaces: Optional[list[str]] = None,
    top_k: int = _DEFAULT_TOP_K,
    is_admin: bool = False,
    doc_ids: Optional[list[str]] = None,
    full_file_doc_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Same retrieval as :func:`retrieve` but returns structured metadata.

    Returns a dict::

        {
          "context":        "<joined chunk text>",   # identical to retrieve()
          "chunks":         [{index, source, text, score, qualified}, ...],
          "confidence":     <float | None>,           # run-level relevance signal
          "chunk_count":    <int>,
          "coverage_trace": <dict | None>,            # populated for full_file docs
        }

    ``coverage_trace`` mirrors the shape that ``KbChat.jsx`` reads from the
    gateway's ``coverage_trace`` field — same keys, same semantics::

        {
          "mode":               "doc_fits" | "map_reduce",
          "retrieval_scope":    "full_file",
          "badge":              "Read all N/N sections (doc-fits, full_file mode)",
          "sections_examined":  int,
          "sections_included":  int,
          "escalate":           False,
          "sufficiency":        1.0,
          "reason":             "full_file mode — entire document read verbatim",
        }

    This powers the Debug Log's ``kb_retrieval`` event and the coverage badge
    in the ABStudio agent chat UI. Never raises — on any failure it returns an
    empty result so the agent run is unaffected.
    """
    empty: dict[str, Any] = {
        "context": "", "chunks": [], "confidence": None,
        "chunk_count": 0, "coverage_trace": None,
    }
    query = (query or "").strip()
    if not query:
        return empty

    try:
        from models.hybrid_retriever import hybrid_retrieve_context  # type: ignore
    except Exception as exc:
        logger.error(f'[AGENT] kb_retriever: hybrid_retrieve_context IMPORT FAILED — KB retrieval disabled for this request. Ensure models.hybrid_retriever is on sys.path. Error: {exc}')
        return empty

    scope = await _resolve_scope(namespaces, doc_ids, owner_email, owner_dept, is_admin)
    if scope is None:
        return empty
    repos, file_filter, user_ctx = scope

    # Normalise full_file_doc_ids — same as retrieve().
    ff_ids: list[str] = []
    if full_file_doc_ids:
        ff_ids = [str(d) for d in full_file_doc_ids if d]

    logger.info(
        f'[AGENT] KB_DEBUG retrieve_with_meta: repos={repos} query={query[:120]!r} '
        f'owner_dept={owner_dept!r} is_admin={is_admin!r} top_k={top_k} '
        f'doc_ids={(len(doc_ids) if doc_ids else None)} '
        f'file_filter={(len(file_filter) if file_filter else None)} '
        f'full_file_doc_ids={len(ff_ids)}'
    )

    all_context_parts: list[str] = []
    coverage_trace: Optional[dict[str, Any]] = None

    # (a) Full-file retrieval — direct Coverage tier, same as retrieve().
    #     Mirrors gateway.py fast-path; reuses kb_doc_cache + coverage_retriever.
    if ff_ids:
        _cov_available = False
        try:
            from store import kb_doc_cache as _kb_cache          # type: ignore
            from models.coverage_retriever import run_coverage   # type: ignore
            _cov_available = True
        except Exception as exc:
            logger.error(
                f'[AGENT] kb_retriever: Coverage tier import FAILED in '
                f'retrieve_with_meta: {exc}'
            )

        if _cov_available:
            _ns_scope = await _resolve_namespace_scope(namespaces) or {}
            _product_id = _ns_scope.get("product_id")
            _spec_version = _ns_scope.get("spec_version")
            _last_cov = None   # track last CoverageResult for coverage_trace

            for ff_doc_id in ff_ids:
                try:
                    _payload = _kb_cache.get_or_warm(
                        _product_id, _spec_version, ff_doc_id
                    )
                    if not _payload:
                        logger.warning(
                            f'[AGENT] kb_retriever: cache miss doc_id={ff_doc_id} '
                            f'(retrieve_with_meta) — attempting force re-warm'
                        )
                        try:
                            _payload = _kb_cache.warm(ff_doc_id)
                        except Exception as _rewarm_err:
                            logger.error(
                                f'[AGENT] kb_retriever: re-warm failed '
                                f'doc_id={ff_doc_id}: {_rewarm_err}'
                            )
                    if not _payload:
                        logger.error(
                            f'[AGENT] kb_retriever: skipping doc_id={ff_doc_id} '
                            f'(retrieve_with_meta) — payload unavailable'
                        )
                        continue

                    _cov = run_coverage(
                        query, _payload, [], retrieval_scope="full_file"
                    )
                    _last_cov = _cov
                    _cov_texts = [
                        e.get("text", "")
                        for e in (_cov.evidence or [])
                        if e.get("text")
                    ]
                    if _cov_texts:
                        _doc_label = (
                            _payload.get("doc_name")
                            or _payload.get("name")
                            or ff_doc_id
                        )
                        _labeled_block = (
                            f"=== Document: {_doc_label} ===\n\n"
                            + "\n\n".join(_cov_texts)
                        )
                        all_context_parts.append(_labeled_block)
                        logger.info(
                            f'[AGENT] kb_retriever: full_file (with_meta) '
                            f'doc_id={ff_doc_id} '
                            f'sections={_cov.sections_included}/'
                            f'{_cov.sections_examined} badge={_cov.badge!r}'
                        )
                except Exception as exc:
                    logger.error(
                        f'[AGENT] kb_retriever: full_file Coverage FAILED '
                        f'(retrieve_with_meta) doc_id={ff_doc_id}: {exc}'
                    )
                    continue

            # Build coverage_trace from the last CoverageResult — same shape
            # as KbChat.jsx reads from gateway's coverage_trace field.
            if _last_cov is not None:
                coverage_trace = {
                    "mode":               _last_cov.mode,
                    "retrieval_scope":    "full_file",
                    "badge":              f"Full-file: {_last_cov.badge}",
                    "sections_examined":  _last_cov.sections_examined,
                    "sections_included":  _last_cov.sections_included,
                    "escalate":           False,
                    "sufficiency":        1.0,
                    "reason":             "full_file mode — entire document read verbatim",
                }

    # (b) Standard RAG retrieval for multi-doc KBs.
    #     Skip when every selected namespace is a single-doc KB (ff_ids covers all).
    has_rag_scope = bool(repos) or bool(file_filter)
    run_rag = True
    if ff_ids and not doc_ids and namespaces and len(namespaces) == len(ff_ids):
        run_rag = False

    confidence: Optional[float] = None
    parsed: list[dict[str, Any]] = []

    if run_rag and has_rag_scope:
        # return_confidence=True surfaces the retriever's run-level relevance
        # score (derived from the top BGE rerank score).
        try:
            result = await asyncio.to_thread(
                hybrid_retrieve_context,
                question=query,
                repo_filter=repos,
                user_ctx=user_ctx,
                max_chunks=top_k,
                file_filter=file_filter,
                return_confidence=True,
            )
        except Exception as exc:
            logger.error(
                f'[AGENT] kb_retriever: hybrid_retrieve_context FAILED '
                f'(repos={repos}, query={query[:80]!r}): {exc}'
            )
            result = []

        # With return_confidence=True the platform returns (chunks, confidence).
        # Be defensive — older builds may still return a bare list.
        if isinstance(result, tuple) and len(result) == 2:
            chunks, confidence = result[0], result[1]
        else:
            chunks = result

        if chunks:
            parsed = _parse_chunks(
                [c for c in chunks if isinstance(c, str) and c.strip()]
            )
            rag_context = "\n\n".join(
                (f"[Source: {c['source']}]\n{c['text']}" if c["source"] else c["text"])
                for c in parsed if c["text"]
            )
            if rag_context:
                all_context_parts.append(rag_context)

    context = "\n\n".join(all_context_parts)
    if not context:
        logger.info(
            f'[AGENT] kb_retriever: no matches (retrieve_with_meta) '
            f'owner_dept={owner_dept}, is_admin={is_admin}, '
            f'namespaces={namespaces}, repos={repos}, top_k={top_k}'
        )
        return {**empty, "confidence": confidence}

    logger.info(
        f'[AGENT] kb_retriever: retrieve_with_meta returned '
        f'{len(parsed)} RAG chunk(s) + {len(ff_ids)} full_file doc(s), '
        f'confidence={confidence} (owner_dept={owner_dept}, is_admin={is_admin})'
    )
    return {
        "context":        context,
        "chunks":         parsed,
        "confidence":     confidence,
        "chunk_count":    len(parsed),
        "coverage_trace": coverage_trace,
    }


async def build_context_section(
    query: str,
    knowledge: Any,
    owner_dept: Optional[str] = None,
    owner_email: str = "",
    top_k: int = _DEFAULT_TOP_K,
    is_admin: bool = False,
) -> str:
    """
    One-shot helper for system-prompt assembly. Inspects ``knowledge`` (the
    agent's ``knowledge`` JSONB blob); when the mode selects retrieval and
    ``query`` is non-empty, runs ``retrieve()`` and returns the fully-formatted
    ``## Reference Context`` block ready to concatenate into the system prompt.
    Returns ``""`` when retrieval is disabled, the query is empty, or no chunks
    matched. Retrieval errors are swallowed and logged — they must never break
    the agent run.

    ``is_admin`` is forwarded into the ACL so admin-invoked runs see PRIVATE docs
    across all departments, matching the chat path.
    """
    if not query:
        return ""
    cfg = _parse_knowledge_config(knowledge)
    if cfg is None:
        return ""
    mode, namespaces, doc_ids, scope_filters = cfg

    # Full-file mode for KB_MODE_EXISTING: when a selected KB (namespace)
    # contains exactly one ACTIVE document, the frontend adds its doc_id to
    # ``full_file_doc_ids``. At runtime we inject ``kb_doc_id`` into user_ctx
    # for each, forcing the Coverage tier to read the entire document verbatim.
    full_file_doc_ids: Optional[list[str]] = None
    if mode == KB_MODE_EXISTING:
        raw_ff_ids = knowledge.get("full_file_doc_ids") or []
        if isinstance(raw_ff_ids, list):
            full_file_doc_ids = [str(d) for d in raw_ff_ids if d]

    # Graph-model scopes → union into doc_ids so the existing file_filter
    # path scopes retrieval exactly to the resolved documents. When both
    # legacy doc_ids and scope-resolved ids are present, the union covers
    # both (a saved agent that pinned specific docs stays pinned even if
    # the user later adds a broader scope). Legacy namespace-only blobs
    # keep the pre-scope behaviour because scope_filters is empty.
    if scope_filters:
        resolved_ids = await _resolve_scope_doc_ids(scope_filters)
        if resolved_ids:
            merged = set(doc_ids or [])
            merged.update(resolved_ids)
            doc_ids = list(merged)

    try:
        chunks = await retrieve(
            query=query,
            owner_email=owner_email,
            owner_dept=owner_dept,
            namespaces=namespaces,
            top_k=top_k,
            is_admin=is_admin,
            doc_ids=doc_ids,
            full_file_doc_ids=full_file_doc_ids,
        )
    except Exception as exc:
        logger.error(f'[AGENT] kb_retriever.build_context_section: retrieve() FAILED — mode={mode} namespaces={namespaces} doc_ids={doc_ids} error: {exc}')
        return ""

    if not chunks:
        return ""
    return _CONTEXT_HEADING + chunks


async def build_context_section_with_meta(
    query: str,
    knowledge: Any,
    owner_dept: Optional[str] = None,
    owner_email: str = "",
    top_k: int = _DEFAULT_TOP_K,
    is_admin: bool = False,
) -> dict[str, Any]:
    """Like :func:`build_context_section` but also returns retrieval metadata.

    Returns::

        {
          "section":        "<## Reference Context …>" | "",   # prompt injection
          "mode":           "<knowledge mode>",
          "chunks":         [{index, source, text, score, qualified}, ...],
          "confidence":     <float | None>,
          "chunk_count":    <int>,
          "query":          "<retrieval query>",
          "coverage_trace": <dict | None>,   # populated when full_file docs used
        }

    ``coverage_trace`` carries the same keys as ``KbChat.jsx`` reads from the
    gateway's ``coverage_trace`` field — ``mode``, ``retrieval_scope``, ``badge``,
    ``sections_examined``, ``sections_included``, ``escalate``, ``sufficiency``,
    ``reason``. The ABStudio agent chat UI renders this as a coverage badge on
    the assistant message, identical to the kb chat coverage badge.

    The ``section`` is identical to what ``build_context_section`` produces, so
    callers get the prompt text AND the structured chunk list (for the Debug
    Log ``kb_retrieval`` event) in a single retrieval pass. Never raises.
    """
    base: dict[str, Any] = {
        "section": "", "mode": KB_MODE_NONE, "chunks": [],
        "confidence": None, "chunk_count": 0, "query": query or "",
        "coverage_trace": None,
    }
    if not query:
        return base
    cfg = _parse_knowledge_config(knowledge)
    if cfg is None:
        return base
    mode, namespaces, doc_ids, scope_filters = cfg
    base["mode"] = mode

    # Full-file mode for KB_MODE_EXISTING: when a selected KB (namespace)
    # contains exactly one ACTIVE document, the frontend adds its doc_id to
    # ``full_file_doc_ids``. At runtime we pass these to retrieve_with_meta()
    # which uses the direct Coverage tier (kb_doc_cache + run_coverage) —
    # the same path kb chat's gateway.py fast-path uses.
    full_file_doc_ids: Optional[list[str]] = None
    if mode == KB_MODE_EXISTING:
        raw_ff_ids = knowledge.get("full_file_doc_ids") or []
        if isinstance(raw_ff_ids, list):
            full_file_doc_ids = [str(d) for d in raw_ff_ids if d]

    # Graph-model scopes → union into doc_ids so the existing file_filter
    # path scopes retrieval exactly to the resolved documents. See the
    # matching block in build_context_section() for the full rationale.
    if scope_filters:
        resolved_ids = await _resolve_scope_doc_ids(scope_filters)
        if resolved_ids:
            merged = set(doc_ids or [])
            merged.update(resolved_ids)
            doc_ids = list(merged)

    try:
        meta = await retrieve_with_meta(
            query=query,
            owner_email=owner_email,
            owner_dept=owner_dept,
            namespaces=namespaces,
            top_k=top_k,
            is_admin=is_admin,
            doc_ids=doc_ids,
            full_file_doc_ids=full_file_doc_ids,
        )
    except Exception as exc:
        logger.error(
            f'[AGENT] kb_retriever.build_context_section_with_meta: FAILED — '
            f'mode={mode} namespaces={namespaces} doc_ids={doc_ids} error: {exc}'
        )
        return base

    section = (_CONTEXT_HEADING + meta["context"]) if meta.get("context") else ""
    return {
        "section":        section,
        "mode":           mode,
        "chunks":         meta.get("chunks", []),
        "confidence":     meta.get("confidence"),
        "chunk_count":    meta.get("chunk_count", 0),
        "query":          query,
        "coverage_trace": meta.get("coverage_trace"),
    }
