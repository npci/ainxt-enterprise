# SPDX-License-Identifier: Apache-2.0
"""
Discussions module — real gateway endpoints (NOT a reverse proxy).

Architecture (see docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md — third
revision): the browser only ever talks to this router. Apache Answer runs
headless at services/discussions_engine/ (127.0.0.1 only) and is called
server-to-server via core/discussions_engine_client.py, which handles
per-user session tokens transparently. Every write is mirrored into AiNxt's
own Postgres (db/models.py DiscussionsQuestion/Answer/Vote/Event) in the SAME
request — discussions_events is the feedback-spine log for future
self-improvement workers.

Reads are served straight from the mirror tables — no engine round trip.

This is the single kill switch for the module along with gateway.py's
ENABLE_DISCUSSIONS-gated registration: delete this file + that one line.
"""
import traceback
import uuid
from datetime import datetime as _dt
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile as FastAPIUploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import case, func, text
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user
from agents.compliance_engine import compliance_engine
from core.discussions_engine_client import (
    accept_answer as engine_accept_answer,
    add_comment as engine_add_comment,
    cast_vote as engine_cast_vote,
    create_answer as engine_create_answer,
    create_question as engine_create_question,
    delete_answer as engine_delete_answer,
    delete_comment as engine_delete_comment,
    delete_question as engine_delete_question,
    edit_answer as engine_edit_answer,
    edit_comment as engine_edit_comment,
    edit_question as engine_edit_question,
    get_own_username as engine_get_own_username,
    get_upload as engine_get_upload,
    hard_delete_answer as engine_hard_delete_answer,
    hard_delete_comment as engine_hard_delete_comment,
    hard_delete_question as engine_hard_delete_question,
    list_badges as engine_list_badges,
    list_tags as engine_list_tags,
    upload_file as engine_upload_file,
    user_badge_awards as engine_user_badge_awards,
    user_ranking as engine_user_ranking,
)
from core.job_queue import enqueue_discussions_job, enqueue_job, Q_DEFAULT
from core.logger import logger
from core.security_validation import (
    validate_discussion_title_and_tags,
    validate_free_text,
    _flatten_errors,
)
from db.database import get_db
from db.models import (
    DiscussionNotifyGroup,
    DiscussionsAnswer, DiscussionsComment, DiscussionsEvent, DiscussionsQuestion, DiscussionsVote, User,
)
from store.inbox_store import publish_inbox_item

router = APIRouter(prefix="/discussions", tags=["discussions"])

_SORT_COLUMNS = {
    "newest": lambda m: m.created_at.desc(),
    "active": lambda m: m.updated_at.desc(),
    "votes": lambda m: m.vote_count.desc(),
}

_BOT_USER_ID = "ainxt-system-bot"


def _resolve_authors(db: Session, user_ids: list) -> dict:
    """Batch-resolve author_user_id -> {name, department} for a page of
    results. Discussions rows only ever store the raw JWT `sub` — every read
    needs a join back to `users` to show a real name/department instead of a
    bare UUID, which is the whole point of an internal (non-anonymous) forum."""
    ids = {uid for uid in user_ids if uid and uid != _BOT_USER_ID}
    rows = db.query(User.id, User.name, User.department).filter(User.id.in_(ids)).all() if ids else []
    resolved = {r.id: {"name": r.name, "department": r.department} for r in rows}
    resolved[_BOT_USER_ID] = {"name": "AiNxt", "department": None}
    return resolved


def _author_fields(resolved: dict, user_id: str) -> dict:
    info = resolved.get(user_id) or {}
    return {"author_name": info.get("name") or user_id, "author_department": info.get("department")}


class AskQuestionReq(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    notify_emails: list[str] = []   # people to notify by email + in-app inbox


class PostAnswerReq(BaseModel):
    content: str


class VoteReq(BaseModel):
    direction: int  # 1 or -1

    # SECURITY (AppSec finding — Business Logic Flaw / CWE-841, CWE-799):
    # `direction` previously accepted any integer. Because vote() applies it
    # as a raw delta to vote_count, an unvalidated value (e.g. direction=999)
    # let a caller inflate/deflate a post's vote_count by an arbitrary amount
    # in a single request. Restrict to the only two values the up/down-vote
    # UI can ever legitimately send.
    @field_validator("direction")
    @classmethod
    def _direction_must_be_unit(cls, v: int) -> int:
        if v not in (1, -1):
            raise ValueError("direction must be 1 (upvote) or -1 (downvote)")
        return v


class AcceptAnswerReq(BaseModel):
    answer_id: str


class CommentReq(BaseModel):
    content: str


class EditQuestionReq(BaseModel):
    title: str
    content: str
    tags: list[str] = []


class EditAnswerReq(BaseModel):
    content: str


class EditCommentReq(BaseModel):
    content: str


def _redact_and_check(text: str, stage: str) -> str:
    """Redact-and-proceed per house rule — never hard-block a Discussions post."""
    try:
        result = compliance_engine.validate_input(text)
        blocking = [f["type"] for f in result.get("findings", []) if f.get("blocked")]
        if blocking:
            logger.warning(f"discussions_router: compliance flags at {stage}: {blocking}")
        return result.get("redacted_text", text) or text
    except Exception as e:
        logger.error(f"discussions_router: compliance check error at {stage}: {e}")
        return text


# A handful of Apache Answer reason codes (internal/base/reason/reason.go)
# have no entry at all in i18n/en_US.yaml — Tr() then returns the raw dotted
# key verbatim (confirmed: "error.answer.restrict_answer" has no translation
# anywhere in the bundle). Friendly text for the ones our own UI can trigger;
# anything else falls through to the generic humanizer below.
_FRIENDLY_REASONS = {
    "error.answer.restrict_answer": "You've already posted a reply to this discussion — add a comment on your existing reply instead of posting another.",
}


def _looks_like_raw_key(s: str) -> bool:
    """RespBody.TrMsg() (internal/base/handler/response.go) falls back to
    `Message = translator.Tr(lang, Reason)` whenever a handler builds an
    error with no explicit .WithMsg(...) — and Tr() itself falls back to
    returning the raw reason key verbatim when no translation exists for it
    in ANY language bundle (confirmed for error.answer.restrict_answer: no
    i18n/*.yaml file has an entry for it at all). When that happens, `msg`
    in the response is the SAME dotted key as `reason`, not real text —
    detect that shape so it still gets humanized instead of shown verbatim."""
    return bool(s) and "." in s and " " not in s and s == s.lower()


def _humanize_reason(reason: str) -> str:
    if reason in _FRIENDLY_REASONS:
        return _FRIENDLY_REASONS[reason]
    return reason.removeprefix("error.").replace(".", " ").replace("_", " ").strip() or reason


async def _engine(coro):
    """Await an engine call, translating a 4xx from Apache Answer into a real
    HTTPException instead of letting it fall through as an unhandled 500.
    The engine's RespBody carries a human-readable msg/reason, and form
    validation failures (internal/base/validator.go::FormErrorField) put
    per-field error_msg entries in `data` — surface whichever is present."""
    try:
        return await coro
    except httpx.HTTPStatusError as e:
        detail = "Discussions engine rejected the request"
        try:
            body = e.response.json()
            data = body.get("data")
            if isinstance(data, list) and data:
                detail = "; ".join(f.get("error_msg", "") for f in data if f.get("error_msg")) or detail
            elif body.get("msg") and not _looks_like_raw_key(body["msg"]):
                detail = body["msg"]
            elif body.get("reason") or body.get("msg"):
                detail = _humanize_reason(body.get("reason") or body["msg"])
        except Exception:
            pass
        raise HTTPException(status_code=422, detail=detail) from e


def _log_event(db: Session, event_type: str, actor_user_id: str,
               target_type: Optional[str] = None, target_id: Optional[str] = None,
               payload: Optional[dict] = None):
    db.add(DiscussionsEvent(
        id=str(uuid.uuid4()),
        event_type=event_type,
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=target_id,
        payload=payload or {},
    ))


def _maybe_trigger_bot(db: Session, content: str, mirror_id: str, answer_post_type: str,
                        mention_author: str, external_question_id: str, external_post_id: str):
    """mirror_id is the AiNxt-native UUID row (question or answer) that
    contains the mention — used for discussions_events.target_id, which is a
    UUID column. external_question_id/external_post_id are the engine's own
    (non-UUID, numeric-string) ids — those go in the job payload only, never
    into a UUID-typed column."""
    if "@AiNxt" not in content:
        return
    run_id = str(uuid.uuid4())
    from db.models import DiscussionsBotRun
    db.add(DiscussionsBotRun(
        id=run_id,
        answer_post_id=external_post_id,
        answer_post_type=answer_post_type,
        mention_author=mention_author,
        status="pending",
    ))
    _log_event(db, "ainxt_mentioned", mention_author, answer_post_type, mirror_id,
               payload={"run_id": run_id, "external_post_id": external_post_id})
    db.flush()
    try:
        enqueue_discussions_job(run_id, {
            "question_id": external_question_id,
            "answer_post_id": external_post_id,
            "answer_post_type": answer_post_type,
            "trigger_user_display_name": mention_author,
        })
    except RuntimeError as e:
        # Back-pressure/RQ-unavailable — the row stays "pending"; don't fail
        # the user's own post over it.
        logger.error(f"discussions_router: could not enqueue bot job for run {run_id}: {e}")


import re as _re

_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _notify_group_emails(db: Session) -> list:
    """Return all email addresses from discussion_notify_groups.

    Every address in this table is notified whenever anyone posts a discussion,
    regardless of the poster's department or any other attribute. This replaces
    the previous DISCUSSIONS_DEFAULT_NOTIFY_EMAILS .env var with a DB-managed
    flat list that admins can update without a redeploy.

    Returns [] when the table is empty or on any DB error — so this is a
    no-op until an admin populates the table.
    """
    try:
        rows = db.query(DiscussionNotifyGroup.notify_email).all()
        return [r.notify_email for r in rows]
    except Exception as e:
        logger.error(f"_notify_group_emails: DB lookup failed: {e}")
        return []


def _notify_people(db: Session, emails: list, current_user: dict, question_id: str,
                   external_id: str, title: str, content: str) -> None:
    """Notify each tagged email about a newly-posted discussion.

    - Internal users (matched by email in `users`) get an in-app inbox item
      (SSE-pushed) AND an email.
    - External / unknown emails get an email only.

    The poster's own picks (`emails`) are merged with the department-specific
    recipients from `discussion_notify_groups` (keyed by the poster's
    `users.department`). If no rows are configured for that department the
    dept list is [] and behaviour is unchanged.

    Email delivery is fanned out to RQ (retry + DLQ) so this never blocks the
    poster's request. Any failure here is swallowed by the caller — the post
    has already been committed and must not be lost over a notification.
    """
    author_email = (current_user.get("email") or "").strip().lower()
    author_name = current_user.get("name") or current_user.get("email") or "Someone"

    # Fetch the flat notify-group list from the DB (replaces the old
    # DISCUSSIONS_DEFAULT_NOTIFY_EMAILS .env var). Every email in
    # discussion_notify_groups is notified on every post, regardless of
    # who posted or what department they belong to.
    group_emails = _notify_group_emails(db)

    # Merge poster picks with the notify-group list, then normalize/validate/
    # dedupe and drop the author themselves.
    seen = set()
    recipients = []
    for raw in [*(emails or []), *group_emails]:
        email = (raw or "").strip().lower()
        if not email or email in seen or email == author_email:
            continue
        if not _EMAIL_RE.match(email):
            logger.info(f"_notify_people: skipping malformed email {mask_email(raw)!r}")
            continue
        seen.add(email)
        recipients.append(email)

    if not recipients:
        return

    # Resolve which recipients are internal users (for the in-app inbox).
    rows = db.query(User.id, User.email).filter(func.lower(User.email).in_(recipients)).all()
    email_to_user_id = {r.email.lower(): r.id for r in rows if r.email}

    excerpt = (content or "").strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:279].rstrip() + "…"

    for email in recipients:
        user_id = email_to_user_id.get(email)
        # In-app inbox — internal users only (keyed by users.id, which is the
        # same JWT `sub` stored in discussions author_user_id).
        if user_id:
            try:
                publish_inbox_item(
                    user_id=user_id,
                    type="discussion_mention",
                    title=f"{author_name} mentioned you in a discussion",
                    body=f"{title}\n\n{excerpt}" if excerpt else title,
                    source_id=question_id,
                    metadata={"author": author_name, "question_id": question_id,
                              "external_id": external_id},
                )
            except Exception as e:
                logger.error(f"_notify_people: inbox publish failed for {mask_email(email)}: {e}")

        # Email — always, via RQ (retry + DLQ). Never blocks the request.
        try:
            enqueue_job(
                "services.discussion_notify.send_discussion_email",
                {
                    "to": email,
                    "author_name": author_name,
                    "title": title,
                    "content": content,
                    "question_id": question_id,
                },
                queue_name=Q_DEFAULT,
            )
        except RuntimeError as e:
            logger.error(f"_notify_people: could not enqueue email for {mask_email(email)}: {e}")


@router.post("/questions")
async def ask_question(body: AskQuestionReq, current_user: dict = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    # TEMPORARY diagnostic wrap — 2026-07-13, remove once the prod 500 on
    # question creation is root-caused. Returns the real exception straight
    # in the response body so it's visible without log-spelunking. Never
    # leave this in a real deployment long-term (raw tracebacks in a
    # response are an info-disclosure risk).
    sub = current_user.get("sub")
    logger.info(
        f"discussions_router.ask_question: ENTER sub={sub!r} "
        f"title_len={len(body.title or '')} content_len={len(body.content or '')} tags={body.tags}"
    )
    try:
        # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
        _is_valid, _field_errors, _san = validate_discussion_title_and_tags(body.title, body.tags)
        if not _is_valid:
            raise HTTPException(status_code=400, detail=_flatten_errors(_field_errors))
        body.title = _san["title"]
        body.tags = _san["tags"]

        title = _redact_and_check(body.title, "question_title")
        content = _redact_and_check(body.content, "question_content")
        logger.info(f"discussions_router.ask_question: compliance/redaction passed sub={sub!r}")

        logger.info(f"discussions_router.ask_question: calling engine create_question sub={sub!r}")
        engine_resp = await _engine(engine_create_question(current_user, title, content, body.tags))
        external_id = str((engine_resp or {}).get("data", {}).get("id", ""))
        logger.info(
            f"discussions_router.ask_question: engine created question sub={sub!r} "
            f"external_id={external_id!r}"
        )

        row = DiscussionsQuestion(
            id=str(uuid.uuid4()),
            external_id=external_id,
            author_user_id=current_user["sub"],
            title=title,
            content=content,
            tags=body.tags,
        )
        db.add(row)
        _log_event(db, "question_asked", current_user["sub"], "question", row.id,
                   payload={"title": title})
        _maybe_trigger_bot(db, content, row.id, "question", current_user.get("email", current_user["sub"]),
                            external_question_id=external_id, external_post_id=external_id)
        db.commit()
        logger.info(
            f"discussions_router.ask_question: SUCCESS sub={sub!r} id={row.id} external_id={external_id!r}"
        )
        # Notify tagged people + department-specific recipients from the DB
        # (email + in-app inbox). _notify_people returns early if there are no
        # valid recipients, so this is a no-op when neither the poster's list
        # nor the department's notify group has any entries. Never fail the post.
        try:
            _notify_people(db, body.notify_emails, current_user, row.id,
                           external_id, title, content)
        except Exception as e:
            logger.error(f"ask_question: notify_people failed (post still ok): {e}")
        return {"id": row.id, "external_id": external_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"discussions_router.ask_question: FAILED sub={sub!r} "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        return JSONResponse(status_code=500, content={
            "debug_exception_type": type(e).__name__,
            "debug_exception_str": str(e),
            "debug_traceback": traceback.format_exc(),
        })


@router.get("/questions")
async def list_questions(db: Session = Depends(get_db), limit: int = 50,
                          sort: str = "newest", tag: list[str] = Query(default=[]),
                          unanswered: bool = False, q: Optional[str] = None, mine: bool = False,
                          status: Optional[str] = None,
                          current_user: dict = Depends(get_current_user)):
    query = db.query(DiscussionsQuestion)
    if tag:
        # tags is a JSONB array column — `?|` (has_any) matches a discussion
        # tagged with ANY of the selected tags, for a multi-select filter.
        query = query.filter(DiscussionsQuestion.tags.has_any(pg_array(tag)))
    if unanswered:
        query = query.filter(DiscussionsQuestion.answer_count == 0)
    # Status filter for the Overview drill-down. Predicates MUST match the
    # /stats endpoint so a card's count equals the filtered result count:
    #   replied = answer_count > 0 ; closed = accepted_answer_id IS NOT NULL ;
    #   raised (or None/unknown) = all rows of that type (no status filter).
    if status == "replied":
        query = query.filter(DiscussionsQuestion.answer_count > 0)
    elif status == "closed":
        query = query.filter(DiscussionsQuestion.accepted_answer_id.isnot(None))
    if mine:
        query = query.filter(DiscussionsQuestion.author_user_id == current_user["sub"])
    if q:
        query = query.filter(
            DiscussionsQuestion.title.ilike(f"%{q}%") | DiscussionsQuestion.content.ilike(f"%{q}%")
        )
    order = _SORT_COLUMNS.get(sort, _SORT_COLUMNS["newest"])(DiscussionsQuestion)
    rows = query.order_by(order).limit(min(limit, 200)).all()
    resolved = _resolve_authors(db, [r.author_user_id for r in rows])
    return [
        {
            "id": r.id, "title": r.title, "tags": r.tags, "vote_count": r.vote_count,
            "answer_count": r.answer_count, "comment_count": r.comment_count,
            "content_preview": (r.content or "")[:200],
            "author_user_id": r.author_user_id,
            "accepted_answer_id": r.accepted_answer_id,
            "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat(),
            **_author_fields(resolved, r.author_user_id),
        }
        for r in rows
    ]


@router.get("/tags")
async def list_tags(page: int = 1, page_size: int = 30, query_cond: str = "popular"):
    return await engine_list_tags(page, page_size, query_cond)


@router.get("/badges")
async def list_badges():
    return await engine_list_badges()


@router.get("/users")
async def list_users():
    return await engine_user_ranking()


@router.get("/badges/mine")
async def my_badges(current_user: dict = Depends(get_current_user)):
    username = await _engine(engine_get_own_username(current_user))
    if not username:
        return []
    return await _engine(engine_user_badge_awards(username))


@router.get("/experts")
async def list_experts(db: Session = Depends(get_db), top_n: int = 5,
                        _user: dict = Depends(get_current_user)):
    """Top answerers per topic tag, computed entirely from our own mirror
    (discussions_answers joined to discussions_questions.tags) — no engine
    round trip, since this data has never left our own Postgres. Answers a
    real gap the engine's own reputation leaderboard can't: "who actually
    knows about X," not just "who has the most votes overall.\""""
    # No explicit schema prefix needed — db/database.py sets search_path on
    # connect, same as every ORM query in this router.
    rows = db.execute(text("""
        SELECT tag, a.author_user_id AS author_user_id, COUNT(*) AS answer_count, SUM(a.vote_count) AS total_votes
        FROM discussions_answers a
        JOIN discussions_questions q ON q.id = a.question_id
        CROSS JOIN LATERAL jsonb_array_elements_text(q.tags) AS tag
        GROUP BY tag, a.author_user_id
        ORDER BY tag, total_votes DESC, answer_count DESC
    """)).all()

    by_tag: dict = {}
    for r in rows:
        by_tag.setdefault(r.tag, []).append(r)

    resolved = _resolve_authors(db, [r.author_user_id for r in rows])
    return {
        tag: [
            {
                "author_user_id": r.author_user_id, "answer_count": r.answer_count,
                "total_votes": r.total_votes, **_author_fields(resolved, r.author_user_id),
            }
            for r in entries[:top_n]
        ]
        for tag, entries in by_tag.items()
    }


@router.get("/stats")
async def get_discussion_stats(db: Session = Depends(get_db),
                                current_user: dict = Depends(get_current_user)):
    """Admin-only: return total / replied / closed counts per discussion type
    (question, feedback, issue).  Types are stored as the first element of the
    JSONB ``tags`` array on each discussion row.

    - **total**   = all rows of that type
    - **replied** = rows where ``answer_count > 0``
    - **closed**  = rows where ``accepted_answer_id IS NOT NULL``
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    result = {}
    for type_tag in ("question", "feedback", "issue"):
        row = db.execute(text("""
            SELECT
                COUNT(*)                                                  AS total,
                SUM(CASE WHEN answer_count > 0 THEN 1 ELSE 0 END)        AS replied,
                SUM(CASE WHEN accepted_answer_id IS NOT NULL THEN 1 ELSE 0 END) AS closed
            FROM discussions_questions
            WHERE tags ->> 0 = :type_tag
        """), {"type_tag": type_tag}).one()
        result[type_tag] = {
            "total":   int(row.total   or 0),
            "replied": int(row.replied or 0),
            "closed":  int(row.closed  or 0),
        }
    return result


@router.get("/questions/{question_id}")
async def get_question(question_id: str, db: Session = Depends(get_db),
                        current_user: dict = Depends(get_current_user)):
    q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == question_id).first()
    if q is None:
        raise HTTPException(status_code=404, detail="question not found")
    answers = (
        db.query(DiscussionsAnswer)
        .filter(DiscussionsAnswer.question_id == question_id)
        .order_by(DiscussionsAnswer.is_accepted.desc(), DiscussionsAnswer.vote_count.desc())
        .all()
    )
    resolved = _resolve_authors(db, [q.author_user_id] + [a.author_user_id for a in answers])
    my_votes = {
        v.target_id: v.direction for v in db.query(DiscussionsVote).filter(
            DiscussionsVote.user_id == current_user["sub"],
            DiscussionsVote.target_id.in_([q.id] + [a.id for a in answers]),
        ).all()
    }
    return {
        "id": q.id, "external_id": q.external_id, "title": q.title, "content": q.content,
        "tags": q.tags, "vote_count": q.vote_count, "comment_count": q.comment_count,
        "author_user_id": q.author_user_id, "my_vote": my_votes.get(q.id, 0),
        "accepted_answer_id": q.accepted_answer_id, "created_at": q.created_at.isoformat(),
        "updated_at": q.updated_at.isoformat(), **_author_fields(resolved, q.author_user_id),
        "answers": [
            {
                "id": a.id, "content": a.content, "vote_count": a.vote_count,
                "is_accepted": a.is_accepted, "comment_count": a.comment_count,
                "author_user_id": a.author_user_id, "my_vote": my_votes.get(a.id, 0),
                "created_at": a.created_at.isoformat(), "updated_at": a.updated_at.isoformat(),
                **_author_fields(resolved, a.author_user_id),
            }
            for a in answers
        ],
    }


@router.post("/questions/{question_id}/answers")
async def post_answer(question_id: str, body: PostAnswerReq,
                       current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == question_id).first()
    if q is None:
        raise HTTPException(status_code=404, detail="question not found")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, body.content = validate_free_text(body.content)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"content": _errs}))

    content = _redact_and_check(body.content, "answer_content")
    engine_resp = await _engine(engine_create_answer(current_user, q.external_id, content))
    # POST /answer's response nests the new answer's id under data.info.id —
    # NOT data.id like POST /question does. Confirmed against the real engine
    # response (verified live, not assumed from the Go schema alone).
    external_id = str(((engine_resp or {}).get("data") or {}).get("info", {}).get("id", ""))

    row = DiscussionsAnswer(
        id=str(uuid.uuid4()), external_id=external_id, question_id=question_id,
        author_user_id=current_user["sub"], content=content,
    )
    db.add(row)
    q.answer_count = (q.answer_count or 0) + 1
    _log_event(db, "answer_posted", current_user["sub"], "answer", row.id,
               payload={"question_id": question_id})
    _maybe_trigger_bot(db, content, row.id, "answer", current_user.get("email", current_user["sub"]),
                        external_question_id=q.external_id, external_post_id=external_id)
    db.commit()
    return {"id": row.id, "external_id": external_id}


@router.post("/{target_type}/{target_id}/vote")
async def vote(target_type: str, target_id: str, body: VoteReq,
                current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if target_type not in ("questions", "answers"):
        raise HTTPException(status_code=404, detail="unknown vote target")
    engine_target_type = "question" if target_type == "questions" else "answer"
    model = DiscussionsQuestion if target_type == "questions" else DiscussionsAnswer

    # SECURITY (AppSec finding — Business Logic Flaw / CWE-841, CWE-799):
    # Lock the parent row for the duration of this vote transaction so two
    # concurrent requests from the same user can't both read a stale
    # vote_count before either commits (the read-modify-write race that let
    # rapid duplicate/replayed requests drift vote_count out of sync with
    # reality). with_for_update() blocks a second concurrent request on the
    # same row until the first transaction commits, matching the existing
    # lock pattern used elsewhere in this codebase for the same class of
    # problem (see services/budget_audit_service.py's BudgetConfig row lock).
    row = db.query(model).filter(model.id == target_id).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    # SECURITY: also lock any existing vote row for this (user, target) up
    # front, BEFORE deciding whether to call the engine — previously the
    # engine was called unconditionally and the vote row was only read
    # afterwards, so a duplicate/replayed request always produced an
    # outbound engine call even when nothing should change locally.
    existing = (
        db.query(DiscussionsVote)
        .filter(DiscussionsVote.target_type == engine_target_type,
                DiscussionsVote.target_id == target_id,
                DiscussionsVote.user_id == current_user["sub"])
        .with_for_update()
        .first()
    )

    old_direction = existing.direction if existing else 0

    # Toggle: clicking the same direction again removes the vote
    new_direction = 0 if old_direction == body.direction else body.direction

    # Apply only the net change so repeated votes don't inflate the count
    delta = new_direction - old_direction

    # SECURITY: skip the outbound engine call entirely when this request is
    # a genuine no-op (delta == 0) — e.g. a replayed/duplicate POST of a vote
    # that's already recorded. A resubmitted identical vote can no longer
    # produce any outbound effect, closing the replay path at its root
    # instead of only in the local mirror table.
    if delta != 0:
        await _engine(engine_cast_vote(current_user, engine_target_type, row.external_id, body.direction))

    if existing:
        if new_direction == 0:
            db.delete(existing)
        else:
            existing.direction = new_direction
    elif new_direction != 0:
        db.add(DiscussionsVote(
            id=str(uuid.uuid4()), target_type=engine_target_type, target_id=target_id,
            user_id=current_user["sub"], direction=new_direction,
        ))

    row.vote_count = (row.vote_count or 0) + delta

    _log_event(db, "vote_cast", current_user["sub"], engine_target_type, target_id,
               payload={"direction": new_direction})
    db.commit()
    return {"vote_count": row.vote_count, "my_vote": new_direction}


@router.post("/questions/{question_id}/accept")
async def accept(question_id: str, body: AcceptAnswerReq,
                  current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == question_id).first()
    a = db.query(DiscussionsAnswer).filter(DiscussionsAnswer.id == body.answer_id).first()
    if q is None or a is None:
        raise HTTPException(status_code=404, detail="not found")

    await _engine(engine_accept_answer(current_user, q.external_id, a.external_id))

    if q.accepted_answer_id:
        prev = db.query(DiscussionsAnswer).filter(DiscussionsAnswer.id == q.accepted_answer_id).first()
        if prev:
            prev.is_accepted = False
    q.accepted_answer_id = a.id
    a.is_accepted = True
    _log_event(db, "answer_accepted", current_user["sub"], "answer", a.id,
               payload={"question_id": question_id})
    db.commit()
    return {"accepted_answer_id": a.id}


@router.get("/{target_type}/{target_id}/comments")
async def list_comments(target_type: str, target_id: str, db: Session = Depends(get_db),
                         _user: dict = Depends(get_current_user)):
    if target_type not in ("questions", "answers"):
        raise HTTPException(status_code=404, detail="unknown comment target")
    rows = (
        db.query(DiscussionsComment)
        .filter(DiscussionsComment.target_id == target_id)
        .order_by(DiscussionsComment.created_at.asc())
        .all()
    )
    resolved = _resolve_authors(db, [c.author_user_id for c in rows])
    return [
        {"id": c.id, "content": c.content, "author_user_id": c.author_user_id,
         "created_at": c.created_at.isoformat(),
         # updated_at is added by the 2026-07-17 catch-up migration; existing
         # comments before the migration will have it back-filled to created_at.
         "updated_at": (getattr(c, "updated_at", None) or c.created_at).isoformat(),
         **_author_fields(resolved, c.author_user_id)}
        for c in rows
    ]


@router.post("/{target_type}/{target_id}/comments")
async def post_comment(target_type: str, target_id: str, body: CommentReq,
                        current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if target_type not in ("questions", "answers"):
        raise HTTPException(status_code=404, detail="unknown comment target")
    engine_target_type = "question" if target_type == "questions" else "answer"
    model = DiscussionsQuestion if target_type == "questions" else DiscussionsAnswer
    row = db.query(model).filter(model.id == target_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, body.content = validate_free_text(body.content)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"content": _errs}))

    content = _redact_and_check(body.content, "comment_content")
    engine_resp = await _engine(engine_add_comment(current_user, row.external_id, content))
    external_id = str((engine_resp or {}).get("data", {}).get("comment_id", ""))

    comment = DiscussionsComment(
        id=str(uuid.uuid4()), external_id=external_id, target_type=engine_target_type,
        target_id=target_id, author_user_id=current_user["sub"], content=content,
    )
    db.add(comment)
    row.comment_count = (row.comment_count or 0) + 1
    _log_event(db, "comment_posted", current_user["sub"], engine_target_type, target_id,
               payload={"comment_id": comment.id})

    # For a comment, the "question" the bot should read for context is the
    # question itself (if commenting on a question) or the answer's parent
    # question (if commenting on an answer).
    if engine_target_type == "question":
        question_external_id = row.external_id
    else:
        parent_q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == row.question_id).first()
        question_external_id = parent_q.external_id if parent_q else ""
    _maybe_trigger_bot(db, content, comment.id, "comment", current_user.get("email", current_user["sub"]),
                        external_question_id=question_external_id, external_post_id=external_id)
    db.commit()
    return {"id": comment.id, "external_id": external_id}


# ── Deletes ──────────────────────────────────────────────────────────────
# Author-only: a user can delete their OWN question/answer/comment, nobody
# else's. The ownership check here is authoritative — the engine enforces its
# own author check too, but we never rely on the client to gate the button.
# Each handler deletes the engine's copy first, then removes the AiNxt mirror
# row(s) so the two stores stay consistent.

def _require_owner(row, current_user: dict):
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.author_user_id != current_user["sub"]:
        raise HTTPException(status_code=403, detail="You can only delete your own posts")


@router.delete("/questions/{question_id}")
async def delete_question(question_id: str,
                          current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == question_id).first()
    if q is None:
        return {"deleted": question_id}  # already deleted — idempotent
    _require_owner(q, current_user)

    # Hard delete on engine — best-effort (row may already be gone)
    try:
        await _engine(engine_hard_delete_question(current_user, q.external_id))
    except Exception as e:
        logger.warning(
            f"delete_question: engine hard delete failed for "
            f"external_id={q.external_id!r}: {type(e).__name__}: {e} — proceeding with mirror cleanup"
        )

    try:
        # Fetch answer IDs as a plain Python list (avoids PostgreSQL subquery type issues)
        answer_id_list = [r.id for r in db.query(DiscussionsAnswer.id).filter(
            DiscussionsAnswer.question_id == q.id).all()]

        # Delete comments and votes for the question and all its answers
        all_target_ids = [q.id] + answer_id_list
        if all_target_ids:
            db.query(DiscussionsComment).filter(
                DiscussionsComment.target_id.in_(all_target_ids)
            ).delete(synchronize_session=False)
            db.query(DiscussionsVote).filter(
                DiscussionsVote.target_id.in_(all_target_ids)
            ).delete(synchronize_session=False)

        # Delete answers explicitly
        if answer_id_list:
            db.query(DiscussionsAnswer).filter(
                DiscussionsAnswer.id.in_(answer_id_list)
            ).delete(synchronize_session=False)

        db.delete(q)
        _log_event(db, "question_deleted", current_user["sub"], "question", q.id)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(
            f"delete_question: mirror cleanup failed for id={question_id!r} "
            f"external_id={q.external_id!r}: {type(e).__name__}: {e}"
        )
    return {"deleted": q.id}


@router.delete("/answers/{answer_id}")
async def delete_answer(answer_id: str,
                        current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.query(DiscussionsAnswer).filter(DiscussionsAnswer.id == answer_id).first()
    if a is None:
        return {"deleted": answer_id}  # already deleted — idempotent
    _require_owner(a, current_user)

    # Hard delete on engine — best-effort (row may already be gone)
    try:
        await _engine(engine_hard_delete_answer(current_user, a.external_id))
    except Exception as e:
        logger.warning(
            f"delete_answer: engine hard delete failed for "
            f"external_id={a.external_id!r}: {type(e).__name__}: {e} — proceeding with mirror cleanup"
        )

    try:
        # Decrement parent question answer count and clear accepted pointer
        db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == a.question_id).update(
            {
                DiscussionsQuestion.answer_count: func.greatest(
                    DiscussionsQuestion.answer_count - 1, 0),
                DiscussionsQuestion.accepted_answer_id: case(
                    (DiscussionsQuestion.accepted_answer_id == a.id, None),
                    else_=DiscussionsQuestion.accepted_answer_id,
                ),
            },
            synchronize_session=False,
        )
        # Delete comments and votes for this answer (plain filter — no subquery)
        db.query(DiscussionsComment).filter(
            DiscussionsComment.target_id == a.id
        ).delete(synchronize_session=False)
        db.query(DiscussionsVote).filter(
            DiscussionsVote.target_id == a.id
        ).delete(synchronize_session=False)
        db.delete(a)
        _log_event(db, "answer_deleted", current_user["sub"], "answer", a.id,
                   payload={"question_id": a.question_id})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(
            f"delete_answer: mirror cleanup failed for id={answer_id!r} "
            f"external_id={a.external_id!r}: {type(e).__name__}: {e}"
        )
    return {"deleted": answer_id}


@router.delete("/{target_type}/{target_id}/comments/{comment_id}")
async def delete_comment(target_type: str, target_id: str, comment_id: str,
                         current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if target_type not in ("questions", "answers"):
        raise HTTPException(status_code=404, detail="unknown comment target")
    c = db.query(DiscussionsComment).filter(DiscussionsComment.id == comment_id).first()
    _require_owner(c, current_user)

    # Use hard delete instead of soft delete to prevent duplicate submission errors.
    await _engine(engine_hard_delete_comment(current_user, c.external_id))

    try:
        model = DiscussionsQuestion if target_type == "questions" else DiscussionsAnswer
        db.query(model).filter(model.id == target_id).update(
            {model.comment_count: func.greatest(model.comment_count - 1, 0)},
            synchronize_session=False,
        )
        db.delete(c)
        _log_event(db, "comment_deleted", current_user["sub"],
                   "question" if target_type == "questions" else "answer", target_id,
                   payload={"comment_id": comment_id})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(
            f"delete_comment: mirror cleanup failed for id={comment_id!r} "
            f"external_id={c.external_id!r}: {type(e).__name__}: {e}"
        )
    return {"deleted": comment_id}


# ── Hard (permanent) deletes ─────────────────────────────────────────────
# Admin-only: permanently remove a question/answer/comment and ALL related
# data from both the engine (via the new /permanent endpoints added to
# Apache Answer) and the AiNxt mirror tables in a single request.
#
# Unlike the soft-delete routes above (which are author-only and leave the
# engine row with status=deleted), these:
#   • Require role="admin" — not just ownership.
#   • Call the engine's hard-delete endpoints which physically remove the row
#     and cascade to answers, comments, revisions, tag-relations, activities,
#     and notifications in one DB transaction on the engine side.
#   • Also clear the duplicate-submission cache on the engine side, so the
#     same title/content can be re-posted immediately.
#   • Remove the AiNxt mirror rows (and their child comments/votes) so the
#     two stores stay consistent.
#
# These are the correct fix for the "duplicate submission" bug caused by
# soft-deleted rows still being visible to the engine's dedupe logic.

def _require_admin(current_user: dict):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


@router.delete("/questions/{question_id}/permanent")
async def hard_delete_question(
    question_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete a question and ALL its related data.

    Cascades on the engine side (single transaction):
      question → answers → comments on question & answers → revisions →
      tag_rel → activity → notification rows.

    Also removes the AiNxt mirror rows (DiscussionsQuestion + its
    DiscussionsAnswer children, plus their DiscussionsComment and
    DiscussionsVote rows).

    Admin-only — not restricted to the question's author.
    """
    _require_admin(current_user)

    q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == question_id).first()
    if q is None:
        raise HTTPException(status_code=404, detail="question not found")

    # Hard-delete on the engine first — this is the authoritative store for
    # content; if it fails we must not remove our mirror (data would be lost).
    await _engine(engine_hard_delete_question(current_user, q.external_id))

    # Remove mirror rows: comments and votes are not FK-linked to the question
    # (they key on target_id), so sweep them explicitly before deleting the
    # question (which cascade-deletes its answers via the ORM relationship).
    answer_ids = db.query(DiscussionsAnswer.id).filter(DiscussionsAnswer.question_id == q.id)
    for model in (DiscussionsComment, DiscussionsVote):
        db.query(model).filter(
            (model.target_id == q.id) | model.target_id.in_(answer_ids)
        ).delete(synchronize_session=False)

    db.delete(q)  # answers cascade-delete with the question via ORM
    _log_event(db, "question_hard_deleted", current_user["sub"], "question", q.id,
               payload={"external_id": q.external_id})
    db.commit()

    logger.info(
        f"discussions_router.hard_delete_question: SUCCESS "
        f"id={question_id} external_id={q.external_id!r} "
        f"by admin sub={current_user.get('sub')!r}"
    )
    return {"hard_deleted": question_id}


@router.delete("/answers/{answer_id}/permanent")
async def hard_delete_answer(
    answer_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete an answer and its related data.

    Cascades on the engine side: answer → comments on the answer → revisions.

    Also removes the AiNxt mirror rows (DiscussionsAnswer + its
    DiscussionsComment and DiscussionsVote rows), and decrements the parent
    question's answer_count.

    Admin-only — not restricted to the answer's author.
    """
    _require_admin(current_user)

    a = db.query(DiscussionsAnswer).filter(DiscussionsAnswer.id == answer_id).first()
    if a is None:
        raise HTTPException(status_code=404, detail="answer not found")

    # Hard-delete on the engine first.
    await _engine(engine_hard_delete_answer(current_user, a.external_id))

    # Decrement parent question's answer_count and clear accepted_answer_id
    # if this was the accepted answer — single UPDATE, no row load needed.
    db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == a.question_id).update(
        {
            DiscussionsQuestion.answer_count: func.greatest(
                DiscussionsQuestion.answer_count - 1, 0
            ),
            DiscussionsQuestion.accepted_answer_id: case(
                (DiscussionsQuestion.accepted_answer_id == a.id, None),
                else_=DiscussionsQuestion.accepted_answer_id,
            ),
        },
        synchronize_session=False,
    )

    # Remove child comments and votes for this answer.
    db.query(DiscussionsComment).filter(
        DiscussionsComment.target_id == a.id
    ).delete(synchronize_session=False)
    db.query(DiscussionsVote).filter(
        DiscussionsVote.target_id == a.id
    ).delete(synchronize_session=False)

    db.delete(a)
    _log_event(db, "answer_hard_deleted", current_user["sub"], "answer", a.id,
               payload={"question_id": a.question_id, "external_id": a.external_id})
    db.commit()

    logger.info(
        f"discussions_router.hard_delete_answer: SUCCESS "
        f"id={answer_id} external_id={a.external_id!r} "
        f"by admin sub={current_user.get('sub')!r}"
    )
    return {"hard_deleted": answer_id}


@router.delete("/{target_type}/{target_id}/comments/{comment_id}/permanent")
async def hard_delete_comment(
    target_type: str,
    target_id: str,
    comment_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete a single comment.

    Also removes the AiNxt mirror row (DiscussionsComment) and decrements
    the parent question/answer's comment_count.

    Admin-only — not restricted to the comment's author.
    """
    _require_admin(current_user)

    if target_type not in ("questions", "answers"):
        raise HTTPException(status_code=404, detail="unknown comment target")

    c = db.query(DiscussionsComment).filter(DiscussionsComment.id == comment_id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="comment not found")

    # Hard-delete on the engine first.
    await _engine(engine_hard_delete_comment(current_user, c.external_id))

    # Decrement the parent object's comment_count.
    model = DiscussionsQuestion if target_type == "questions" else DiscussionsAnswer
    db.query(model).filter(model.id == target_id).update(
        {model.comment_count: func.greatest(model.comment_count - 1, 0)},
        synchronize_session=False,
    )

    db.delete(c)
    _log_event(
        db, "comment_hard_deleted", current_user["sub"],
        "question" if target_type == "questions" else "answer",
        target_id,
        payload={"comment_id": comment_id, "external_id": c.external_id},
    )
    db.commit()

    logger.info(
        f"discussions_router.hard_delete_comment: SUCCESS "
        f"id={comment_id} external_id={c.external_id!r} "
        f"by admin sub={current_user.get('sub')!r}"
    )
    return {"hard_deleted": comment_id}


# ── Edits ────────────────────────────────────────────────────────────────
# Author-only, no time limit, no accept-lock. Engine call first, then update
# the AiNxt mirror in the same request so reads (which are served from the
# mirror, not the engine) reflect the edit immediately. The engine also
# enforces its own author check server-side — the _require_owner() gate here
# is defense in depth, not the only line of defense.
# `updated_at` is bumped via _now so AuthorLine's "· edited …" affordance in
# the UI (ai-ui/src/components/Discussions.jsx AuthorLine) lights up.


@router.put("/questions/{question_id}")
async def edit_question(question_id: str, body: EditQuestionReq,
                         current_user: dict = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == question_id).first()
    _require_owner(q, current_user)

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _is_valid, _field_errors, _san = validate_discussion_title_and_tags(body.title, body.tags)
    if not _is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(_field_errors))
    body.title = _san["title"]
    body.tags = _san["tags"]

    title = _redact_and_check(body.title, "question_title_edit")
    content = _redact_and_check(body.content, "question_content_edit")

    await _engine(engine_edit_question(current_user, q.external_id, title, content, body.tags))

    q.title = title
    q.content = content
    q.tags = body.tags
    q.updated_at = _dt.utcnow()
    _log_event(db, "question_edited", current_user["sub"], "question", q.id,
               payload={"title": title})
    db.commit()
    return {"id": q.id, "external_id": q.external_id}


@router.put("/answers/{answer_id}")
async def edit_answer(answer_id: str, body: EditAnswerReq,
                       current_user: dict = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    a = db.query(DiscussionsAnswer).filter(DiscussionsAnswer.id == answer_id).first()
    _require_owner(a, current_user)
    q = db.query(DiscussionsQuestion).filter(DiscussionsQuestion.id == a.question_id).first()
    if q is None:
        raise HTTPException(status_code=404, detail="parent question not found")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, body.content = validate_free_text(body.content)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"content": _errs}))

    content = _redact_and_check(body.content, "answer_content_edit")

    await _engine(engine_edit_answer(current_user, q.external_id, a.external_id, content))

    a.content = content
    a.updated_at = _dt.utcnow()
    _log_event(db, "answer_edited", current_user["sub"], "answer", a.id,
               payload={"question_id": q.id})
    db.commit()
    return {"id": a.id, "external_id": a.external_id}


@router.put("/{target_type}/{target_id}/comments/{comment_id}")
async def edit_comment(target_type: str, target_id: str, comment_id: str,
                        body: EditCommentReq,
                        current_user: dict = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    if target_type not in ("questions", "answers"):
        raise HTTPException(status_code=404, detail="unknown comment target")
    c = db.query(DiscussionsComment).filter(DiscussionsComment.id == comment_id).first()
    _require_owner(c, current_user)

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, body.content = validate_free_text(body.content)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"content": _errs}))

    content = _redact_and_check(body.content, "comment_content_edit")

    await _engine(engine_edit_comment(current_user, c.external_id, content))

    c.content = content
    # DiscussionsComment.updated_at is added by the 2026-07-17 catch-up migration
    # (db/sql/prod_catchup_2026_07_17_discussions_comment_updated_at.sql).
    if hasattr(c, "updated_at"):
        c.updated_at = _dt.utcnow()
    _log_event(db, "comment_edited", current_user["sub"],
               "question" if target_type == "questions" else "answer", target_id,
               payload={"comment_id": comment_id})
    db.commit()
    return {"id": c.id, "external_id": c.external_id}


@router.post("/upload")
async def upload_image(file: FastAPIUploadFile = File(...), current_user: dict = Depends(get_current_user)):
    content = await file.read()
    url = await _engine(engine_upload_file(current_user, file.filename, content, file.content_type or "application/octet-stream"))
    return {"url": url}


@router.get("/uploads/{path:path}")
async def get_upload(path: str):
    """Serves files uploaded via /discussions/upload. The headless engine only
    writes these to disk (its upload_path); it never serves them over HTTP, so
    discussions_engine_client.get_upload reads the bytes straight off disk.
    No auth here: uploaded post images are meant to render inline for anyone
    who can see the post, same as the engine's own default."""
    try:
        content, content_type = engine_get_upload(path)
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        raise HTTPException(status_code=404, detail="upload not found")
    return Response(content=content, media_type=content_type)
