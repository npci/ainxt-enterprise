# SPDX-License-Identifier: MIT
# ============================================================
# MEETING WORKER — post-meeting automation (scope §4/§5)
#
# Runs on the Q_CONNECTOR RQ queue. Pipeline (all INSIDE AiNxt):
#   1. claim/dedup the meeting (meeting_jobs UNIQUE(meeting_id) + Redis lock)
#   2. fetch transcript (WebVTT) + participants via app-only Graph
#   3. redact-and-proceed (compliance_engine) + audit the ingest (hash only)
#   4. parse VTT → attribute speakers → flatten
#   5. generate Minutes-of-Meeting via model_router (Claude/GPT/Gemini — agnostic)
#   6. redact output + audit the summary
#   7. distribute (Outlook email to participants) + audit the send
#
# Microsoft is transport only: Graph moves bytes; all reasoning is model_router;
# all gating is compliance_engine. No Copilot / Azure OpenAI / Azure Speech.
#
# Detection: run_post_meeting_job is the core (driven by manual trigger now,
# Graph webhooks later). poll_recent_meetings() is the polling fallback.
# ============================================================

import os
from typing import Optional

from sqlalchemy import text as _text

from core.logger import logger
from db.database import SessionLocal, DB_SCHEMA

_LOCK_TTL = 900  # seconds — covers a full job; prevents double-processing
_TERMINAL = ("done", "distributing", "summarizing", "fetching")


# ── small infra helpers ──────────────────────────────────────────────────
def _redis():
    try:
        import redis
        from core.config import REDIS_HOST, REDIS_PORT
        c = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=2,
                        decode_responses=True, socket_connect_timeout=2)
        c.ping()
        return c
    except Exception as e:
        logger.warning(f"meeting_worker: Redis unavailable → {e}")
        return None


def _claim(meeting_id: str, organizer_id: str, detected_via: str) -> bool:
    """Atomically claim a meeting for processing. Returns False if already taken."""
    rc = _redis()
    if rc is not None:
        if not rc.set(f"meeting:lock:{meeting_id}", "1", nx=True, ex=_LOCK_TTL):
            logger.info(f"meeting_worker: {meeting_id} locked by another worker — skip")
            return False
    db = SessionLocal()
    try:
        row = db.execute(
            _text(f"SELECT status FROM {DB_SCHEMA}.meeting_jobs WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).first()
        if row and row[0] in _TERMINAL:
            logger.info(f"meeting_worker: {meeting_id} already in status={row[0]} — skip")
            return False
        db.execute(
            _text(
                f"INSERT INTO {DB_SCHEMA}.meeting_jobs "
                f"(meeting_id, organizer_id, detected_via, status, attempts) "
                f"VALUES (:m, :o, :v, 'fetching', 1) "
                f"ON CONFLICT (meeting_id) DO UPDATE SET "
                f"  status='fetching', attempts={DB_SCHEMA}.meeting_jobs.attempts+1, "
                f"  detected_via=:v, updated_at=NOW()"
            ),
            {"m": meeting_id, "o": organizer_id, "v": detected_via},
        )
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"meeting_worker: claim failed for {meeting_id} → {e}")
        return False
    finally:
        db.close()


def _update(meeting_id: str, status: str, **fields) -> None:
    sets = ["status = :status", "updated_at = NOW()"]
    params = {"m": meeting_id, "status": status}
    for k, v in fields.items():
        sets.append(f"{k} = :{k}")
        params[k] = v
    db = SessionLocal()
    try:
        db.execute(
            _text(f"UPDATE {DB_SCHEMA}.meeting_jobs SET {', '.join(sets)} WHERE meeting_id = :m"),
            params,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"meeting_worker: status update failed ({meeting_id}={status}) → {e}")
    finally:
        db.close()


# ── core pipeline ──────────────────────────────────────────────────────────
def run_post_meeting_job(payload: dict) -> dict:
    """RQ entrypoint. payload = {meeting_id, organizer_id, detected_via?}.

    Returns a small status dict (also persisted to meeting_jobs).
    """
    meeting_id   = (payload or {}).get("meeting_id", "")
    organizer_id = (payload or {}).get("organizer_id", "")
    detected_via = (payload or {}).get("detected_via", "manual")
    if not meeting_id or not organizer_id:
        return {"ok": False, "error": "meeting_id and organizer_id required"}

    if not _claim(meeting_id, organizer_id, detected_via):
        return {"ok": True, "skipped": True, "meeting_id": meeting_id}

    stream = f"meeting:{meeting_id}"
    try:
        from integrations import graph_app_client as graph
        from services import meeting_transcript as mt
        from agents.compliance_engine import ComplianceEngine
        from models.model_router import model_router
        from core import graph_audit

        ce = ComplianceEngine()

        # 1. Meeting metadata + participants
        meeting = graph.get_meeting(organizer_id, meeting_id)
        subject = meeting.get("subject", "") or "(untitled meeting)"
        participants = graph.extract_participants(meeting)
        part_names = [p["name"] for p in participants if p.get("name")]
        part_emails = [p["email"] for p in participants if p.get("email")]

        # 2. Transcript (pick the latest)
        transcripts = graph.list_transcripts(organizer_id, meeting_id)
        if not transcripts:
            _update(meeting_id, "skipped", subject=subject, error="no transcript available")
            logger.info(f"meeting_worker: {meeting_id} has no transcript — skipped")
            return {"ok": True, "skipped": True, "reason": "no_transcript", "meeting_id": meeting_id}
        transcripts.sort(key=lambda t: t.get("createdDateTime", ""), reverse=True)
        transcript_id = transcripts[0].get("id", "")
        raw_vtt = graph.get_transcript_vtt(organizer_id, meeting_id, transcript_id)

        # 3. Redact-and-proceed on the ingested transcript + audit (hash only)
        in_check = ce.validate_input(raw_vtt)
        safe_vtt = in_check.get("redacted_text") or raw_vtt
        graph_audit.record(
            stream, graph_audit.EVENT_GRAPH_INGEST,
            data=raw_vtt, user_id=organizer_id, resource=transcript_id,
            meta={"bytes": len(raw_vtt), "redacted": in_check.get("was_redacted", False),
                  "redacted_types": in_check.get("redacted_types", [])},
        )

        # 4. Parse → attribute → flatten
        cues = mt.attribute_speakers(mt.parse_vtt(safe_vtt), participants=part_names)
        if not cues:
            _update(meeting_id, "skipped", subject=subject, transcript_id=transcript_id,
                    error="transcript empty after parse")
            return {"ok": True, "skipped": True, "reason": "empty_transcript", "meeting_id": meeting_id}
        transcript_text = mt.build_transcript_text(cues, max_chars=int(os.getenv("MOM_MAX_TRANSCRIPT_CHARS", "24000")))

        # 5. Generate MoM INSIDE AiNxt (model-agnostic)
        _update(meeting_id, "summarizing", subject=subject, transcript_id=transcript_id)
        prompt = mt.build_mom_prompt(subject, transcript_text, part_names)
        mom = model_router.generate(prompt, model_hint="complex") or ""

        # 6. Audit
        graph_audit.record(
            stream, graph_audit.EVENT_MOM_SUMMARIZE,
            data=mom, user_id=organizer_id, resource=meeting_id,
            meta={"model": getattr(model_router, "last_tier", ""), "chars": len(mom),
                  "participation": mt.summarize_participation(cues)},
        )

        # 7. Distribute via Outlook email to participants + audit the send
        _update(meeting_id, "distributing")
        recipients = part_emails or ([participants[0]["email"]] if participants and participants[0].get("email") else [])
        distributed = False
        if recipients:
            body = f"Minutes of Meeting — {subject}\n\n{mom}\n\n— Generated by AiNxt"
            try:
                graph.send_mail(organizer_id, recipients, f"[AiNxt MoM] {subject}", body)
                distributed = True
                graph_audit.record(
                    stream, graph_audit.EVENT_OUTLOOK_SEND,
                    data=body, user_id=organizer_id, resource=meeting_id,
                    meta={"recipients": len(recipients)},
                )
            except Exception as e:
                logger.error(f"meeting_worker: MoM email send failed for {meeting_id} → {e}")

        _update(meeting_id, "done", subject=subject, transcript_id=transcript_id, error=None)
        logger.info(f"meeting_worker: {meeting_id} done (distributed={distributed}, recipients={len(recipients)})")
        return {"ok": True, "meeting_id": meeting_id, "subject": subject,
                "distributed": distributed, "recipients": len(recipients)}

    except Exception as e:
        logger.error(f"meeting_worker: job failed for {meeting_id} → {e}")
        _update(meeting_id, "failed", error=str(e)[:500])
        return {"ok": False, "meeting_id": meeting_id, "error": str(e)[:300]}


# ── polling fallback (scope: "polling first, webhooks later") ──────────────
def poll_recent_meetings(payload: Optional[dict] = None) -> dict:
    """Discover recently-ended meetings via Graph callRecords and enqueue jobs.

    Best-effort: callRecords lists recent calls (app perm CallRecords.Read.All);
    each carries an organizer + joinWebUrl which we resolve to an onlineMeeting
    id. Anything we can't resolve is logged (never silently dropped) and left for
    the manual trigger / webhook path. Schedule this via cron.
    """
    from datetime import datetime, timezone, timedelta
    from integrations import graph_app_client as graph
    from core.job_queue import enqueue_job, Q_CONNECTOR

    lookback_min = int((payload or {}).get("lookback_minutes", os.getenv("MEETING_POLL_LOOKBACK_MIN", "30")))
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_min)).strftime("%Y-%m-%dT%H:%M:%SZ")

    enqueued, skipped = 0, 0
    try:
        data = graph.get_json("/v1.0/communications/callRecords",
                              params={"$filter": f"startDateTime ge {since}"})
        records = data.get("value", []) or []
    except Exception as e:
        logger.warning(f"meeting_worker.poll: callRecords query failed → {e}")
        return {"ok": False, "error": str(e)[:300]}

    for rec in records:
        organizer = (rec.get("organizer", {}) or {}).get("user", {}) or {}
        organizer_id = organizer.get("id", "")
        join_url = rec.get("joinWebUrl", "")
        if not organizer_id or not join_url:
            skipped += 1
            continue
        try:
            # Resolve onlineMeeting id from joinWebUrl
            esc = join_url.replace("'", "''")
            mres = graph.get_json(
                f"/v1.0/users/{organizer_id}/onlineMeetings",
                params={"$filter": f"joinWebUrl eq '{esc}'"},
            )
            meetings = mres.get("value", []) or []
            if not meetings:
                skipped += 1
                continue
            meeting_id = meetings[0].get("id", "")
            enqueue_job(
                "workers.meeting_worker.run_post_meeting_job",
                {"meeting_id": meeting_id, "organizer_id": organizer_id, "detected_via": "poll"},
                queue_name=Q_CONNECTOR,
            )
            enqueued += 1
        except Exception as e:
            logger.warning(f"meeting_worker.poll: resolve/enqueue failed for a record → {e}")
            skipped += 1

    logger.info(f"meeting_worker.poll: enqueued={enqueued} skipped={skipped} (since {since})")
    return {"ok": True, "enqueued": enqueued, "skipped": skipped, "since": since}
