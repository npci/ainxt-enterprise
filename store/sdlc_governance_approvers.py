# SPDX-License-Identifier: Apache-2.0
"""
store/sdlc_governance_approvers.py — governance domain-approver list + per-run
per-domain approval gate state.

governance_domain_approvers: admin-managed explicit approver list per governance
domain (IS/EA/DPDP/…). NOT department-derived — an explicit list of people.

sdlc_governance_domain_approvals: per-run per-domain gate state. Seeded pending
for every domain that has ≥1 open finding before the AWAITING_GOVERNANCE_APPROVAL
gate. Advance to approved/changes_requested by domain approvers.

Rules:
- All queries: parameterized (never f-string SQL).
- Never raise — log + return safe defaults.
- all_finding_domains_approved returns False on ANY error (fail-closed).
- Domains stored/compared UPPERCASED.
"""
from __future__ import annotations

from typing import List, Optional, Set

from core.logger import logger


# ---------------------------------------------------------------------------
# Internal helper — mirrors sdlc_artifacts._get_session()
# ---------------------------------------------------------------------------

def _get_session():
    try:
        from db.database import SessionLocal
        return SessionLocal()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Approver-list CRUD  (table: governance_domain_approvers)
# ---------------------------------------------------------------------------

def list_approvers(domain: Optional[str] = None) -> list:
    """Return all active approver rows, optionally filtered by domain."""
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] list_approvers: no DB session")
        return []
    try:
        if domain is not None:
            rows = session.execute(
                __import__("sqlalchemy").text(
                    "SELECT id, domain, approver_email, approver_user_id, "
                    "       created_by, created_at, active "
                    "FROM governance_domain_approvers "
                    "WHERE active = TRUE AND domain = :domain "
                    "ORDER BY domain, approver_email"
                ),
                {"domain": domain.upper()},
            ).fetchall()
        else:
            rows = session.execute(
                __import__("sqlalchemy").text(
                    "SELECT id, domain, approver_email, approver_user_id, "
                    "       created_by, created_at, active "
                    "FROM governance_domain_approvers "
                    "WHERE active = TRUE "
                    "ORDER BY domain, approver_email"
                ),
            ).fetchall()
        return [
            {
                "id":                str(r.id),
                "domain":            r.domain,
                "approver_email":    r.approver_email,
                "approver_user_id":  r.approver_user_id,
                "created_by":        r.created_by,
                "created_at":        r.created_at.isoformat() if r.created_at else None,
                "active":            r.active,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning(f"[SDLC-GOV] list_approvers failed: {exc}")
        return []
    finally:
        session.close()


def add_approver(
    domain: str,
    email: str,
    user_id: Optional[str],
    created_by: str,
) -> bool:
    """
    Insert or reactivate an approver for a governance domain.
    ON CONFLICT (domain, approver_email) reactivates and refreshes user_id.
    Returns True on success, False on error.
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] add_approver: no DB session")
        return False
    try:
        from sqlalchemy import text
        session.execute(
            text(
                "INSERT INTO governance_domain_approvers "
                "    (domain, approver_email, approver_user_id, created_by) "
                "VALUES (:domain, :email, :user_id, :created_by) "
                "ON CONFLICT (domain, approver_email) DO UPDATE "
                "    SET active = TRUE, approver_user_id = :user_id"
            ),
            {
                "domain":     domain.upper(),
                "email":      email,
                "user_id":    user_id,
                "created_by": created_by,
            },
        )
        session.commit()
        logger.info(
            "[SDLC-GOV] add_approver",
            domain=domain.upper(),
            email=email,
        )
        return True
    except Exception as exc:
        logger.warning(f"[SDLC-GOV] add_approver failed domain={domain} email={email}: {exc}")
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def remove_approver(domain: str, email: str) -> bool:
    """
    Soft-delete an approver by setting active=FALSE.
    Returns True on success, False on error.
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] remove_approver: no DB session")
        return False
    try:
        from sqlalchemy import text
        session.execute(
            text(
                "UPDATE governance_domain_approvers "
                "SET active = FALSE "
                "WHERE domain = :domain AND approver_email = :email"
            ),
            {"domain": domain.upper(), "email": email},
        )
        session.commit()
        logger.info(
            "[SDLC-GOV] remove_approver",
            domain=domain.upper(),
            email=email,
        )
        return True
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] remove_approver failed domain={domain} email={email}: {exc}"
        )
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def approver_domains_for(user: dict) -> Set[str]:
    """
    Return the set of governance domains (uppercased) for which *user* is an
    active approver.  user is a JWT payload dict with 'email' and optionally
    'sub' (user_id).  Returns empty set on any error (non-raising).
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] approver_domains_for: no DB session")
        return set()
    try:
        from sqlalchemy import text
        email   = (user.get("email") or "").strip()
        user_id = user.get("sub") or None
        rows = session.execute(
            text(
                "SELECT DISTINCT domain "
                "FROM governance_domain_approvers "
                "WHERE active = TRUE "
                "  AND (approver_email = :email "
                "       OR (:user_id IS NOT NULL "
                "           AND approver_user_id = :user_id))"
            ),
            {"email": email, "user_id": user_id},
        ).fetchall()
        return {r.domain.upper() for r in rows}
    except Exception as exc:
        logger.warning(f"[SDLC-GOV] approver_domains_for failed: {exc}")
        return set()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Per-run gate state  (table: sdlc_governance_domain_approvals)
# ---------------------------------------------------------------------------

def seed_domain_approvals(run_id: str, open_counts_by_domain: dict,
                          *, all_domains=None) -> bool:
    """
    Seed 'pending' approval rows for the run's governance domains.

    - Default (all_domains=None): legacy behaviour — one row per domain that has
      ≥1 open finding.
    - all_domains provided (an iterable of domain strings): seed a row for EVERY
      scanned domain, INCLUDING domains with 0 open findings. This is the clean-PASS
      acknowledge gate (2026-07-30): a governance scan that finds nothing still
      requires explicit per-domain team sign-off, so each scanned domain gets a
      'pending' row with open_count 0 (or its real count when it has findings).

    open_count for each seeded domain is taken from open_counts_by_domain (default 0).
    Idempotent: ON CONFLICT (run_id, domain) DO NOTHING.
    Returns True on success, False on error.
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] seed_domain_approvals: no DB session")
        return False
    try:
        from sqlalchemy import text
        # Normalize the open-count map to UPPERCASE domain keys (findings store
        # domains uppercased; the approve endpoint uppercases the path param).
        _counts = {
            str(k).strip().upper(): int(v or 0)
            for k, v in (open_counts_by_domain or {}).items()
            if str(k).strip()
        }
        if all_domains is not None:
            # Clean-PASS acknowledge gate: seed EVERY scanned domain (union with any
            # finding-bearing domain), zero-count domains included.
            _domains = {str(d).strip().upper() for d in all_domains if str(d).strip()}
            _domains |= set(_counts.keys())
        else:
            # Legacy: only domains that actually have ≥1 open finding.
            _domains = {d for d, c in _counts.items() if c > 0}
        logger.info(
            "[SDLC-GOV] seed_domain_approvals",
            run_id=run_id,
            domains=sorted(_domains),
            include_clean=all_domains is not None,
        )
        for raw_domain in sorted(_domains):
            count = _counts.get(raw_domain, 0)
            session.execute(
                text(
                    "INSERT INTO sdlc_governance_domain_approvals "
                    "    (run_id, domain, open_count) "
                    "VALUES (:run_id, :domain, :count) "
                    "ON CONFLICT (run_id, domain) DO NOTHING"
                ),
                {
                    "run_id": run_id,
                    "domain": raw_domain.upper(),
                    "count":  int(count),
                },
            )
        session.commit()
        return True
    except Exception as exc:
        logger.warning(f"[SDLC-GOV] seed_domain_approvals failed run={run_id}: {exc}")
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def list_domain_approvals(run_id: str) -> list:
    """Return all domain approval rows for a run, ordered by domain."""
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] list_domain_approvals: no DB session")
        return []
    try:
        from sqlalchemy import text
        rows = session.execute(
            text(
                "SELECT id, run_id, domain, open_count, status, "
                "       decided_by, decided_at, note, iteration, last_send_back_at, "
                "       created_at "
                "FROM sdlc_governance_domain_approvals "
                "WHERE run_id = :run_id "
                "ORDER BY domain"
            ),
            {"run_id": run_id},
        ).fetchall()
        return [
            {
                "id":                str(r.id),
                "run_id":            str(r.run_id),
                "domain":            r.domain,
                "open_count":        r.open_count,
                "status":            r.status,
                "decided_by":        r.decided_by,
                "decided_at":        r.decided_at.isoformat() if r.decided_at else None,
                "note":              r.note,
                # iteration / last_send_back_at surface send-back history so the
                # author board can show "this domain was sent back to you".
                "iteration":         getattr(r, "iteration", None),
                "last_send_back_at": r.last_send_back_at.isoformat() if getattr(r, "last_send_back_at", None) else None,
                "created_at":        r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning(f"[SDLC-GOV] list_domain_approvals failed run={run_id}: {exc}")
        return []
    finally:
        session.close()


def decide_domain(
    run_id:               str,
    domain:               str,
    status:               str,
    decided_by:           str,
    note:                 Optional[str] = None,
    approved_snapshot_id: Optional[str] = None,
) -> bool:
    """
    Record an approver's decision (approved / changes_requested / …) for a
    governance domain on a specific run.  Returns True on success, False on error.

    When ``approved_snapshot_id`` is provided (B2.4 approve path), the domain's
    ``approved_snapshot_id`` column is stamped atomically with the status change,
    binding the approval to the exact snapshot that was signed off. When it is
    None the column is left untouched (so a non-approve decision does not clobber
    a prior stamp).
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] decide_domain: no DB session")
        return False
    try:
        from sqlalchemy import text
        params = {
            "status":     status,
            "decided_by": decided_by,
            "note":       note,
            "run_id":     run_id,
            "domain":     domain.upper(),
        }
        if approved_snapshot_id is not None:
            params["approved_snapshot_id"] = approved_snapshot_id
            sql = (
                "UPDATE sdlc_governance_domain_approvals "
                "SET status = :status, decided_by = :decided_by, "
                "    decided_at = NOW(), note = :note, "
                "    approved_snapshot_id = :approved_snapshot_id "
                "WHERE run_id = :run_id AND domain = :domain"
            )
        else:
            sql = (
                "UPDATE sdlc_governance_domain_approvals "
                "SET status = :status, decided_by = :decided_by, "
                "    decided_at = NOW(), note = :note "
                "WHERE run_id = :run_id AND domain = :domain"
            )
        session.execute(text(sql), params)
        session.commit()
        logger.info(
            "[SDLC-GOV] decide_domain",
            run_id=run_id,
            domain=domain.upper(),
            status=status,
            decided_by=decided_by,
        )
        return True
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] decide_domain failed run={run_id} domain={domain}: {exc}"
        )
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def all_finding_domains_approved(run_id: str) -> bool:
    """
    Returns True iff every seeded domain for the run has status='approved'
    AND there is at least one seeded domain.

    FAIL-CLOSED: any DB error → return False.
    Returns False when zero domains are seeded (nothing seeded ≠ all approved).
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] all_finding_domains_approved: no DB session — fail-closed")
        return False
    try:
        from sqlalchemy import text
        row = session.execute(
            text(
                "SELECT "
                "    COUNT(*) AS total, "
                "    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved_count "
                "FROM sdlc_governance_domain_approvals "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).fetchone()

        if row is None:
            return False

        total          = int(row.total or 0)
        approved_count = int(row.approved_count or 0)

        # Zero seeded domains → not all approved (nothing to approve)
        if total == 0:
            return False

        return total == approved_count
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] all_finding_domains_approved failed run={run_id} "
            f"(fail-closed → False): {exc}"
        )
        return False
    finally:
        session.close()


def reset_domain_to_pending(run_id: str, domain: str) -> bool:
    """
    Reset a domain's approval state back to 'pending' after a trigger-fix
    re-opens it for re-approval.  Clears decided_by, decided_at, note.
    Returns True on success, False on error.
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] reset_domain_to_pending: no DB session")
        return False
    try:
        from sqlalchemy import text
        session.execute(
            text(
                "UPDATE sdlc_governance_domain_approvals "
                "SET status = 'pending', decided_by = NULL, "
                "    decided_at = NULL, note = NULL "
                "WHERE run_id = :run_id AND domain = :domain"
            ),
            {"run_id": run_id, "domain": domain.upper()},
        )
        session.commit()
        logger.info(
            "[SDLC-GOV] reset_domain_to_pending",
            run_id=run_id,
            domain=domain.upper(),
        )
        return True
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] reset_domain_to_pending failed run={run_id} domain={domain}: {exc}"
        )
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def get_domain_approval(run_id: str, domain: str) -> Optional[dict]:
    """
    Return the approval row for a specific (run_id, domain) pair, or None if
    not found or on any error.
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] get_domain_approval: no DB session")
        return None
    try:
        from sqlalchemy import text
        row = session.execute(
            text(
                "SELECT id, run_id, domain, open_count, status, "
                "       decided_by, decided_at, note, created_at "
                "FROM sdlc_governance_domain_approvals "
                "WHERE run_id = :run_id AND domain = :domain "
                "LIMIT 1"
            ),
            {"run_id": run_id, "domain": domain.upper()},
        ).fetchone()

        if row is None:
            return None

        return {
            "id":          str(row.id),
            "run_id":      str(row.run_id),
            "domain":      row.domain,
            "open_count":  row.open_count,
            "status":      row.status,
            "decided_by":  row.decided_by,
            "decided_at":  row.decided_at.isoformat() if row.decided_at else None,
            "note":        row.note,
            "created_at":  row.created_at.isoformat() if row.created_at else None,
        }
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] get_domain_approval failed run={run_id} domain={domain}: {exc}"
        )
        return None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# GOVERNANCE-axis per-finding decisions  (table: sdlc_governance_finding_decisions)
#
# End-gate overhaul (2026-07-23, B2.4) — the TEAM sign-off axis, distinct from
# the AUTHOR disposition axis and the immutable DETECTION observations. Decision
# vocabulary is APP-enforced (there is NO DB CHECK): accept | send_back.
# Keyed on (snapshot_id, domain, fingerprint) so a decision is bound to the exact
# snapshot the team reviewed.
# ---------------------------------------------------------------------------

# Vocabulary the team sign-off enforces (no DB CHECK — see table DDL).
_VALID_DECISIONS = {"accept", "send_back"}


def record_finding_decision(
    run_id:      str,
    snapshot_id: str,
    domain:      str,
    fingerprint: str,
    decision:    str,
    comment:     Optional[str],
    decided_by:  str,
) -> bool:
    """UPSERT the GOVERNANCE-axis per-finding decision for the team sign-off gate.

    Decision is validated against {accept, send_back} (app-enforced — an invalid
    value is logged and the call is a no-op returning False). UPSERTs on
    (snapshot_id, domain, fingerprint), refreshing decision, comment, decided_by
    and decided_at=NOW() on conflict. Returns True on success, False on error.
    """
    dec = (decision or "").strip().lower()
    if dec not in _VALID_DECISIONS:
        logger.warning("[SDLC-GOV] record_finding_decision: invalid decision — skipped",
                       run_id=run_id, decision=decision)
        return False
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] record_finding_decision: no DB session", run_id=run_id)
        return False
    try:
        from sqlalchemy import text
        session.execute(
            text(
                "INSERT INTO sdlc_governance_finding_decisions "
                "(run_id, snapshot_id, domain, fingerprint, decision, comment, decided_by) "
                "VALUES (:run_id, :snapshot_id, :domain, :fingerprint, :decision, :comment, :decided_by) "
                "ON CONFLICT (snapshot_id, domain, fingerprint) DO UPDATE SET "
                "  decision   = EXCLUDED.decision, "
                "  comment    = EXCLUDED.comment, "
                "  decided_by = EXCLUDED.decided_by, "
                "  decided_at = NOW()"
            ),
            {
                "run_id":      run_id,
                "snapshot_id": snapshot_id,
                "domain":      domain.upper(),
                "fingerprint": fingerprint,
                "decision":    dec,
                "comment":     comment,
                "decided_by":  decided_by,
            },
        )
        session.commit()
        logger.info("[SDLC-GOV] record_finding_decision", run_id=run_id,
                    domain=domain.upper(), fingerprint=fingerprint, decision=dec)
        return True
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] record_finding_decision failed run={run_id} "
            f"domain={domain} fp={fingerprint}: {exc}"
        )
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def get_finding_decisions(
    run_id:      str,
    snapshot_id: str,
    domain:      Optional[str] = None,
) -> dict:
    """Return {fingerprint: {decision, comment, decided_by, decided_at}} for the
    given snapshot (optionally narrowed to one domain). Never raises — returns {}
    on any error."""
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] get_finding_decisions: no DB session", run_id=run_id)
        return {}
    try:
        from sqlalchemy import text
        where = "WHERE run_id = :run_id AND snapshot_id = :snapshot_id"
        params: dict = {"run_id": run_id, "snapshot_id": snapshot_id}
        if domain is not None:
            where += " AND domain = :domain"
            params["domain"] = domain.upper()
        rows = session.execute(
            text(
                "SELECT fingerprint, decision, comment, decided_by, decided_at "
                f"FROM sdlc_governance_finding_decisions {where}"
            ),
            params,
        ).fetchall()
        result: dict = {}
        for r in rows:
            result[r.fingerprint] = {
                "decision":   r.decision,
                "comment":    r.comment,
                "decided_by": r.decided_by,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
        return result
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] get_finding_decisions failed run={run_id} "
            f"snapshot={snapshot_id}: {exc}"
        )
        return {}
    finally:
        session.close()


def clear_send_back_decisions(
    run_id:       str,
    snapshot_id:  str,
    domain:       Optional[str] = None,
    fingerprints: Optional[list] = None,
) -> int:
    """Delete the team ``send_back`` decision rows for the CURRENT snapshot so a
    finding the author has re-addressed becomes team-decidable again WITHOUT a
    re-scan.

    Team decisions are keyed on ``(snapshot_id, domain, fingerprint)`` and are only
    naturally cleared by minting a NEW snapshot (a re-scan). When the author resolves
    a send-back on the author axis instead — marks it a false positive, or clicks
    "re-send to teams" after any triage — no new snapshot is minted, so a stale
    ``send_back`` row persists on the current snapshot. That leaves the team board
    with the accept/send-back buttons hidden ("awaiting author fix") and the domain
    permanently un-approvable (``decide_governance_domain`` 409s on un-actioned
    send-backs) → a dead-end. Clearing the ``send_back`` decision returns the finding
    to an undecided state the team can act on again.

    Scope: only ``decision='send_back'`` rows are removed (``accept`` decisions are
    never touched). Optionally narrow to one ``domain`` and/or a specific list of
    ``fingerprints``. Returns the number of rows deleted (0 on no-op or error).
    Never raises."""
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] clear_send_back_decisions: no DB session", run_id=run_id)
        return 0
    try:
        from sqlalchemy import text
        base = ("WHERE run_id = :run_id AND snapshot_id = :snapshot_id "
                "AND decision = 'send_back'")
        params: dict = {"run_id": run_id, "snapshot_id": snapshot_id}
        if domain is not None:
            base += " AND domain = :domain"
            params["domain"] = domain.upper()
        _fps = [fp for fp in (fingerprints or []) if fp]
        n = 0
        if _fps:
            # Delete each fingerprint individually — the codebase avoids the
            # ANY(:fps) bind-param with sqlalchemy.text (see set_status).
            for fp in _fps:
                res = session.execute(
                    text(f"DELETE FROM sdlc_governance_finding_decisions {base} "
                         "AND fingerprint = :fp"),
                    {**params, "fp": fp},
                )
                n += int(getattr(res, "rowcount", 0) or 0)
        else:
            res = session.execute(
                text(f"DELETE FROM sdlc_governance_finding_decisions {base}"),
                params,
            )
            n = int(getattr(res, "rowcount", 0) or 0)
        session.commit()
        logger.info("[SDLC-GOV] clear_send_back_decisions", run_id=run_id,
                    snapshot_id=snapshot_id, domain=(domain or "*"), cleared=n)
        return n
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] clear_send_back_decisions failed run={run_id} "
            f"snapshot={snapshot_id} domain={domain}: {exc}"
        )
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        session.close()


def bump_domain_send_back(run_id: str, domain: str) -> bool:
    """Bump a domain's send-back counters (iteration += 1, last_send_back_at=NOW())
    when a team member sends back a finding, routing the run back to the author
    loop. Returns True on success, False on error. Never raises."""
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] bump_domain_send_back: no DB session", run_id=run_id)
        return False
    try:
        from sqlalchemy import text
        session.execute(
            text(
                "UPDATE sdlc_governance_domain_approvals "
                "SET iteration = iteration + 1, last_send_back_at = NOW() "
                "WHERE run_id = :run_id AND domain = :domain"
            ),
            {"run_id": run_id, "domain": domain.upper()},
        )
        session.commit()
        logger.info("[SDLC-GOV] bump_domain_send_back", run_id=run_id,
                    domain=domain.upper())
        return True
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] bump_domain_send_back failed run={run_id} domain={domain}: {exc}"
        )
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


# ---------------------------------------------------------------------------
# B2.5 — per-domain, fingerprint-granular approval CARRY-FORWARD on a new snapshot
#
# Each governance re-scan mints a NEW immutable snapshot. A domain that was already
# 'approved' should NOT be blindly re-prompted: if the author's fix introduced no
# new or changed finding for that domain, the approval carries forward untouched.
# Only the domains that actually gained a new/changed finding revert to 'pending',
# and even then the still-unchanged accepts are copied forward so the B2.4 gate
# blocks a domain solely on its genuinely new/changed findings. Set-diff is on the
# line-independent gv1 fingerprints — deterministic, no heuristics, per-domain.
# ---------------------------------------------------------------------------

def evaluate_carry_forward(run_id: str, new_snapshot_id: str,
                           targeted_domains: Optional[set] = None) -> dict:
    """Re-evaluate every currently-'approved' domain against a NEW snapshot.

    ``targeted_domains`` (2026-08-03): the set of governance domains (any case)
    whose findings the current fix batch actually targeted. When provided, an
    already-approved domain that is NOT in this set is UNCONDITIONALLY carried
    forward — its approval survives the re-scan untouched (accepts copied onto the
    new snapshot, ``approved_snapshot_id`` restamped) even if the whole-diff re-scan
    re-emitted a new/changed fingerprint for it. This scopes a single-domain
    auto-fix (e.g. InfoSec) so it re-opens ONLY that domain and never spuriously
    reverts an unrelated approved domain (EA/DPDP). Targeted domains — and all
    domains when ``targeted_domains`` is None (legacy behaviour) — follow the
    CASE A / CASE B set-diff logic below.

    For each domain whose approval row status == 'approved' with a non-null
    approved_snapshot_id (= OLD_SID):

      approved_accepts = fingerprints ACCEPTED by the domain at OLD_SID
                         (get_finding_decisions(run_id, OLD_SID, domain), decision=='accept')
      new_visible      = the domain's approval-relevant (open|author_fp) fingerprints
                         at NEW_SID (snapshot_domain_visible_fingerprints)
      new_or_changed   = new_visible - approved_accepts

      CASE A (new_or_changed EMPTY — identical, or a strict subset because some
              findings were fixed/disappeared):
        CARRY FORWARD. Keep status='approved', restamp approved_snapshot_id=NEW_SID,
        and copy each still-present accepted fingerprint's accept decision into
        NEW_SID (decided_by='carry-forward') so the current-snapshot view shows them
        accepted. The approver is NOT re-prompted.

      CASE B (new_or_changed NON-EMPTY):
        REVERT the domain to status='pending', but copy the accepts for the UNCHANGED
        fingerprints (new_visible ∩ approved_accepts) into NEW_SID so ONLY the
        new_or_changed findings lack a decision → the B2.4 approve gate then blocks
        the domain solely on those.

    Domains not currently 'approved' are left untouched (already in review). Strictly
    per-domain: an IS-only change never reverts EA and vice-versa. Idempotent-safe if
    called twice on the same new snapshot (a CASE-A domain already restamped to
    NEW_SID is skipped; a CASE-B domain is now 'pending' and no longer selected).
    Never raises — logs and returns {} on any error.

    Returns {domain: {"carried_forward": bool, "new_or_changed_count": int}}.
    """
    if not run_id or not new_snapshot_id:
        return {}
    result: dict = {}
    new_sid = str(new_snapshot_id)
    try:
        from store.sdlc_governance_findings import snapshot_domain_visible_fingerprints

        # Snapshot the currently-approved domains + their bound (OLD) snapshot id in
        # one short read session; the mutating helpers below open their own sessions.
        session = _get_session()
        if session is None:
            logger.warning("[SDLC-GOV] evaluate_carry_forward: no DB session", run_id=run_id)
            return {}
        try:
            from sqlalchemy import text
            rows = session.execute(
                text(
                    "SELECT domain, approved_snapshot_id "
                    "FROM sdlc_governance_domain_approvals "
                    "WHERE run_id = :run_id AND status = 'approved' "
                    "  AND approved_snapshot_id IS NOT NULL"
                ),
                {"run_id": run_id},
            ).fetchall()
            approved_domains = [(r.domain, str(r.approved_snapshot_id)) for r in rows]
        finally:
            try:
                session.close()
            except Exception:
                pass

        # Normalize the targeted-domain filter to UPPER for comparison. None means
        # "no scoping" (legacy: every approved domain is re-evaluated).
        _targeted_up = (
            {str(d).strip().upper() for d in targeted_domains if str(d).strip()}
            if targeted_domains is not None else None
        )

        for domain, old_sid in approved_domains:
            # Idempotent: a CASE-A domain already restamped to this snapshot is done.
            if old_sid == new_sid:
                continue

            new_visible = snapshot_domain_visible_fingerprints(new_sid, domain)

            # UNTARGETED carry-forward (2026-08-03): this approved domain was not
            # touched by the current fix batch, so its sign-off must survive the
            # re-scan regardless of any new/changed fingerprint the whole-diff scan
            # re-emitted for it. Copy accepts for ALL currently-visible fingerprints
            # onto the new snapshot (so nothing is left undecided), keep the domain
            # 'approved', and restamp approved_snapshot_id → new snapshot.
            if _targeted_up is not None and (domain or "").upper() not in _targeted_up:
                for fp in new_visible:
                    record_finding_decision(
                        run_id, new_sid, domain, fp, "accept",
                        "carry-forward: domain not targeted by this fix batch",
                        "carry-forward",
                    )
                decide_domain(
                    run_id, domain, "approved", "carry-forward",
                    note="carry-forward: domain not targeted by this fix batch",
                    approved_snapshot_id=new_sid,
                )
                result[domain] = {"carried_forward": True, "new_or_changed_count": 0,
                                  "untargeted": True}
                logger.info("[SDLC-GOV] carry-forward decision (untargeted — kept approved)",
                            run_id=run_id, domain=domain, carried_forward=True,
                            new_or_changed_count=0)
                continue

            old_decisions = get_finding_decisions(run_id, old_sid, domain)
            approved_accepts = {
                fp for fp, d in (old_decisions or {}).items()
                if (d.get("decision") or "") == "accept"
            }
            new_or_changed = new_visible - approved_accepts
            unchanged = new_visible & approved_accepts

            # In BOTH cases the still-present, previously-accepted findings carry
            # their accept decision forward onto the new snapshot.
            for fp in unchanged:
                record_finding_decision(
                    run_id, new_sid, domain, fp, "accept",
                    "carry-forward: unchanged since approved snapshot",
                    "carry-forward",
                )

            if not new_or_changed:
                # CASE A — carry the whole approval forward, restamp to NEW_SID.
                decide_domain(
                    run_id, domain, "approved", "carry-forward",
                    note="carry-forward: no new or changed findings since approval",
                    approved_snapshot_id=new_sid,
                )
                result[domain] = {"carried_forward": True, "new_or_changed_count": 0}
                logger.info("[SDLC-GOV] carry-forward decision", run_id=run_id,
                            domain=domain, carried_forward=True, new_or_changed_count=0)
            else:
                # CASE B — revert to pending; only new_or_changed will lack a decision.
                # approved_snapshot_id is left as-is (the domain is now 'pending', so
                # the stale stamp is never read again and B2.4 re-stamps on re-approve).
                decide_domain(
                    run_id, domain, "pending", "carry-forward",
                    note="reverted to pending: new or changed findings since approval",
                )
                result[domain] = {"carried_forward": False,
                                  "new_or_changed_count": len(new_or_changed)}
                logger.info("[SDLC-GOV] carry-forward decision", run_id=run_id,
                            domain=domain, carried_forward=False,
                            new_or_changed_count=len(new_or_changed))
                logger.warning("[SDLC-GOV] domain reverted to pending", run_id=run_id,
                               domain=domain, approved_snapshot_id=old_sid,
                               new_snapshot_id=new_sid)
        return result
    except Exception as exc:
        logger.warning(
            f"[SDLC-GOV] evaluate_carry_forward failed run={run_id} "
            f"new_snapshot={new_sid} (returning {{}}): {exc}"
        )
        return {}
