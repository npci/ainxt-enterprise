# SPDX-License-Identifier: MIT
"""
RQ job: runs when an @AiNxt mention is detected in a Discussions post
(routers/discussions_router.py's write path calls enqueue_discussions_job()
directly — no webhook, no Go plugin involved; see docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md
"Revision history", third architecture).

Fetches the underlying question's content via the headless engine, runs it
through the shared AgentRunner singleton (which already does compliance_engine
checks on input/prompt/output internally, redact-and-proceed per house rule —
see agents/agent_builder.py AgentRunner docstring), posts the reply back
through the SAME core/discussions_engine_client.py used by the gateway's own
write path (the bot authenticates as a real AiNxt identity via the assertion
mechanism, not a separate API token), and mirrors the reply into
ainxt.discussions_answers + a discussions_events row — same as every other
write in this module.

Deliberately does NOT repeat the bug in the existing Threads @AiNxt flow
(routers/threads_router.py's _ainxt_flow calls agents/react_engine.py
directly and skips compliance_engine entirely) — this goes through
agent_runner (agents/agent_builder.py) instead, which does not skip it.

Consumed only by services/discussions_svc/worker.py's dedicated RQ worker.
"""
import asyncio
import uuid

from core.discussions_engine_client import create_answer, get_question_content
from core.logger import logger
from db.database import SessionLocal
from db.models import DiscussionsAnswer, DiscussionsBotRun, DiscussionsEvent, DiscussionsQuestion
from services.discussions_svc.config import DISCUSSIONS_BOT_AGENT_NAME, DISCUSSIONS_BOT_USER_CLAIMS


def _build_user_message(mention_event: dict, question: dict) -> str:
    trigger = mention_event.get("trigger_user_display_name") or "a user"
    title = question.get("title") or mention_event.get("question_title") or "(untitled)"
    content = question.get("content") or ""
    return (
        f"You were mentioned by {trigger} on the AiNxt Discussions board.\n\n"
        f"Question title: {title}\n\n"
        f"Question content:\n{content}\n\n"
        "Give a helpful, concise, technically accurate answer suitable to post "
        "as a reply on this Q&A board."
    )


def run_discussions_bot_job(payload: dict) -> str:
    """rq job (queue=discussions_queue): run the @AiNxt bot and reply."""
    run_id = payload.get("run_id")
    mention_event = payload.get("mention_event", {})
    engine_question_id = mention_event.get("question_id", "")

    db = SessionLocal()
    try:
        run = db.query(DiscussionsBotRun).filter(DiscussionsBotRun.id == run_id).first()
        if run is None:
            logger.error(f"discussions_svc: run {run_id!r} not found — dropping job")
            return "run not found"

        run.status = "running"
        db.commit()

        question_content = asyncio.run(get_question_content(engine_question_id))
        user_message = _build_user_message(mention_event, question_content)

        from agents.agent_builder import agent_runner

        result = agent_runner.run(
            DISCUSSIONS_BOT_AGENT_NAME,
            user_message,
            user_id=f"discussions_bot:{engine_question_id}",
        )

        # AgentRunner's compliance_flags cover input/prompt/output stages
        # combined — we don't have separated visibility, so both bookkeeping
        # columns get the same (conservative) value. See agents/agent_builder.py
        # AgentRunner._compliance_check.
        redacted = bool(result.compliance_flags)
        run.input_redacted = redacted
        run.output_redacted = redacted

        if not result.success or not result.answer:
            run.status = "error"
            run.error_message = result.error or "agent run produced no answer"
            db.commit()
            return f"error: {run.error_message}"

        try:
            answer_resp = asyncio.run(
                create_answer(DISCUSSIONS_BOT_USER_CLAIMS, engine_question_id, result.answer)
            )
            # POST /answer's response nests the new answer's id under
            # data.info.id — NOT data.id like POST /question does (verified
            # live against the real engine response).
            external_answer_id = str(((answer_resp or {}).get("data") or {}).get("info", {}).get("id", ""))
            run.reply_post_id = external_answer_id
            run.status = "complete"

            question_row = (
                db.query(DiscussionsQuestion)
                .filter(DiscussionsQuestion.external_id == engine_question_id)
                .first()
            )
            if question_row is not None:
                mirror_answer = DiscussionsAnswer(
                    id=str(uuid.uuid4()),
                    external_id=external_answer_id,
                    question_id=question_row.id,
                    author_user_id=DISCUSSIONS_BOT_USER_CLAIMS["sub"],
                    content=result.answer,
                )
                db.add(mirror_answer)
                question_row.answer_count = (question_row.answer_count or 0) + 1
                db.add(DiscussionsEvent(
                    id=str(uuid.uuid4()),
                    event_type="ainxt_replied",
                    actor_user_id=DISCUSSIONS_BOT_USER_CLAIMS["sub"],
                    target_type="answer",
                    target_id=mirror_answer.id,
                    payload={"run_id": run_id, "question_id": question_row.id},
                ))
            else:
                logger.warning(
                    f"discussions_svc: no mirror question found for external_id={engine_question_id!r} "
                    "— bot reply posted to the engine but not mirrored"
                )
        except Exception as post_err:
            run.status = "error"
            run.error_message = f"posted reply failed: {post_err}"
        db.commit()
        return run.status
    except Exception as e:
        logger.error(f"discussions_svc: job for run {run_id!r} failed → {e}")
        try:
            db.rollback()
            run = db.query(DiscussionsBotRun).filter(DiscussionsBotRun.id == run_id).first()
            if run is not None:
                run.status = "error"
                run.error_message = str(e)[:2000]
                db.commit()
        except Exception:
            pass
        raise
    finally:
        db.close()
