# SPDX-License-Identifier: Apache-2.0
"""
agents/sdlc_governance_change_ticket.py — change-ticket orchestrator for
governance approval evidence (V7, 2026-08-04).

Composes already-implemented helpers to:
  1. lazily create (or reuse) ONE linked Jira Change ticket per SDLC run,
  2. post a per-domain evidence comment each time a governance domain is
     decided (approved / send-back), exactly once per decision,
  3. attach the final evidence bundle (JSON + markdown + SHA-256) and
     transition the change ticket once the run's governance gate fully
     converges, exactly once per snapshot.

Everything here is best-effort. This module is called from HITL/approval
code paths and worker jobs — a Jira/DB hiccup must never abort or fail the
governance decision that triggered it, so every public function swallows
its own exceptions and only logs. Nothing here raises to its caller.

Kill switch: `core.config.SDLC_GOVERNANCE_EVIDENCE_ENABLED`. When False,
every public function is a no-op.
"""
from __future__ import annotations

from typing import Optional

from core.logger import logger
from core.config import (
    SDLC_GOVERNANCE_EVIDENCE_ENABLED,
    SDLC_GOVERNANCE_CHANGE_PROJECT,
    SDLC_GOVERNANCE_CHANGE_ISSUE_TYPE,
    SDLC_GOVERNANCE_CHANGE_TRANSITION,
    SDLC_GOVERNANCE_CHANGE_LABEL,
    SDLC_GOVERNANCE_CHANGE_LINK_TYPE,
)

from tools.jira_tools import (
    jira_create_issue,
    jira_link_issues,
    jira_add_comment_adf,
    jira_add_label,
    jira_add_attachment,
    jira_update_issue,
    _adf_doc,
)

from store.sdlc_governance_evidence import (
    build_governance_evidence,
    render_evidence_json,
    render_evidence_markdown,
    evidence_sha256,
    evidence_log_claim,
    domain_event_key,
    final_event_key,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_session():
    """Mirrors store/sdlc_governance_evidence.py::_get_session(). Never raises."""
    try:
        from db.database import SessionLocal
        return SessionLocal()
    except Exception:
        return None


def _persist_run_field(run_id: str, **fields) -> None:
    """
    Best-effort UPDATE of one or more sdlc_runs columns for `run_id`.

    Supported keys: `governance_evidence_jira_key`, `governance_evidence_sha`
    (both persisted alongside `governance_evidence_posted_at = NOW()` when the
    jira_key is set for the first time). Never raises — logs a WARNING on any
    DB error and returns.
    """
    if not fields:
        return
    session = _get_session()
    if session is None:
        logger.warning("[GOV-EVIDENCE] _persist_run_field: no DB session", run_id=run_id)
        return
    try:
        from sqlalchemy import text

        set_clauses = []
        params: dict = {"rid": run_id}

        if "governance_evidence_jira_key" in fields:
            set_clauses.append("governance_evidence_jira_key = :jira_key")
            set_clauses.append("governance_evidence_posted_at = NOW()")
            params["jira_key"] = fields["governance_evidence_jira_key"]

        if "governance_evidence_sha" in fields:
            set_clauses.append("governance_evidence_sha = :sha")
            params["sha"] = fields["governance_evidence_sha"]

        if not set_clauses:
            return

        session.execute(
            text(f"UPDATE sdlc_runs SET {', '.join(set_clauses)} WHERE id = :rid"),
            params,
        )
        session.commit()
    except Exception as exc:
        logger.warning("[GOV-EVIDENCE] _persist_run_field failed", run_id=run_id, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        try:
            session.close()
        except Exception:
            pass


def _read_change_key(run_id: str) -> Optional[str]:
    """
    Read `governance_evidence_jira_key` straight from Postgres for `run_id`.

    Used by the lock-lost fallback so idempotency does not depend on whichever
    columns `get_run()`'s dict projection happens to include. Best-effort —
    returns None on any error (no session, missing row, DB failure).
    """
    session = _get_session()
    if session is None:
        return None
    try:
        from sqlalchemy import text
        row = session.execute(
            text("SELECT governance_evidence_jira_key FROM sdlc_runs WHERE id = :rid"),
            {"rid": run_id},
        ).first()
        return (row[0] or None) if row else None
    except Exception as exc:
        logger.debug(f"[GOV-EVIDENCE] _read_change_key failed run_id={run_id}: {exc}")
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def _acquire_create_lock(run_id: str):
    """
    Best-effort short Redis lock so two concurrent first-decisions don't each
    create a change ticket. Returns a redis client to release the lock with
    (or None if no lock was acquired / redis unavailable — caller then
    proceeds WITHOUT the lock, relying on the dedup ledger + key-already-set
    check as the real idempotency guard). Never raises.
    """
    try:
        import redis as _redis_lib
        from core.config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD

        client = _redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=2,
            password=REDIS_PASSWORD,
            socket_connect_timeout=2,
        )
        key = f"gov:evidence:createlock:{run_id}"
        got = client.set(key, "1", nx=True, ex=30)
        if got:
            return client
        return None
    except Exception as exc:
        logger.debug(f"[GOV-EVIDENCE] _acquire_create_lock unavailable run_id={run_id}: {exc}")
        return None


def _release_create_lock(client, run_id: str) -> None:
    if client is None:
        return
    try:
        client.delete(f"gov:evidence:createlock:{run_id}")
    except Exception as exc:
        logger.debug(f"[GOV-EVIDENCE] _release_create_lock failed run_id={run_id}: {exc}")


# ---------------------------------------------------------------------------
# 1) ensure_change_ticket
# ---------------------------------------------------------------------------

def ensure_change_ticket(run: dict, user_id: str = "", user_email: str = "") -> Optional[str]:
    """
    Lazily create (or reuse) the linked Jira Change ticket for this run's
    governance evidence trail. Idempotent: reuses `governance_evidence_jira_key`
    if already set. Returns the change-ticket key, or None if evidence logging
    is disabled or ticket creation failed (best-effort — never raises).
    """
    if not SDLC_GOVERNANCE_EVIDENCE_ENABLED:
        return None

    run = run or {}
    run_id = run.get("id") or run.get("run_id")

    existing = run.get("governance_evidence_jira_key")
    if existing:
        logger.info(
            "[GOV-EVIDENCE] change ticket ready",
            run_id=run_id, change_key=existing, reused=True,
            dev_key=run.get("jira_key"),
        )
        return existing

    lock_client = _acquire_create_lock(run_id)
    try:
        if lock_client is None:
            # Didn't win the lock (or redis unavailable) — briefly re-check
            # whether another worker just created the ticket. Read the column
            # directly so this guard never depends on get_run()'s dict shape.
            fresh_key = _read_change_key(run_id)
            if fresh_key:
                logger.info(
                    "[GOV-EVIDENCE] change ticket ready",
                    run_id=run_id, change_key=fresh_key, reused=True,
                    dev_key=run.get("jira_key"),
                )
                return fresh_key
            # Proceed WITHOUT the lock — dedup ledger + key-already-set check
            # are the real idempotency guard.

        try:
            project = SDLC_GOVERNANCE_CHANGE_PROJECT or ""
            dev_key = run.get("jira_key")
            summary = f"[CHANGE] Governance approval evidence — {dev_key or run_id}"
            description = (
                "Auto-generated change ticket for governance approval evidence. "
                "This ticket accumulates per-domain approval decisions and, on "
                "final sign-off, the full evidence bundle (JSON + markdown + "
                "SHA-256) for prod-promotion change management."
            )

            url = jira_create_issue(
                summary=summary,
                description=description,
                project=project,
                issue_type=SDLC_GOVERNANCE_CHANGE_ISSUE_TYPE,
                user_id=user_id,
                user_email=user_email,
                repo_name=run.get("repo") or "",
            )

            if not url or url.startswith("Error") or "/browse/" not in url:
                logger.warning(
                    "[GOV-EVIDENCE] change ticket create/link failed",
                    run_id=run_id, error=f"jira_create_issue returned: {url}",
                )
                return None

            change_key = url.rsplit("/browse/", 1)[-1].strip()
            if not change_key:
                logger.warning(
                    "[GOV-EVIDENCE] change ticket create/link failed",
                    run_id=run_id, error=f"could not parse key from url: {url}",
                )
                return None

            # Persist BEFORE any comment so a mid-sequence retry reuses it.
            _persist_run_field(run_id, governance_evidence_jira_key=change_key)

            if dev_key:
                try:
                    jira_link_issues(
                        change_key, dev_key,
                        SDLC_GOVERNANCE_CHANGE_LINK_TYPE or "Relates",
                        user_id=user_id, user_email=user_email,
                    )
                except Exception as exc:
                    logger.warning(
                        "[GOV-EVIDENCE] change ticket create/link failed",
                        run_id=run_id, error=str(exc),
                    )
                    # Link failure is non-fatal — keep the ticket.

            logger.info(
                "[GOV-EVIDENCE] change ticket ready",
                run_id=run_id, change_key=change_key, reused=False, dev_key=dev_key,
            )
            return change_key

        except Exception as exc:
            logger.warning("[GOV-EVIDENCE] change ticket create/link failed",
                           run_id=run_id, error=str(exc))
            return None
    finally:
        _release_create_lock(lock_client, run_id)


# ---------------------------------------------------------------------------
# 2) post_domain_decision
# ---------------------------------------------------------------------------

def post_domain_decision(
        run: dict,
        domain: str,
        status: str,
        decided_by: str,
        snapshot_id: str,
        user_id: str = "",
        user_email: str = "",
) -> None:
    """
    Post a domain-scoped evidence comment to the run's change ticket exactly
    once per (run, domain, status, decided_at) event. Never raises.
    """
    if not SDLC_GOVERNANCE_EVIDENCE_ENABLED:
        return

    run = run or {}
    run_id = run.get("id") or run.get("run_id")

    try:
        change_key = ensure_change_ticket(run, user_id, user_email)
        if not change_key:
            return

        evidence = build_governance_evidence(run_id)
        domain_upper = (domain or "").upper()

        decided_at_iso = ""
        domain_row = None
        for d in evidence.get("domains") or []:
            if (d.get("domain") or "").upper() == domain_upper:
                domain_row = d
                decided_at_iso = d.get("decided_at") or ""
                break

        event_key = domain_event_key(run_id, domain_upper, status, decided_at_iso)

        if not evidence_log_claim(event_key, run_id, "domain_decision", change_key):
            logger.info(
                "[GOV-EVIDENCE] domain decision dedup skip",
                run_id=run_id, domain=domain_upper, event_key=event_key,
            )
            return

        findings = (domain_row or {}).get("findings") or []
        authorized = bool((domain_row or {}).get("authorized"))
        rows = []
        for f in findings:
            fp = (f.get("fingerprint") or "")[:12]
            rows.append([
                fp,
                f.get("decision") or "",
                f.get("decided_by") or decided_by or "",
                "yes" if authorized else "no",
                f.get("decided_at") or "",
            ])

        blocks = [
            {"kind": "heading", "level": 3,
             "text": f"Governance decision — {domain_upper}: {status}"},
            {"kind": "paragraph",
             "text": (
                 f"Approver: {decided_by or '(unknown)'} | "
                 f"Authorized: {'yes' if authorized else 'no'} | "
                 f"When: {decided_at_iso or '(unknown)'}"
             )},
        ]
        if rows:
            blocks.append({
                "kind": "table",
                "headers": ["Finding", "Decision", "Approver", "Authorized?", "When"],
                "rows": rows,
            })

        adf_doc = _adf_doc(blocks)
        jira_add_comment_adf(change_key, adf_doc, user_id=user_id, user_email=user_email)

        logger.info(
            "[GOV-EVIDENCE] domain evidence comment posted",
            run_id=run_id, domain=domain_upper, status=status,
            change_key=change_key, event_key=event_key,
        )
    except Exception as exc:
        logger.warning(
            "[GOV-EVIDENCE] post_domain_decision failed",
            run_id=run_id, domain=domain, error=str(exc),
        )
        return


# ---------------------------------------------------------------------------
# 3) post_final_attestation
# ---------------------------------------------------------------------------

def post_final_attestation(
        run: dict,
        snapshot_id: str,
        user_id: str = "",
        user_email: str = "",
) -> None:
    """
    Attach the final evidence bundle (JSON + markdown), persist its SHA-256,
    label + transition the change ticket, and post an attestation comment —
    exactly once per (run, snapshot). Never raises.
    """
    if not SDLC_GOVERNANCE_EVIDENCE_ENABLED:
        return

    run = run or {}
    run_id = run.get("id") or run.get("run_id")

    try:
        change_key = ensure_change_ticket(run, user_id, user_email)
        if not change_key:
            return

        event_key = final_event_key(run_id, snapshot_id)
        if not evidence_log_claim(event_key, run_id, "final", change_key):
            logger.info(
                "[GOV-EVIDENCE] final attestation dedup skip",
                run_id=run_id, snapshot_id=snapshot_id, event_key=event_key,
            )
            return

        evidence = build_governance_evidence(run_id)
        json_bytes = render_evidence_json(evidence)
        md = render_evidence_markdown(evidence)
        sha = evidence_sha256(json_bytes)

        short_id = (run_id or "")[:8]
        jira_add_attachment(
            change_key, f"governance-evidence-{short_id}.json", json_bytes,
            "application/json", user_id, user_email,
        )
        jira_add_attachment(
            change_key, f"governance-evidence-{short_id}.md", md.encode("utf-8"),
            "text/markdown", user_id, user_email,
        )

        _persist_run_field(run_id, governance_evidence_sha=sha)

        jira_add_label(change_key, SDLC_GOVERNANCE_CHANGE_LABEL, user_id, user_email)

        if SDLC_GOVERNANCE_CHANGE_TRANSITION:
            try:
                jira_update_issue(
                    change_key, status=SDLC_GOVERNANCE_CHANGE_TRANSITION,
                    user_id=user_id, user_email=user_email,
                )
            except Exception as exc:
                logger.warning(
                    "[GOV-EVIDENCE] final attestation transition failed",
                    run_id=run_id, change_key=change_key, error=str(exc),
                )

        attestation_doc = _adf_doc([
            {"kind": "paragraph",
             "text": f"Final governance evidence attached. SHA-256: {sha}"},
        ])
        jira_add_comment_adf(change_key, attestation_doc, user_id=user_id, user_email=user_email)

        logger.info(
            "[GOV-EVIDENCE] final bundle attached",
            run_id=run_id, change_key=change_key, sha256=sha,
            transitioned=bool(SDLC_GOVERNANCE_CHANGE_TRANSITION),
        )
    except Exception as exc:
        logger.warning(
            "[GOV-EVIDENCE] post_final_attestation failed",
            run_id=run_id, snapshot_id=snapshot_id, error=str(exc),
        )
        return
