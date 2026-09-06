# SPDX-License-Identifier: MIT
# ============================================================
# KAFKA PRODUCER  (fire-and-forget, sync fallback to Redis log)
# ============================================================
#
# Topics:
#   ainxt.embeddings   — new/updated document embeddings
#   ainxt.chat_history — chat turn records
#   ainxt.audit_log    — compliance + RAG access audit events
#   ainxt.metrics      — token usage, latency, model routing
#
# Usage:
#   from core.kafka_producer import produce
#   produce("ainxt.chat_history", {"user_id": ..., "text": ...})
#
# When Kafka is unavailable the event is written to a Redis list
# (kafka:fallback:{topic}) as a fire-and-forget fallback so no
# events are lost silently.  The kafka_consumer.py drains this
# list on startup as well.
#
# KAFKA_ENABLED guard:
#   Kafka is only used when KAFKA_ENABLED=true is set in the environment.
#   If KAFKA_ENABLED is absent or not "true", all events fall back to Redis
#   immediately — no connection attempt is made, no noisy warnings are logged.
# ============================================================

import json
import time
import threading
import os

from core.logger import logger

# Set KAFKA_BOOTSTRAP to your broker addresses — e.g. broker1:9092,broker2:9092.
# No hardcoded default: this is only read at all when KAFKA_ENABLED=true
# (below), and an unset/unreachable value fails inside the try/except in
# _init_producer(), which already falls back to the Redis event log.
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "")

# Kafka is opt-in — must be explicitly enabled via KAFKA_ENABLED=true.
# This prevents noisy "topic not available" errors in dev/local environments
# where Kafka may not be running.
KAFKA_ENABLED = os.getenv("KAFKA_ENABLED", "false").strip().lower() == "true"

# Topics — prefix driven by KAFKA_TOPIC_PREFIX (defaults to APP_OWNER)
from core.config import KAFKA_TOPIC_PREFIX as _pfx, COACH_EVENT_TOPIC as _coach_topic

TOPIC_EMBEDDINGS    = f"{_pfx}.embeddings"
TOPIC_CHAT_HISTORY  = f"{_pfx}.chat_history"
TOPIC_AUDIT_LOG     = f"{_pfx}.audit_log"
TOPIC_METRICS       = f"{_pfx}.metrics"
TOPIC_THREAD_EVENTS = f"{_pfx}.thread_events"   # thread message persistence + collaboration events
TOPIC_SDLC_EVENTS   = f"{_pfx}.sdlc_events"    # SDLC run create + state transitions (async DB write)
TOPIC_BUDGET_EVENTS = f"{_pfx}.budget_events"   # project budget increments (async DB write)
TOPIC_AGENT_EVENTS  = f"{_pfx}.agent_events"    # agent conversation turns + model usage (async DB write)
TOPIC_COACH_EVENT   = _coach_topic               # AiNxt Coach — normalised per-interaction practice events

# All topics that must exist before the producer is considered ready.
_ALL_TOPICS = [
    TOPIC_EMBEDDINGS,
    TOPIC_CHAT_HISTORY,
    TOPIC_AUDIT_LOG,
    TOPIC_METRICS,
    TOPIC_THREAD_EVENTS,
    TOPIC_SDLC_EVENTS,
    TOPIC_BUDGET_EVENTS,
    TOPIC_COACH_EVENT,
]

# ── Lazy singleton producer ────────────────────────────────────────────────

_producer = None
_producer_lock = threading.Lock()
_kafka_available = False
_kafka_checked = False   # True once we've attempted connection (success or fail)


def _ensure_topics(bootstrap_servers: str, topics: list, retries: int = 5, delay_s: float = 2.0) -> None:
    """
    Pre-create all required Kafka topics via AdminClient so that the producer
    never races against Kafka's auto-create initialization.

    This is called once during producer init.  Uses --if-not-exists semantics:
    existing topics are silently skipped.  Retries up to `retries` times with
    `delay_s` seconds between attempts to handle KRaft leader-election lag on
    a freshly started broker.
    """
    try:
        from kafka.admin import KafkaAdminClient, NewTopic  # type: ignore
        from kafka.errors import TopicAlreadyExistsError    # type: ignore
    except ImportError:
        logger.warning("kafka_producer: KafkaAdminClient not available — skipping topic pre-creation")
        return

    for attempt in range(1, retries + 1):
        try:
            admin = KafkaAdminClient(
                bootstrap_servers=bootstrap_servers,
                client_id="ainxt-topic-setup",
                request_timeout_ms=10_000,
            )
            # Fetch existing topics to skip already-created ones
            existing = set(admin.list_topics())
            new_topics = [
                NewTopic(name=t, num_partitions=3, replication_factor=1)
                for t in topics
                if t not in existing
            ]
            if new_topics:
                try:
                    admin.create_topics(new_topics, validate_only=False)
                    logger.info(
                        f"kafka_producer: pre-created topics: "
                        f"{[t.name for t in new_topics]}"
                    )
                except TopicAlreadyExistsError:
                    pass  # race between workers — harmless
            else:
                logger.info("kafka_producer: all topics already exist")
            admin.close()
            return  # success
        except Exception as e:
            if attempt < retries:
                logger.warning(
                    f"kafka_producer: topic pre-creation attempt {attempt}/{retries} failed "
                    f"({e.__class__.__name__}: {e}) — retrying in {delay_s}s"
                )
                time.sleep(delay_s)
            else:
                logger.error(
                    f"kafka_producer: topic pre-creation failed after {retries} attempts "
                    f"({e.__class__.__name__}: {e}) — topics may not exist yet"
                )


def _get_producer():
    """
    Return a cached KafkaProducer, or None if:
      - KAFKA_ENABLED is not set to 'true' (opt-in guard)
      - kafka-python is not installed
      - broker is unreachable

    Only attempts the broker connection once; subsequent calls return the
    cached result immediately.
    """
    global _producer, _kafka_available, _kafka_checked

    # Fast path: if Kafka is disabled, never even attempt a connection
    if not KAFKA_ENABLED:
        return None

    if _kafka_checked:
        return _producer
    with _producer_lock:
        if _kafka_checked:
            return _producer
        _kafka_checked = True
        try:
            from kafka import KafkaProducer  # type: ignore

            # Pre-create topics BEFORE the producer is created to avoid
            # the "topic not available during auto-create initialization" race.
            _ensure_topics(KAFKA_BOOTSTRAP, _ALL_TOPICS)

            _producer = KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode(),
                key_serializer=lambda k: k.encode() if k else None,
                acks=1,              # leader ACK only — throughput priority
                linger_ms=10,        # batch micro-delay
                retries=3,
                request_timeout_ms=5_000,
                max_block_ms=3_000,  # don't block caller if broker is slow
            )
            _kafka_available = True
            logger.info(f"kafka_producer: connected to {KAFKA_BOOTSTRAP}")
        except ImportError:
            logger.info("kafka_producer: kafka-python not installed — using Redis fallback for all events")
        except Exception as e:
            logger.warning(
                f"kafka_producer: broker unavailable ({e.__class__.__name__}: {e}) "
                f"— using Redis fallback for all events"
            )
    return _producer


def produce(topic: str, event: dict, key: str = None) -> bool:
    """
    Fire-and-forget publish to a Kafka topic.

    Parameters
    ----------
    topic   Kafka topic name.
    event   JSON-serialisable dict payload.
    key     Optional partition key (e.g. user_id for ordering guarantees).

    Returns True if the event was sent to Kafka, False if it fell back to Redis.

    LOGGING (added so a producer failure — e.g. a chat request whose
    model_usages row never appears — is diagnosable from the app log instead
    of failing silently):
      - Kafka send      → INFO   "kafka_producer: PUBLISHED topic=... → Kafka"
      - Redis fallback  → WARNING "kafka_producer: FALLBACK topic=... → Redis (reason=...)"
    grep the app log for "kafka_producer:" to see, per event, whether it went
    to the broker or the Redis fallback queue (drained by
    workers/kafka_consumer.py on startup — if that consumer process is not
    running, events sitting in the Redis fallback queue never reach Postgres).
    """
    _event_tag = event.get("event") or event.get("chat_id") or ""
    producer = _get_producer()
    if producer is not None:
        try:
            producer.send(topic, value=event, key=key)
            # Do NOT call flush() here — fire-and-forget; linger_ms handles batching
            logger.info(
                f"kafka_producer: PUBLISHED topic={topic} key={key} event={_event_tag} → Kafka broker={KAFKA_BOOTSTRAP}"
            )
            return True
        except Exception as e:
            logger.error(f"kafka_producer: send failed for {topic}: {e}")
            # Fall through to Redis fallback

    # ── Redis fallback ──────────────────────────────────────────────────────
    _redis_fallback(topic, event)
    _reason = "KAFKA_ENABLED=false" if not KAFKA_ENABLED else "broker unavailable"
    logger.warning(
        f"kafka_producer: FALLBACK topic={topic} key={key} event={_event_tag} "
        f"→ Redis queue kafka:fallback:{topic} (reason={_reason}) — will be drained into "
        f"Postgres by workers/kafka_consumer.py on its next startup"
    )
    return False


def _redis_fallback(topic: str, event: dict) -> None:
    """Append event to a KV list as a durable fallback when Kafka is down.

    Uses DB=5 (queue KV). Backend selected via REDIS_CLIENT_CONFIG_DB5.
    """
    try:
        from core.config import RDB_QUEUE
        from core.kv import get_kv
        r = get_kv(RDB_QUEUE, decode_responses=True)
        r.rpush(f"kafka:fallback:{topic}", json.dumps(event))
        r.expire(f"kafka:fallback:{topic}", 86400 * 7)  # 7-day TTL
    except Exception as e:
        logger.error(f"kafka_producer: KV fallback also failed for {topic}: {e}")


def flush() -> None:
    """Flush pending batches — call before graceful shutdown."""
    if _producer is not None:
        try:
            _producer.flush(timeout=5)
        except Exception as e:
            logger.warning(f"kafka_producer: flush failed: {e}")
