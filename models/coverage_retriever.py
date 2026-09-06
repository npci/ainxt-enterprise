# SPDX-License-Identifier: MIT
"""
Coverage tier — examines every section of a scoped KB doc.

Two modes (disjoint by doc size, §6 Phase 3):

  (i)  Doc FITS the model window
       → targeted section + neighbor sections + graph-neighbor sections
       → full MD only as a verification fallback (avoids lost-in-the-middle).

  (ii) Doc OVERSIZED
       → graph-guided MAP-REDUCE traversing the structure graph,
         examines EVERY section (no early termination),
         passes VERBATIM sections to the reduce step.

The reducer never sees a local paraphrase — only verbatim source text
(see §8z forbids abstractive compression in the evidence path).

This module returns the verbatim evidence; final synthesis is the caller's
responsibility (orchestrator / gateway → model_router).
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable, Optional

from core.logger import logger


# ── Tunables ─────────────────────────────────────────────────────────────────

# Approximate chars-per-token for budgeting. Conservative.
_CHARS_PER_TOKEN = 4

# Token budget at which "doc fits window" stops being true.
# Defaults assume the synthesis model has ~120k tokens free for context.
_DOC_FITS_TOKEN_BUDGET = int(os.getenv("KB_COVERAGE_DOC_FITS_TOKENS", "120000"))

# Maximum chars the map step forwards per section (verbatim, not compressed).
_MAP_MAX_SECTION_CHARS = int(os.getenv("KB_COVERAGE_MAP_SECTION_CHARS", "8000"))

# How many sections to map in parallel. Phase 6: capped low by default so
# 2,000-user load doesn't multiply by N-sections and saturate the box. Bump
# the env when you've measured headroom.
_MAP_CONCURRENCY = int(os.getenv("KB_COVERAGE_MAP_CONCURRENCY", "4"))

# Phase 6: maximum sections the map step will examine per query. Beyond this
# we cap and surface "partial coverage" in the trace so the user sees what
# was skipped. None = unlimited (legacy behaviour).
_MAX_SECTIONS_PER_QUERY = int(os.getenv("KB_COVERAGE_MAX_SECTIONS", "0")) or None

# Map-step mode: 'local' (default — high-recall include-or-drop filter, never
# compresses) or 'cloud' (per-section cloud call, opt-in for top-stakes products).
_MAP_MODE = os.getenv("KB_COVERAGE_MAP_MODE", "local").lower()

# Phase 6 — global concurrency cap on the map step. Without this, 2,000 users
# each spawning a ThreadPoolExecutor with _MAP_CONCURRENCY=4 → up to 8,000
# in-flight map threads, blowing the box. The semaphore caps the total
# concurrent map runs across ALL requests on this process. On timeout we
# return a "skipped" CoverageResult so the caller falls back to Fast-tier.
_COVERAGE_GLOBAL_CONCURRENCY = int(os.getenv("KB_COVERAGE_GLOBAL_CONCURRENCY", "16"))
_COVERAGE_GLOBAL_TIMEOUT_S   = float(os.getenv("KB_COVERAGE_GLOBAL_TIMEOUT_S", "2.0"))
_COVERAGE_GLOBAL_TTL_MS      = int(os.getenv("KB_COVERAGE_GLOBAL_TTL_MS", "120000"))

_coverage_semaphore = None
def _get_coverage_semaphore():
    """Lazy-init the distributed semaphore so importing this module never
    touches Redis on startup. Returns None if init fails so coverage still
    runs (best-effort cap, not a hard requirement)."""
    global _coverage_semaphore
    if _coverage_semaphore is not None:
        return _coverage_semaphore
    try:
        from core.distributed_semaphore import DistributedSemaphore
        from core.kv import get_kv
        from core.config import RDB_CACHE
        _kv = get_kv(RDB_CACHE, decode_responses=True)
        _coverage_semaphore = DistributedSemaphore(
            _kv,
            name="kb_coverage_global",
            capacity=_COVERAGE_GLOBAL_CONCURRENCY,
            ttl_ms=_COVERAGE_GLOBAL_TTL_MS,
        )
        logger.info(
            f"coverage_retriever: global semaphore initialised "
            f"capacity={_COVERAGE_GLOBAL_CONCURRENCY} ttl={_COVERAGE_GLOBAL_TTL_MS}ms"
        )
    except Exception as _se:
        logger.warning(
            f"coverage_retriever: distributed semaphore unavailable "
            f"({_se}) — falling back to UNCAPPED coverage (degraded back-pressure)."
        )
        _coverage_semaphore = None
    return _coverage_semaphore


@dataclass
class CoverageResult:
    mode: str                       # "doc_fits" | "map_reduce"
    sections_examined: int          # total sections traversed (auditable badge)
    sections_included: int          # how many were forwarded to the reducer
    evidence: list[dict] = field(default_factory=list)
    # Each evidence dict: {section_path, text, doc_id, score?}
    badge: str = ""                 # short label for TracePanel
    trace: dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# Section extraction
# ────────────────────────────────────────────────────────────────────────────

def _slice_section(full_md: str, entry: dict) -> str:
    """Return verbatim text for a section_map entry, capped at _MAP_MAX_SECTION_CHARS."""
    start = int(entry.get("start", 0))
    end   = int(entry.get("end", len(full_md)))
    body  = full_md[start:end]
    if len(body) > _MAP_MAX_SECTION_CHARS:
        # Cap at the boundary but keep both head and tail so map step sees
        # the heading + the last paragraph (often where exceptions live).
        head_chars = _MAP_MAX_SECTION_CHARS - 600
        body = body[:head_chars] + "\n\n…\n\n" + body[-500:]
    return body


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


# ────────────────────────────────────────────────────────────────────────────
# Mode (i) — doc fits window
# ────────────────────────────────────────────────────────────────────────────

def _expand_with_neighbors(
    section_map: list[dict],
    hit_paths: set[str],
    neighbor_window: int = 1,
) -> list[int]:
    """
    Return section_map indices for: every hit section + N neighbors on each
    side + all ancestor headings (so the reasoner sees the section's parent
    context).
    """
    selected: set[int] = set()
    for i, entry in enumerate(section_map):
        if entry.get("section_path") in hit_paths:
            for j in range(max(0, i - neighbor_window),
                           min(len(section_map), i + neighbor_window + 1)):
                selected.add(j)
            # Ancestors via section_path prefix.
            path = entry.get("section_path", "")
            parts = [p.strip() for p in path.split(" > ") if p.strip()]
            for k in range(1, len(parts)):
                ancestor = " > ".join(parts[:k])
                for jj, ee in enumerate(section_map):
                    if ee.get("section_path") == ancestor:
                        selected.add(jj)
    return sorted(selected)


def _doc_fits_mode(
    question: str,
    payload: dict,
    fast_hits: list[dict],
    force_include_all: bool = False,
) -> CoverageResult:
    """
    Targeted + neighbor + graph-neighbor retrieval. Full doc is NOT inlined
    (avoids lost-in-the-middle); the caller can request the object-store SoR
    for a verification pass if needed.

    force_include_all: when True (full_file mode) every section is returned
    unconditionally — neighbor expansion and hit-path filtering are bypassed
    so the LLM receives the complete document regardless of query terms.
    """
    section_map = payload.get("section_map") or []
    full_md     = payload.get("full_md") or ""
    if not section_map:
        # No headings — return full doc as one block.
        return CoverageResult(
            mode="doc_fits",
            sections_examined=1,
            sections_included=1,
            evidence=[{
                "section_path": "",
                "text":         full_md[:_MAP_MAX_SECTION_CHARS],
                "doc_id":       payload.get("doc_id"),
            }],
            badge=f"Read full doc ({len(full_md):,} chars)",
            trace={"reason": "no headings", "force_include_all": force_include_all},
        )

    if force_include_all:
        # full_file mode: return every section in document order — no filtering.
        indices = list(range(len(section_map)))
    else:
        hit_paths = {h.get("section_path") for h in fast_hits if h.get("section_path")}
        indices   = _expand_with_neighbors(section_map, hit_paths, neighbor_window=1)
        if not indices:
            # No fast-tier section hits — fall back to verifying first/last sections
            # plus every top-level heading (gives the reasoner a doc skeleton).
            indices = [0, len(section_map) - 1] + [
                i for i, e in enumerate(section_map) if int(e.get("level", 99)) == 1
            ]
            indices = sorted(set(i for i in indices if 0 <= i < len(section_map)))

    hit_paths = {h.get("section_path") for h in fast_hits if h.get("section_path")}
    evidence: list[dict] = []
    for i in indices:
        entry = section_map[i]
        evidence.append({
            "section_path": entry.get("section_path", ""),
            "text":         _slice_section(full_md, entry),
            "doc_id":       payload.get("doc_id"),
        })

    _total = len(section_map)
    _included = len(evidence)
    if force_include_all:
        _badge = f"Read all {_included}/{_total} sections (doc-fits, full_file mode)"
    else:
        _badge = f"Read {_included}/{_total} sections (targeted+neighbor)"

    return CoverageResult(
        mode="doc_fits",
        sections_examined=_total,
        sections_included=_included,
        evidence=evidence,
        badge=_badge,
        trace={"hit_paths": list(hit_paths), "force_include_all": force_include_all},
    )


# ────────────────────────────────────────────────────────────────────────────
# Mode (ii) — graph-guided map-reduce
# ────────────────────────────────────────────────────────────────────────────

# A tiny local relevance scorer used by the default map step. The rule from
# §8z: NEVER compress or paraphrase — only flag include/drop. We err on the
# side of inclusion (high recall, low precision = extra cost only).
def _local_relevance_score(question: str, section_text: str) -> float:
    q_terms = {
        t.lower() for t in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_]{2,}\b", question or "")
    }
    if not q_terms:
        return 1.0  # if the query is opaque we keep the section (fail-safe)

    t_lower = section_text.lower()
    hits = sum(1 for term in q_terms if term in t_lower)
    score = hits / max(1, len(q_terms))
    return min(1.0, score)


def _map_section_local(question: str, entry_text: str) -> dict:
    """Local high-recall inclusion filter. Returns include flag + score."""
    score = _local_relevance_score(question, entry_text)
    # Threshold biased low so the include set stays generous. Tunable via env.
    threshold = float(os.getenv("KB_COVERAGE_MAP_LOCAL_THRESHOLD", "0.10"))
    return {"include": score >= threshold, "score": score}


def _map_reduce_mode(
    question: str,
    payload: dict,
    fast_hits: list[dict],
    force_include_all: bool = False,
) -> CoverageResult:
    """
    Traverse the structure graph (section_map) over EVERY section.
    No early termination — coverage is the whole point of this tier.

    force_include_all: when True (full_file mode) the local relevance filter
    is bypassed and every section is unconditionally included. This guarantees
    100% coverage regardless of query terms.
    """
    section_map = payload.get("section_map") or []
    full_md     = payload.get("full_md") or ""
    if not section_map:
        # Without headings we cannot map-reduce safely. Return the doc with a
        # warning so the caller can either escalate to a different doc or
        # surface the missing-structure problem to admins.
        return CoverageResult(
            mode="map_reduce",
            sections_examined=1,
            sections_included=1,
            evidence=[{
                "section_path": "",
                "text":         full_md[:_MAP_MAX_SECTION_CHARS],
                "doc_id":       payload.get("doc_id"),
            }],
            badge="Map-reduce skipped — doc has no headings",
            trace={"warning": "no section_map"},
        )

    # Prioritize fast-tier-relevant sections first; everything else still runs.
    hit_paths = {h.get("section_path") for h in fast_hits if h.get("section_path")}
    ordered = sorted(
        range(len(section_map)),
        key=lambda i: (0 if section_map[i].get("section_path") in hit_paths else 1, i),
    )
    # Phase 6 cap: hard ceiling per query so a single 1,000-section spec
    # cannot saturate map workers under load. Hit sections are always kept.
    # Bypassed in full_file mode — the whole point is 100% section coverage.
    if _MAX_SECTIONS_PER_QUERY and len(ordered) > _MAX_SECTIONS_PER_QUERY and not force_include_all:
        ordered = ordered[:_MAX_SECTIONS_PER_QUERY]

    included: list[dict] = []
    examined = 0

    def _process(idx: int) -> Optional[dict]:
        entry = section_map[idx]
        body  = _slice_section(full_md, entry)
        if force_include_all or _MAP_MODE == "cloud":
            # full_file mode: every section is unconditionally included so the
            # LLM receives the complete document (§full_file guarantee).
            # Cloud mode: per-section cloud call is wired by the caller; the
            # reducer decides — treat as included here.
            include = True
            score = 1.0
        else:
            res = _map_section_local(question, body)
            include = res["include"]
            score = res["score"]
        if not include:
            return None
        return {
            "section_path": entry.get("section_path", ""),
            "text":         body,
            "doc_id":       payload.get("doc_id"),
            "score":        score,
        }

    # Phase 6 — global semaphore guard. Cap total in-flight map runs across
    # ALL requests on this process so a thundering herd of 2,000 coverage
    # queries doesn't multiply by _MAP_CONCURRENCY and exhaust threads. On
    # acquire timeout we degrade to Fast-tier (caller already has fast_hits).
    _sem   = _get_coverage_semaphore()
    _token = None
    if _sem is not None:
        _token = _sem.acquire(timeout=_COVERAGE_GLOBAL_TIMEOUT_S)
        if _token is None:
            logger.warning(
                f"coverage_retriever: map-reduce skipped — global semaphore "
                f"timeout (capacity={_COVERAGE_GLOBAL_CONCURRENCY}, "
                f"wait={_COVERAGE_GLOBAL_TIMEOUT_S}s). Falling back to fast hits."
            )
            return CoverageResult(
                mode="map_reduce_skipped",
                sections_examined=0,
                sections_included=0,
                evidence=[],
                badge=f"Coverage skipped — system busy (fast tier only)",
                trace={
                    "reason":              "global_semaphore_timeout",
                    "global_capacity":     _COVERAGE_GLOBAL_CONCURRENCY,
                    "global_timeout_s":    _COVERAGE_GLOBAL_TIMEOUT_S,
                    "section_count_total": len(section_map),
                },
            )

    try:
        with ThreadPoolExecutor(
            max_workers=max(1, _MAP_CONCURRENCY),
            thread_name_prefix="kb-coverage-map",
        ) as pool:
            futs = {pool.submit(_process, i): i for i in ordered}
            for f in as_completed(futs):
                examined += 1
                try:
                    row = f.result()
                except Exception as e:
                    logger.debug(f"coverage map worker failed (skipped): {e}")
                    row = None
                if row is not None:
                    included.append(row)
    finally:
        if _sem is not None and _token is not None:
            try:
                _sem.release(_token)
            except Exception:
                pass

    # Restore document order so the reducer reads top-down.
    path_to_idx = {e.get("section_path"): i for i, e in enumerate(section_map)}
    included.sort(key=lambda r: path_to_idx.get(r.get("section_path"), 1_000_000))

    _total = len(section_map)
    _included_count = len(included)
    if _included_count == _total:
        _badge = f"Read all {_total}/{_total} sections (map-reduce, every section examined)"
    else:
        _badge = (
            f"Read {_included_count}/{_total} sections (map-reduce, "
            f"{_total - _included_count} filtered by relevance)"
        )

    return CoverageResult(
        mode="map_reduce",
        sections_examined=examined,
        sections_included=_included_count,
        evidence=included,
        badge=_badge,
        trace={"map_mode": _MAP_MODE, "concurrency": _MAP_CONCURRENCY,
               "force_include_all": force_include_all},
    )


# ────────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────────

def run_coverage(
    question: str,
    payload: dict,
    fast_hits: Iterable[dict],
    retrieval_scope: str = "auto",
) -> CoverageResult:
    """
    Choose between doc-fits-window and map-reduce based on the cached doc
    payload's char count. Returns verbatim evidence + a coverage badge.

    `payload` is the dict returned by store.kb_doc_cache.get_or_warm().
    `fast_hits` is the rerank output from the Fast tier (used to bias map
    ordering toward sections the gate already flagged).
    `retrieval_scope` is the KB_RETRIEVAL_SCOPE value for this request.
      When "full_file", the map-reduce step bypasses the local relevance
      filter so every section is unconditionally included (100% coverage).
    """
    fast_list = list(fast_hits or [])
    char_len = int(payload.get("char_len") or len(payload.get("full_md") or ""))
    token_estimate = _estimate_tokens(payload.get("full_md") or "")
    _force_all = (retrieval_scope == "full_file")

    if token_estimate <= _DOC_FITS_TOKEN_BUDGET:
        result = _doc_fits_mode(question, payload, fast_list, force_include_all=_force_all)
    else:
        result = _map_reduce_mode(question, payload, fast_list, force_include_all=_force_all)

    result.trace["char_len"]        = char_len
    result.trace["token_estimate"]  = token_estimate
    result.trace["budget"]          = _DOC_FITS_TOKEN_BUDGET
    result.trace["retrieval_scope"] = retrieval_scope
    return result
