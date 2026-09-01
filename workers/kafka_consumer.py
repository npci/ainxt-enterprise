#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# KAFKA CONSUMER — Postgres bulk writer
# ============================================================
#
# Consumes from four Kafka topics and bulk-writes to Postgres.
# Also drains the Redis fallback lists on startup.
#
# Topics consumed:
#   ainxt.embeddings   — update document_embeddings.content_hash / metadata
#   ainxt.chat_history — insert chat_messages rows
#   ainxt.audit_log    — insert rag_access_log + model_usages rows
#   ainxt.metrics      — insert model_usages (token/cost metrics)
#
# Run:
#   python workers/kafka_consumer.py
#
# Or as a systemd service: deploy/ainxt-kafka-consumer.service
# ============================================================

import json
import logging
import os
import signal
import sys
import time

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before any DB/config imports so POSTGRES_USER etc. are set
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

# CKMS — decrypt POSTGRES_PASSWORD / REDIS_PASSWORD etc. before core.config /
# db.database are imported.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from core.logger import logger, _LOG_DIR, SizeAndTimeRotatingFileHandler, LOG_MAX_BYTES, LOG_ROTATION_WHEN, LOG_ROTATION_INTERVAL, LOG_BACKUP_COUNT, LOG_ROTATION_UTC
from core.config import KAFKA_BOOTSTRAP

# ============================================================
# DEDICATED USER-PROMPT LOG  —  log/app/user_prompts.log
# One JSON line per compliance-blocked user prompt arriving via Kafka.
# Fields: timestamp, user_id, user_name, login_id, chat_id, request_id, prompt
# ============================================================

_PROMPT_LOG_FILE = os.path.join(_LOG_DIR, "user_prompts.log")

_prompt_file_logger = logging.getLogger("ainxt.user_prompts")
_prompt_file_logger.setLevel(logging.INFO)
_prompt_file_logger.propagate = False  # do not leak into root / agent.log

if not any(
    isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == _PROMPT_LOG_FILE
    for h in _prompt_file_logger.handlers
):
    _ph = SizeAndTimeRotatingFileHandler(
        filename=_PROMPT_LOG_FILE,
        max_bytes=LOG_MAX_BYTES,
        when=LOG_ROTATION_WHEN,
        interval=LOG_ROTATION_INTERVAL,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        errors="replace",
        utc=LOG_ROTATION_UTC,
        delay=False,
    )
    _ph.setLevel(logging.INFO)
    _ph.setFormatter(logging.Formatter("%(message)s"))
    _prompt_file_logger.addHandler(_ph)


def _log_user_prompt(
    *,
    timestamp: str,
    user_id: str,
    user_name: str,
    login_id: str,
    chat_id: str,
    request_id: str,
    prompt: str,
    compliance_blocked: bool = False,
    block_reason: str = "",
    block_policy: str = "",
    block_category: str = "",
    confidence_score: float | None = None,
) -> None:
    """
    Write a single JSON line to user_prompts.log for every compliance-blocked
    user-initiated prompt.

    Fields:
      timestamp          – UTC ISO-8601 ms timestamp of the event
      user_id            – authenticated user identifier
      user_name          – display name of the authenticated user
      login_id           – login / AD identifier / email used for the user
      chat_id            – conversation / session identifier
      request_id         – gateway request correlation ID
      prompt             – the raw user question text
      compliance_blocked – True when the compliance/guardrail engine rejected the prompt
      block_reason       – comma-separated violation types (e.g. "pci_pan,pii_mobile")
      block_policy       – policy name that triggered the block
                           (e.g. "AI Safety policy", "compliance policy")
      block_category     – semantic category that triggered the block
                           (e.g. "criminal_justice" for HardBlock, or
                           "pci_pan,pii_mobile" for PCI/PII violations).
      confidence_score   – Optional float in [0.0, 1.0] indicating the safety
                           stack's confidence in the block. Computed by
                           core.confidence_scorer.compute_block_confidence on
                           the gateway side; None when the producer did not
                           send a score. Higher values = stronger confidence.
    """
    try:
        # Normalize confidence_score: coerce numeric strings, clamp into
        # [0, 1], and emit None when the value is missing or unparseable.
        _normalized_confidence: float | None
        if confidence_score is None:
            _normalized_confidence = None
        else:
            try:
                _cs = float(confidence_score)
                if _cs < 0.0:
                    _cs = 0.0
                elif _cs > 1.0:
                    _cs = 1.0
                _normalized_confidence = round(_cs, 4)
            except (TypeError, ValueError):
                _normalized_confidence = None

        _prompt_file_logger.info(
            json.dumps(
                {
                    "timestamp":          timestamp,
                    "user_id":            user_id,
                    "user_name":          user_name,
                    "login_id":           login_id,
                    "chat_id":            chat_id,
                    "request_id":         request_id,
                    "prompt":             prompt,
                    "compliance_blocked": compliance_blocked,
                    "block_reason":       block_reason,
                    "block_policy":       block_policy,
                    "block_category":     block_category,
                    "confidence_score":   _normalized_confidence,
                },
                ensure_ascii=False,
            )
        )
    except Exception as _e:
        logger.warning(f"kafka_consumer: failed to write user_prompts.log: {_e}")


from core.kafka_producer import (
    TOPIC_EMBEDDINGS, TOPIC_CHAT_HISTORY, TOPIC_AUDIT_LOG, TOPIC_METRICS,
    TOPIC_THREAD_EVENTS, TOPIC_SDLC_EVENTS, TOPIC_BUDGET_EVENTS, TOPIC_AGENT_EVENTS,
)

TOPICS = [
    TOPIC_EMBEDDINGS,
    TOPIC_CHAT_HISTORY,
    TOPIC_AUDIT_LOG,
    TOPIC_METRICS,
    TOPIC_THREAD_EVENTS,   # thread message persistence (was produced but never consumed)
    TOPIC_SDLC_EVENTS,     # SDLC run create + state transitions
    TOPIC_BUDGET_EVENTS,   # project budget increments
    TOPIC_AGENT_EVENTS,    # agent conversation turns + model usage
]

BATCH_SIZE    = 100    # rows per DB transaction
POLL_TIMEOUT  = 1.0    # seconds


# ============================================================
# HANDLERS — one per topic
# ============================================================

def _handle_embeddings(records: list) -> None:
    """Update content_hash / metadata columns on existing embedding rows."""
    if not records:
        return
    from db.database import VectorSessionLocal
    from db.models import DocumentEmbedding
    from sqlalchemy import text as _sql
    db = VectorSessionLocal()
    try:
        for rec in records:
            chunk_id    = rec.get("chunk_id")
            content_hash = rec.get("content_hash")
            if chunk_id and content_hash:
                db.execute(
                    _sql("UPDATE document_embeddings SET content_hash=:h WHERE id=:id"),
                    {"h": content_hash, "id": chunk_id},
                )
        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: embeddings handler failed: {e}")
        db.rollback()
    finally:
        db.close()


def _handle_chat_history(records: list) -> None:
    """
    Upsert chat rows and insert user+assistant ChatMessage pairs.
    Handles two event shapes:
      1. Legacy (role/content) — inserted as a single message row.
      2. Full chat turn (question/answer) — upserts the Chat record + inserts 2 rows.
    """
    if not records:
        return
    from db.database import SessionLocal
    from db.models import Chat, ChatMessage
    import uuid as _uuid
    from datetime import datetime as _dt
    db = SessionLocal()
    try:
        for rec in records:
            chat_id = rec.get("chat_id")
            if not chat_id:
                logger.warning(f"[kafka consumer] chat ID not present")
                continue

            # Full turn event (produced by gateway.py fire-and-forget path)
            if "question" in rec and "answer" in rec:
                logger.info("[kafka consumer] : Inside kafka async persistence")
                user_id    = rec.get("user_id", "")

                # ── Prompt audit log ───────────────────────────────────
                # Written only for compliance-blocked prompts.
                # compliance_blocked=True  → prompt was blocked by the guardrail engine
                # block_reason             → comma-separated violation types (e.g. "pci_pan")
                try:
                    from datetime import datetime as _dt_p, timezone as _tz_p
                    _is_blocked    = bool(rec.get("compliance_blocked", False))
                    _block_reason  = rec.get("block_reason") or ""
                    _block_policy  = rec.get("block_policy") or ""
                    # Backward compatibility: accept the legacy "hardblock_category"
                    # field if a producer still sends it.
                    _block_category = (
                        rec.get("block_category")
                        or rec.get("hardblock_category")
                        or ""
                    )
                    # confidence_score is optional; may be absent on legacy
                    # events emitted before the scorer was introduced.
                    _confidence_score = rec.get("confidence_score", None)
                    if _is_blocked:
                        _log_user_prompt(
                            timestamp          = _dt_p.now(_tz_p.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                            user_id            = user_id or "",
                            user_name          = rec.get("user_name", "") or "",
                            login_id           = rec.get("login_id", "") or "",
                            chat_id            = chat_id or "",
                            request_id         = rec.get("request_id") or "",
                            prompt             = rec.get("question", ""),
                            compliance_blocked = True,
                            block_reason       = _block_reason,
                            block_policy       = _block_policy,
                            block_category     = _block_category,
                            confidence_score   = _confidence_score,
                        )
                        logger.info(
                            "[kafka consumer] compliance-blocked prompt logged",
                            user_id=user_id, chat_id=chat_id,
                            block_reason=_block_reason,
                            block_policy=_block_policy,
                            block_category=_block_category,
                            confidence_score=_confidence_score,
                        )
                except Exception as _pe:
                    logger.warning(f"kafka_consumer: prompt audit log error: {_pe}")
                # ──────────────────────────────────────────────────────

                # Compliance-blocked prompts: skip DB persistence — only the
                # prompt audit log entry above is needed for monitoring.
                if rec.get("compliance_blocked"):
                    continue
                title_hint = rec.get("title_hint") or rec.get("question", "")[:400]
                chat = db.query(Chat).filter(Chat.id == chat_id).first()
                if not chat:
                    logger.info("[kafka consumer] : Adding new Chat")
                    db.add(Chat(
                        id=chat_id,
                        user_id=user_id if user_id not in ("default", "") else None,
                        title=title_hint[:400],
                        project_id=rec.get("project_id") or None,
                        # Channel isolation: preserve the producer's client_source
                        # (e.g. "office" for Buddy turns) Defaults to "platform"
                        # for legacy events that predate this field.
                        client_source=rec.get("client_source") or "platform",
                    ))
                else:
                    if chat.title in ("New Chat", "", None) and title_hint:
                        chat.title = title_hint[:400]
                    chat.updated_at = _dt.utcnow()
                # Allow the producer to pin specific UUIDs so the streaming
                # client (gateway __meta__) and the persisted Postgres rows
                # share the same id — needed for Continue / Edit endpoints.
                _user_msg_id = rec.get("user_message_id") or str(_uuid.uuid4())
                _ast_msg_id  = rec.get("assistant_message_id") or str(_uuid.uuid4())
                _rec_rag_mode = rec.get("rag_mode") or None
                db.add(ChatMessage(
                    id=_user_msg_id,
                    chat_id=chat_id,
                    role="user",
                    content=rec.get("question", ""),
                    attachment_ids=rec.get("attachment_ids") or [],
                    rag_mode=_rec_rag_mode,
                ))
                db.add(ChatMessage(
                    id=_ast_msg_id,
                    chat_id=chat_id,
                    role="assistant",
                    content=rec.get("answer", ""),
                    model_used=rec.get("model") or None,
                    tokens_used=(rec.get("in_tok", 0) or 0) + (rec.get("out_tok", 0) or 0),
                    in_tok=rec.get("in_tok") or None,
                    out_tok=rec.get("out_tok") or None,
                    latency=float(rec["latency"]) if rec.get("latency") else None,
                    cost_usd=rec.get("cost") or None,
                    language=rec.get("language") or None,
                    rag_mode=_rec_rag_mode,
                ))
                logger.info("[chat worker] : Added chat message for user and assistant")
            else:
                # Legacy single-row insert (from chat_worker.py produce path)
                db.add(ChatMessage(
                    chat_id=chat_id,
                    role=rec.get("role", "assistant"),
                    content=rec.get("content", ""),
                ))
        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: chat_history handler failed: {e}")
        db.rollback()
    finally:
        db.close()


def _handle_audit_log(records: list) -> None:
    """Bulk-insert rag_access_log rows from audit events."""
    if not records:
        return
    from db.database import SessionLocal
    from db.models import RAGAccessLog
    db = SessionLocal()
    try:
        for rec in records:
            if rec.get("event_type") != "rag_access":
                continue
            db.add(RAGAccessLog(
                user_id=rec.get("user_id", ""),
                user_role=rec.get("user_role", ""),
                org_id=rec.get("org_id", ""),
                query_hash=rec.get("query_hash", ""),
                chunk_id=rec.get("chunk_id", ""),
                repo=rec.get("repo", ""),
                file_path=rec.get("file_path", ""),
                classification=rec.get("classification", "INTERNAL"),
                access_granted=rec.get("access_granted", True),
                deny_reason=rec.get("deny_reason", ""),
                session_id=rec.get("session_id", ""),
            ))
        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: audit_log handler failed: {e}")
        db.rollback()
    finally:
        db.close()


def _handle_metrics(records: list) -> None:
    """
    Bulk-insert model_usages rows from ainxt.metrics events.

    user_id is OPTIONAL — model_usages.user_id is a nullable FK (an endpoint's
    system_user_id, a CLI anonymous request, etc. may legitimately have none).
    A missing/non-UUID user_id stores NULL rather than dropping the row, so
    the row (and its cost/token audit trail) is never silently discarded —
    this mirrors memory.postgres_memory.PostgresMemory.create_model_usage(),
    which every daemon-thread call site used to call directly before being
    migrated onto this topic.
    """
    if not records:
        return
    import uuid as _uuid
    from db.database import SessionLocal
    from db.models import ModelUsage
    db = SessionLocal()
    try:
        for rec in records:
            uid = rec.get("user_id")
            if uid:
                try:
                    _uuid.UUID(str(uid))
                except Exception:
                    uid = None  # store NULL instead of dropping the row
            in_tok = rec.get("input_tokens", rec.get("prompt_tokens", 0)) or 0
            out_tok = rec.get("output_tokens", rec.get("completion_tokens", 0)) or 0
            total = rec.get("total_tokens") or (in_tok + out_tok)
            db.add(ModelUsage(
                user_id=uid,
                agent_id=rec.get("agent_id"),
                project_id=rec.get("project_id"),
                product_id=rec.get("product_id"),
                endpoint=rec.get("endpoint"),
                source_channel=rec.get("source_channel"),
                model=rec.get("model", "unknown"),
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=total,
                latency_ms=rec.get("latency_ms"),
                cost_usd=rec.get("cost_usd", 0.0),
                request_id=rec.get("request_id"),
                cache_read_tokens=rec.get("cache_read_tokens", 0) or 0,
                cache_write_tokens=rec.get("cache_write_tokens", 0) or 0,
            ))
        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: metrics handler failed: {e}")
        db.rollback()
    finally:
        db.close()


def _handle_thread_events(records: list) -> None:
    """Bulk-insert thread_messages rows and bump thread updated_at."""
    if not records:
        return
    from db.database import SessionLocal
    from db.models import ThreadMessage, Thread
    import uuid as _uuid
    from datetime import datetime as _dt
    db = SessionLocal()
    try:
        thread_ids_seen: set = set()
        for rec in records:
            if rec.get("event") != "message_added":
                continue
            msg_id = rec.get("message_id")
            if not msg_id:
                continue
            db.add(ThreadMessage(
                id=msg_id,
                thread_id=rec.get("thread_id"),
                content=rec.get("content", ""),
                author=rec.get("author", ""),
                author_name=rec.get("author_name") or None,
                author_band=rec.get("author_band"),
                message_type=rec.get("message_type", "text"),
                mentions=rec.get("mentions", []),
                parent_message_id=rec.get("parent_message_id") or None,
                model_used=rec.get("model_used") or None,
                tokens_in=rec.get("tokens_in") or None,
                tokens_out=rec.get("tokens_out") or None,
                cost_usd=rec.get("cost_usd") or None,
                latency_ms=rec.get("latency_ms") or None,
                reactions={},
            ))
            thread_ids_seen.add(rec.get("thread_id"))
        # Bump updated_at on all touched threads
        for tid in thread_ids_seen:
            t = db.query(Thread).filter(Thread.id == tid).first()
            if t:
                t.updated_at = _dt.utcnow()
        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: thread_events handler failed: {e}")
        db.rollback()
    finally:
        db.close()


def _handle_sdlc_events(records: list) -> None:
    """Persist SDLC run creates and state transitions to Postgres.

    Dedupe design (W-J, 2026-06-04):
    - run_state_changed: ONLY updates the SDLCRun row.  It no longer inserts
      an SDLCRunEvent row — that was the primary source of duplicate events
      (update_run_state emits run_state_changed AND add_run_event emits
      run_event_appended for the same logical transition).
    - run_event_appended: uses INSERT ... ON CONFLICT (dedupe_key) DO NOTHING
      so that Kafka replay / double-produce for the same logical event (same
      UUID) cannot produce duplicate rows.  dedupe_key = event_id (UUID minted
      by add_run_event()).  Two genuinely distinct same-second events have
      different UUIDs → both persist.
    """
    if not records:
        return
    from db.database import SessionLocal
    from db.models import SDLCRun
    from sqlalchemy import text as _sqlt
    from datetime import datetime as _dt
    import json as _json
    db = SessionLocal()
    try:
        # SEC-F-MISC-008: verify HMAC signature once, before dispatching on
        # event type, so ALL event variants (run_created, run_state_changed,
        # run_event_appended, …) are covered by the same trust boundary.
        # Previously the check was applied only inside the run_event_appended
        # branch, leaving run_created and run_state_changed unconditionally
        # trusted — an attacker who can write to the Kafka topic could forge
        # those events with arbitrary run_id / jira_key / repo values.
        from core.audit_signer import verify_event as _verify_event
        for rec in records:
            event = rec.get("event")
            run_id = rec.get("run_id")
            if not run_id:
                continue

            # Signature check — reject any event whose HMAC doesn't verify.
            _sig = rec.get("signature", "")
            if not _verify_event(rec, _sig):
                logger.warning(
                    f"kafka_consumer: sdlc event signature mismatch "
                    f"(event={event!r} run={run_id}) — skipping"
                )
                continue

            if event == "run_created":
                # Idempotent insert (W-race, 2026-08-06).
                #
                # The synchronous writer in sdlc_store.create_run() now commits
                # the initial row and OWNS it before this event is even
                # published, so this consumer path is normally a no-op. It stays
                # as a safety net for the Kafka-only fallback (sync DB write was
                # unavailable).
                #
                # It MUST be race-safe: a read-then-add (query().first() then
                # db.add()) could still lose the race against the sync writer and
                # raise UniqueViolation on sdlc_runs_pkey at commit time. Because
                # every record in this batch shares ONE transaction, that abort
                # would roll back the WHOLE batch — including a later
                # run_state_changed carrying the suspend to
                # AWAITING_DOMAIN_APPROVAL — and leave the UI-polled row stuck.
                #
                # INSERT ... ON CONFLICT (id) DO NOTHING is atomic and never
                # aborts the transaction, so subsequent state transitions in the
                # same batch always persist.
                _created_dt = (
                    _dt.fromisoformat(rec["ts"]) if rec.get("ts") else _dt.utcnow()
                )
                db.execute(
                    _sqlt(
                        "INSERT INTO sdlc_runs "
                        "(id, type, jira_key, jira_summary, repo, state, "
                        " context, triggered_by, created_at, updated_at) "
                        "VALUES (:id, :type, :jira_key, :jira_summary, :repo, "
                        " :state, CAST(:context AS jsonb), :triggered_by, "
                        " :created_at, :updated_at) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id":           run_id,
                        "type":         rec.get("run_type", ""),
                        "jira_key":     rec.get("jira_key", ""),
                        "jira_summary": rec.get("jira_summary", ""),
                        "repo":         rec.get("repo", ""),
                        "state":        "CREATED",
                        "context":      _json.dumps({}),
                        "triggered_by": rec.get("triggered_by", ""),
                        "created_at":   _created_dt,
                        "updated_at":   _created_dt,
                    },
                )

            elif event == "run_state_changed":
                # ONLY update the SDLCRun row — no event insert.
                # The companion add_run_event() call (if any) produces its own
                # run_event_appended message which is inserted below.  Writing
                # an event row here was the primary cause of the duplicate-event
                # problem (one row from run_state_changed + one from
                # run_event_appended for the same logical transition).
                #
                # Create-if-missing (W-race, 2026-08-06): if the run_created
                # insert never landed (sync DB write was down AND the
                # run_created event was lost/reordered), a transition would
                # otherwise be silently dropped and the UI-polled row would stay
                # stuck in its initial state — never reaching the suspend to
                # AWAITING_DOMAIN_APPROVAL. Insert a minimal row idempotently so
                # the subsequent update below always has a row to write to.
                _skeleton_dt = (
                    _dt.fromisoformat(rec["ts"]) if rec.get("ts") else _dt.utcnow()
                )
                db.execute(
                    _sqlt(
                        "INSERT INTO sdlc_runs (id, type, state, context, created_at, updated_at) "
                        "VALUES (:id, :type, :state, CAST(:context AS jsonb), :ts, :ts) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {
                        "id":      run_id,
                        "type":    rec.get("run_type", ""),
                        "state":   "CREATED",
                        "context": _json.dumps({}),
                        "ts":      _skeleton_dt,
                    },
                )
                db.flush()
                db_run = db.query(SDLCRun).filter(SDLCRun.id == run_id).first()
                if db_run:
                    db_run.state = rec.get("to_state", db_run.state)
                    if rec.get("current_stage") is not None:
                        db_run.current_stage = rec["current_stage"]
                    if rec.get("context_patch"):
                        db_run.context = {**(db_run.context or {}), **rec["context_patch"]}
                    if rec.get("branch"):
                        db_run.branch = rec["branch"]
                    if rec.get("pr_number") is not None:
                        db_run.pr_number = rec["pr_number"]
                    if rec.get("pr_url"):
                        db_run.pr_url = rec["pr_url"]
                    if rec.get("confluence_url"):
                        db_run.confluence_url = rec["confluence_url"]
                    if rec.get("error"):
                        db_run.error = rec["error"]
                    db_run.updated_at = _dt.utcnow()

            elif event == "run_event_appended":
                # Serialised insert from add_run_event().  All events come
                # through this single consumer so concurrent gateway instances
                # cannot race.  INSERT ... ON CONFLICT (dedupe_key) DO NOTHING
                # suppresses exact duplicates (same UUID) from:
                #   a) Kafka replay / at-least-once redelivery.
                #   b) A partial Kafka produce that raised an exception after
                #      enqueue but before ack — causing the fallback direct-
                #      insert in sdlc_store to fire alongside the consumer.
                #
                # Note: signature verification (SEC-F-MISC-008) is now done
                # once at the top of the loop, before dispatching on event type,
                # so all event variants share the same trust boundary.

                event_id  = rec.get("event_id")
                dedupe_key = rec.get("dedupe_key") or event_id   # event_id IS the key
                created_at = (
                    _dt.fromisoformat(rec["ts"]) if rec.get("ts") else _dt.utcnow()
                )
                try:
                    db.execute(
                        _sqlt(
                            "INSERT INTO sdlc_run_events "
                            "(id, run_id, from_state, to_state, stage, actor, output, data, signature, dedupe_key, created_at) "
                            "VALUES (:id, :run_id, :from_state, :to_state, :stage, :actor, :output, CAST(:data AS jsonb), :signature, :dedupe_key, :created_at) "
                            "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"
                        ),
                        {
                            "id":         event_id,
                            "run_id":     run_id,
                            "from_state": rec.get("from_state", ""),
                            "to_state":   rec.get("to_state", ""),
                            "stage":      (rec.get("stage") or "")[:100],
                            "actor":      (rec.get("actor") or "system")[:255],
                            "output":     (rec.get("output") or "")[:2000],
                            "data":       _json.dumps(rec.get("data") or {}),
                            "signature":  rec.get("signature", ""),
                            "dedupe_key": dedupe_key,
                            "created_at": created_at,
                        },
                    )
                except Exception as _ev_err:
                    logger.warning(
                        f"kafka_consumer: sdlc_run_event_appended insert failed "
                        f"(run={run_id} event_id={event_id}): {_ev_err}"
                    )

        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: sdlc_events handler failed: {e}")
        db.rollback()
    finally:
        db.close()


def _handle_budget_events(records: list) -> None:
    """Apply project budget increments and fire inbox alerts at thresholds."""
    if not records:
        return
    from db.database import SessionLocal
    from db.models import ProjectRecord
    db = SessionLocal()
    try:
        for rec in records:
            if rec.get("event") != "project_budget_incremented":
                continue
            project_id = rec.get("project_id")
            cost_usd   = float(rec.get("cost_usd", 0.0))
            if not project_id or cost_usd <= 0:
                continue
            proj = db.query(ProjectRecord).filter(ProjectRecord.id == project_id).first()
            if not proj:
                continue
            proj.budget_used_usd = (proj.budget_used_usd or 0.0) + cost_usd
            # Threshold alerts — 80% and 100%
            if proj.budget_limit_usd:
                _pct = proj.budget_used_usd / proj.budget_limit_usd
                if _pct >= 1.0 or (0.8 <= _pct < 1.0 and round(_pct, 2) == 0.80):
                    try:
                        from store.inbox_store import publish_inbox_item as _pub
                        _pub(
                            user_id=rec.get("user_id", ""),
                            type="budget_alert",
                            title=f"Budget {'exceeded' if _pct >= 1.0 else 'at 80%'} for project '{proj.name}'",
                            body=f"Used ${proj.budget_used_usd:.4f} of ${proj.budget_limit_usd:.2f}",
                            source_id=str(proj.id),
                            metadata={"project_id": str(proj.id)},
                        )
                    except Exception:
                        pass
        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: budget_events handler failed: {e}")
        db.rollback()
    finally:
        db.close()


def _handle_agent_events(records: list) -> None:
    """
    Persist agent conversation turns and model usage to Postgres.
    Event shape (produced by AgentRunner._run_inner):
      {
        "event":        "conversation_turn",
        "session_id":   str,
        "agent_name":   str,
        "run_id":       str,
        "user_id":      str | None,
        "user_message": str,
        "answer":       str | None,   # None when blocked by compliance
        "blocked":      bool,
        "model":        str,
        "in_tok":       int,
        "out_tok":      int,
        "cost_usd":     float,
        "duration_ms":  float,
      }
    """
    if not records:
        return
    from memory.postgres_memory import PostgresMemory as _PM
    from db.database import SessionLocal
    from db.models import ModelUsage
    from core.time_utils import now_ist as _now_ist_agents
    import uuid as _uuid

    pm  = _PM()
    db  = SessionLocal()
    try:
        for rec in records:
            if rec.get("event") != "conversation_turn":
                continue

            session_id   = rec.get("session_id")
            agent_name   = rec.get("agent_name", "")
            run_id       = rec.get("run_id", "")
            user_id      = rec.get("user_id")
            user_message = rec.get("user_message", "")
            answer       = rec.get("answer")
            blocked      = rec.get("blocked", False)

            # ── Conversation rows (conversations table via PostgresMemory) ──
            if session_id and user_message:
                _meta = {"agent": agent_name, "run_id": run_id}
                if user_id:
                    _meta["user_id"] = user_id
                try:
                    pm.save_message(session_id, "user", user_message, metadata=_meta)
                    if not blocked and answer:
                        pm.save_message(session_id, "assistant", answer, metadata=_meta)
                except Exception as _me:
                    logger.warning(f"kafka_consumer: agent conversation save failed: {_me}")

            # ── Model usage row ─────────────────────────────────────────────
            model    = rec.get("model")
            in_tok   = int(rec.get("in_tok", 0) or 0)
            out_tok  = int(rec.get("out_tok", 0) or 0)
            if model:
                # user_id must be a valid UUID or NULL (matches model_usages FK)
                _uid = user_id
                if _uid:
                    try:
                        import uuid as _uv
                        _uv.UUID(str(_uid))
                    except (ValueError, AttributeError):
                        _uid = None
                db.add(ModelUsage(
                    id=str(_uuid.uuid4()),
                    user_id=_uid,
                    agent_id=agent_name,
                    endpoint=f"/agents/{agent_name}/run",
                    source_channel="AGENTS",
                    model=model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=in_tok + out_tok,
                    latency_ms=rec.get("duration_ms"),
                    cost_usd=rec.get("cost_usd"),
                    request_id=run_id,
                    # IST, not UTC — matches ModelUsage.created_at's default
                    # (see db.models._now_ist) for every other model_usages
                    # insert path; explicit here because this handler passes
                    # created_at itself instead of relying on the column default.
                    created_at=_now_ist_agents(),
                ))

        db.commit()
    except Exception as e:
        logger.error(f"kafka_consumer: agent_events handler failed: {e}")
        db.rollback()
    finally:
        try:
            pm.close()
        except Exception:
            pass
        db.close()


_HANDLERS = {
    TOPIC_EMBEDDINGS:    _handle_embeddings,
    TOPIC_CHAT_HISTORY:  _handle_chat_history,
    TOPIC_AUDIT_LOG:     _handle_audit_log,
    TOPIC_METRICS:       _handle_metrics,
    TOPIC_THREAD_EVENTS: _handle_thread_events,
    TOPIC_SDLC_EVENTS:   _handle_sdlc_events,
    TOPIC_BUDGET_EVENTS: _handle_budget_events,
    TOPIC_AGENT_EVENTS:  _handle_agent_events,
}


# ============================================================
# REDIS FALLBACK DRAIN
# ============================================================

def _drain_redis_fallback() -> None:
    """
    On startup, drain any events that were written to the KV fallback lists
    while Kafka was unavailable. Reads from DB=5 (queue KV).
    """
    try:
        from core.config import RDB_QUEUE
        from core.kv import get_kv
        r = get_kv(RDB_QUEUE, decode_responses=True)
        for topic in TOPICS:
            key   = f"kafka:fallback:{topic}"
            count = 0
            batch = []
            while True:
                raw = r.lpop(key)
                if raw is None:
                    break
                try:
                    batch.append(json.loads(raw))
                except Exception:
                    pass
                if len(batch) >= BATCH_SIZE:
                    handler = _HANDLERS.get(topic)
                    if handler:
                        handler(batch)
                    count += len(batch)
                    batch = []
            if batch:
                handler = _HANDLERS.get(topic)
                if handler:
                    handler(batch)
                count += len(batch)
            if count:
                logger.info(f"kafka_consumer: drained {count} KV fallback events for {topic}")
    except Exception as e:
        logger.error(f"kafka_consumer: KV fallback drain failed: {e}")


# ============================================================
# MAIN CONSUMER LOOP
# ============================================================

_running = True


def _handle_shutdown(sig, frame):
    global _running
    logger.info("kafka_consumer: received shutdown signal")
    _running = False


def run():
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    logger.info(f"kafka_consumer: starting — broker={KAFKA_BOOTSTRAP} topics={TOPICS}")

    _drain_redis_fallback()

    try:
        from kafka import KafkaConsumer  # type: ignore
        consumer = KafkaConsumer(
            *TOPICS,
            bootstrap_servers=KAFKA_BOOTSTRAP,
            group_id="ainxt-postgres-writer",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            value_deserializer=lambda v: json.loads(v.decode()),
            max_poll_records=BATCH_SIZE,
            session_timeout_ms=30_000,
            heartbeat_interval_ms=10_000,
        )
    except ImportError:
        logger.error("kafka_consumer: kafka-python not installed — cannot start consumer")
        return
    except Exception as e:
        logger.error(f"kafka_consumer: failed to connect to broker: {e}")
        return

    logger.info("kafka_consumer: consumer started")

    try:
        while _running:
            batch: dict = consumer.poll(timeout_ms=int(POLL_TIMEOUT * 1000))
            if not batch:
                continue

            for tp, messages in batch.items():
                topic   = tp.topic
                handler = _HANDLERS.get(topic)
                records = []
                for msg in messages:
                    try:
                        records.append(msg.value)
                    except Exception:
                        pass
                if handler and records:
                    try:
                        for i in range(0, len(records), BATCH_SIZE):
                            handler(records[i:i + BATCH_SIZE])
                    except Exception as e:
                        logger.error(f"kafka_consumer: handler error for {topic}: {e}")

            try:
                consumer.commit()
            except Exception as e:
                logger.error(f"kafka_consumer: commit failed: {e}")

    finally:
        consumer.close()
        logger.info("kafka_consumer: consumer closed")


if __name__ == "__main__":
    run()
