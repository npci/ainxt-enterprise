# SPDX-License-Identifier: MIT
# ============================================================
# SKILL LOOP WORKER — self-improving skill loop (propose → HITL approval)
#
# Periodic, OUT-OF-REQUEST-PATH detector. Scans the successful-run signatures
# captured by store/skill_loop_store.py (Redis db=1); when a signature repeats
# >= SKILL_LOOP_THRESHOLD, it:
#   1. dedups against existing skills (gate A) + open proposals (gate B)
#   2. synthesizes a candidate skill via services.skill_synthesis (the SAME
#      generator the /skills/generate endpoint uses)
#   3. compliance-scans the synthesized code — discards on a hard block
#   4. creates a SkillRecord as PENDING_APPROVAL (never PRODUCTION) +
#      a durable skill_proposals row
#   5. notifies approvers via the existing governance inbox broadcast
#
# The loop NEVER promotes to PRODUCTION — the governance state machine
# (approve → promote) is the only path, gated to approvers. This is the
# compliance guarantee.
#
# Entry points:
#   detect_and_propose(payload=None) — the RQ job (heavy: clustering + LLM)
#   enqueue_detect()                 — cron-thread hook; enqueues the RQ job so
#                                      the heavy work runs on a shared worker
# ============================================================
from __future__ import annotations

import re as _re

from core.logger import logger


def enqueue_detect() -> None:
    """Cron-thread hook (workers/start_workers.py interval_jobs). Enqueues the
    detector onto Q_DEFAULT so the LLM/clustering work runs on a shared RQ
    worker rather than blocking the single cron scheduler thread."""
    try:
        from core.config import ENABLE_SKILL_LOOP
        if not ENABLE_SKILL_LOOP:
            return
        from core.job_queue import enqueue_job, Q_DEFAULT
        enqueue_job("workers.skill_loop_worker.detect_and_propose", {}, queue_name=Q_DEFAULT,
                    timeout=600, retry_count=0)
    except Exception as e:
        logger.warning(f"[SkillLoop] enqueue_detect failed: {e}")


def _derive_skill_name(key: str, representative_prompt: str) -> str:
    """Build a stable snake_case skill name from the source anchor + top tokens."""
    from store.skill_loop_store import _normalize_intent, _STOP
    base_tokens = [t for t in _normalize_intent(representative_prompt).split() if t not in _STOP]
    head = "_".join(base_tokens[:4]) if base_tokens else "task"
    head = _re.sub(r"[^a-z0-9_]", "", head).strip("_") or "task"
    return f"auto_{head}"[:60]


def _dedup_against_existing_skills(proposed_name: str, representative_prompt: str) -> bool:
    """Dedup gate A. True if an existing skill already covers this — by exact
    name OR by strong token overlap on the description. Conservative: when in
    doubt we DON'T dedup (the HITL approver is the final filter)."""
    try:
        from db.database import SessionLocal
        from db.models import SkillRecord
        from store.skill_loop_store import _normalize_intent

        want = set(_normalize_intent(representative_prompt).split())
        db = SessionLocal()
        try:
            if db.query(SkillRecord).filter(SkillRecord.name == proposed_name).first():
                return True
            if not want:
                return False
            # Compare token overlap against existing skill descriptions.
            for (desc,) in db.query(SkillRecord.description).filter(
                SkillRecord.description.isnot(None)
            ).limit(2000):
                have = set(_normalize_intent(desc or "").split())
                if not have:
                    continue
                overlap = len(want & have) / max(1, len(want))
                if overlap >= 0.8:
                    return True
            return False
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[SkillLoop] dedup gate A skipped (treating as no-dup): {e}")
        return False


def _create_pending_skill(name: str, description: str, code: str, skill_type: str,
                          department: str) -> bool:
    """Create the SkillRecord as PENDING_APPROVAL (never PRODUCTION). Returns
    True on success."""
    try:
        from db.database import SessionLocal
        from db.models import SkillRecord
        db = SessionLocal()
        try:
            if db.query(SkillRecord).filter(SkillRecord.name == name).first():
                return False  # race: already created
            tags = ["skill-loop", "auto-proposed"]
            if department:
                tags.append(department)
            rec = SkillRecord(
                name=name,
                description=description,
                code=code,
                skill_type=skill_type,
                tools=[],
                tags=tags,
                status="PENDING_APPROVAL",
                created_by="platform-skill-loop",
                department=department or None,
                is_production=False,
                visibility="private",
            )
            db.add(rec)
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logger.warning(f"[SkillLoop] create PENDING skill {name!r} failed: {e}")
            return False
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[SkillLoop] _create_pending_skill error: {e}")
        return False


def detect_and_propose(payload=None) -> dict:
    """RQ job: detect repeated successful runs and PROPOSE candidate skills.

    Never raises — returns a small observability summary:
      {scanned, candidates, proposed, discarded}
    """
    summary = {"scanned": 0, "candidates": 0, "proposed": 0, "discarded": 0}
    try:
        from core.config import (
            ENABLE_SKILL_LOOP, SKILL_LOOP_THRESHOLD, SKILL_LOOP_WINDOW_SEC,
            SKILL_LOOP_MAX_PROPOSALS_PER_RUN, SKILL_LOOP_SOURCES,
        )
        if not ENABLE_SKILL_LOOP:
            return summary

        from store.skill_loop_store import iter_hot_signatures, clear_signature
        from store.skill_proposal_store import open_proposal_exists, create_proposal
        from services.skill_synthesis import synthesize_skill
        from agents.compliance_engine import compliance_engine

        hot = iter_hot_signatures(SKILL_LOOP_THRESHOLD, SKILL_LOOP_WINDOW_SEC)
        summary["scanned"] = len(hot)
        allowed_sources = set(SKILL_LOOP_SOURCES or [])

        for cand in hot:
            if summary["proposed"] >= SKILL_LOOP_MAX_PROPOSALS_PER_RUN:
                logger.info(
                    f"[SkillLoop] per-run proposal cap ({SKILL_LOOP_MAX_PROPOSALS_PER_RUN}) "
                    f"reached — {len(hot) - summary['candidates']} candidate(s) deferred to next tick"
                )
                break

            source = cand.get("source", "")
            if allowed_sources and source not in allowed_sources:
                continue
            summary["candidates"] += 1

            sig   = cand["signature"]
            dept  = cand.get("department", "")
            prompt = cand.get("representative_prompt", "")
            tools = cand.get("tool_sequence", [])

            # Dedup gate B — already an open proposal for this signature.
            if open_proposal_exists(sig):
                continue

            proposed_name = _derive_skill_name(cand.get("key", ""), prompt)

            # Dedup gate A — an existing skill already covers this.
            if _dedup_against_existing_skills(proposed_name, prompt):
                create_proposal(
                    signature=sig, proposed_name=proposed_name, source=source,
                    department=dept, occurrence_count=cand.get("count", 0),
                    representative_prompt=prompt, tool_sequence=tools,
                    status="DISCARDED_DUP",
                )
                clear_signature(sig, dept)
                summary["discarded"] += 1
                continue

            # Build a description from the redacted prompt + observed tools.
            tool_hint = f" Typically uses tools: {', '.join(tools)}." if tools else ""
            description = (
                f"Reusable skill auto-proposed from {cand.get('count', 0)} repeated "
                f"successful runs.{tool_hint} Representative task: {prompt[:400]}"
            )

            # Open the proposal row FIRST (claims the signature via the partial
            # unique index, blocking a concurrent tick from double-proposing).
            pid = create_proposal(
                signature=sig, proposed_name=proposed_name, source=source,
                department=dept, occurrence_count=cand.get("count", 0),
                representative_prompt=prompt, tool_sequence=tools,
                status="PROPOSED",
            )
            if not pid:
                # Lost the race (unique index) or DB error — skip quietly.
                continue

            # Synthesize the skill body (LLM via the shared generator).
            try:
                syn = synthesize_skill(proposed_name, description, "execution", dept)
                code = syn["code"]
                skill_type = syn["skill_type"]
            except Exception as e:
                # Transient (LLM down/timeout): release the claim, KEEP the
                # signature so it retries on a later tick. Don't pollute audit.
                logger.warning(f"[SkillLoop] synthesis failed for {proposed_name!r} (will retry): {e}")
                db_delete_proposal(pid)
                continue

            # Compliance HARD gate on the synthesized code.
            try:
                chk = compliance_engine.validate_input(code)
                findings = chk.get("findings", [])
                if compliance_engine.should_block(findings):
                    blocked = [f.get("type") for f in findings if f.get("type")]
                    logger.warning(f"[SkillLoop] synthesized skill {proposed_name!r} "
                                   f"DISCARDED on compliance: {blocked}")
                    # Record the discard with findings; never create a SkillRecord.
                    db_update_proposal_discarded(pid, findings)
                    clear_signature(sig, dept)
                    summary["discarded"] += 1
                    continue
            except Exception as e:
                # Fail-CLOSED: cannot verify synthesized code → do not create a
                # skill. Treat as transient (release claim, keep signature).
                logger.warning(f"[SkillLoop] compliance check failed for {proposed_name!r} "
                               f"(fail-closed, will retry): {e}")
                db_delete_proposal(pid)
                continue

            # Create the PENDING_APPROVAL skill + finalize the proposal.
            if not _create_pending_skill(proposed_name, description, code, skill_type, dept):
                # Name collision / race — release claim, dedup gate A will catch
                # it next time if a skill now exists.
                db_delete_proposal(pid)
                continue

            db_finalize_proposal(pid, proposed_name, code)

            # Notify approvers via the existing governance broadcast (ad_level<=3/admin).
            try:
                from routers.governance_router import _governance_notify
                _governance_notify("skills", proposed_name, "submit",
                                   "DRAFT", "PENDING_APPROVAL", actor="platform-skill-loop")
            except Exception as e:
                logger.debug(f"[SkillLoop] approver notify failed (non-fatal): {e}")

            clear_signature(sig, dept)
            summary["proposed"] += 1
            logger.info(f"[SkillLoop] proposed skill {proposed_name!r} "
                        f"(sig={sig[:8]}, count={cand.get('count')}, dept={dept or '-'})")

        if summary["proposed"] or summary["discarded"]:
            logger.info(f"[SkillLoop] tick done: {summary}")
        return summary
    except Exception as e:
        logger.warning(f"[SkillLoop] detect_and_propose error: {e}")
        return summary


# ── Small proposal-row mutators (kept here to avoid widening the store API) ──

def db_update_proposal_discarded(pid: str, findings: list) -> None:
    """Mark a proposal DISCARDED_COMPLIANCE and attach the (type-only) findings."""
    try:
        import json
        import sqlalchemy as sa
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            safe = [{"type": f.get("type")} for f in (findings or []) if f.get("type")]
            db.execute(
                sa.text(
                    "UPDATE skill_proposals SET status = 'DISCARDED_COMPLIANCE', "
                    "compliance_findings = CAST(:f AS JSONB), resolved_at = NOW() "
                    "WHERE id = :id"
                ),
                {"f": json.dumps(safe), "id": pid},
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[SkillLoop] db_update_proposal_discarded skipped: {e}")


def db_delete_proposal(pid: str) -> None:
    """Delete a PROPOSED row to release the signature claim on a transient
    failure (LLM down, unverifiable). The signature stays in Redis so the
    proposal is re-attempted on a later tick."""
    try:
        import sqlalchemy as sa
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text("DELETE FROM skill_proposals WHERE id = :id AND status = 'PROPOSED'"),
                {"id": pid},
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[SkillLoop] db_delete_proposal skipped: {e}")


def db_finalize_proposal(pid: str, skill_name: str, code: str) -> None:
    """Mark a proposal SKILL_CREATED and link it to the created SkillRecord."""
    try:
        import sqlalchemy as sa
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text(
                    "UPDATE skill_proposals SET status = 'SKILL_CREATED', "
                    "skill_name = :sname, synthesized_code = :code, resolved_at = NOW() "
                    "WHERE id = :id"
                ),
                {"sname": skill_name, "code": code, "id": pid},
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[SkillLoop] db_finalize_proposal skipped: {e}")
