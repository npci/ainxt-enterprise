# SPDX-License-Identifier: MIT
# ============================================================
# GRAPH AUDIT — tamper-evident boundary log for Teams/Office
#
# Scope doc §7.4: PCI-aligned controls, full audit logging, and
# tamper-evident logs for AI interactions. Every time data crosses
# the AiNxt ↔ Microsoft boundary (Graph ingest, MoM summarize, Teams
# / Outlook send, subscription create, OBO exchange) we append one
# row here.
#
# DATA-SOVEREIGNTY RULE (§2.1 / §7.3): we store ONLY a SHA-256
# data_hash of the payload — NEVER the raw transcript, summary, or
# prompt. The hash proves "this exact content was processed" without
# the content ever leaving the boundary into the audit store.
#
# Tamper-evidence: each row carries a per-stream monotonic `seq`, a
# `prev_hash` (the previous row's signature → hash-chain), and an
# HMAC-SHA256 `signature` over the canonical row (core/audit_signer).
# Editing or deleting any row breaks the chain at that point, which
# verify_stream() detects.
# ============================================================

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError

from db.database import SessionLocal, DB_SCHEMA
from core.audit_signer import sign_event, verify_event
from core.logger import logger

# Boundary event names (keep in sync with the column comment in 03_tables.sql)
EVENT_GRAPH_INGEST   = "graph_ingest"
EVENT_MOM_SUMMARIZE  = "mom_summarize"
EVENT_TEAMS_SEND     = "teams_send"
EVENT_OUTLOOK_SEND   = "outlook_send"
EVENT_SUBSCRIPTION   = "subscription_create"
EVENT_OBO_EXCHANGE   = "obo_exchange"

_MAX_RETRIES = 5  # per-stream (stream, seq) UNIQUE race retries


def _hash_payload(data: Any) -> str:
    """SHA-256 hex of the payload. Never stores the payload itself.

    Accepts str / bytes / anything JSON-serialisable. dicts/lists are
    canonicalised (sorted keys) so the same logical content hashes stably.
    """
    if data is None:
        b = b""
    elif isinstance(data, bytes):
        b = data
    elif isinstance(data, str):
        b = data.encode("utf-8")
    else:
        try:
            b = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            b = str(data).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def record(
    stream: str,
    event: str,
    *,
    data: Any = None,
    user_id: Optional[str] = None,
    resource: Optional[str] = None,
    meta: Optional[dict] = None,
) -> Optional[dict]:
    """Append one tamper-evident audit entry to a stream.

    Args:
        stream:   logical chain id, e.g. "meeting:{meeting_id}" or "obo:{user_id}".
        event:    one of the EVENT_* constants.
        data:     the payload that crossed the boundary — hashed, never stored.
        user_id:  AiNxt user id (JWT sub) the action is attributed to.
        resource: Microsoft resource id (meeting / message / subscription id).
        meta:     small, NON-SENSITIVE counters only (token counts, byte sizes,
                  model name). Stored as JSONB and included in the signature.

    Returns the written row summary {stream, seq, signature, data_hash} or None
    if the write failed (audit is best-effort durable — it logs but never raises
    into the caller's boundary operation).
    """
    data_hash = _hash_payload(data)
    meta = meta or {}
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    for attempt in range(_MAX_RETRIES):
        db = SessionLocal()
        try:
            row = db.execute(
                _text(
                    f"SELECT seq, signature FROM {DB_SCHEMA}.graph_audit_log "
                    f"WHERE stream = :s ORDER BY seq DESC LIMIT 1"
                ),
                {"s": stream},
            ).first()

            seq       = (row[0] + 1) if row else 1
            prev_hash = row[1] if row else None

            # The exact dict that is signed AND reconstructable from the DB row.
            event_dict = {
                "stream":     stream,
                "seq":        seq,
                "event":      event,
                "user_id":    user_id,
                "resource":   resource,
                "data_hash":  data_hash,
                "meta":       meta,
                "prev_hash":  prev_hash,
                "created_at": created_at,
            }
            signature = sign_event(event_dict)

            db.execute(
                _text(
                    f"INSERT INTO {DB_SCHEMA}.graph_audit_log "
                    f"(stream, seq, event, user_id, resource, data_hash, meta, "
                    f" prev_hash, signature, created_at) "
                    f"VALUES (:stream, :seq, :event, :user_id, :resource, :data_hash, "
                    f" CAST(:meta AS JSONB), :prev_hash, :signature, CAST(:created_at AS TIMESTAMPTZ))"
                ),
                {
                    "stream": stream, "seq": seq, "event": event,
                    "user_id": user_id, "resource": resource,
                    "data_hash": data_hash, "meta": json.dumps(meta),
                    "prev_hash": prev_hash, "signature": signature,
                    "created_at": created_at,
                },
            )
            db.commit()
            return {"stream": stream, "seq": seq, "signature": signature, "data_hash": data_hash}

        except IntegrityError:
            # Another writer took this (stream, seq) — rollback and retry with seq+1.
            db.rollback()
            if attempt == _MAX_RETRIES - 1:
                logger.warning(f"graph_audit: gave up on (stream={stream}) after {_MAX_RETRIES} seq races")
            continue
        except Exception as e:
            db.rollback()
            logger.error(f"graph_audit: record failed (stream={stream}, event={event}) → {e}")
            return None
        finally:
            db.close()

    return None


def fetch_chain(stream: str) -> list[dict]:
    """Return all entries for a stream in seq order (for verification / display)."""
    db = SessionLocal()
    try:
        rows = db.execute(
            _text(
                f"SELECT stream, seq, event, user_id, resource, data_hash, meta, "
                f" prev_hash, signature, created_at "
                f"FROM {DB_SCHEMA}.graph_audit_log WHERE stream = :s ORDER BY seq ASC"
            ),
            {"s": stream},
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"graph_audit: fetch_chain({stream}) failed → {e}")
        return []
    finally:
        db.close()


def verify_stream(stream: str) -> dict:
    """Verify both the HMAC signature of each entry AND the prev_hash linkage.

    Returns {valid, total, verified, first_invalid_seq, broken_link_seq}.
    A tampered or deleted row shows up as a signature failure and/or a broken link.
    """
    entries = fetch_chain(stream)
    total = len(entries)
    verified = 0
    first_invalid_seq = None
    broken_link_seq = None
    expected_prev = None

    for e in entries:
        # Rebuild the exact signed dict (datetime → naive UTC isoformat, as signed).
        created = e["created_at"]
        if isinstance(created, datetime):
            created = created.replace(tzinfo=None).isoformat()
        signed = {
            "stream":     e["stream"],
            "seq":        e["seq"],
            "event":      e["event"],
            "user_id":    e["user_id"],
            "resource":   e["resource"],
            "data_hash":  e["data_hash"],
            "meta":       e["meta"] or {},
            "prev_hash":  e["prev_hash"],
            "created_at": created,
        }
        if verify_event(signed, e["signature"]):
            verified += 1
        elif first_invalid_seq is None:
            first_invalid_seq = e["seq"]

        # Hash-chain linkage: each prev_hash must equal the prior row's signature.
        if e["prev_hash"] != expected_prev and broken_link_seq is None:
            broken_link_seq = e["seq"]
        expected_prev = e["signature"]

    return {
        "valid":             verified == total and broken_link_seq is None,
        "total":             total,
        "verified":          verified,
        "first_invalid_seq": first_invalid_seq,
        "broken_link_seq":   broken_link_seq,
    }
