#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Deterministically aggregate the four parallel DL-SAR clause branches.

This is the single, deterministic writer that the ``clause-results-aggregator``
workflow node runs. It GUARANTEES that ``enriched.json`` ends with exactly 13
ordered clause results (Clause 1 carrying all 68 data-element rows), regardless
of whether the four parallel clause validators finished. That guarantee is what
keeps the rendered ``validation-report-complete-*.pdf`` from collapsing to a
metadata-only page when a clause branch runs out of iteration budget.

Per-branch recovery precedence (most trustworthy first):
    1. ``<branch>/result.json``           -- the branch's own finalized output.
    2. ``<branch>/partials.json`` (or the legacy ``clause_partials.json``)
       reduced deterministically with present-if-any via ``reduce_all`` from
       the sibling ``chunk_dslar_pages.py``.
    3. a not-concluded skeleton            -- present=null / inconclusive=true,
       and for Clause 1 all 68 rows synthesized from the embedded table.

It then forces exactly the 13 canonical clauses in order, backfills any missing
Clause-1 data-element serials, recomputes ``points_not_concluded`` (preserving
the metadata-validator's existing entries), writes ``clause_results`` and
``points_not_concluded`` at the TOP LEVEL of ``enriched.json``, and normalizes
``validation_type`` to the lowercase ``dlsar`` token the rest of the pipeline
keys off. It never raises on an empty branch; it only hard-fails if
``enriched.json`` itself cannot be read.

CLI:
    python aggregate_dslar_clauses.py \
        --work-dir <WORKFLOW_ARTIFACT_DIR> \
        [--enriched-json <path/to/enriched.json>] \
        [--output-json <path>]      # default: write back in place

Prints a compact JSON summary to stdout:
    {"artifact_dir", "enriched_json", "clause_count", "clause1_data_elements",
     "recovery": {branch: source}, "validation_type", "status"}
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Reuse the PURE reducers from the sibling chunk script (import, not subprocess)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent


def _load_reduce_all():
    """Import ``reduce_all`` from the sibling chunk_dslar_pages.py.

    Resolved from this file's directory so it works regardless of CWD. Falls
    back to a subprocess invocation of ``--mode reduce`` only if the import
    cannot be performed for some reason."""
    chunk_path = _HERE / "chunk_dslar_pages.py"
    try:
        spec = importlib.util.spec_from_file_location("chunk_dslar_pages", str(chunk_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module.reduce_all
    except Exception:
        import subprocess

        def _reduce_via_subprocess(partials: list, reduce_kind: str) -> dict:
            # Write the partials to a temp file next to the script and reduce.
            tmp = _HERE / "_aggregate_tmp_partials.json"
            tmp.write_text(json.dumps(partials, ensure_ascii=False), encoding="utf-8")
            try:
                out = subprocess.run(
                    [sys.executable, str(chunk_path), "--mode", "reduce",
                     "--partials-json", str(tmp), "--reduce-kind", reduce_kind],
                    capture_output=True, text=True, check=True,
                )
                return json.loads(out.stdout.strip().splitlines()[-1])
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass

        return _reduce_via_subprocess


reduce_all = _load_reduce_all()


# ---------------------------------------------------------------------------
# Canonical data (no config file exists; mirrors renderer + clause1 SKILL.md)
# ---------------------------------------------------------------------------

CLAUSE_NAMES = {
    "1": "Payments Data Elements",
    "2": "Transaction/Data Flow",
    "3": "Application Architecture",
    "4": "Network Diagram/Architecture",
    "5": "Data Storage",
    "6": "Transaction Processing",
    "7": "Activities Related to Payment Processing",
    "8": "Cross Border Transactions",
    "9": "Database Storage and Maintenance",
    "10": "Data Backup & Restoration",
    "11": "Data Security",
    "12": "Access Management",
    "13": "Data Sharing",
}

CLAUSE_IDS = [str(i) for i in range(1, 14)]

# Explicit Clause-1 labels from dslar-clause1-validation/SKILL.md. Serials not
# listed here are participant-defined / blank in the AiNxt template -> label "-".
_CLAUSE1_LABELS = {
    1: "Customer Name", 2: "Mobile Number", 3: "VPA", 4: "Aadhar Number", 5: "Email",
    9: "Transaction Reference", 10: "Transaction Type", 11: "Amount",
    20: "Payer VPA", 21: "Payee VPA", 22: "Account Number", 23: "OTP",
    31: "UPI PIN", 32: "Passwords",
}


def _clause1_category(serial: int) -> str:
    if 1 <= serial <= 8:
        return "Customer Data"
    if 9 <= serial <= 19:
        return "Transaction Data"
    if 20 <= serial <= 30:
        return "Payment Sensitive Data"
    if 31 <= serial <= 34:
        return "Payment Credentials Data"
    return "Non-Payments Data"


def _clause1_row_template(serial: int) -> dict[str, Any]:
    """Canonical scope/category/label for a Clause-1 serial (1..68)."""
    if serial <= 34:
        scope = "payments"
        category = _clause1_category(serial)
        label = _CLAUSE1_LABELS.get(serial, "-")
    else:
        scope = "non_payments"
        category = "Non-Payments Data"
        label = f"Data Element {serial}"
    return {"serial": serial, "scope": scope, "category": category, "label": label}


def _clause1_skeleton_row(serial: int) -> dict[str, Any]:
    row = _clause1_row_template(serial)
    row.update({
        "present": None,
        "inconclusive": True,
        "satisfactory": None,
        "rest_or_processing": None,
        "jurisdiction": None,
        "brought_back_status": None,
        "evidence_refs": [],
        "raw_agent_output": "Not recovered from disk",
    })
    return row


def _clause_skeleton(cid: str) -> dict[str, Any]:
    return {
        "clause_id": cid,
        "clause_name": CLAUSE_NAMES.get(cid, ""),
        "present": None,
        "inconclusive": True,
        "satisfactory": None,
        "evidence_refs": [],
        "raw_agent_output": "Not recovered from disk",
        "data_element_results": [],
    }


# ---------------------------------------------------------------------------
# Branch layout
# ---------------------------------------------------------------------------

# (branch_dir_name, reduce_kind, [clause ids owned])
BRANCHES: list[tuple[str, str, list[str]]] = [
    ("_chunk_clause1", "data_element", ["1"]),
    ("_chunk_clauses_2_5", "clause", ["2", "3", "4", "5"]),
    ("_chunk_clauses_6_9", "clause", ["6", "7", "8", "9"]),
    ("_chunk_clauses_10_13", "clause", ["10", "11", "12", "13"]),
]

_PARTIALS_NAMES = ("partials.json", "clause_partials.json")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _find_partials(branch_dir: Path) -> list | None:
    for name in _PARTIALS_NAMES:
        data = _read_json(branch_dir / name)
        if isinstance(data, list) and data:
            return data
    return None


# ---------------------------------------------------------------------------
# Deterministic chunk fallback (recovery tier 3)
#
# When a clause branch ran out of iteration budget after splitting + reading
# its chunks but BEFORE writing partials.json / result.json, the chunk_*.json
# files still hold the capped evidence. Rather than collapse those clauses to a
# blank skeleton, we scan the chunks deterministically (no LLM) and emit a
# present-if-any verdict grounded in matched phrases. This is coarser than an
# agent-reasoned verdict (it cannot judge "satisfactory"), so satisfactory is
# left null and the clause is marked present-but-inconclusive — accurate, since
# the evidence exists but no auditor conclusion was reduced.
# ---------------------------------------------------------------------------

# Per-clause keyword sets. A clause is "present in a chunk" if any phrase is
# found in that chunk's lowercased full_text. Phrases are lowercase + specific
# enough to avoid matching the table-of-contents noise that appears everywhere.
_CLAUSE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "2": ("transaction / data flow", "transaction/data flow", "data flow diagram", "transaction flow"),
    "3": ("application architecture", "application component", "application module"),
    "4": ("network diagram", "network architecture", "disaster recovery site", "pr and dr", "pr site", "dr site"),
    "5": ("data storage", "stored exclusively within india", "data at rest", "payment data is only stored", "data residency"),
    "6": ("transaction processing", "processing of payment", "settlement"),
    "7": ("activities related to payment", "post-payment", "payment processing activities", "after payment processing"),
    "8": ("cross-border transaction", "cross border transaction", "cross-border data", "offshore"),
    "9": ("database storage", "database maintenance", "database hosted", "data base storage"),
    "10": ("data backup", "backup and restoration", "restoration", "backup policy", "rpo", "rto"),
    "11": ("data security", "encryption", "aes-256", "sha-256", "security controls", "ssl/tls"),
    "12": ("access management", "access control", "role-based access", "rbac", "privileged access", "authentication"),
    "13": ("data sharing", "shared with", "third-party", "third party", "master services agreement", "data sharing & access"),
}

_CHUNK_GLOB = "chunk_[0-9][0-9][0-9].json"
_EVIDENCE_SNIPPET_CHARS = 160
_MAX_EVIDENCE_REFS_PER_CLAUSE = 6


def _chunk_files(branch_dir: Path) -> list[Path]:
    return sorted(branch_dir.glob(_CHUNK_GLOB))


def _first_match_snippet(text_lc: str, raw_text: str, phrase: str) -> str | None:
    """Return a short raw-text snippet around the first match of ``phrase``."""
    idx = text_lc.find(phrase)
    if idx < 0:
        return None
    start = max(0, idx - 20)
    end = min(len(raw_text), idx + len(phrase) + _EVIDENCE_SNIPPET_CHARS)
    return raw_text[start:end].strip().replace("\n", " ")


def _collect_keyword_hits(
    branch_dir: Path, keyword_map: dict[Any, tuple[str, ...]]
) -> dict[Any, list[str]] | None:
    """Scan every chunk's full_text for each key's phrases (present-if-any).

    Returns ``{key: [page-cited evidence_ref, ...]}`` capped at
    ``_MAX_EVIDENCE_REFS_PER_CLAUSE`` per key (one ref per chunk per key), or
    ``None`` if the branch has no chunk files. Shared by the clause and Clause-1
    scans — only the key namespace (clause id vs data-element serial) differs."""
    chunks = _chunk_files(branch_dir)
    if not chunks:
        return None

    hits: dict[Any, list[str]] = {key: [] for key in keyword_map}
    for path in chunks:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        raw = data.get("full_text") or ""
        if not raw:
            continue
        text_lc = raw.lower()
        page_start = data.get("page_start")
        page_end = data.get("page_end")
        page_ref = f"pages {page_start}-{page_end}" if page_start is not None else "unknown page"
        for key, phrases in keyword_map.items():
            if len(hits[key]) >= _MAX_EVIDENCE_REFS_PER_CLAUSE:
                continue
            for phrase in phrases:
                snippet = _first_match_snippet(text_lc, raw, phrase)
                if snippet is not None:
                    hits[key].append(f"{page_ref}: '{snippet}'")
                    break  # one ref per chunk per key is enough
    return hits


def _scan_chunks_for_clauses(branch_dir: Path, owned_ids: list[str]) -> dict[str, dict]:
    """Deterministically build clause results for ``owned_ids`` from chunk files.

    present-if-any: a clause is present if any chunk's full_text contains one of
    its keyword phrases. evidence_refs cite the page window so it stays
    traceable. satisfactory is always null (a keyword scan cannot judge it)."""
    keyword_map = {cid: _CLAUSE_KEYWORDS.get(cid, ()) for cid in owned_ids}
    hits = _collect_keyword_hits(branch_dir, keyword_map)
    if hits is None:
        return {}

    out: dict[str, dict] = {}
    for cid in owned_ids:
        refs = hits.get(cid) or []
        present = bool(refs)
        out[cid] = {
            "clause_id": cid,
            "clause_name": CLAUSE_NAMES.get(cid, ""),
            "present": True if present else None,
            # Evidence exists but no auditor conclusion was reduced -> inconclusive.
            "inconclusive": True,
            "satisfactory": None,
            "evidence_refs": refs,
            "raw_agent_output": (
                "Recovered deterministically from chunk evidence (branch did not "
                "checkpoint partials); keyword-matched, not auditor-concluded."
                if present else
                "No keyword evidence found in chunk scan."
            ),
            "data_element_results": [],
        }
    return out


# ---------------------------------------------------------------------------
# Clause-1 helpers
# ---------------------------------------------------------------------------

def _rollup_clause1(data_elements: list[dict]) -> dict[str, Any]:
    """Roll the parent Clause-1 verdict over its reduced data-element rows.

    Mirrors the dslar-clause1-validation rollup rule: any row inconclusive ->
    parent inconclusive; else present only if every row present; satisfactory
    False if any row False, True if all True, else None."""
    if not data_elements:
        return {"present": None, "inconclusive": True, "satisfactory": None, "evidence_refs": []}

    any_inconclusive = any(r.get("inconclusive") is True for r in data_elements)
    all_present = all(r.get("present") is True for r in data_elements)
    if any_inconclusive:
        present: Any = None
    elif all_present:
        present = True
    else:
        present = False

    sat_vals = [r.get("satisfactory") for r in data_elements if r.get("satisfactory") is not None]
    if any(v is False for v in sat_vals):
        satisfactory: Any = False
    elif sat_vals and all(v is True for v in sat_vals):
        satisfactory = True
    else:
        satisfactory = None

    seen: dict[str, None] = {}
    for r in data_elements:
        for ref in (r.get("evidence_refs") or []):
            seen.setdefault(str(ref), None)
    return {
        "present": present,
        "inconclusive": present is None,
        "satisfactory": satisfactory,
        "evidence_refs": list(seen.keys()),
    }


def _normalize_clause1_entry(entry: dict) -> dict[str, Any]:
    """Ensure a Clause-1 entry carries all 68 data-element rows, backfilling
    any missing serials with not-concluded skeleton rows, then re-roll parent."""
    rows_by_serial: dict[int, dict] = {}
    for r in entry.get("data_element_results") or []:
        if not isinstance(r, dict):
            continue
        try:
            serial = int(r.get("serial"))
        except (TypeError, ValueError):
            continue
        # Backfill canonical scope/category/label if the row left them blank.
        tmpl = _clause1_row_template(serial)
        for k in ("scope", "category", "label"):
            if not r.get(k):
                r[k] = tmpl[k]
        rows_by_serial[serial] = r

    full_rows = [rows_by_serial.get(s) or _clause1_skeleton_row(s) for s in range(1, 69)]
    rollup = _rollup_clause1(full_rows)
    return {
        "clause_id": "1",
        "clause_name": CLAUSE_NAMES["1"],
        "present": rollup["present"],
        "inconclusive": rollup["inconclusive"],
        "satisfactory": rollup["satisfactory"],
        "evidence_refs": rollup["evidence_refs"],
        "raw_agent_output": entry.get("raw_agent_output") or "",
        "data_element_results": full_rows,
    }


# Keyword phrases for the labeled Clause-1 data elements. Unlabeled / generic
# serials (label "-" or "Data Element N") have no reliable phrase, so the chunk
# scan can only confirm presence for the named payment elements; the rest stay
# skeleton rows. This is intentional: a keyword scan must not fabricate a verdict
# for an element it cannot actually locate.
_CLAUSE1_ELEMENT_KEYWORDS: dict[int, tuple[str, ...]] = {
    1: ("customer name", "customer id"),
    2: ("mobile number", "mobile no"),
    3: ("vpa", "virtual payment address"),
    4: ("aadhar", "aadhaar"),
    5: ("email",),
    9: ("transaction reference", "transaction id", "rrn"),
    10: ("transaction type",),
    11: ("amount",),
    20: ("payer vpa",),
    21: ("payee vpa",),
    22: ("account number", "account no"),
    23: ("otp", "one time password"),
    31: ("upi pin", "upi-pin"),
    32: ("password",),
}


def _scan_chunks_for_clause1(branch_dir: Path) -> dict | None:
    """Deterministic Clause-1 recovery from chunk files.

    Builds all 68 data-element rows; named payment elements found via keyword
    match are marked present-but-inconclusive with a page-cited ref, the rest
    stay not-concluded skeleton rows. Returns a normalized Clause-1 entry, or
    None if no chunks exist."""
    element_refs = _collect_keyword_hits(branch_dir, _CLAUSE1_ELEMENT_KEYWORDS)
    if element_refs is None:
        return None

    rows: list[dict] = []
    for serial in range(1, 69):
        refs = element_refs.get(serial) or []
        if refs:
            row = _clause1_row_template(serial)
            row.update({
                "present": True,
                "inconclusive": True,  # present but no auditor conclusion reduced
                "satisfactory": None,
                "rest_or_processing": None,
                "jurisdiction": None,
                "brought_back_status": None,
                "evidence_refs": refs,
                "raw_agent_output": "Recovered deterministically from chunk evidence (keyword-matched).",
            })
            rows.append(row)
        else:
            rows.append(_clause1_skeleton_row(serial))

    return _normalize_clause1_entry({
        "data_element_results": rows,
        "raw_agent_output": "Clause 1 recovered deterministically from chunk evidence (branch did not checkpoint partials).",
    })


# ---------------------------------------------------------------------------
# Per-branch recovery
# ---------------------------------------------------------------------------

def _clause_has_evidence(clause: dict | None) -> bool:
    """True if a recovered clause dict carries any grounded evidence.

    Covers both shapes: a clause-2-13 entry is positive when its top-level
    ``present`` is True; a Clause-1 entry is positive when the parent
    ``present`` is True or any of its ``data_element_results`` rows is present.
    """
    if not isinstance(clause, dict):
        return False
    if clause.get("present") is True:
        return True
    return any(
        isinstance(row, dict) and row.get("present") is True
        for row in (clause.get("data_element_results") or [])
    )


def _recover_branch(branch_dir: Path, reduce_kind: str, owned_ids: list[str]
                    ) -> tuple[dict[str, dict], list, str]:
    """Return (clause_by_id, branch_points_not_concluded, source_label)."""
    clause_by_id: dict[str, dict] = {}
    points: list = []

    # (1) result.json
    result = _read_json(branch_dir / "result.json")
    if isinstance(result, dict) and isinstance(result.get("clause_results"), list) \
            and result["clause_results"]:
        for c in result["clause_results"]:
            if isinstance(c, dict) and c.get("clause_id") is not None:
                clause_by_id[str(c["clause_id"])] = c
        if isinstance(result.get("points_not_concluded"), list):
            points = list(result["points_not_concluded"])
        if any(cid in clause_by_id for cid in owned_ids):
            return clause_by_id, points, "result.json"

    # (2) partials.json / clause_partials.json -> reduce_all
    partials = _find_partials(branch_dir)
    if partials:
        if reduce_kind == "data_element":
            reduced = reduce_all(partials, "data_element")
            data_elements = reduced.get("data_element_results") or []
            entry = _normalize_clause1_entry({"data_element_results": data_elements})
            clause_by_id["1"] = entry
        else:
            reduced = reduce_all(partials, "clause")
            for c in reduced.get("clause_results") or []:
                if isinstance(c, dict) and c.get("clause_id") is not None:
                    cid = str(c["clause_id"])
                    c.setdefault("clause_name", CLAUSE_NAMES.get(cid, ""))
                    clause_by_id[cid] = c
        if any(cid in clause_by_id for cid in owned_ids):
            return clause_by_id, points, "partials.json"

    # (3) deterministic chunk scan — the branch split + read its chunks but
    # never checkpointed partials/result before running out of budget. The
    # chunk_*.json files still hold the capped evidence, so recover present-if-any
    # verdicts from them rather than collapsing to a blank skeleton.
    if reduce_kind == "data_element":
        entry = _scan_chunks_for_clause1(branch_dir)
        if entry is not None:
            clause_by_id["1"] = entry
    else:
        clause_by_id.update(_scan_chunks_for_clauses(branch_dir, owned_ids))
    # Treat the scan as a recovery only if it found grounded evidence for at
    # least one owned clause; an all-empty scan is no better than a skeleton, so
    # fall through to the caller's skeleton backfill.
    if any(_clause_has_evidence(clause_by_id.get(cid)) for cid in owned_ids):
        return clause_by_id, points, "chunk_scan"

    # (4) skeleton (handled by caller backfill); signal nothing recovered
    return clause_by_id, points, "skeleton"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(work_dir: Path, payload: dict) -> tuple[dict, dict[str, str]]:
    clause_by_id: dict[str, dict] = {}
    branch_points: list = []
    recovery: dict[str, str] = {}

    for branch_name, reduce_kind, owned_ids in BRANCHES:
        recovered, points, source = _recover_branch(
            work_dir / branch_name, reduce_kind, owned_ids)
        for cid, c in recovered.items():
            clause_by_id[cid] = c
        branch_points.extend(points)
        recovery[branch_name] = source

    # Force exactly 13 ordered clauses; inject skeletons for any missing.
    clause_results: list[dict] = []
    for cid in CLAUSE_IDS:
        entry = clause_by_id.get(cid)
        if entry is None:
            entry = (_normalize_clause1_entry({"data_element_results": []})
                     if cid == "1" else _clause_skeleton(cid))
        else:
            entry.setdefault("clause_name", CLAUSE_NAMES.get(cid, ""))
            if cid == "1":
                entry = _normalize_clause1_entry(entry)
            else:
                entry.setdefault("data_element_results", [])
        clause_results.append(entry)

    # points_not_concluded: existing + branch + per-inconclusive-clause strings.
    points_out: list = []
    seen_strings: set[str] = set()

    def _add_point(p: Any) -> None:
        if isinstance(p, str):
            if p not in seen_strings:
                seen_strings.add(p)
                points_out.append(p)
        else:
            points_out.append(p)  # preserve dict entries (e.g. metadata reasons)

    for p in (payload.get("points_not_concluded") or []):
        _add_point(p)
    for p in branch_points:
        _add_point(p)
    for c in clause_results:
        if c.get("inconclusive") is True:
            cid = str(c.get("clause_id"))
            _add_point(f"Clause {cid} ({CLAUSE_NAMES.get(cid, '')}): could not be concluded")

    payload["clause_results"] = clause_results
    payload["points_not_concluded"] = points_out

    # Normalize validation_type -> 'report' or 'dlsar' (never 'DL-SAR' etc.).
    vt = str(payload.get("validation_type") or "").strip().lower()
    payload["validation_type"] = "report" if vt == "report" else "dlsar"

    return payload, recovery


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _force_utf8_stdout() -> None:
    """Emit UTF-8 on stdout/stderr regardless of the host code page (Windows
    defaults to cp1252, which raises UnicodeEncodeError on non-cp1252 glyphs)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="Aggregate DL-SAR clause branches deterministically.")
    ap.add_argument("--work-dir", default=os.environ.get("WORKFLOW_ARTIFACT_DIR", ""))
    ap.add_argument("--enriched-json", default="")
    ap.add_argument("--output-json", default="")
    args = ap.parse_args(argv)

    work_dir = Path(args.work_dir) if args.work_dir else Path.cwd()
    enriched_path = Path(args.enriched_json) if args.enriched_json else work_dir / "enriched.json"
    output_path = Path(args.output_json) if args.output_json else enriched_path

    if not enriched_path.is_file():
        print(json.dumps({"error": f"enriched.json not found: {enriched_path}"}))
        return 2
    try:
        payload = json.loads(enriched_path.read_text(encoding="utf-8"))
    except Exception as exc:  # unreadable enriched.json is the only hard failure
        print(json.dumps({"error": f"enriched.json unreadable: {exc}"}))
        return 2
    if not isinstance(payload, dict):
        print(json.dumps({"error": "enriched.json is not a JSON object"}))
        return 2

    payload, recovery = aggregate(work_dir, payload)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    clause1 = next((c for c in payload["clause_results"] if str(c.get("clause_id")) == "1"), {})
    print(json.dumps({
        "artifact_dir": str(work_dir),
        "enriched_json": str(output_path),
        "clause_count": len(payload["clause_results"]),
        "clause1_data_elements": len(clause1.get("data_element_results") or []),
        "recovery": recovery,
        "validation_type": payload.get("validation_type"),
        "status": "clause_results_aggregated",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
