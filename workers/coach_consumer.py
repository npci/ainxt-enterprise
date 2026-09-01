#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt COACH — Kafka consumer
# ============================================================
#
# Consumes the dedicated coach topic and runs the ingestor on each event
# (redact → encrypt → persist → evaluate). Also drains the Redis fallback
# list on startup so no event emitted while Kafka was down is lost.
#
# Topic consumed:
#   ainxt.coach_event   — normalised per-interaction practice events
#
# In dev (COACH_DIRECT_INGEST=true) the gateway already ingests synchronously,
# so this consumer is a no-op-safe second path: ingest() is idempotent at the
# row level (a fresh event_id per emit). In prod COACH_DIRECT_INGEST=false and
# this consumer is the sole ingestion path.
#
# Run:
#   python workers/coach_consumer.py
# ============================================================

import json
import os
import signal
import sys
import time

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before any DB/config imports.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

# CKMS — decrypt protected env vars before core.config / db.database import.
from core.ckms import load_at_boot as _ckms_load_at_boot
_ckms_load_at_boot()

from core.logger import logger
from core.config import KAFKA_BOOTSTRAP, COACH_EVENT_TOPIC

BATCH_SIZE   = int(os.getenv("COACH_CONSUMER_BATCH", "100"))
POLL_TIMEOUT = float(os.getenv("COACH_CONSUMER_POLL_SECS", "1.0"))
GROUP_ID     = os.getenv("COACH_CONSUMER_GROUP", "ainxt-coach-consumer")

_running = True


def _ingest_one(payload: dict) -> None:
    """Run the ingestor on a single payload. Swallows all errors."""
    try:
        logger.info(
            f"coach_consumer: ingesting user={payload.get('user_id')} channel={payload.get('channel')} "
            f"model={payload.get('model') or '-'} req_id={payload.get('request_id') or '-'} "
            f"thread_id={payload.get('thread_id') or '-'} prompt_len={len((payload.get('prompt') or ''))}"
        )
        from services.coach_ingestor import ingest
        ingest(payload)
    except Exception as e:
        logger.error(f"coach_consumer: ingest failed ({e.__class__.__name__}: {e})")


def _handle_coach_events(records: list) -> None:
    for rec in records:
        if isinstance(rec, dict):
            _ingest_one(rec)


def _drain_redis_fallback() -> None:
    """Drain events written to the Redis fallback list while Kafka was down.

    kafka_producer writes to kafka:fallback:{topic} on DB=5 (RDB_QUEUE)."""
    try:
        from core.config import RDB_QUEUE
        from core.kv import get_kv
        r = get_kv(RDB_QUEUE, decode_responses=True)
        key = f"kafka:fallback:{COACH_EVENT_TOPIC}"
        drained = 0
        while True:
            raw = r.lpop(key)
            if raw is None:
                break
            try:
                _ingest_one(json.loads(raw))
                drained += 1
            except Exception as e:
                logger.warning(f"coach_consumer: fallback record skipped ({e.__class__.__name__})")
        if drained:
            logger.info(f"coach_consumer: drained {drained} fallback event(s) from Redis")
    except Exception as e:
        logger.warning(f"coach_consumer: redis fallback drain failed ({e.__class__.__name__}: {e})")


def _handle_shutdown(sig, frame):
    global _running
    logger.info("coach_consumer: shutdown signal received")
    _running = False


def _run_once() -> None:
    """Connect to Kafka and consume until shutdown or a fatal error.

    Returns normally on shutdown or on broker connection failure so the outer
    retry loop in run() can reconnect."""
    try:
        from kafka import KafkaConsumer  # type: ignore
        consumer = KafkaConsumer(
            COACH_EVENT_TOPIC,
            bootstrap_servers=KAFKA_BOOTSTRAP,
            group_id=GROUP_ID,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            value_deserializer=lambda v: json.loads(v.decode()),
            max_poll_records=BATCH_SIZE,
            session_timeout_ms=30_000,
            heartbeat_interval_ms=10_000,
        )
    except ImportError:
        logger.error("coach_consumer: kafka-python not installed — cannot start consumer")
        return
    except Exception as e:
        logger.error(f"coach_consumer: failed to connect to broker: {e}")
        return

    logger.info("coach_consumer: consumer started")

    try:
        while _running:
            batch = consumer.poll(timeout_ms=int(POLL_TIMEOUT * 1000))
            if not batch:
                continue
            batch_count = 0
            for tp, messages in batch.items():
                batch_count += len(messages or [])
                records = []
                for msg in messages:
                    try:
                        records.append(msg.value)
                    except Exception:
                        pass
                if records:
                    logger.info(
                        f"coach_consumer: polled topic={tp.topic} partition={tp.partition} records={len(records)}"
                    )
                    try:
                        _handle_coach_events(records)
                    except Exception as e:
                        logger.error(f"coach_consumer: handler error: {e}")
            try:
                consumer.commit()
                logger.info(f"coach_consumer: committed batch records={batch_count}")
            except Exception as e:
                logger.error(f"coach_consumer: commit failed: {e}")
    finally:
        try:
            consumer.close()
        except Exception:
            pass
        logger.info("coach_consumer: consumer closed")


def run():
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    logger.info(f"coach_consumer: starting — broker={KAFKA_BOOTSTRAP} topic={COACH_EVENT_TOPIC}")

    _drain_redis_fallback()

    backoff = 5
    while _running:
        try:
            _run_once()
        except Exception as e:
            logger.error(f"coach_consumer: run_once crashed ({e.__class__.__name__}: {e})")

        if not _running:
            break

        logger.warning(f"coach_consumer: not connected — retrying in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    run()
