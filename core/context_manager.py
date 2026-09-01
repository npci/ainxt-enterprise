# SPDX-License-Identifier: Apache-2.0
"""
Engineer Work Context Manager — P1-NEW-1

Maintains a rolling, cross-session context summary per engineer.
Rebuilt after each session via LLM summarisation (≤500 tokens).
Used to cold-start the next session with relevant context.

Integration points:
  - chat_worker.py: inject ctx at session start, rebuild at session end
  - Called with user_id (from JWT payload)
"""

import json
from datetime import datetime, timezone
from typing import Optional

from core.logger import logger

_MAX_ACTIVE_REPOS     = 10
_MAX_ACTIVE_TICKETS   = 20
_MAX_RECENT_FILES     = 30
_MAX_RECENT_DECISIONS = 20


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_db():
    from db.database import SessionLocal
    return SessionLocal()


def _load_record(user_id: str):
    """Return (db_session, EngineerWorkContext | None)."""
    from db.models import EngineerWorkContext
    db = _get_db()
    rec = db.query(EngineerWorkContext).filter(
        EngineerWorkContext.user_id == user_id
    ).first()
    return db, rec


# ── Public API ────────────────────────────────────────────────────────────────

def get_context_for_session(user_id: str) -> str:
    """
    Return a formatted string injected at the top of the first message in a
    new session.  Pulls the persisted rolling summary + recent work signals.

    Returns "" if no context exists yet (first ever session).
    """
    if not user_id or user_id == "default":
        return ""
    try:
        db, rec = _load_record(user_id)
        try:
            if rec is None:
                return ""

            parts = []
            if rec.summary:
                parts.append(f"[Engineer context from previous sessions]\n{rec.summary}")

            if rec.active_repos:
                repos = ", ".join((rec.active_repos or [])[:5])
                parts.append(f"Recently active repos: {repos}")

            if rec.active_tickets:
                tickets = ", ".join(str(t) for t in (rec.active_tickets or [])[:5])
                parts.append(f"Open tickets: {tickets}")

            if rec.recent_files:
                files = ", ".join((rec.recent_files or [])[:6])
                parts.append(f"Recently touched files: {files}")

            if not parts:
                return ""

            return "\n".join(parts) + "\n"
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"context_manager.get_context_for_session failed: {e}")
        return ""


def update_context_after_session(
    user_id: str,
    question: str,
    answer: str,
    repo_filter: Optional[str] = None,
    context_chunks: Optional[list] = None,
) -> None:
    """
    Called after a chat turn completes.  Extracts signals (repo, file paths)
    and schedules a background summary rebuild every 5 turns.

    Non-blocking: all errors are swallowed.
    """
    if not user_id or user_id == "default":
        return
    try:
        from db.models import EngineerWorkContext
        db = _get_db()
        try:
            rec = db.query(EngineerWorkContext).filter(
                EngineerWorkContext.user_id == user_id
            ).first()

            if rec is None:
                from uuid import uuid4
                rec = EngineerWorkContext(
                    id=str(uuid4()),
                    user_id=user_id,
                    summary="",
                    active_repos=[],
                    active_tickets=[],
                    recent_files=[],
                    recent_decisions=[],
                    session_count=0,
                )
                db.add(rec)

            # ── Update signals ─────────────────────────────────────────
            active_repos = list(rec.active_repos or [])
            if repo_filter and repo_filter not in active_repos:
                active_repos.insert(0, repo_filter)
            rec.active_repos = active_repos[:_MAX_ACTIVE_REPOS]

            # Extract file paths mentioned in context chunks
            recent_files = list(rec.recent_files or [])
            if context_chunks:
                for chunk in context_chunks:
                    if isinstance(chunk, str) and chunk.startswith("[Source: "):
                        end = chunk.find("]")
                        if end > 9:
                            fp = chunk[9:end].strip()
                            if fp and fp not in recent_files:
                                recent_files.insert(0, fp)
            rec.recent_files = recent_files[:_MAX_RECENT_FILES]

            # Extract Jira ticket references from question
            import re as _re
            ticket_re = _re.compile(r"\b([A-Z]{2,10}-\d+)\b")
            found_tickets = ticket_re.findall(question)
            if found_tickets:
                active_tickets = list(rec.active_tickets or [])
                for t in found_tickets:
                    if t not in active_tickets:
                        active_tickets.insert(0, t)
                rec.active_tickets = active_tickets[:_MAX_ACTIVE_TICKETS]

            rec.session_count = (rec.session_count or 0) + 1
            rec.last_session_at = _now()
            rec.updated_at = _now()

            db.commit()

            # Rebuild LLM summary every 5 turns (fire-and-forget)
            if rec.session_count % 5 == 0:
                _rebuild_summary_async(user_id, question, answer)

        finally:
            db.close()
    except Exception as e:
        logger.debug(f"context_manager.update_context_after_session failed: {e}")


def _rebuild_summary_async(user_id: str, latest_q: str, latest_a: str) -> None:
    """
    Regenerate the rolling summary for this engineer using the LLM.
    Runs in a daemon thread so it never blocks the chat response.
    """
    import threading
    t = threading.Thread(
        target=_rebuild_summary,
        args=(user_id, latest_q, latest_a),
        daemon=True,
    )
    t.start()


def _rebuild_summary(user_id: str, latest_q: str, latest_a: str) -> None:
    """
    Synchronous LLM summarisation — runs in background thread.
    Updates `engineer_work_context.summary` in Postgres.
    """
    try:
        from db.models import EngineerWorkContext
        db = _get_db()
        try:
            rec = db.query(EngineerWorkContext).filter(
                EngineerWorkContext.user_id == user_id
            ).first()
            if rec is None:
                return

            existing_summary = rec.summary or ""
            repos    = ", ".join((rec.active_repos or [])[:5])
            tickets  = ", ".join(str(t) for t in (rec.active_tickets or [])[:5])
            files    = ", ".join((rec.recent_files or [])[:8])

            prompt = (
                "You are maintaining a rolling work context for a software engineer.\n"
                "Update the context summary based on new information.\n\n"
                f"Existing summary:\n{existing_summary}\n\n"
                f"Active repos: {repos}\n"
                f"Open tickets: {tickets}\n"
                f"Recent files: {files}\n\n"
                f"Latest question: {latest_q[:400]}\n"
                f"Latest answer excerpt: {latest_a[:400]}\n\n"
                "Produce a NEW rolling summary (max 3 sentences, plain text, no markdown) "
                "capturing: current focus area, key decisions, and any open blockers. "
                "Be specific about repos, tickets, and file names."
            )

            from models.model_router import model_router
            new_summary = model_router.generate(prompt, model_hint="simple")
            if new_summary:
                rec.summary    = new_summary[:1000]
                rec.updated_at = _now()
                db.commit()
                logger.debug(
                    f"context_manager: rebuilt summary for user={user_id} "
                    f"({len(new_summary)} chars)"
                )
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"context_manager._rebuild_summary failed: {e}")
