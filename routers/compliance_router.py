# SPDX-License-Identifier: MIT
# ============================================================
# COMPLIANCE ROUTER — /compliance
# Signed audit reports, chain verification, CSV/JSON export,
# and batch PII/PCI check endpoint for bulk testing.
# ============================================================

import csv
import io
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from core.logger import logger
from auth.dependencies import get_current_user
from auth.rbac import require_role

router = APIRouter(prefix="/compliance", tags=["compliance"])


# ── POST /compliance/batch ────────────────────────────────────

class BatchCheckRequest(BaseModel):
    texts: List[str]


class BatchCheckItem(BaseModel):
    index:                    int
    original:                 str       # what the user typed
    prompt_to_model:          str       # what actually gets sent to GPT/Claude/Gemini
    pii_detected:             bool      # true if ANY PII was found (regardless of block/redact)
    allowed:                  bool      # true = request proceeds; false = request rejected (403)
    blocked:                  bool      # true = rejected entirely; false = allowed (possibly redacted)
    was_redacted:             bool      # true = PII found and masked in prompt_to_model
    redacted_types:           List[str] # which PII types were masked
    findings:                 List[dict]# full detection details (regex + ML)
    total_latency_ms:         float     # wall-clock time for full compliance check
    privacy_svc_latency_ms:   float     # HTTP round-trip to privacy svc (0 if not called)
    ml_called:                bool      # whether the ML privacy filter was invoked


class BatchCheckResponse(BaseModel):
    total:            int
    pii_detected:     int    # inputs where PII was found (blocked OR redacted)
    blocked:          int    # inputs rejected entirely
    redacted:         int    # inputs allowed but with PII masked
    clean:            int    # inputs with no PII detected
    ml_called_count:  int    # how many inputs triggered the ML privacy filter
    # Latency stats (ms) — total compliance time per input
    avg_latency_ms:   float
    p50_latency_ms:   float
    p95_latency_ms:   float
    p99_latency_ms:   float
    max_latency_ms:   float
    # Privacy svc ML latency stats (ms) — only over inputs where ML was called
    ml_avg_latency_ms: float
    ml_p95_latency_ms: float
    ml_p99_latency_ms: float
    throughput_rps:    float  # texts/second for this batch
    results:           List[BatchCheckItem]


@router.post("/batch", response_model=BatchCheckResponse)
def compliance_batch_check(
        req: BatchCheckRequest,
        current_user: dict = Depends(get_current_user),
):
    """
    Run the full compliance pipeline (regex + ML privacy filter) on up to 1000
    texts and return findings + redacted output for each — without calling any LLM.

    Use this for bulk PII/PCI regression testing. Set COMPLIANCE_AUDIT_LOG in .env
    to write every result to a JSONL file for offline validation.

    Requires authentication. No LLM call is made.
    """
    if not req.texts:
        raise HTTPException(status_code=400, detail="texts list is empty")
    if len(req.texts) > 1000:
        raise HTTPException(status_code=400, detail="Max 1000 texts per request")

    from agents.compliance_engine import ComplianceEngine
    engine = ComplianceEngine()

    results          = []
    n_blocked        = 0
    n_redacted       = 0
    n_pii_detected   = 0
    n_ml_called      = 0
    all_latencies    = []   # total_latency_ms per item
    ml_latencies     = []   # privacy_svc_latency_ms — only when ml_called=True

    t_batch_start = time.perf_counter()

    for i, text in enumerate(req.texts):
        try:
            r = engine.validate_input(text)
        except Exception as e:
            logger.error(f"compliance_batch: error on index {i}: {e}")
            r = {
                "allowed": True, "blocked": False,
                "was_redacted": False, "redacted_text": text,
                "redacted_types": [], "findings": [],
                "blocked_types": [],
                "total_latency_ms": 0.0,
                "privacy_svc_latency_ms": 0.0,
                "ml_called": False,
            }

        pii_detected = bool(r.get("findings")) or r["was_redacted"] or r["blocked"]
        item_total_ms = r.get("total_latency_ms", 0.0)
        item_ml_ms    = r.get("privacy_svc_latency_ms", 0.0)
        item_ml_called = r.get("ml_called", False)

        if r["blocked"]:        n_blocked += 1
        if r["was_redacted"]:   n_redacted += 1
        if pii_detected:        n_pii_detected += 1
        if item_ml_called:      n_ml_called += 1

        all_latencies.append(item_total_ms)
        if item_ml_called:
            ml_latencies.append(item_ml_ms)

        results.append(BatchCheckItem(
            index                  = i,
            original               = text,
            prompt_to_model        = r["redacted_text"],
            pii_detected           = pii_detected,
            allowed                = r["allowed"],
            blocked                = r["blocked"],
            was_redacted           = r["was_redacted"],
            redacted_types         = r.get("redacted_types", []),
            findings               = r.get("findings", []),
            total_latency_ms       = item_total_ms,
            privacy_svc_latency_ms = item_ml_ms,
            ml_called              = item_ml_called,
        ))

    batch_elapsed_s = max(time.perf_counter() - t_batch_start, 0.001)

    def _pct(lst, p):
        if not lst:
            return 0.0
        lst_sorted = sorted(lst)
        idx = max(0, int(len(lst_sorted) * p / 100) - 1)
        return round(lst_sorted[idx], 2)

    avg_lat    = round(sum(all_latencies) / max(len(all_latencies), 1), 2)
    ml_avg_lat = round(sum(ml_latencies)  / max(len(ml_latencies),  1), 2)

    logger.info(
        f"compliance_batch: total={len(req.texts)} pii={n_pii_detected} "
        f"blocked={n_blocked} redacted={n_redacted} ml_called={n_ml_called} "
        f"avg_ms={avg_lat} p95_ms={_pct(all_latencies,95)} "
        f"ml_avg_ms={ml_avg_lat} throughput={round(len(req.texts)/batch_elapsed_s,1)}/s "
        f"user={current_user.get('email','?')}"
    )

    return BatchCheckResponse(
        total              = len(req.texts),
        pii_detected       = n_pii_detected,
        blocked            = n_blocked,
        redacted           = n_redacted,
        clean              = len(req.texts) - n_pii_detected,
        ml_called_count    = n_ml_called,
        avg_latency_ms     = avg_lat,
        p50_latency_ms     = _pct(all_latencies, 50),
        p95_latency_ms     = _pct(all_latencies, 95),
        p99_latency_ms     = _pct(all_latencies, 99),
        max_latency_ms     = round(max(all_latencies, default=0.0), 2),
        ml_avg_latency_ms  = ml_avg_lat,
        ml_p95_latency_ms  = _pct(ml_latencies, 95),
        ml_p99_latency_ms  = _pct(ml_latencies, 99),
        throughput_rps     = round(len(req.texts) / batch_elapsed_s, 1),
        results            = results,
    )


# ── GET /compliance/runs/{run_id}/report ─────────────────────

@router.get("/runs/{run_id}/report")
def get_run_report(run_id: str, current_user: dict = Depends(get_current_user)):
    """
    Return a full compliance report for an SDLC run.

    Includes: agents, state timeline, signed events, PCI flags,
    code review outcome, PR/Confluence links.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing a full
    signed audit/compliance report (including PCI flags and PR/Confluence
    links) for any SDLC run to any anonymous caller.
    Fix: added `current_user: dict = Depends(get_current_user)` as a
    function parameter so FastAPI rejects unauthenticated requests with
    401 before the handler runs, matching POST /compliance/batch on this
    same router. No other logic changed.
    """
    from store.sdlc_store import get_run, get_run_events

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    events = get_run_events(run_id)

    # Build state timeline
    timeline = []
    for e in events:
        timeline.append({
            "from_state": e.get("from_state", ""),
            "to_state":   e.get("to_state", ""),
            "stage":      e.get("stage", ""),
            "actor":      e.get("actor", ""),
            "timestamp":  e.get("created_at", ""),
            "signed":     bool(e.get("signature")),
        })

    # Extract unique agents from event actors
    agents = list({e.get("actor", "") for e in events if e.get("actor")})

    # PCI flags from context
    context = run.get("context", {})
    pci_flags = context.get("pci_flags", [])

    return {
        "report_version": "1.0",
        "generated_at":   datetime.utcnow().isoformat(),
        "run": {
            "id":             run_id,
            "type":           run.get("type", ""),
            "jira_key":       run.get("jira_key", ""),
            "jira_summary":   run.get("jira_summary", ""),
            "state":          run.get("state", ""),
            "repo":           run.get("repo", ""),
            "branch":         run.get("branch", ""),
            "pr_number":      run.get("pr_number"),
            "pr_url":         run.get("pr_url", ""),
            "confluence_url": run.get("confluence_url", ""),
            "triggered_by":   run.get("triggered_by", ""),
            "created_at":     run.get("created_at", ""),
        },
        "agents":           agents,
        "state_timeline":   timeline,
        "signed_events":    events,
        "pci_flags":        pci_flags,
        "code_review":      context.get("code_review_outcome", ""),
        "total_events":     len(events),
        "signed_events_count": sum(1 for e in events if e.get("signature")),
    }


# ── GET /compliance/runs/{run_id}/verify ─────────────────────

@router.get("/runs/{run_id}/verify")
def verify_run_audit_chain(run_id: str, current_user: dict = Depends(get_current_user)):
    """
    Verify the cryptographic signature chain for all events in a run.
    Returns {valid, total, verified, first_invalid_index}.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing
    audit-chain verification details for any run to any anonymous caller.
    Fix: added `current_user: dict = Depends(get_current_user)` as a
    function parameter so FastAPI rejects unauthenticated requests with
    401 before the handler runs. No other logic changed.
    """
    from store.sdlc_store import get_run, get_run_events
    from core.audit_signer import verify_chain

    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    events = get_run_events(run_id)
    result = verify_chain(events)
    result["run_id"] = run_id
    result["total_events"] = len(events)
    return result


# ── GET /compliance/export/audit ──────────────────────────────

@router.get("/export/audit")
def export_audit(
        from_date: Optional[str] = Query(
            default=None, description="ISO date YYYY-MM-DD (inclusive)"
        ),
        to_date: Optional[str] = Query(
            default=None, description="ISO date YYYY-MM-DD (inclusive)"
        ),
        format: str = Query(default="json", description="json or csv"),
        _u: dict = Depends(require_role("admin")),
):
    """
    Export signed audit events across all runs filtered by date range.
    Returns JSON array or CSV download.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, letting any
    anonymous caller bulk-export every signed audit event across every SDLC
    run.
    Fix: added `_u: dict = Depends(require_role("admin"))` as a function
    parameter (also added `from auth.rbac import require_role` at the top
    of this file). This raises 401 for unauthenticated callers and 403 for
    non-admin callers, restricting the bulk export to admins — the same
    restriction already used for /metrics, /metrics/prometheus, and
    /metrics/compression elsewhere on this platform.
    """
    from store.sdlc_store import list_runs, get_run_events

    # Default: last 30 days
    if not to_date:
        to_date = datetime.utcnow().strftime("%Y-%m-%d")
    if not from_date:
        from_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt   = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) \
                  + timedelta(days=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    # Collect all events in date range
    runs = list_runs(limit=500)
    all_events = []
    for run in runs:
        run_created = run.get("created_at", "")
        try:
            run_dt = datetime.fromisoformat(run_created).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if from_dt <= run_dt < to_dt:
            events = get_run_events(run["id"])
            for e in events:
                e["run_type"]    = run.get("type", "")
                e["jira_key"]    = run.get("jira_key", "")
                all_events.append(e)

    if format.lower() == "csv":
        output = io.StringIO()
        fields = [
            "id", "run_id", "run_type", "jira_key",
            "from_state", "to_state", "stage", "actor",
            "output", "created_at", "signature",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for e in all_events:
            writer.writerow({f: e.get(f, "") for f in fields})

        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="audit_{from_date}_{to_date}.csv"'
            },
        )

    return {
        "from":         from_date,
        "to":           to_date,
        "total_events": len(all_events),
        "events":       all_events,
    }
