# SPDX-License-Identifier: Apache-2.0
"""
store/sdlc_governance_evidence.py — read-only governance-approval evidence
builder + exactly-once dedup ledger for exporting evidence to a linked Jira
Change ticket (V7, 2026-08-04).

Context: every governance domain approval/send-back decision and per-finding
team decision is already durably recorded across
`sdlc_governance_domain_approvals`, `sdlc_governance_finding_decisions`,
`sdlc_governance_finding_comments` and the immutable
`sdlc_governance_scan_snapshots`. This module assembles that state into one
deterministic evidence record per run (who approved what, on which snapshot,
whether they were an authorized approver at decision time) for attachment to
a linked Jira Change ticket used by prod-promotion change management, and
provides an exactly-once ledger (`sdlc_governance_evidence_log`) so the same
domain-approval / final-approval event is never posted twice.

Read-only against the five governance tables above EXCEPT for the dedup-log
insert (`evidence_log_claim`).

Rules (mirrors store/sdlc_governance_approvers.py):
- All queries: parameterized (never f-string SQL).
- Never raise — log + return safe defaults ([] / {} / None / False).
- Domains stored/compared UPPERCASED.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Optional

from core.logger import logger


# ---------------------------------------------------------------------------
# Internal helper — mirrors sdlc_governance_approvers._get_session()
# ---------------------------------------------------------------------------

def _get_session():
    try:
        from db.database import SessionLocal
        return SessionLocal()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Event-key builders (pure — no DB) used by the orchestrator to dedup-key an
# evidence-post event before calling evidence_log_claim / evidence_log_seen.
# ---------------------------------------------------------------------------

def domain_event_key(run_id: str, domain: str, status: str, decided_at_iso: str) -> str:
    """Deterministic dedup key for one domain-decision evidence event."""
    raw = f"{run_id}|{(domain or '').upper()}|{status}|{decided_at_iso}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def final_event_key(run_id: str, snapshot_id: str) -> str:
    """Deterministic dedup key for the run's final (all-domains-approved)
    evidence event, bound to the snapshot that was signed off."""
    raw = f"{run_id}|FINAL|{snapshot_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Best-effort identity / authorization resolution
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value) -> bool:
    return bool(value) and bool(_UUID_RE.match(str(value).strip()))


def _resolve_identity(raw) -> Optional[str]:
    """Best-effort resolve a `decided_by`/`decided_by` audit value to a human
    email. Most decided_by values already ARE an email (the router falls back
    to JWT `sub` only when no email claim exists), so this only attempts a
    `users` lookup when the raw value looks like a uuid/sub. On any lookup
    miss or DB error, the raw value is returned unchanged. Never raises."""
    if not raw:
        return raw
    if not _looks_like_uuid(raw):
        return raw
    session = _get_session()
    if session is None:
        return raw
    try:
        from sqlalchemy import text
        row = session.execute(
            text("SELECT email FROM users WHERE id = :uid LIMIT 1"),
            {"uid": str(raw)},
        ).fetchone()
        if row is not None and row.email:
            return row.email
        return raw
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] _resolve_identity failed raw={raw}: {exc}")
        return raw
    finally:
        try:
            session.close()
        except Exception:
            pass


def _is_authorized_approver(domain: str, raw_decided_by) -> bool:
    """Best-effort: was `raw_decided_by` listed as an ACTIVE approver for
    `domain` in the governance_domain_approvers roster (matched against either
    approver_email or approver_user_id, whichever the raw value looks like)?
    Returns False on missing input or any DB error — this flag is advisory
    evidence metadata, not a gate, so it fails closed to "unauthorized" rather
    than raising. Never raises."""
    if not raw_decided_by or not domain:
        return False
    session = _get_session()
    if session is None:
        return False
    try:
        from sqlalchemy import text
        row = session.execute(
            text(
                "SELECT 1 FROM governance_domain_approvers "
                "WHERE active = TRUE AND domain = :domain "
                "  AND (approver_email = :raw OR approver_user_id = :raw) "
                "LIMIT 1"
            ),
            {"domain": domain.upper(), "raw": str(raw_decided_by)},
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] _is_authorized_approver failed domain={domain}: {exc}")
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


def _fetch_skill_versions(snapshot_id) -> Optional[dict]:
    """latest_snapshot() (store.sdlc_governance_findings) does not project the
    skill_versions JSONB column, so it is fetched separately here. Returns
    None on any error or missing snapshot."""
    if not snapshot_id:
        return None
    session = _get_session()
    if session is None:
        return None
    try:
        from sqlalchemy import text
        row = session.execute(
            text(
                "SELECT skill_versions FROM sdlc_governance_scan_snapshots "
                "WHERE id = :sid LIMIT 1"
            ),
            {"sid": snapshot_id},
        ).fetchone()
        if row is None:
            return None
        return row.skill_versions or {}
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] _fetch_skill_versions failed snapshot={snapshot_id}: {exc}")
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def _list_run_comments(run_id: str) -> list:
    """Read the full finding-comment audit thread for a run
    (sdlc_governance_finding_comments), oldest first. Never raises — [] on
    any error."""
    session = _get_session()
    if session is None:
        logger.warning("[GOV-EVIDENCE] _list_run_comments: no DB session", run_id=run_id)
        return []
    try:
        from sqlalchemy import text
        rows = session.execute(
            text(
                "SELECT domain, fingerprint, role, author_email, body, "
                "       decision_context, created_at "
                "FROM sdlc_governance_finding_comments "
                "WHERE run_id = :run_id "
                "ORDER BY created_at ASC"
            ),
            {"run_id": run_id},
        ).fetchall()
        return [
            {
                "domain":           r.domain,
                "fingerprint":      r.fingerprint,
                "role":             r.role,
                "author_email":     r.author_email,
                "body":             r.body,
                "decision_context": r.decision_context,
                "created_at":       r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] _list_run_comments failed run={run_id}: {exc}")
        return []
    finally:
        try:
            session.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------

def build_governance_evidence(run_id: str) -> dict:
    """Assemble the immutable governance-approval evidence record for a run.

    Composed entirely of non-raising reads (store.sdlc_store.get_run,
    store.sdlc_governance_findings.latest_snapshot,
    store.sdlc_governance_approvers.list_domain_approvals /
    get_finding_decisions, plus this module's own comment/roster reads).
    Any sub-section that fails degrades to a safe default (None / [] / {})
    rather than aborting the whole record. Never raises.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        run = None
        try:
            from store.sdlc_store import get_run
            run = get_run(run_id)
        except Exception as exc:
            logger.warning("[GOV-EVIDENCE] build read failure — safe default",
                           run_id=run_id, error=str(exc))
            run = None

        jira_key = (run or {}).get("jira_key")
        repo     = (run or {}).get("repo")
        branch   = (run or {}).get("branch")
        pr_url   = (run or {}).get("pr_url")
        # Run state + any suspend/error reason so a "no issues" / suspended
        # outcome still carries context in the evidence bundle (2026-08-06).
        run_state = (run or {}).get("state")
        run_error = (run or {}).get("error")

        snap = None
        try:
            from store.sdlc_governance_findings import latest_snapshot
            snap = latest_snapshot(run_id)
        except Exception as exc:
            logger.warning("[GOV-EVIDENCE] build read failure — safe default",
                           run_id=run_id, error=str(exc))
            snap = None

        snapshot_out = None
        snapshot_id = None
        if snap:
            snapshot_id = snap.get("id")
            snapshot_out = {
                "id":             snapshot_id,
                "scan_seq":       snap.get("scan_seq"),
                "diff_hash":      snap.get("diff_hash"),
                "bundle_version": snap.get("bundle_version"),
                "skill_versions": _fetch_skill_versions(snapshot_id),
                "created_at":     snap.get("created_at"),
            }

        approvals = []
        get_finding_decisions = None
        try:
            from store.sdlc_governance_approvers import (
                list_domain_approvals,
                get_finding_decisions as _gfd,
            )
            get_finding_decisions = _gfd
            approvals = list_domain_approvals(run_id)
        except Exception as exc:
            logger.warning("[GOV-EVIDENCE] build read failure — safe default",
                           run_id=run_id, error=str(exc))
            approvals = []

        domains_out = []
        for row in approvals or []:
            domain = (row.get("domain") or "").upper()
            raw_decided_by = row.get("decided_by")

            findings_out = []
            if snapshot_id and get_finding_decisions is not None:
                try:
                    decisions = get_finding_decisions(run_id, snapshot_id, domain=domain)
                except Exception as exc:
                    logger.warning("[GOV-EVIDENCE] build read failure — safe default",
                                   run_id=run_id, error=str(exc))
                    decisions = {}
                for fp, dec in (decisions or {}).items():
                    findings_out.append({
                        "fingerprint": fp,
                        "decision":    dec.get("decision"),
                        "comment":     dec.get("comment"),
                        "decided_by":  _resolve_identity(dec.get("decided_by")),
                        "decided_at":  dec.get("decided_at"),
                    })

            domains_out.append({
                "domain":     domain,
                "status":     row.get("status"),
                "decided_by": _resolve_identity(raw_decided_by),
                "decided_at": row.get("decided_at"),
                "note":       row.get("note"),
                "authorized": _is_authorized_approver(domain, raw_decided_by),
                "findings":   findings_out,
            })

        comments_out = _list_run_comments(run_id)

        return {
            "run_id":       run_id,
            "jira_key":     jira_key,
            "repo":         repo,
            "branch":       branch,
            "pr_url":       pr_url,
            "run_state":    run_state,
            "run_error":    run_error,
            "snapshot":     snapshot_out,
            "domains":      domains_out,
            "comments":     comments_out,
            "generated_at": generated_at,
        }
    except Exception as exc:
        # Outer guard: never raise even if an unforeseen error slips past the
        # per-section guards above.
        logger.warning("[GOV-EVIDENCE] build_governance_evidence outer failure — minimal record",
                       run_id=run_id, error=str(exc))
        return {
            "run_id":       run_id,
            "jira_key":     None,
            "repo":         None,
            "branch":       None,
            "pr_url":       None,
            "run_state":    None,
            "run_error":    None,
            "snapshot":     None,
            "domains":      [],
            "comments":     [],
            "generated_at": generated_at,
        }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_evidence_json(evidence: dict) -> bytes:
    """Deterministic JSON rendering (sorted keys, stable indent) so the
    SHA-256 computed over it is reproducible for the same evidence content."""
    try:
        return json.dumps(evidence, indent=2, sort_keys=True, default=str).encode("utf-8")
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] render_evidence_json failed: {exc}")
        return b"{}"


def render_evidence_markdown(evidence: dict) -> str:
    """Human-readable GitHub-flavored markdown report of the evidence record,
    suitable for a Jira Change-ticket comment/attachment body."""
    try:
        evidence = evidence or {}
        lines = []
        lines.append(f"# Governance Approval Evidence — Run {evidence.get('run_id')}")
        lines.append("")
        lines.append(f"- **Jira ticket:** {evidence.get('jira_key') or '(none)'}")
        lines.append(f"- **Repo:** {evidence.get('repo') or '(unknown)'}")
        lines.append(f"- **Branch:** {evidence.get('branch') or '(unknown)'}")
        lines.append(f"- **PR/MR:** {evidence.get('pr_url') or '(none)'}")
        lines.append(f"- **Run state:** {evidence.get('run_state') or '(unknown)'}")
        if evidence.get("run_error"):
            lines.append(f"- **Reason / note:** {evidence.get('run_error')}")
        lines.append("")

        snap = evidence.get("snapshot") or {}
        lines.append("## Scanned Snapshot")
        if snap:
            lines.append(f"- **Scan seq:** {snap.get('scan_seq')}")
            lines.append(f"- **Diff hash:** `{snap.get('diff_hash')}`")
            lines.append(f"- **Bundle version:** {snap.get('bundle_version')}")
            lines.append(f"- **Snapshot created at:** {snap.get('created_at')}")
        else:
            lines.append("- _No scan snapshot — no code changes were scanned "
                         "(clean pass / nothing to scan)._")
        lines.append("")

        domains = evidence.get("domains") or []
        lines.append("## Domain Approvals")
        if not domains:
            lines.append("- _No governance issues found — no domain sign-off "
                         "was required for this run._")
            lines.append("")
        for dom in domains:
            auth_flag = "yes" if dom.get("authorized") else "NO (unverified approver)"
            lines.append(f"### {dom.get('domain')} — {dom.get('status')}")
            lines.append(f"- **Decided by:** {dom.get('decided_by') or '(none)'} "
                         f"(authorized: {auth_flag})")
            lines.append(f"- **Decided at:** {dom.get('decided_at') or '(n/a)'}")
            if dom.get("note"):
                lines.append(f"- **Note:** {dom.get('note')}")
            findings = dom.get("findings") or []
            if findings:
                lines.append("")
                lines.append("| Fingerprint | Decision | Decided by | Decided at | Comment |")
                lines.append("|---|---|---|---|---|")
                for f in findings:
                    comment = (f.get("comment") or "").replace("\n", " ").replace("|", "/")
                    lines.append(
                        f"| `{f.get('fingerprint')}` | {f.get('decision')} | "
                        f"{f.get('decided_by') or ''} | {f.get('decided_at') or ''} | "
                        f"{comment} |"
                    )
            lines.append("")

        comments = evidence.get("comments") or []
        if comments:
            lines.append("## Audit Comments")
            for c in comments:
                lines.append(
                    f"- [{c.get('created_at')}] **{c.get('role')}** "
                    f"({c.get('author_email') or 'unknown'}) on "
                    f"`{c.get('fingerprint')}` [{c.get('domain')}]: {c.get('body')}"
                )
            lines.append("")

        lines.append("---")
        lines.append(f"_Generated at {evidence.get('generated_at')}_")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] render_evidence_markdown failed: {exc}")
        run_id = evidence.get("run_id") if isinstance(evidence, dict) else None
        return f"# Governance Approval Evidence — Run {run_id}\n\n(render error — see logs)\n"


def evidence_sha256(json_bytes: bytes) -> str:
    """SHA-256 hex digest over the deterministic JSON rendering."""
    try:
        return hashlib.sha256(json_bytes).hexdigest()
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] evidence_sha256 failed: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Exactly-once dedup ledger  (table: sdlc_governance_evidence_log)
# ---------------------------------------------------------------------------

def evidence_log_claim(event_key: str, run_id: str, kind: str,
                        jira_key: Optional[str] = None) -> bool:
    """Attempt to CLAIM an evidence-post event exactly once.

    Inserts (run_id, event_key, jira_key, kind) with
    ON CONFLICT (event_key) DO NOTHING and returns True ONLY if this call
    actually won the claim (the row did not already exist). A caller that
    gets False should treat the event as already posted / not its turn to
    post — NOT retry the post itself.

    On a genuine DB error this returns False (fail-closed against a double
    post) and logs at ERROR so an operator can see the ledger write itself is
    broken; a later retry of the whole evidence-post flow may still succeed
    once the DB issue clears. Never raises.
    """
    session = _get_session()
    if session is None:
        logger.error("[GOV-EVIDENCE] evidence_log_claim failed",
                     event_key=event_key, run_id=run_id, error="no DB session")
        return False
    try:
        from sqlalchemy import text
        row = session.execute(
            text(
                "INSERT INTO sdlc_governance_evidence_log "
                "    (run_id, event_key, jira_key, kind) "
                "VALUES (:run_id, :event_key, :jira_key, :kind) "
                "ON CONFLICT (event_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "run_id":    run_id,
                "event_key": event_key,
                "jira_key":  jira_key,
                "kind":      kind,
            },
        ).fetchone()
        session.commit()
        won = row is not None
        logger.info("[GOV-EVIDENCE] evidence_log_claim", event_key=event_key,
                    run_id=run_id, kind=kind, won=won)
        return won
    except Exception as exc:
        logger.error("[GOV-EVIDENCE] evidence_log_claim failed", event_key=event_key,
                     run_id=run_id, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def evidence_log_seen(event_key: str) -> bool:
    """Return True iff `event_key` already has a row in the dedup ledger.
    On any DB error, returns False (never raises)."""
    session = _get_session()
    if session is None:
        logger.warning("[GOV-EVIDENCE] evidence_log_seen: no DB session", event_key=event_key)
        return False
    try:
        from sqlalchemy import text
        row = session.execute(
            text(
                "SELECT 1 FROM sdlc_governance_evidence_log "
                "WHERE event_key = :event_key LIMIT 1"
            ),
            {"event_key": event_key},
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.warning(f"[GOV-EVIDENCE] evidence_log_seen failed event_key={event_key}: {exc}")
        return False
    finally:
        session.close()
