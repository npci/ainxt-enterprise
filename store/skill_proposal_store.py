# SPDX-License-Identifier: MIT
# ============================================================
# SKILL PROPOSAL STORE — Postgres (ainxt.skill_proposals)
#
# Durable audit of every auto-synthesized skill PROPOSAL from the
# self-improving skill loop, plus its HITL outcome. See db/migrate.py
# _part_w2_skill_proposals_2026_06_14 and workers/skill_loop_worker.py.
#
# Run signatures themselves are ephemeral (Redis db=1, store/skill_loop_store.py);
# only threshold-crossing signatures become a row here.
# ============================================================
from __future__ import annotations

import json
import uuid
from typing import List, Optional

import sqlalchemy as sa

from core.logger import logger
from db.database import SessionLocal


def open_proposal_exists(signature: str) -> bool:
    """True if there is already an open (PROPOSED) proposal for this signature.
    Dedup gate B — prevents re-proposing while one awaits resolution."""
    db = SessionLocal()
    try:
        row = db.execute(
            sa.text(
                "SELECT 1 FROM skill_proposals "
                "WHERE signature = :sig AND status = 'PROPOSED' LIMIT 1"
            ),
            {"sig": signature},
        ).first()
        return row is not None
    except Exception as e:
        logger.warning(f"[SkillProposal] open_proposal_exists failed: {e}")
        # Fail-safe: assume one exists so we DON'T spam duplicate proposals.
        return True
    finally:
        db.close()


def create_proposal(
    *,
    signature: str,
    proposed_name: str,
    source: str,
    department: str = "",
    occurrence_count: int = 0,
    representative_prompt: str = "",
    tool_sequence: Optional[list] = None,
    skill_type: str = "execution",
    synthesized_code: str = "",
    compliance_findings: Optional[list] = None,
    skill_name: Optional[str] = None,
    status: str = "PROPOSED",
) -> str:
    """Insert a proposal row. Returns the row id, or "" on failure."""
    pid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.execute(
            sa.text(
                "INSERT INTO skill_proposals "
                "(id, signature, proposed_name, skill_name, skill_type, source, "
                " department, occurrence_count, representative_prompt, tool_sequence, "
                " synthesized_code, compliance_findings, status, created_at) "
                "VALUES (:id, :sig, :pname, :sname, :stype, :source, :dept, :cnt, "
                " :rprompt, CAST(:tseq AS JSONB), :code, CAST(:findings AS JSONB), "
                " :status, NOW())"
            ),
            {
                "id":      pid,
                "sig":     signature,
                "pname":   proposed_name[:255],
                "sname":   (skill_name or None),
                "stype":   skill_type,
                "source":  source,
                "dept":    (department or None),
                "cnt":     int(occurrence_count or 0),
                "rprompt": representative_prompt or "",
                "tseq":    json.dumps(list(tool_sequence or [])),
                "code":    synthesized_code or "",
                "findings": json.dumps(list(compliance_findings or [])),
                "status":  status,
            },
        )
        db.commit()
        return pid
    except Exception as e:
        db.rollback()
        logger.warning(f"[SkillProposal] create_proposal failed: {e}")
        return ""
    finally:
        db.close()


def resolve_proposal(signature: str, status: str) -> None:
    """Mark the open proposal for a signature resolved with a terminal status
    (SKILL_CREATED / DISCARDED_COMPLIANCE / DISCARDED_DUP / REJECTED)."""
    db = SessionLocal()
    try:
        db.execute(
            sa.text(
                "UPDATE skill_proposals SET status = :status, resolved_at = NOW() "
                "WHERE signature = :sig AND status = 'PROPOSED'"
            ),
            {"status": status, "sig": signature},
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[SkillProposal] resolve_proposal failed: {e}")
    finally:
        db.close()


def resolve_by_skill_name(skill_name: str, status: str) -> None:
    """Resolve the proposal that produced a given skill (used by the governance
    reject hook, which only knows the skill name)."""
    db = SessionLocal()
    try:
        db.execute(
            sa.text(
                "UPDATE skill_proposals SET status = :status, resolved_at = NOW() "
                "WHERE skill_name = :sname AND resolved_at IS NULL"
            ),
            {"status": status, "sname": skill_name},
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[SkillProposal] resolve_by_skill_name failed: {e}")
    finally:
        db.close()


def list_proposals(status: Optional[str] = None, department: Optional[str] = None,
                   limit: int = 100) -> List[dict]:
    """List proposals for the admin/observability surface."""
    db = SessionLocal()
    try:
        clauses, params = [], {"lim": int(limit)}
        if status:
            clauses.append("status = :status")
            params["status"] = status
        if department:
            clauses.append("department = :dept")
            params["dept"] = department
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.execute(
            sa.text(
                "SELECT id, signature, proposed_name, skill_name, skill_type, source, "
                "department, occurrence_count, representative_prompt, tool_sequence, "
                "status, created_at, resolved_at "
                f"FROM skill_proposals{where} ORDER BY created_at DESC LIMIT :lim"
            ),
            params,
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[SkillProposal] list_proposals failed: {e}")
        return []
    finally:
        db.close()
