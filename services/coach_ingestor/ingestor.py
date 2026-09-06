# SPDX-License-Identifier: MIT
# ============================================================
# COACH INGESTOR — normalise → redact → encrypt → persist → evaluate
# ============================================================
#
# A single entry point: ingest(payload) -> event_id.
#
# Pipeline (redact-at-write — raw prompts NEVER touch the DB):
#   1. Normalise the raw emit payload into a coach_event row dict.
#   2. Run the prompt text through compliance_engine.redact_text() so any
#      PAN/PII/secret is masked BEFORE anything is stored.
#   3. Hash the (pre-redaction) prompt with SHA-256 for dedup/correlation
#      without persisting the original.
#   4. Encrypt the redacted prompt at rest (AES-256-GCM) via crypto.encrypt().
#   5. Persist the CoachEvent row.
#   6. Hand the event to the evaluator (agents.coach_evaluator.evaluate_event)
#      which writes coach_rule_hit rows and back-fills event.rule_hits.
#
# Every stage is defensive: a failure in evaluation must not lose the event,
# and ingestion must never raise into the caller (it runs in a fire-and-forget
# background thread / Kafka consumer).
# ============================================================

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.logger import logger
from db.database import SessionLocal
from db.models import CoachEvent
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services.coach_ingestor import crypto


# ── helpers ─────────────────────────────────────────────────────────────────

def _sha256(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    try:
        return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
    except Exception:
        return None


_PII_TYPES = {"PAN", "INDIA_PAN", "AADHAAR", "EMAIL", "MOBILE", "UPI", "ACCOUNT_NUMBER", "ACCOUNT_NAME_COMBO", "IFSC_CODE", "IP_ADDRESS"}
_SECRET_TYPES = {"SECRET", "API_KEY", "ACCESS_TOKEN", "PRIVATE_KEY_LEAK", "CERTIFICATE_LEAK", "SSH_KEY_LEAK", "KEY_ASSIGNMENT_LEAK", "PAYMENT_KEY_LEAK"}
_COMPLIANCE_TYPES = {"CVV", "EXPIRY", "PIN_BLOCK"}
_COACH_SENSITIVE_PATTERNS = (
    (re.compile(r"\b(?:mobile|phone|contact)\s+(?:number\s+)?\d[\d\s\-]{5,14}\b", re.IGNORECASE), "MOBILE"),
    (re.compile(r"\b(?:card\s+number|credit\s+card|debit\s+card)\b[\s\S]{0,40}?\b\d[\d\s\-]{5,18}\b", re.IGNORECASE), "ACCOUNT_NUMBER"),
    (re.compile(r"\b\d[\d\s\-]{5,18}\b[\s\S]{0,40}?\b(?:card\s+number|credit\s+card|debit\s+card)\b", re.IGNORECASE), "ACCOUNT_NUMBER"),
    (re.compile(r"\b(?:account\s+number|bank\s+account|acct)\b[\s\S]{0,40}?\b\d[\d\s\-]{5,18}\b", re.IGNORECASE), "ACCOUNT_NUMBER"),
    (re.compile(r"\b\d[\d\s\-]{5,18}\b[\s\S]{0,40}?\b(?:account\s+number|bank\s+account|acct)\b", re.IGNORECASE), "ACCOUNT_NUMBER"),
)


def _merge_flags(existing: Any, detected: list) -> list:
    return sorted({str(x) for x in _as_list(existing) + _as_list(detected) if x})


def _mask_numeric_identifier(match: re.Match) -> str:
    value = match.group(0)
    digits = re.sub(r"\D", "", value)
    if len(digits) <= 4:
        return "*" * len(digits)
    return digits[:2] + "*" * max(1, len(digits) - 4) + digits[-2:]


def _redact(text: Optional[str]) -> tuple[Optional[str], list, list, list]:
    """Redact prompt text and return detected pii/secret/compliance flags."""
    if not text:
        return text, [], [], []
    try:
        from agents.compliance_engine import compliance_engine
        result = compliance_engine.validate_input(text)
        redacted = result.get("redacted_text") or ""
        findings = result.get("findings") or []
        pii = [f.get("type") for f in findings if f.get("type") in _PII_TYPES]
        secret = [f.get("type") for f in findings if f.get("type") in _SECRET_TYPES or f.get("category") in {"SECRET", "KEY"}]
        compliance = [f.get("type") for f in findings if f.get("type") in _COMPLIANCE_TYPES or f.get("type") in (result.get("blocked_types") or [])]
        for pattern, coach_flag in _COACH_SENSITIVE_PATTERNS:
            if pattern.search(text):
                pii.append(coach_flag)
                redacted = re.sub(r"\b\d[\d\s\-]{5,18}\b", _mask_numeric_identifier, redacted)
        return redacted, sorted(set(pii)), sorted(set(secret)), sorted(set(compliance))
    except Exception as e:
        logger.warning(f"coach.ingestor: prompt redaction failed ({e.__class__.__name__}) — masking entire prompt")
        return "[redaction unavailable]", [], [], []


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _normalize_model(raw: Any) -> str | None:
    """Canonicalise model identifiers so the same model isn't counted twice.

    Handles the three common variants produced by the gateway / CLI:
      - ``local:kimi-k2.7-code``   → ``kimi-k2.7-code``
      - ``Local (kimi-k2.7-code)`` → ``kimi-k2.7-code``
      - ``kimi-k2.7-code``         → ``kimi-k2.7-code``  (unchanged)
    """
    if raw is None:
        return None
    import re as _re
    name = str(raw).strip()
    # Strip "local:" prefix (CLI shorthand)
    if name.lower().startswith("local:"):
        name = name[len("local:"):].strip()
    # Strip "Local (...)" wrapper produced by some gateway paths
    m = _re.fullmatch(r"[Ll]ocal\s*\((.+)\)", name)
    if m:
        name = m.group(1).strip()
    return name or None


def _coerce_ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            pass
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _update_eval_fields(event_id: str, result: dict) -> None:
    """Write EvalEngine judge results back to the coach_event row.

    Called from a daemon thread after evaluate_event() completes. Opens its
    own DB session so it never interferes with the main ingest transaction.
    Never raises — a failure here must not affect the already-persisted event.
    """
    db = SessionLocal()
    try:
        row = db.query(CoachEvent).filter(CoachEvent.event_id == event_id).first()
        if row is not None:
            row.eval_score   = result.get("score")
            row.eval_verdict = result.get("verdict")
            row.eval_issues  = result.get("issues") or []
            db.commit()
            logger.debug(
                f"coach.ingestor: eval fields written event_id={event_id} "
                f"score={row.eval_score} verdict={row.eval_verdict}"
            )
    except Exception as e:
        db.rollback()
        logger.warning(
            f"coach.ingestor: eval field update failed event_id={event_id} "
            f"({e.__class__.__name__}: {e})"
        )
    finally:
        db.close()


def _build_context(event: CoachEvent) -> Dict[str, Any]:
    """Build evaluator context from recent events for the same user.

    Context lets stateful rules fire: duplicate prompts, cross-channel repeats,
    session saturation, continue-nudge frequency, and acceptance-rate trends.
    """
    ctx: Dict[str, Any] = {}
    ctx_db = SessionLocal()
    try:
        from sqlalchemy import desc
        recent = (ctx_db.query(CoachEvent)
                    .filter(CoachEvent.user_id == event.user_id,
                            CoachEvent.event_id != event.event_id)
                    .order_by(desc(CoachEvent.ts))
                    .limit(50)
                    .all())
        ctx["recent_prompt_hashes"] = [r.prompt_hash for r in recent if r.prompt_hash]
        ctx["recent_channels"]      = [r.channel for r in recent if r.channel]
        ctx["recent_prompts"]       = [r.prompt_redacted for r in recent if r.prompt_redacted]
        ctx["accepted_history"]     = [r.accepted for r in recent if r.accepted is not None]
        by_hash: Dict[str, list] = {}
        for r in recent:
            if r.prompt_hash and r.channel:
                by_hash.setdefault(r.prompt_hash, []).append(r.channel)
        ctx["recent_channels_by_prompt_hash"] = by_hash
        if event.thread_id:
            ctx["thread_msg_count"] = ctx_db.query(CoachEvent).filter(
                CoachEvent.user_id == event.user_id,
                CoachEvent.thread_id == event.thread_id,
            ).count()
        else:
            ctx["thread_msg_count"] = 0
    except Exception as e:
        logger.warning(f"coach.ingestor: context build failed ({e.__class__.__name__}: {e})")
    finally:
        try:
            ctx_db.close()
        except Exception:
            pass
    return ctx


# ── main entry point ────────────────────────────────────────────────────────

def ingest(payload: Dict[str, Any]) -> Optional[str]:
    """Normalise, redact, encrypt, persist a coach event and evaluate it.

    Parameters
    ----------
    payload : dict
        Raw event emitted by core.coach_events.emit_coach_event(). Expected
        keys (all optional except user_id/channel): user_id, channel, model,
        prompt (RAW — redacted here), completion (RAW — only hashed),
        tokens_in, tokens_out, cost_usd, context_window_pct, tool_calls,
        accepted, governance_flags, compliance_flags, pii_flags, secret_flags,
        latency_ms, thread_id, request_id, project, workspace, department, ts.

    Returns
    -------
    event_id (str) on success, None on failure. Never raises.
    """
    # ── Safety-net: drop IDE context dumps before touching the DB ────────────
    # Browser extensions (Kilo Code, Cline, Cursor, etc.) inject machine-
    # generated context (page snapshots, environment details, repo maps, etc.)
    # into the user message. These are housekeeping calls — not real user
    # prompts — and must never appear in Coach.
    #
    # General heuristic (no hardcoded markers): IDE context dumps use ALL-CAPS
    # markers followed by colons (PAGE SNAPSHOT:, URL:, TITLE:, INSTRUCTION:,
    # etc.). A normal user prompt won't have 3+ such markers. If we see them,
    # the event is a context dump — drop it. The extraction in
    # core.coach_events._extract_coach_task already pulled the real task out
    # before the event reaches here, so this is the safety net for any path
    # that bypasses extraction.
    _raw_prompt_check = (payload.get("prompt") or "").strip()
    if not _raw_prompt_check:
        logger.info(
            f"coach.ingestor: dropping empty prompt event user={payload.get('user_id')} "
            f"channel={payload.get('channel')} req_id={payload.get('request_id') or '-'}"
        )
        return None

    # Drop prompts that start with known context-dump prefixes (standalone)
    _CONTEXT_DUMP_PREFIXES = (
        "<environment_details>", "<repo_map>", "<file_list>", "<system-reminder>",
    )
    if any(_raw_prompt_check.startswith(p) for p in _CONTEXT_DUMP_PREFIXES):
        logger.info(
            f"coach.ingestor: dropping context-dump event user={payload.get('user_id')} "
            f"channel={payload.get('channel')} req_id={payload.get('request_id') or '-'} "
            f"prefix={_raw_prompt_check[:50]!r}"
        )
        return None

    # General heuristic: count ALL-CAPS markers (2+ uppercase words followed
    # by colon). 3+ markers = machine-generated context dump.
    import re as _re
    _markers = _re.findall(r"(?:^|\s)([A-Z][A-Z\s]{1,30}):", _raw_prompt_check)
    if len(_markers) >= 3:
        logger.info(
            f"coach.ingestor: dropping context-dump event user={payload.get('user_id')} "
            f"channel={payload.get('channel')} req_id={payload.get('request_id') or '-'} "
            f"markers={len(_markers)} len={len(_raw_prompt_check)}"
        )
        return None

    db = SessionLocal()
    try:
        raw_prompt     = payload.get("prompt")
        raw_completion = payload.get("completion")

        prompt_redacted, detected_pii, detected_secret, detected_compliance = _redact(raw_prompt)
        pii_flags        = _merge_flags(payload.get("pii_flags"), detected_pii)
        secret_flags     = _merge_flags(payload.get("secret_flags"), detected_secret)
        compliance_flags = _merge_flags(payload.get("compliance_flags"), detected_compliance)
        prompt_hash      = _sha256(raw_prompt)
        completion_hash  = _sha256(raw_completion)

        user_id     = str(payload.get("user_id") or "unknown")
        request_id  = payload.get("request_id")
        thread_id   = payload.get("thread_id")

        # ── Prompt-level deduplication ────────────────────────────────────────
        # IDE agentic loops (Kilo Code / Cline / Continue) fire multiple HTTP
        # requests for one logical user prompt, each with a fresh request_id and
        # no thread_id. Collapse identical prompts from the same user on the same
        # channel within a 2-minute window so Query Explorer shows one turn.
        #
        # thread_id is included in the dedup key so the same prompt sent in two
        # different conversations (different thread_id) is stored separately.
        # When thread_id is None (unthreaded), dedup still works across the
        # (user, channel, prompt_hash) space as before.
        #
        # Use a Postgres advisory lock keyed by (user_id, channel, thread_id,
        # prompt_hash) so concurrent ingestor threads are race-safe.
        channel = str(payload.get("channel") or "web")
        try:
            lock_key = int(hashlib.sha256(
                f"coach:dedup:{user_id}:{channel}:{thread_id or ''}:{prompt_hash}".encode("utf-8")
            ).hexdigest()[:16], 16) % (2**63)
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        except Exception as e:
            logger.warning(f"coach.ingestor: advisory lock failed ({e.__class__.__name__}) — continuing")

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
            dup_q = (db.query(CoachEvent)
                       .filter(CoachEvent.user_id == user_id,
                               CoachEvent.channel == channel,
                               CoachEvent.prompt_hash == prompt_hash,
                               CoachEvent.ts >= cutoff))
            # Scope dedup to the same conversation when thread_id is present;
            # otherwise fall back to the user+channel+hash window (unthreaded).
            if thread_id:
                dup_q = dup_q.filter(CoachEvent.thread_id == thread_id)
            dup = dup_q.first()
            if dup:
                logger.info(f"coach.ingestor: duplicate prompt dropped user={user_id} channel={channel} thread_id={thread_id} hash={prompt_hash[:16]}")
                db.close()
                return None
        except Exception as e:
            logger.warning(f"coach.ingestor: dedup check failed ({e.__class__.__name__}) — continuing")

        # Encrypt the already-redacted prompt for at-rest defence-in-depth.
        prompt_redacted_enc = crypto.encrypt(prompt_redacted)

        event = CoachEvent(
            ts               = _coerce_ts(payload.get("ts")),
            user_id          = user_id,
            channel          = str(payload.get("channel") or "web"),
            workspace        = payload.get("workspace"),
            project          = payload.get("project"),
            thread_id        = thread_id,
            request_id       = request_id,
            model            = _normalize_model(payload.get("model")),
            prompt_hash      = prompt_hash,
            prompt_redacted  = prompt_redacted_enc,
            completion_hash  = completion_hash,
            tokens_in        = _as_int(payload.get("tokens_in")),
            tokens_out       = _as_int(payload.get("tokens_out")),
            cost_usd         = _as_float(payload.get("cost_usd")),
            context_window_pct = _as_float(payload.get("context_window_pct")),
            tool_calls       = _as_list(payload.get("tool_calls")),
            accepted         = payload.get("accepted"),
            governance_flags = _as_list(payload.get("governance_flags")),
            compliance_flags = compliance_flags,
            pii_flags        = pii_flags,
            secret_flags     = secret_flags,
            latency_ms       = _as_int(payload.get("latency_ms")),
            rule_hits        = ["__pending__"],
            department       = payload.get("department"),
        )
        db.add(event)
        db.commit()
        event_id = event.event_id
        logger.debug(f"coach.ingestor: persisted event {event_id} user={event.user_id} channel={event.channel}")
    except IntegrityError as e:
        db.rollback()
        # Race lost against another ingestor writing the same (user, channel, prompt, minute).
        logger.info(f"coach.ingestor: duplicate prompt rejected by DB — dropping ({e.__class__.__name__})")
        return None
    except Exception as e:
        db.rollback()
        logger.error(f"coach.ingestor: persist failed ({e.__class__.__name__}: {e})")
        db.close()
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass

    # ── Evaluate (out of the persist transaction so a hit-write failure
    #    never rolls back the event itself) ────────────────────────────────
    try:
        from agents.coach_evaluator import evaluate_event
        # Build a plain dict view for the evaluator (decoupled from the ORM).
        event_view = {
            "event_id": event_id,
            "user_id": event.user_id,
            "channel": event.channel,
            "department": event.department,
            "model": event.model,
            "prompt_redacted": prompt_redacted,   # plaintext redacted (not encrypted) for predicate inspection
            "prompt_hash": prompt_hash,
            "completion_hash": completion_hash,
            "tokens_in": event.tokens_in,
            "tokens_out": event.tokens_out,
            "cost_usd": event.cost_usd,
            "context_window_pct": event.context_window_pct,
            "tool_calls": event.tool_calls,
            "accepted": event.accepted,
            "governance_flags": event.governance_flags,
            "compliance_flags": event.compliance_flags,
            "pii_flags": event.pii_flags,
            "secret_flags": event.secret_flags,
            "latency_ms": event.latency_ms,
            "thread_id": event.thread_id,
            "request_id": event.request_id,
            "project": event.project,
            "workspace": event.workspace,
        }
        ctx = _build_context(event)
        evaluate_event(event_id, event_view, ctx)
    except Exception as e:
        logger.error(f"coach.ingestor: evaluation failed for {event_id} ({e.__class__.__name__}: {e})")

    # ── EvalEngine (LLM-as-judge) — second, independent validation layer ──────
    # Runs AFTER the deterministic rule evaluator. Uses the already-redacted
    # prompt text (never raw). Fires in a daemon thread so it never blocks the
    # ingestor's return path. Gated by EVAL_ENABLED. Fail-open: a judge failure
    # never loses the already-persisted event.
    #
    # Bug-fixes applied here:
    #   1. session_id=_uid_capture (user_id) → session_id=_sid_capture (thread_id)
    #      EvalResult.session_id is the chat/thread session identifier, not the
    #      user ID. Passing user_id here caused every coach_prompt row to have
    #      session_id=<user_id>, making session-level filtering in the Eval
    #      Observatory completely broken. The correct value is event.thread_id
    #      (the chat_id that flows in from emit_coach_event(thread_id=_chat_id)).
    #
    #   2. Legacy eval_platform fallback: channel values ("web", "cli", "mcp")
    #      do not match any PLATFORM_EVAL_CONFIG key ("chat", "knowledge_base",
    #      etc.). Mapping channel → platform directly caused the Observatory's
    #      platform=chat filter to never match legacy rows. The fallback now
    #      maps channel to the correct platform key via a lookup table so
    #      coach_prompt rows always land under the right platform in the
    #      Observatory even when eval_platform is absent from the payload.
    try:
        from core.evals import eval_engine, EVAL_ENABLED
        if EVAL_ENABLED and (prompt_redacted or "").strip():
            _eid_capture = event_id
            _pr_capture  = prompt_redacted
            # FIX 1: use thread_id (chat session) as session_id, not user_id.
            # Falls back to request_id then event_id so the field is never NULL.
            _sid_capture = (
                event.thread_id
                or event.request_id
                or event_id
            )
            # Read eval_platform from the Kafka payload — set by emit_coach_event()
            # callers (gateway._emit_coach, projects_router, chat_worker, etc.).
            # Falls back to channel-based derivation for legacy payloads that
            # predate eval_platform (backward compatible).
            _raw_eval_platform = payload.get("eval_platform") or ""
            if not _raw_eval_platform:
                # FIX 2: map channel → platform key instead of using channel
                # value directly. Channel values ("web", "cli", "mcp") are not
                # valid PLATFORM_EVAL_CONFIG keys — the Observatory filters by
                # platform ("chat", "knowledge_base", etc.).
                _ch = (getattr(event, "channel", None) or "").lower()
                _CHANNEL_TO_PLATFORM = {
                    "web":          "chat",
                    "cli":          "cli",
                    "api":          "chat",
                    "teams":        "chat",
                    "slack":        "chat",
                    "mcp":          "ide_extension",
                    "voice":        "chat",
                    "mobile":       "chat",
                    "embed":        "chat",
                    "workflow":     "workflows",
                    "agent":        "agent_studio",
                    "sdlc":         "workflows",
                    "my_workspace": "my_workspace",
                }
                _raw_eval_platform = _CHANNEL_TO_PLATFORM.get(_ch, "chat")
            _ep_capture = _raw_eval_platform

            _model_capture = getattr(event, "model", None) or None
            def _run_eval_judge():
                try:
                    result = eval_engine.eval_coach_prompt(
                        prompt=_pr_capture,
                        session_id=_sid_capture,   # FIX 1: thread_id, not user_id
                        run_id=_eid_capture,
                        blocking=True,   # safe — we are already inside a daemon thread
                        platform=_ep_capture,
                        model=_model_capture,
                    )
                    if result:
                        _update_eval_fields(_eid_capture, result)
                        logger.info(
                            f"coach.ingestor: eval_coach_prompt done event_id={_eid_capture} "
                            f"score={result.get('score')} verdict={result.get('verdict')} "
                            f"platform={_ep_capture}"
                        )
                except Exception as _e:
                    logger.warning(
                        f"coach.ingestor: eval_coach_prompt failed event_id={_eid_capture} "
                        f"({_e.__class__.__name__}: {_e})"
                    )

            import threading as _threading
            _threading.Thread(target=_run_eval_judge, daemon=True,
                              name=f"coach-eval-{event_id[:8]}").start()
    except Exception as _eval_err:
        logger.warning(
            f"coach.ingestor: eval dispatch failed event_id={event_id} "
            f"({_eval_err.__class__.__name__}: {_eval_err})"
        )

    return event_id
