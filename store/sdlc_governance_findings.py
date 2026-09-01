# SPDX-License-Identifier: Apache-2.0
"""
store/sdlc_governance_findings.py — per-run governance findings persistence.

Source of truth for what THIS governance run found and must eventually fix.
Keyed on (run_id, skill, fingerprint) for idempotent upserts.

All functions: parameterized queries only (never f-string SQL); never raise
(log + return safe default), matching the fail-safe idiom in apply_suppressions.
"""
from __future__ import annotations

import datetime
from typing import List, Optional

from core.logger import logger


def _get_session():
    try:
        from db.database import SessionLocal
        return SessionLocal()
    except Exception:
        return None


def persist_findings(run_id: str, findings: list, domain_by_skill: dict = None) -> int:
    """Upsert findings for a run. Returns count upserted. Never raises.

    `findings` is a list of Finding objects (from agents.sdlc_governance.schema).
    `domain_by_skill` is an optional dict mapping skill slug → domain string (for tagging).
    Upserts on (run_id, skill, fingerprint); updates all mutable columns on conflict.
    """
    if not findings:
        return 0
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] persist_findings: no DB session — findings not stored",
                       run_id=run_id)
        return 0
    try:
        from sqlalchemy import text
        from agents.sdlc_governance.schema import (
            fingerprint as compute_fingerprint,
            content_fingerprint as compute_content_key,
        )

        _domain_map = domain_by_skill or {}
        count = 0
        for f in findings:
            fp = compute_fingerprint(f)
            content_key = compute_content_key(f)
            # D2 fix (end-gate overhaul 2026-07-23): PREFER the domain the engine
            # already stamped on the finding at scan time (f.domain); only fall back
            # to the slug-keyed map, never rely solely on the map (which mis-buckets
            # findings whose skill slug isn't in the map). See engine.py:702.
            domain = (getattr(f, "domain", "") or "").strip()
            if not domain:
                domain = _domain_map.get(getattr(f, "skill", ""), None) or ""

            session.execute(
                text(
                    "INSERT INTO sdlc_governance_findings "
                    "(run_id, skill, domain, fingerprint, content_key, severity, file, line, "
                    " rule, title, detail, fix_hint, snippet, status) "
                    "VALUES (:run_id, :skill, :domain, :fingerprint, :content_key, :severity, :file, :line, "
                    "        :rule, :title, :detail, :fix_hint, :snippet, :status) "
                    "ON CONFLICT (run_id, skill, fingerprint) DO UPDATE SET "
                    "  status      = EXCLUDED.status, "
                    "  severity    = EXCLUDED.severity, "
                    "  title       = EXCLUDED.title, "
                    "  detail      = EXCLUDED.detail, "
                    "  fix_hint    = EXCLUDED.fix_hint, "
                    "  snippet     = EXCLUDED.snippet, "
                    "  content_key = EXCLUDED.content_key, "
                    "  domain      = EXCLUDED.domain"
                ),
                {
                    "run_id":      run_id,
                    "skill":       getattr(f, "skill", "") or "",
                    "domain":      domain,
                    "fingerprint": fp,
                    "content_key": content_key,
                    "severity":    getattr(f, "severity", "low") or "low",
                    "file":        getattr(f, "file", "") or "",
                    "line":        getattr(f, "line", None),
                    "rule":        getattr(f, "rule", "") or "",
                    "title":       getattr(f, "title", "") or "",
                    "detail":      getattr(f, "detail", "") or "",
                    "fix_hint":    getattr(f, "fix_hint", "") or "",
                    "snippet":     getattr(f, "snippet", "") or "",
                    "status":      getattr(f, "status", "open") or "open",
                },
            )
            count += 1

        session.commit()
        logger.info("[SDLC-GOV] persist_findings", run_id=run_id, upserted=count)
        return count
    except Exception as exc:
        logger.warning("[SDLC-GOV] persist_findings failed — findings not stored",
                       run_id=run_id, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass


def persist_snapshot(run_id: str, findings: list, *, diff_hash: str = "",
                     bundle_version: str = "", skill_versions: dict = None,
                     trigger: str = "initial", created_by: str = None,
                     domain_by_skill: dict = None) -> Optional[str]:
    """End-gate overhaul (2026-07-23) — DUAL-WRITE alongside persist_findings.

    Write ONE immutable `sdlc_governance_scan_snapshots` row (next scan_seq for the
    run, allocated atomically) plus one append-only `sdlc_governance_finding_observations`
    row per finding (the DETECTION axis — record ALL findings passed in, open AND
    suppressed). Returns the new snapshot_id (str) or None on failure. NEVER raises —
    a failure here must not break the legacy finding path (which is written separately).

    `findings` is a list of Finding objects. `domain_by_skill` maps skill slug → domain
    for tagging (falls back to each finding's `.domain`). Observations are IMMUTABLE:
    only ever INSERTed, never UPDATEd.
    """
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] persist_snapshot: no DB session — snapshot not stored",
                       run_id=run_id)
        return None
    try:
        import json as _json
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        from agents.sdlc_governance.schema import (
            fingerprint as compute_fingerprint,
            content_fingerprint as compute_content_key,
        )

        _domain_map = domain_by_skill or {}
        _sv_json = _json.dumps(skill_versions or {})

        # Allocate scan_seq atomically: compute MAX+1 in the same INSERT. On the rare
        # concurrent-insert race the UNIQUE(run_id, scan_seq) raises — retry a few times.
        snapshot_id = None
        scan_seq = None
        last_exc = None
        for _attempt in range(4):
            try:
                row = session.execute(
                    text(
                        "INSERT INTO sdlc_governance_scan_snapshots "
                        "(run_id, scan_seq, diff_hash, bundle_version, skill_versions, trigger, created_by) "
                        "SELECT :run_id, COALESCE(MAX(scan_seq), 0) + 1, :diff_hash, :bundle_version, "
                        "       CAST(:skill_versions AS JSONB), :trigger, :created_by "
                        "FROM sdlc_governance_scan_snapshots WHERE run_id = :run_id "
                        "RETURNING id, scan_seq"
                    ),
                    {
                        "run_id":         run_id,
                        "diff_hash":      diff_hash or "",
                        "bundle_version": bundle_version or "",
                        "skill_versions": _sv_json,
                        "trigger":        trigger or "initial",
                        "created_by":     created_by,
                    },
                ).fetchone()
                session.commit()
                snapshot_id = str(row.id)
                scan_seq = int(row.scan_seq)
                break
            except IntegrityError as _ie:  # scan_seq race — roll back and retry
                last_exc = _ie
                try:
                    session.rollback()
                except Exception:
                    pass
        if snapshot_id is None:
            logger.warning("[SDLC-GOV] persist_snapshot: scan_seq allocation failed",
                           run_id=run_id, error=str(last_exc))
            return None

        finding_count = 0
        for f in (findings or []):
            fp = compute_fingerprint(f)
            content_key = compute_content_key(f)
            # Same D2-aware domain policy as persist_findings: prefer f.domain.
            domain = (getattr(f, "domain", "") or "").strip()
            if not domain:
                domain = _domain_map.get(getattr(f, "skill", ""), None) or ""
            session.execute(
                text(
                    "INSERT INTO sdlc_governance_finding_observations "
                    "(snapshot_id, run_id, fingerprint, content_key, skill, domain, severity, file, line, "
                    " rule, title, detail, fix_hint, snippet) "
                    "VALUES (:snapshot_id, :run_id, :fingerprint, :content_key, :skill, :domain, :severity, "
                    "        :file, :line, :rule, :title, :detail, :fix_hint, :snippet)"
                ),
                {
                    "snapshot_id": snapshot_id,
                    "run_id":      run_id,
                    "fingerprint": fp,
                    "content_key": content_key,
                    "skill":       getattr(f, "skill", "") or "",
                    "domain":      domain,
                    "severity":    getattr(f, "severity", "low") or "low",
                    "file":        getattr(f, "file", "") or "",
                    "line":        getattr(f, "line", None),
                    "rule":        getattr(f, "rule", "") or "",
                    "title":       getattr(f, "title", "") or "",
                    "detail":      getattr(f, "detail", "") or "",
                    "fix_hint":    getattr(f, "fix_hint", "") or "",
                    "snippet":     getattr(f, "snippet", "") or "",
                },
            )
            finding_count += 1

        session.commit()
        logger.info("[SDLC-GOV] Snapshot persisted", run_id=run_id, snapshot_id=snapshot_id,
                    scan_seq=scan_seq, finding_count=finding_count, bundle_version=bundle_version)
        return snapshot_id
    except Exception as exc:
        logger.warning("[SDLC-GOV] persist_snapshot failed — snapshot not stored",
                       run_id=run_id, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def list_findings(run_id: str, status: Optional[str] = None, domain: Optional[str] = None) -> list:
    """Return findings for run as list of dicts. Never raises.

    Optional `status` and `domain` filters narrow the result set.
    """
    session = _get_session()
    if session is None:
        return []
    try:
        from sqlalchemy import text

        # Build WHERE clause with optional filters — all via bind params.
        where = "WHERE run_id = :run_id"
        params: dict = {"run_id": run_id}
        if status is not None:
            where += " AND status = :status"
            params["status"] = status
        if domain is not None:
            where += " AND domain = :domain"
            params["domain"] = domain

        rows = session.execute(
            text(
                "SELECT id, run_id, skill, domain, fingerprint, content_key, severity, file, line, "
                "       rule, title, detail, fix_hint, snippet, status, "
                "       triaged_by, triaged_at, created_at "
                f"FROM sdlc_governance_findings {where} "
                "ORDER BY created_at ASC"
            ),
            params,
        ).fetchall()

        result = []
        for r in rows:
            result.append({
                "id":          str(r.id) if r.id else None,
                "run_id":      r.run_id,
                "skill":       r.skill,
                "domain":      r.domain,
                "fingerprint": r.fingerprint,
                "content_key": r.content_key,
                "severity":    r.severity,
                "file":        r.file,
                "line":        r.line,
                "rule":        r.rule,
                "title":       r.title,
                "detail":      r.detail,
                "fix_hint":    r.fix_hint,
                "snippet":     r.snippet,
                "status":      r.status,
                "triaged_by":  r.triaged_by,
                "triaged_at":  r.triaged_at.isoformat() if r.triaged_at else None,
                "created_at":  r.created_at.isoformat() if r.created_at else None,
            })
        return result
    except Exception as exc:
        logger.warning("[SDLC-GOV] list_findings failed — returning empty list",
                       run_id=run_id, error=str(exc))
        return []
    finally:
        try:
            session.close()
        except Exception:
            pass


def set_status(run_id: str, fingerprints: list, status: str, actor: str,
               domain: Optional[str] = None) -> int:
    """Set status (open/false_positive/fixed) for findings by fingerprint.

    Returns count updated. Never raises.
    Updates each fingerprint individually to avoid the ANY(:fps) bind-param
    issue with sqlalchemy.text.
    Optional `domain` further narrows which findings are updated.
    """
    if not fingerprints:
        return 0
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] set_status: no DB session",
                       run_id=run_id, status=status, actor=actor)
        return 0
    try:
        from sqlalchemy import text

        count = 0
        for fp in fingerprints:
            params: dict = {
                "status":  status,
                "actor":   actor,
                "run_id":  run_id,
                "fp":      fp,
            }
            if domain is not None:
                result = session.execute(
                    text(
                        "UPDATE sdlc_governance_findings "
                        "SET status = :status, triaged_by = :actor, triaged_at = NOW() "
                        "WHERE run_id = :run_id AND fingerprint = :fp AND domain = :domain"
                    ),
                    {**params, "domain": domain},
                )
            else:
                result = session.execute(
                    text(
                        "UPDATE sdlc_governance_findings "
                        "SET status = :status, triaged_by = :actor, triaged_at = NOW() "
                        "WHERE run_id = :run_id AND fingerprint = :fp"
                    ),
                    params,
                )
            count += result.rowcount

        session.commit()
        logger.info("[SDLC-GOV] set_status", run_id=run_id, domain=domain,
                    count=count, actor=actor, status=status)
        return count
    except Exception as exc:
        logger.warning("[SDLC-GOV] set_status failed",
                       run_id=run_id, status=status, actor=actor, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass


def open_findings(run_id: str) -> list:
    """Return open (non-false-positive, non-fixed) Finding objects. Never raises."""
    session = _get_session()
    if session is None:
        return []
    try:
        from sqlalchemy import text
        from agents.sdlc_governance.schema import Finding

        rows = session.execute(
            text(
                "SELECT skill, severity, file, line, rule, title, detail, fix_hint, snippet "
                "FROM sdlc_governance_findings "
                "WHERE run_id = :run_id AND status = 'open' "
                "ORDER BY created_at ASC"
            ),
            {"run_id": run_id},
        ).fetchall()

        result = []
        for r in rows:
            try:
                f = Finding(
                    skill=r.skill or "",
                    severity=r.severity or "low",
                    file=r.file or "",
                    rule=r.rule or "",
                    title=r.title or "",
                    detail=r.detail or "",
                    fix_hint=r.fix_hint or "",
                    snippet=r.snippet or "",
                    line=r.line,
                    status="open",
                )
                result.append(f)
            except Exception as row_exc:
                logger.warning("[SDLC-GOV] open_findings: could not reconstruct Finding row",
                               run_id=run_id, error=str(row_exc))
        return result
    except Exception as exc:
        logger.warning("[SDLC-GOV] open_findings failed — returning empty list",
                       run_id=run_id, error=str(exc))
        return []
    finally:
        try:
            session.close()
        except Exception:
            pass


def mark_fixed(run_id: str, fingerprints: list) -> int:
    """Mark findings as fixed. Returns count updated. Never raises."""
    if not fingerprints:
        return 0
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] mark_fixed: no DB session", run_id=run_id)
        return 0
    try:
        from sqlalchemy import text

        count = 0
        for fp in fingerprints:
            result = session.execute(
                text(
                    "UPDATE sdlc_governance_findings "
                    "SET status = 'fixed' "
                    "WHERE run_id = :run_id AND fingerprint = :fp"
                ),
                {"run_id": run_id, "fp": fp},
            )
            count += result.rowcount

        session.commit()
        logger.info("[SDLC-GOV] mark_fixed", run_id=run_id, count=count)
        return count
    except Exception as exc:
        logger.warning("[SDLC-GOV] mark_fixed failed",
                       run_id=run_id, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass


def domain_open_counts(run_id: str) -> dict:
    """Return {domain: count} for open findings grouped by domain. Never raises."""
    session = _get_session()
    if session is None:
        return {}
    try:
        from sqlalchemy import text

        rows = session.execute(
            text(
                "SELECT COALESCE(domain, '') AS domain, COUNT(*) AS cnt "
                "FROM sdlc_governance_findings "
                "WHERE run_id = :run_id AND status = 'open' "
                "GROUP BY domain"
            ),
            {"run_id": run_id},
        ).fetchall()

        return {r.domain: r.cnt for r in rows}
    except Exception as exc:
        logger.warning("[SDLC-GOV] domain_open_counts failed — returning empty dict",
                       run_id=run_id, error=str(exc))
        return {}
    finally:
        try:
            session.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# AUTHOR-axis disposition CRUD  (table: sdlc_governance_finding_disposition)
#
# End-gate overhaul (2026-07-23, B2.2) — the AUTHOR remediation axis, separate
# from the immutable DETECTION axis (observations) and the GOVERNANCE axis
# (decisions). Disposition vocabulary is APP-enforced (there is NO DB CHECK):
#   open | author_fp | fix_requested | fix_confirmed
# ---------------------------------------------------------------------------

# Vocabulary the author-loop enforces (no DB CHECK — see table DDL).
_VALID_DISPOSITIONS = {"open", "author_fp", "fix_requested", "fix_confirmed"}
# The two "resolved" dispositions that remove a finding from the convergence
# open-set (author declared it a false positive, or a fix was confirmed).
_RESOLVED_DISPOSITIONS = ("author_fp", "fix_confirmed")


def set_disposition(run_id: str, fingerprints: list, disposition: str,
                    updated_by: str, fp_justification: str = None) -> int:
    """UPSERT the AUTHOR-axis disposition for findings by fingerprint.

    Disposition is validated against {open, author_fp, fix_requested,
    fix_confirmed} (app-enforced — an invalid value is logged and the whole call
    is skipped as a no-op). UPSERTs on (run_id, fingerprint), setting disposition,
    fp_justification, updated_by and updated_at=NOW() on conflict. Returns the
    count upserted. Never raises.
    """
    disp = (disposition or "").strip().lower()
    if disp not in _VALID_DISPOSITIONS:
        logger.warning("[SDLC-GOV] set_disposition: invalid disposition — skipped",
                       run_id=run_id, disposition=disposition)
        return 0
    if not fingerprints:
        return 0
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] set_disposition: no DB session",
                       run_id=run_id, disposition=disp)
        return 0
    try:
        from sqlalchemy import text

        count = 0
        for fp in fingerprints:
            if not fp:
                continue
            session.execute(
                text(
                    "INSERT INTO sdlc_governance_finding_disposition "
                    "(run_id, fingerprint, disposition, fp_justification, updated_by, updated_at) "
                    "VALUES (:run_id, :fp, :disposition, :fp_justification, :updated_by, NOW()) "
                    "ON CONFLICT (run_id, fingerprint) DO UPDATE SET "
                    "  disposition      = EXCLUDED.disposition, "
                    "  fp_justification = EXCLUDED.fp_justification, "
                    "  updated_by       = EXCLUDED.updated_by, "
                    "  updated_at       = NOW()"
                ),
                {
                    "run_id":           run_id,
                    "fp":               fp,
                    "disposition":      disp,
                    "fp_justification": fp_justification,
                    "updated_by":       updated_by,
                },
            )
            count += 1

        session.commit()
        logger.info("[SDLC-GOV] set_disposition", run_id=run_id,
                    disposition=disp, count=count, updated_by=updated_by)
        return count
    except Exception as exc:
        logger.warning("[SDLC-GOV] set_disposition failed", run_id=run_id,
                       disposition=disp, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass


def get_dispositions(run_id: str) -> dict:
    """Return {fingerprint: {disposition, fp_justification, updated_by, updated_at}}
    for a run. Never raises — returns {} on any error."""
    session = _get_session()
    if session is None:
        return {}
    try:
        from sqlalchemy import text

        rows = session.execute(
            text(
                "SELECT fingerprint, disposition, fp_justification, updated_by, updated_at "
                "FROM sdlc_governance_finding_disposition WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).fetchall()

        result: dict = {}
        for r in rows:
            result[r.fingerprint] = {
                "disposition":      r.disposition,
                "fp_justification": r.fp_justification,
                "updated_by":       r.updated_by,
                "updated_at":       r.updated_at.isoformat() if r.updated_at else None,
            }
        return result
    except Exception as exc:
        logger.warning("[SDLC-GOV] get_dispositions failed — returning empty dict",
                       run_id=run_id, error=str(exc))
        return {}
    finally:
        try:
            session.close()
        except Exception:
            pass


def bulk_insert_suppressions(rows: list, created_by: str) -> int:
    """Bulk-upsert governance suppression rows (end-gate overhaul 2026-07-23,
    B3.1 — bulk false-positive upload). Returns the count of rows inserted/updated.
    Never raises (log + return 0), matching the fail-safe idiom in this module.

    Each `row` is a dict with keys:
      product_id  — product UUID or None
      repo_name   — repo slug (required)
      skill       — real skill SLUG (the matcher key; required)
      fingerprint — gv1:… content fingerprint (required)
      rule        — optional rule id/name
      reason      — optional human note
      source      — 'uploaded' (default) | 'in_pipeline' | 'prior_run'
      pending_signoff — bool; uploaded rows arrive TRUE (INERT until a
                        governance lead signs off — the matcher ignores
                        pending rows).

    ON CONFLICT (product_id, repo_name, skill, fingerprint) re-activates the row
    and refreshes rule/reason/source/created_by, and carries pending_signoff from
    the incoming row (EXCLUDED). For an 'uploaded' batch that means the row STAYS
    pending_signoff=TRUE on conflict — an upload can never silently promote a
    finding into the suppressed set without an explicit sign-off. Parameterized
    only. Rows missing repo_name/skill/fingerprint are skipped.
    """
    if not rows:
        return 0
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] bulk_insert_suppressions: no DB session — nothing stored",
                       created_by=created_by)
        return 0
    try:
        import uuid as _uuid_mod
        from sqlalchemy import text

        count = 0
        for row in rows:
            repo = (row.get("repo_name") or "").strip()
            skill = (row.get("skill") or "").strip()
            fp = (row.get("fingerprint") or "").strip()
            if not (repo and skill and fp):
                logger.warning("[SDLC-GOV] bulk_insert_suppressions: skipping incomplete row",
                               repo=repo, skill=skill, has_fp=bool(fp))
                continue
            pending = bool(row.get("pending_signoff", True))
            session.execute(
                text(
                    "INSERT INTO sdlc_governance_suppressions "
                    "(id, product_id, repo_name, skill, fingerprint, rule, reason, "
                    " created_by, active, created_at, source, pending_signoff) "
                    "VALUES (:id, :pid, :repo, :skill, :fp, :rule, :reason, "
                    "        :created_by, TRUE, NOW(), :source, :pending) "
                    "ON CONFLICT (product_id, repo_name, skill, fingerprint) DO UPDATE SET "
                    "  active          = TRUE, "
                    "  rule            = EXCLUDED.rule, "
                    "  reason          = EXCLUDED.reason, "
                    "  source          = EXCLUDED.source, "
                    "  pending_signoff = EXCLUDED.pending_signoff, "
                    "  created_by      = EXCLUDED.created_by"
                ),
                {
                    "id":         str(_uuid_mod.uuid4()),
                    "pid":        row.get("product_id"),
                    "repo":       repo,
                    "skill":      skill,
                    "fp":         fp,
                    "rule":       row.get("rule"),
                    "reason":     row.get("reason"),
                    "created_by": created_by,
                    "source":     (row.get("source") or "uploaded").strip() or "uploaded",
                    "pending":    pending,
                },
            )
            count += 1

        session.commit()
        logger.info("[SDLC-GOV] bulk_insert_suppressions", created_by=created_by, upserted=count)
        return count
    except Exception as exc:
        logger.warning("[SDLC-GOV] bulk_insert_suppressions failed — nothing stored",
                       created_by=created_by, error=str(exc))
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass


def latest_snapshot(run_id: str) -> Optional[dict]:
    """Return the LATEST (highest scan_seq) snapshot row for a run as a dict, or
    None if the run has no snapshot yet. Never raises."""
    session = _get_session()
    if session is None:
        return None
    try:
        from sqlalchemy import text

        row = session.execute(
            text(
                "SELECT id, scan_seq, diff_hash, bundle_version, trigger, created_by, created_at "
                "FROM sdlc_governance_scan_snapshots "
                "WHERE run_id = :run_id ORDER BY scan_seq DESC LIMIT 1"
            ),
            {"run_id": run_id},
        ).fetchone()
        if row is None:
            return None
        return {
            "id":             str(row.id),
            "scan_seq":       int(row.scan_seq),
            "diff_hash":      row.diff_hash,
            "bundle_version": row.bundle_version,
            "trigger":        row.trigger,
            "created_by":     row.created_by,
            "created_at":     row.created_at.isoformat() if row.created_at else None,
        }
    except Exception as exc:
        logger.warning("[SDLC-GOV] latest_snapshot failed — returning None",
                       run_id=run_id, error=str(exc))
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


def current_findings(run_id: str) -> list:
    """Return the CURRENT findings projection for a run: the observations of the
    LATEST snapshot only, joined with the AUTHOR-axis disposition (open |
    author_fp | fix_requested | fix_confirmed, defaulting to 'open') and the
    GOVERNANCE-axis decision (accept | send_back, nullable — B2.4 may not have
    run yet). Historical snapshots are untouched and remain queryable for audit;
    this function only ever reads the latest one. Never raises — returns []
    (caller falls back to the legacy `list_findings` read) on no-snapshot or
    any error.
    """
    snap = latest_snapshot(run_id)
    if snap is None:
        return []
    session = _get_session()
    if session is None:
        logger.warning("[SDLC-GOV] current_findings: no DB session", run_id=run_id)
        return []
    try:
        from sqlalchemy import text

        rows = session.execute(
            text(
                "SELECT o.fingerprint, o.content_key, o.skill, o.domain, o.severity, o.file, o.line, "
                "       o.rule, o.title, o.detail, o.fix_hint, o.snippet, "
                "       COALESCE(disp.disposition, 'open') AS disposition, "
                "       disp.fp_justification AS fp_justification, "
                "       dec.decision AS decision, dec.comment AS decision_comment "
                "FROM sdlc_governance_finding_observations o "
                "LEFT JOIN sdlc_governance_finding_disposition disp "
                "  ON disp.run_id = o.run_id AND disp.fingerprint = o.fingerprint "
                "LEFT JOIN sdlc_governance_finding_decisions dec "
                "  ON dec.snapshot_id = o.snapshot_id AND dec.domain = o.domain "
                "  AND dec.fingerprint = o.fingerprint "
                "WHERE o.snapshot_id = :sid "
                "ORDER BY o.created_at ASC"
            ),
            {"sid": snap["id"]},
        ).fetchall()

        result = []
        for r in rows:
            result.append({
                "fingerprint":       r.fingerprint,
                "content_key":       r.content_key,
                "skill":             r.skill,
                "domain":            r.domain,
                "severity":          r.severity,
                "file":              r.file,
                "line":              r.line,
                "rule":              r.rule,
                "title":             r.title,
                "detail":            r.detail,
                "fix_hint":          r.fix_hint,
                "snippet":           r.snippet,
                "disposition":       r.disposition,
                "fp_justification":  r.fp_justification,
                "decision":          r.decision,
                "decision_comment":  r.decision_comment,
                "snapshot_id":       snap["id"],
                "scan_seq":          snap["scan_seq"],
            })
        logger.info("[SDLC-GOV] current_findings", run_id=run_id, snapshot_id=snap["id"],
                    scan_seq=snap["scan_seq"], count=len(result))
        return result
    except Exception as exc:
        logger.warning("[SDLC-GOV] current_findings failed — returning empty list",
                       run_id=run_id, error=str(exc))
        return []
    finally:
        try:
            session.close()
        except Exception:
            pass


def current_findings_checked(run_id: str) -> tuple:
    """Return ``(rows, True)`` on a successful read of the latest-snapshot
    projection (including when the result is genuinely empty — ``([], True)``
    is correct for a run with no snapshot or zero observations).  Returns
    ``([], False)`` on ANY exception (DB error, driver failure, missing
    session, etc.).  Never raises.

    Callers that need to distinguish *"the DB said there are zero findings"*
    from *"the DB read failed"* should use this function instead of the
    lenient :func:`current_findings`, which collapses both into ``[]``.
    ``current_findings`` is intentionally left byte-for-byte identical.
    """
    try:
        snap = latest_snapshot(run_id)
        if snap is None:
            # No snapshot yet — the run may be brand-new; this is not an error.
            return ([], True)

        session = _get_session()
        if session is None:
            logger.warning(
                "[SDLC-GOV] current_findings_checked: no DB session",
                run_id=run_id,
            )
            return ([], False)

        try:
            from sqlalchemy import text

            rows = session.execute(
                text(
                    "SELECT o.fingerprint, o.content_key, o.skill, o.domain, o.severity, o.file, o.line, "
                    "       o.rule, o.title, o.detail, o.fix_hint, o.snippet, "
                    "       COALESCE(disp.disposition, 'open') AS disposition, "
                    "       disp.fp_justification AS fp_justification, "
                    "       dec.decision AS decision, dec.comment AS decision_comment "
                    "FROM sdlc_governance_finding_observations o "
                    "LEFT JOIN sdlc_governance_finding_disposition disp "
                    "  ON disp.run_id = o.run_id AND disp.fingerprint = o.fingerprint "
                    "LEFT JOIN sdlc_governance_finding_decisions dec "
                    "  ON dec.snapshot_id = o.snapshot_id AND dec.domain = o.domain "
                    "  AND dec.fingerprint = o.fingerprint "
                    "WHERE o.snapshot_id = :sid "
                    "ORDER BY o.created_at ASC"
                ),
                {"sid": snap["id"]},
            ).fetchall()

            result = []
            for r in rows:
                result.append({
                    "fingerprint":       r.fingerprint,
                    "content_key":       r.content_key,
                    "skill":             r.skill,
                    "domain":            r.domain,
                    "severity":          r.severity,
                    "file":              r.file,
                    "line":              r.line,
                    "rule":              r.rule,
                    "title":             r.title,
                    "detail":            r.detail,
                    "fix_hint":          r.fix_hint,
                    "snippet":           r.snippet,
                    "disposition":       r.disposition,
                    "fp_justification":  r.fp_justification,
                    "decision":          r.decision,
                    "decision_comment":  r.decision_comment,
                    "snapshot_id":       snap["id"],
                    "scan_seq":          snap["scan_seq"],
                })
            logger.info(
                "[SDLC-GOV] current_findings_checked",
                run_id=run_id,
                snapshot_id=snap["id"],
                scan_seq=snap["scan_seq"],
                count=len(result),
            )
            return (result, True)
        except Exception as exc:
            logger.warning(
                "[SDLC-GOV] current_findings_checked failed — returning ([], False)",
                run_id=run_id,
                error=str(exc),
            )
            return ([], False)
        finally:
            try:
                session.close()
            except Exception:
                pass
    except Exception as exc:
        # Outer guard: latest_snapshot() or any other pre-query step raised.
        logger.warning(
            "[SDLC-GOV] current_findings_checked outer error — returning ([], False)",
            run_id=run_id,
            error=str(exc),
        )
        return ([], False)


def open_fingerprint_set(run_id: str, snapshot_id: str = None) -> set:
    """Return the set of fingerprints that are OPEN for convergence purposes.

    = the fingerprints OBSERVED in the given snapshot (or the run's LATEST
    snapshot when snapshot_id is None) whose AUTHOR disposition is NOT in
    {author_fp, fix_confirmed}. Findings with no disposition row default to
    'open' (still counted). Used to compute the convergence open-set hash in the
    author remediation loop. Never raises — returns an empty set on any error.
    """
    session = _get_session()
    if session is None:
        return set()
    try:
        from sqlalchemy import text

        sid = snapshot_id
        if sid is None:
            row = session.execute(
                text(
                    "SELECT id FROM sdlc_governance_scan_snapshots "
                    "WHERE run_id = :run_id ORDER BY scan_seq DESC LIMIT 1"
                ),
                {"run_id": run_id},
            ).fetchone()
            if row is None:
                return set()
            sid = str(row.id)

        rows = session.execute(
            text(
                "SELECT DISTINCT o.fingerprint "
                "FROM sdlc_governance_finding_observations o "
                "LEFT JOIN sdlc_governance_finding_disposition d "
                "  ON d.run_id = o.run_id AND d.fingerprint = o.fingerprint "
                "WHERE o.snapshot_id = :sid "
                "  AND COALESCE(d.disposition, 'open') NOT IN ('author_fp', 'fix_confirmed')"
            ),
            {"sid": sid},
        ).fetchall()
        return {r.fingerprint for r in rows}
    except Exception as exc:
        logger.warning("[SDLC-GOV] open_fingerprint_set failed — returning empty set",
                       run_id=run_id, error=str(exc))
        return set()
    finally:
        try:
            session.close()
        except Exception:
            pass


def snapshot_domain_visible_fingerprints(snapshot_id: str, domain: str) -> set:
    """Return the "approval-relevant" fingerprint set for one domain in one snapshot
    (B2.5 carry-forward). = the fingerprints of that domain's observations in the
    given snapshot whose current run-scoped AUTHOR disposition (COALESCE default
    'open') is in {open, author_fp} — i.e. the VISIBLE items still needing sign-off.
    fix_confirmed / fix_requested and absent findings are excluded. The run_id used
    to join the (run-scoped) disposition axis comes from the observation rows
    themselves. Domain matched case-insensitively (approvals store UPPER). Never
    raises — returns an empty set on any error.
    """
    if not snapshot_id or not domain:
        return set()
    session = _get_session()
    if session is None:
        return set()
    try:
        from sqlalchemy import text

        rows = session.execute(
            text(
                "SELECT DISTINCT o.fingerprint "
                "FROM sdlc_governance_finding_observations o "
                "LEFT JOIN sdlc_governance_finding_disposition d "
                "  ON d.run_id = o.run_id AND d.fingerprint = o.fingerprint "
                "WHERE o.snapshot_id = :sid "
                "  AND UPPER(o.domain) = UPPER(:domain) "
                "  AND COALESCE(d.disposition, 'open') IN ('open', 'author_fp')"
            ),
            {"sid": snapshot_id, "domain": domain},
        ).fetchall()
        return {r.fingerprint for r in rows}
    except Exception as exc:
        logger.warning("[SDLC-GOV] snapshot_domain_visible_fingerprints failed — empty set",
                       snapshot_id=snapshot_id, domain=domain, error=str(exc))
        return set()
    finally:
        try:
            session.close()
        except Exception:
            pass
