# SPDX-License-Identifier: Apache-2.0
"""Page-chunking + map-reduce helpers for AiNxt DL-SAR clause validation.

Large audit PDFs (100+ pages) overflow the single-read evidence caps used by
the clause validators (``full_text[:50000]``, ``sections[:50]``,
``tables[:20]``), so evidence on later pages is silently dropped and clauses
are wrongly reported as not-present / inconclusive.

This script splits the already-extracted ``enriched.json`` into page-windowed
chunks so each chunk fits comfortably under the per-read caps, lets the clause
validator agent evaluate each chunk independently, and then reduces the
per-chunk partial verdicts with a **present-if-any** rule. The deterministic
split + reduce live here; the per-chunk LLM judgment is orchestrated by the
agent prompt (a ``code_executor`` block cannot issue the agent's own model
calls).

Modes (``--mode``):

* ``split``       — read ``enriched.json``, write ``chunk_000.json`` … and print
                    a manifest ``{chunk_count, chunk_files, chunk_pages, total_pages}``.
* ``read``        — print one chunk's capped evidence (``--chunk-index``).
* ``read-batch``  — print SEVERAL chunks' evidence in one call (``--batch-start``
                    + ``--batch-size``, default 4) plus a ``next_batch_start``
                    cursor. This is the preferred read mode: it collapses an
                    N-chunk evidence sweep from N agent tool-iterations into
                    ``ceil(N / batch_size)`` calls, so large reports no longer
                    exhaust the per-node iteration budget mid-loop (which would
                    otherwise truncate the branch and emit no clause_results).
* ``reduce``      — read per-chunk partial verdicts (``--partials-json``) and
                    print reduced clause / data-element results (``--reduce-kind``).

Backward compatibility: a document with ``total_pages <= chunk_pages`` yields a
single chunk whose caps equal the original full-document caps, so present-if-any
over one partial is the identity — behavior is unchanged for small PDFs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


def _force_utf8_stdout() -> None:
    """Make stdout/stderr emit UTF-8 regardless of the host code page.

    On Windows the interpreter defaults to the legacy cp1252 console encoding,
    so ``print(json.dumps(..., ensure_ascii=False))`` raises ``UnicodeEncodeError``
    the moment a chunk contains a non-cp1252 glyph (bullets ``\u25aa``, smart
    quotes, dashes, ...). Real DL-SAR PDFs are full of these, so every
    ``read``/``read-batch`` call crashed and the validator branch produced no
    partials. Reconfiguring to UTF-8 (with replacement as a last resort) keeps
    the JSON contract intact on every platform."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

# Per-chunk evidence caps. These mirror the original single-read caps in
# _DSLAR_READ_ENRICHED_SNIPPET / the clause SKILL.md files, but are now applied
# PER CHUNK so each LLM turn stays within context.
FULL_TEXT_CAP = 50000
SECTIONS_CAP = 50
TABLES_CAP = 20
ROWS_PER_TABLE_CAP = 50
IMAGES_CAP = 100

DEFAULT_CHUNK_PAGES = 15
DEFAULT_BATCH_SIZE = 4


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

def _pages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the page-level list built by extract_dslar_pdf.py.

    Looks in ``extracted.ingested.pages`` first (the normalized location), then
    falls back to a top-level ``ingested_doc.pages``. Downstream nodes sometimes
    overwrite ``extracted.ingested`` with a non-dict marker (e.g. the bool
    ``True``), so every level is type-guarded before ``.get`` to avoid crashing
    and to let the ``ingested_doc`` fallback run.
    """
    extracted = payload.get("extracted")
    if isinstance(extracted, dict):
        ingested = extracted.get("ingested")
        if isinstance(ingested, dict):
            pages = ingested.get("pages")
            if isinstance(pages, list) and pages:
                return [p for p in pages if isinstance(p, dict)]
    ingested_doc = payload.get("ingested_doc")
    if isinstance(ingested_doc, dict):
        pages = ingested_doc.get("pages")
        if isinstance(pages, list):
            return [p for p in pages if isinstance(p, dict)]
    return []


def _in_range(page_value: Any, start: int, end: int) -> bool:
    """True if ``page_value`` (a 0-based page index) is within [start, end]."""
    try:
        p = int(page_value)
    except (TypeError, ValueError):
        return False
    return start <= p <= end


def split_pages(payload: dict[str, Any], chunk_pages: int = DEFAULT_CHUNK_PAGES) -> list[dict[str, Any]]:
    """Split the extracted content into page-windowed evidence chunks.

    Each chunk mirrors the evidence shape the clause validators already expect
    (``full_text``, ``sections``, ``tables``, ``images``) but restricted to a
    contiguous window of pages. ``sections``/``tables``/``images`` already carry
    a ``page`` field (see extract_dslar_pdf.py), so windowing is a simple range
    membership test.
    """
    if chunk_pages < 1:
        chunk_pages = DEFAULT_CHUNK_PAGES

    extracted = payload.get("extracted") or {}
    pages = _pages_from_payload(payload)
    total_pages = len(pages)

    all_sections = extracted.get("sections") or []
    all_tables = extracted.get("tables") or []
    all_images = extracted.get("images") or []

    # Fallback: no page-level ingestion available (older payloads). Emit a
    # single chunk from the flattened evidence so behavior degrades to the
    # original single-read path rather than failing.
    if total_pages == 0:
        return [{
            "chunk_index": 0,
            "page_start": 0,
            "page_end": 0,
            "full_text": (extracted.get("full_text") or "")[:FULL_TEXT_CAP],
            "sections": list(all_sections)[:SECTIONS_CAP],
            "tables": _cap_tables(all_tables)[:TABLES_CAP],
            "images": _cap_images(all_images)[:IMAGES_CAP],
        }]

    chunks: list[dict[str, Any]] = []
    for chunk_index, start in enumerate(range(0, total_pages, chunk_pages)):
        end = min(start + chunk_pages - 1, total_pages - 1)
        window_pages = pages[start:end + 1]

        text_parts = [p.get("text") or "" for p in window_pages if (p.get("text") or "").strip()]
        full_text = "\n\n".join(text_parts)[:FULL_TEXT_CAP]

        sections = [s for s in all_sections if isinstance(s, dict) and _in_range(s.get("page"), start, end)][:SECTIONS_CAP]
        tables = _cap_tables(
            [t for t in all_tables if isinstance(t, dict) and _in_range(t.get("page"), start, end)]
        )[:TABLES_CAP]
        images = _cap_images(
            [i for i in all_images if isinstance(i, dict) and _in_range(i.get("page"), start, end)]
        )[:IMAGES_CAP]

        chunks.append({
            "chunk_index": chunk_index,
            "page_start": start,
            "page_end": end,
            "full_text": full_text,
            "sections": sections,
            "tables": tables,
            "images": images,
        })

    return chunks


def _cap_tables(tables: list[Any]) -> list[dict[str, Any]]:
    capped: list[dict[str, Any]] = []
    for t in tables:
        if not isinstance(t, dict):
            continue
        capped.append({**t, "rows": (t.get("rows") or [])[:ROWS_PER_TABLE_CAP]})
    return capped


def _cap_images(images: list[Any]) -> list[dict[str, Any]]:
    capped: list[dict[str, Any]] = []
    for i in images:
        if not isinstance(i, dict):
            continue
        capped.append({
            "page": i.get("page"),
            "ref": i.get("ref"),
            "xref": i.get("xref"),
            "description": i.get("description") or "",
            "description_status": i.get("description_status"),
            "description_error": i.get("description_error"),
            "description_response_preview": i.get("description_response_preview"),
        })
    return capped


def write_chunks(work_dir: Path, chunks: list[dict[str, Any]], chunk_pages: int, total_pages: int) -> dict[str, Any]:
    """Write each chunk to ``chunk_{i:03d}.json`` and return a manifest."""
    work_dir.mkdir(parents=True, exist_ok=True)
    chunk_files: list[str] = []
    for chunk in chunks:
        name = f"chunk_{chunk['chunk_index']:03d}.json"
        path = work_dir / name
        path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
        chunk_files.append(str(path))
    return {
        "chunk_count": len(chunks),
        "chunk_files": chunk_files,
        "chunk_pages": chunk_pages,
        "total_pages": total_pages,
    }


def read_chunk(work_dir: Path, chunk_index: int) -> dict[str, Any]:
    """Return one chunk's capped evidence payload."""
    path = work_dir / f"chunk_{int(chunk_index):03d}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _count_chunks(work_dir: Path) -> int:
    """Count chunk_*.json files previously written by --mode split."""
    return sum(1 for _ in work_dir.glob("chunk_[0-9][0-9][0-9].json"))


def read_batch(work_dir: Path, batch_start: int, batch_size: int) -> dict[str, Any]:
    """Return a contiguous group of chunks' evidence in a single call.

    Reading chunks in batches keeps the agent's *tool-iteration* count bounded
    (``ceil(total_chunks / batch_size)`` reads instead of one per chunk) while
    each chunk's evidence stays individually capped, so the agent's *context*
    stays manageable too. The returned ``next_batch_start`` is the index to pass
    on the next call, or ``null`` when the batch reaches the final chunk.
    """
    if batch_size < 1:
        batch_size = DEFAULT_BATCH_SIZE
    total = _count_chunks(work_dir)
    start = max(0, int(batch_start))
    end = min(start + batch_size, total)  # exclusive
    chunks = [read_chunk(work_dir, i) for i in range(start, end)]
    next_start = end if end < total else None
    return {
        "total_chunks": total,
        "batch_start": start,
        "batch_end": end - 1 if end > start else start,
        "batch_size": batch_size,
        "next_batch_start": next_start,
        "chunks": chunks,
    }


# ---------------------------------------------------------------------------
# Reduce (present-if-any)
# ---------------------------------------------------------------------------

def _reduce_present(partials: list[dict[str, Any]]) -> Any:
    """present-if-any: True if any partial is present; False only if every
    partial is a clear not-present; None (inconclusive) otherwise."""
    if any(p.get("present") is True for p in partials):
        return True
    if partials and all(p.get("present") is False for p in partials):
        return False
    return None


def _reduce_satisfactory(partials: list[dict[str, Any]]) -> Any:
    contributing = [p for p in partials if p.get("satisfactory") is not None]
    if any(p.get("satisfactory") is False for p in contributing):
        return False
    if contributing and all(p.get("satisfactory") is True for p in contributing):
        return True
    return None


def _union_evidence(partials: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for p in partials:
        for ref in (p.get("evidence_refs") or []):
            key = str(ref)
            if key not in seen:
                seen[key] = None
    return list(seen.keys())


def _first_present_field(partials: list[dict[str, Any]], field: str) -> Any:
    for p in partials:
        if p.get("present") is True and p.get(field) not in (None, ""):
            return p.get(field)
    for p in partials:
        if p.get(field) not in (None, ""):
            return p.get(field)
    return None


def _contributing_chunks(partials: list[dict[str, Any]]) -> list[int]:
    return sorted({
        int(p["chunk_index"])
        for p in partials
        if p.get("present") is True and isinstance(p.get("chunk_index"), (int, float))
    })


def reduce_clause_partials(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce per-chunk partials for a single clause (clauses 2-13)."""
    present = _reduce_present(partials)
    sample = partials[0] if partials else {}
    chunks = _contributing_chunks(partials)
    return {
        "clause_id": sample.get("clause_id"),
        "clause_name": sample.get("clause_name"),
        "present": present,
        "inconclusive": present is None,
        "satisfactory": _reduce_satisfactory(partials),
        "evidence_refs": _union_evidence(partials),
        "raw_agent_output": (
            f"Evidence found in chunk(s): {chunks}" if chunks
            else "No grounded evidence in any chunk"
        ),
        "data_element_results": [],
    }


def reduce_data_element_partials(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce per-chunk partials for a single Clause 1 data element (by serial)."""
    present = _reduce_present(partials)
    sample = partials[0] if partials else {}
    chunks = _contributing_chunks(partials)
    return {
        "serial": sample.get("serial"),
        "scope": sample.get("scope"),
        "category": sample.get("category"),
        "label": sample.get("label"),
        "present": present,
        "inconclusive": present is None,
        "satisfactory": _reduce_satisfactory(partials),
        "rest_or_processing": _first_present_field(partials, "rest_or_processing"),
        "jurisdiction": _first_present_field(partials, "jurisdiction"),
        "brought_back_status": _first_present_field(partials, "brought_back_status"),
        "evidence_refs": _union_evidence(partials),
        "raw_agent_output": (
            f"Evidence found in chunk(s): {chunks}" if chunks
            else "No grounded evidence in any chunk"
        ),
    }


def _group_by(partials: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for p in partials:
        if not isinstance(p, dict):
            continue
        grouped.setdefault(p.get(key), []).append(p)
    return grouped


def reduce_all(partials: list[dict[str, Any]], reduce_kind: str) -> dict[str, Any]:
    """Group flat per-chunk partials and reduce them.

    ``reduce_kind='clause'`` groups by ``clause_id`` and returns
    ``{"clause_results": [...]}``. ``reduce_kind='data_element'`` groups by
    ``serial`` and returns ``{"data_element_results": [...]}``.
    """
    if reduce_kind == "data_element":
        grouped = _group_by(partials, "serial")
        results = [
            reduce_data_element_partials(grouped[k])
            for k in sorted(grouped, key=lambda v: (v is None, v))
        ]
        return {"data_element_results": results}

    grouped = _group_by(partials, "clause_id")
    results = [
        reduce_clause_partials(grouped[k])
        for k in sorted(grouped, key=lambda v: (v is None, str(v)))
    ]
    return {"clause_results": results}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description="Page-chunk + reduce helper for DSLAR clause validation.")
    parser.add_argument("--mode", required=True, choices=["split", "read", "read-batch", "reduce"])
    parser.add_argument("--work-dir", default=os.getenv("WORKFLOW_ARTIFACT_DIR", ""), help="Workflow artifact directory.")
    parser.add_argument("--enriched-json", help="Path to enriched.json (defaults to <work-dir>/enriched.json).")
    parser.add_argument("--chunk-pages", type=int, default=DEFAULT_CHUNK_PAGES, help="Pages per chunk (default 15).")
    parser.add_argument("--chunk-index", type=int, help="Chunk index to read in --mode read.")
    parser.add_argument("--batch-start", type=int, default=0, help="First chunk index for --mode read-batch (default 0).")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Chunks per batch for --mode read-batch (default 4).")
    parser.add_argument("--partials-json", help="Path to a JSON file containing the list of per-chunk partials for --mode reduce.")
    parser.add_argument("--reduce-kind", default="clause", choices=["clause", "data_element"], help="Reduce granularity.")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else Path.cwd()

    if args.mode == "split":
        enriched_path = Path(args.enriched_json).expanduser().resolve() if args.enriched_json else work_dir / "enriched.json"
        payload = json.loads(enriched_path.read_text(encoding="utf-8"))
        total_pages = len(_pages_from_payload(payload))
        chunks = split_pages(payload, chunk_pages=args.chunk_pages)
        manifest = write_chunks(work_dir, chunks, args.chunk_pages, total_pages)
        print(json.dumps(manifest, ensure_ascii=False))
        return

    if args.mode == "read":
        if args.chunk_index is None:
            raise SystemExit("--chunk-index is required in --mode read")
        print(json.dumps(read_chunk(work_dir, args.chunk_index), ensure_ascii=False))
        return

    if args.mode == "read-batch":
        print(json.dumps(read_batch(work_dir, args.batch_start, args.batch_size), ensure_ascii=False))
        return

    # reduce
    if not args.partials_json:
        raise SystemExit("--partials-json is required in --mode reduce")
    partials = json.loads(Path(args.partials_json).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(partials, list):
        raise SystemExit("--partials-json must contain a JSON list of per-chunk partials")
    print(json.dumps(reduce_all(partials, args.reduce_kind), ensure_ascii=False))


if __name__ == "__main__":
    main()
