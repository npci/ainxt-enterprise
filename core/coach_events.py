# SPDX-License-Identifier: Apache-2.0
# ============================================================
# COACH EVENTS — emit + normalise hook for the AiNxt Coach pipeline
# ============================================================
#
# This is the single producer-side entry point every channel calls after an
# LLM interaction completes:
#
#     from core.coach_events import emit_coach_event
#     emit_coach_event(user_id=..., channel="web", model=..., prompt=...,
#                      completion=..., compliance_result=..., ...)
#
# Dispatch (both, controlled by env):
#   1. Kafka  — publish the raw payload to COACH_EVENT_TOPIC
#               (ainxt.coach_event). The coach_consumer drains it in prod.
#   2. Direct — when COACH_DIRECT_INGEST=true (default, dev) OR the Kafka
#               publish fell back to Redis, submit to a small bounded executor
#               so local dev works with no Kafka.
#
# emit_coach_event NEVER raises and NEVER blocks the request path — every
# failure is swallowed and logged. Coach is strictly observational.
#
# Gating: when ENABLE_COACH is false this is a no-op.
# ============================================================

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from core.logger import logger

try:
    from core.config import (
        ENABLE_COACH,
        COACH_DIRECT_INGEST,
        COACH_EVENT_TOPIC,
    )
except Exception:  # pragma: no cover — config import guard
    import os
    ENABLE_COACH        = os.getenv("ENABLE_COACH", "false").lower() == "true"
    COACH_DIRECT_INGEST = os.getenv("COACH_DIRECT_INGEST", "true").lower() == "true"
    _pfx = os.getenv("KAFKA_TOPIC_PREFIX", os.getenv("APP_OWNER", "ainxt"))
    COACH_EVENT_TOPIC   = os.getenv("COACH_EVENT_TOPIC", f"{_pfx}.coach_event")


# ── client-source → canonical channel ───────────────────────────────────────

# Maps the many free-form client_source / surface identifiers seen across the
# platform onto the closed channel vocabulary stored on coach_event.channel.
_CHANNEL_MAP = {
    "web": "web", "ui": "web", "chat": "web", "browser": "web", "platform": "web",
    "browser-agent": "embed", # Chrome browser-automation extension — embedded surface
    "cli": "cli", "terminal": "cli",
    "api": "api", "rest": "api", "gateway": "api",
    "teams": "teams", "msteams": "teams", "microsoft_teams": "teams",
    "slack": "slack",
    "mcp": "mcp",
    "voice": "voice", "stt": "voice", "tts": "voice",
    "mobile": "mobile",
    "embed": "embed", "widget": "embed",
    "workflow": "workflow", "dag": "workflow",
    "agent": "agent", "agentic": "agent", "multi_agent": "agent",
    # IDE/plugin traffic is stored under the canonical MCP channel.
    "ide-vscode": "mcp", "ide-jetbrains": "mcp",
    "ide": "mcp", "vscode": "mcp", "plugin": "mcp",
    "sdlc": "sdlc",
    # My Workspace (Projects.jsx) — preserved as-is so the ingestor
    # channel→platform map resolves to "my_workspace" without needing
    # eval_platform in the payload.
    "my_workspace": "my_workspace",
}


def channel_from_client_source(src: Optional[str]) -> str:
    """Normalise a free-form client_source/surface string to a coach channel."""
    if not src:
        return "web"
    key = str(src).strip().lower()
    if key in _CHANNEL_MAP:
        return _CHANNEL_MAP[key]
    # Prefix / substring fallbacks for compound sources like "web:chat".
    for token, channel in _CHANNEL_MAP.items():
        if key.startswith(token) or token in key:
            return channel
    return "web"


# ── compliance findings → coach flag buckets ────────────────────────────────

# compliance_engine findings carry a `category` ("PII" | "SECRET" | "KEY" |
# "ML" | ...) and a `type` ("PAN", "AADHAAR", "API_KEY", ...). We bucket them
# into the flag arrays coach_event stores, so the security predicates can fire
# without ever seeing the raw value.
_SECRET_CATEGORIES = {"SECRET", "KEY"}
_PII_CATEGORIES    = {"PII", "ML"}


def extract_coach_flags(compliance_result: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Bucket a compliance_engine result's findings into coach flag lists.

    Returns a dict with keys: pii_flags, secret_flags, compliance_flags.
    Values are de-duplicated finding *types* (never raw values).
    """
    pii: List[str] = []
    secret: List[str] = []
    compliance: List[str] = []

    if not compliance_result:
        return {"pii_flags": [], "secret_flags": [], "compliance_flags": []}

    findings = compliance_result.get("findings") or []
    blocked_types = compliance_result.get("blocked_types") or []

    for f in findings:
        if not isinstance(f, dict):
            continue
        ftype = (f.get("type") or "UNKNOWN")
        category = (f.get("category") or "").upper()
        if category in _SECRET_CATEGORIES:
            secret.append(ftype)
        elif category in _PII_CATEGORIES:
            pii.append(ftype)
        else:
            compliance.append(ftype)

    # Any blocked type is a compliance signal regardless of bucket.
    for bt in blocked_types:
        if bt not in compliance:
            compliance.append(bt)

    return {
        "pii_flags": sorted(set(pii)),
        "secret_flags": sorted(set(secret)),
        "compliance_flags": sorted(set(compliance)),
    }


# ── direct-ingest helper ────────────────────────────────────────────────────

_DIRECT_INGEST_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coach-direct-ingest")
_DIRECT_INGEST_SUBMIT_LOCK = threading.Lock()
_DIRECT_INGEST_CAPACITY = threading.BoundedSemaphore(256)


def _ingest_safely(payload: Dict[str, Any]) -> None:
    """Run the synchronous ingestor from the bounded direct-ingest executor."""
    try:
        from services.coach_ingestor import ingest
        ingest(payload)
    except Exception as e:
        logger.error(f"coach_events: direct ingest failed ({e.__class__.__name__}: {e})")
    finally:
        try:
            _DIRECT_INGEST_CAPACITY.release()
        except Exception:
            pass


def _spawn_direct_ingest(payload: Dict[str, Any]) -> None:
    if not _DIRECT_INGEST_CAPACITY.acquire(blocking=False):
        logger.warning("coach_events: direct ingest queue full; dropping observational event")
        return
    try:
        with _DIRECT_INGEST_SUBMIT_LOCK:
            _DIRECT_INGEST_EXECUTOR.submit(_ingest_safely, payload)
    except Exception as e:
        try:
            _DIRECT_INGEST_CAPACITY.release()
        except Exception:
            pass
        logger.error(f"coach_events: failed to submit direct ingest ({e.__class__.__name__}: {e})")


# ── Anthropic /v1/messages → coach prompt extraction ───────────────────────

# Message content may be a plain string or a list of content blocks (text,
# image, tool_use, tool_result). Coach only needs the user-authored prose.

def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _extract_coach_task(raw: str) -> str:
    """Strip IDE/agent framing and return the user-authored task.

    General approach: IDE extensions (Kilo Code, Cline, Cursor, etc.) inject
    machine-generated context (page snapshots, environment details, repo maps,
    file listings, etc.) into the user message. These are housekeeping payloads,
    not real user prompts. Instead of hardcoding every possible marker, we:

    1. Strip known XML-style blocks (<environment_details>, <repo_map>, etc.)
    2. Strip known framing prefixes (Task:, [USER QUESTION], etc.)
    3. Split on ALL-CAPS markers (the universal IDE context-block convention)
       and keep only the short human-readable task. The user's actual
       instruction is almost always the SHORTEST segment.
    4. If the result is still too long (>4000 chars), it's not a real prompt.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    # ── Strip known framing prefixes ──────────────────────────────────────
    if "---\n\nTask:" in raw:
        raw = raw.split("---\n\nTask:", 1)[-1].strip()
    elif "\n\nTask:" in raw:
        raw = raw.split("\n\nTask:", 1)[-1].strip()
    elif "[USER QUESTION]" in raw:
        raw = raw.split("[USER QUESTION]", 1)[-1].strip()

    # ── Strip XML-style context blocks (greedy, multi-line) ───────────────
    import re
    raw = re.sub(r"<environment_details>.*?</environment_details>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<repo_map>.*?</repo_map>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<file_list>.*?</file_list>", "", raw, flags=re.DOTALL)
    # ainxt-cli injects a <system-reminder> block into every user message turn
    # (platform identity, skill reminders, etc.). These are machine-generated
    # housekeeping payloads — not the user's actual prompt — and must never
    # appear in Coach Query Explorer. Strip them before any further extraction.
    raw = re.sub(r"<system-reminder>.*?</system-reminder>", "", raw, flags=re.DOTALL)
    raw = raw.strip()
    # ainxt-cli wraps prompts in <user_query>…</user_query> — extract inner text.
    _uq = re.search(r"<user_query>(.*?)</user_query>", raw, flags=re.DOTALL)
    if _uq:
        raw = _uq.group(1).strip()

    # ── General heuristic: split on ALL-CAPS markers ──────────────────────
    # IDE extensions produce prompts like:
    #   "Mode hint: exploration PAGE SNAPSHOT: ... INSTRUCTION: open https://..."
    #   "QUERY: tech blog... PAGE SNAPSHOT: URL: ... TITLE: ... PAGE TEXT: ..."
    #   "INSTRUCTION: do X CONTEXT: <huge dump>"
    #
    # Instead of hardcoding every marker, we split on ALL-CAPS markers that
    # are followed by a colon (the universal IDE context-block convention).
    # This catches PAGE SNAPSHOT, INSTRUCTION, QUERY, TASK, VIEWPORT, TITLE,
    # HEADINGS, INTERACTIVE ELEMENTS, PAGE TEXT, CONTEXT, USER, GOAL, and any
    # FUTURE marker the IDE might invent.
    #
    # Strategy: if there are 2+ ALL-CAPS markers, the prompt is a structured
    # IDE payload. We prefer content after task-like markers (INSTRUCTION,
    # QUERY, TASK, GOAL, USER), and fall back to the shortest segment.
    segments = re.split(r"(?:^|\s)([A-Z][A-Z\s]{1,30}):", raw)
    # re.split with a capture group returns [pre, marker1, content1, marker2, content2, ...]
    marker_count = (len(segments) - 1) // 2
    if marker_count >= 2:
        # Build {marker: content} pairs
        pairs = []
        for i in range(1, len(segments) - 1, 2):
            marker = segments[i].strip().upper()
            content = segments[i + 1].strip()
            if content:
                pairs.append((marker, content))
        # Also include pre-marker text
        if segments[0].strip():
            pairs.append(("", segments[0].strip()))

        # Prefer task-like markers (user's actual instruction)
        _TASK_MARKERS = {"INSTRUCTION", "QUERY", "TASK", "GOAL", "USER", "REQUEST"}
        task_candidates = [
            c for m, c in pairs
            if m in _TASK_MARKERS and 10 <= len(c) <= 500
        ]
        if task_candidates:
            raw = task_candidates[0]  # first task marker wins
        else:
            # Fall back: shortest meaningful segment (>=10 chars)
            meaningful = [c for _, c in pairs if 10 <= len(c) <= 500]
            if meaningful:
                raw = min(meaningful, key=len)

    # Strip leading "Mode hint: ..." prefix if it survived
    raw = re.sub(r"^Mode hint:\s*\S+\s*", "", raw, flags=re.IGNORECASE)

    if raw.startswith(("<environment_details>", "<repo_map>", "<file_list>")):
        return ""
    if len(raw) > 4000:
        return ""
    return raw


def _coach_prompt_from_messages(messages: list[dict]) -> str:
    """Extract the user-authored task from Anthropic-format message list.

    Walks backwards through the message history looking for the last genuine
    human-typed user message. Skips AI-injected continuation turns — these are
    user-role messages that immediately follow an assistant message containing
    tool_use blocks (the CLI agentic loop feeds tool results back as user
    messages, but they are machine-generated, not typed by the human).
    """
    msgs = list(messages or [])
    for i in range(len(msgs) - 1, -1, -1):
        msg = msgs[i]
        if msg.get("role") != "user":
            continue
        # If the immediately preceding message is an assistant turn that
        # contained tool_use blocks, this user message is a tool-result
        # continuation injected by the CLI framework — skip it.
        if i > 0:
            prev = msgs[i - 1]
            if prev.get("role") == "assistant":
                prev_content = prev.get("content", [])
                if isinstance(prev_content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in prev_content
                ):
                    continue  # AI-injected continuation — not a real user prompt
        raw = _message_text(msg.get("content", "")).strip()
        if raw:
            return _extract_coach_task(raw)
    return ""


def emit_coach_event_from_messages(
    *,
    user_id: str,
    messages: list[dict],
    model: Optional[str] = None,
    request_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    thread_id: Optional[str] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    channel: str = "cli",
    compliance_result: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a Coach event from a raw Anthropic /v1/messages payload.

    All extraction, normalisation, thread_id resolution, and prompt cleaning
    live here. Callers pass raw context only — no Coach logic outside this file.

    thread_id resolution order:
      1. Explicit thread_id arg — e.g. x-ainxt-conv-id header (stable per CLI
         conversation, same value across all turns of one session).
      2. metadata.session_id / metadata.chat_id — Anthropic SDK metadata field.
    """
    try:
        coach_prompt = _coach_prompt_from_messages(messages)
        if not coach_prompt:
            return
        # Resolve thread_id — explicit arg wins, then fall back to metadata.
        resolved_thread_id = (thread_id or "").strip()
        if not resolved_thread_id and isinstance(metadata, dict):
            resolved_thread_id = str(
                metadata.get("session_id") or metadata.get("chat_id") or ""
            ).strip()
        emit_coach_event(
            user_id=user_id,
            channel=channel,
            model=model,
            prompt=coach_prompt,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            request_id=request_id,
            thread_id=resolved_thread_id or None,
            compliance_result=compliance_result,
        )
    except Exception as e:
        logger.warning(f"coach_events: emit_coach_event_from_messages failed ({e.__class__.__name__}: {e})")


# ── public API ──────────────────────────────────────────────────────────────

def emit_coach_event(
    *,
    user_id: str,
    channel: str = "web",
    model: Optional[str] = None,
    prompt: Optional[str] = None,
    completion: Optional[str] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    context_window_pct: float = 0.0,
    tool_calls: Optional[list] = None,
    accepted: Optional[bool] = None,
    latency_ms: int = 0,
    thread_id: Optional[str] = None,
    request_id: Optional[str] = None,
    project: Optional[str] = None,
    workspace: Optional[str] = None,
    department: Optional[str] = None,
    compliance_result: Optional[Dict[str, Any]] = None,
    governance_flags: Optional[List[str]] = None,
    eval_platform: Optional[str] = None,
    **extra: Any,
) -> None:
    """Emit one Coach practice event. No-op when ENABLE_COACH is false.

    Publishes to Kafka (COACH_EVENT_TOPIC) and, when COACH_DIRECT_INGEST is
    true or the Kafka publish fell back to Redis, also ingests synchronously
    in a bounded executor. Never raises; never blocks meaningfully.

    NOTE: `prompt`/`completion` here are RAW — they are redacted/hashed inside
    the ingestor (redact-at-write). They are placed in the Kafka payload only
    so the consumer-side ingestor performs the identical redaction; in a
    Kafka-on deployment the topic is internal and short-retention. The DB only
    ever stores the redacted+encrypted form.
    """
    if not ENABLE_COACH:
        logger.info("coach_events: skipped emit because ENABLE_COACH=false")
        return

    try:
        flags = extract_coach_flags(compliance_result)
        payload: Dict[str, Any] = {
            "user_id": str(user_id) if user_id else "unknown",
            "channel": channel_from_client_source(channel),
            "model": model,
            "prompt": prompt,
            "completion": completion,
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "cost_usd": float(cost_usd or 0.0),
            "context_window_pct": float(context_window_pct or 0.0),
            "tool_calls": tool_calls or [],
            "accepted": accepted,
            "latency_ms": int(latency_ms or 0),
            "thread_id": thread_id,
            "request_id": request_id,
            "project": project,
            "workspace": workspace,
            "department": department,
            "governance_flags": governance_flags or [],
            "compliance_flags": flags["compliance_flags"],
            "pii_flags": flags["pii_flags"],
            "secret_flags": flags["secret_flags"],
            # Named explicitly so it is always in the payload — never dropped
            # by the **extra guard or lost in serialisation.
            "eval_platform": eval_platform,
        }
        if extra:
            payload.update({k: v for k, v in extra.items() if k not in payload})
    except Exception as e:
        logger.error(f"coach_events: payload build failed ({e.__class__.__name__}: {e})")
        return

    logger.info(
        f"coach_events: emit built user={payload['user_id']} channel={payload['channel']} "
        f"model={payload.get('model') or '-'} req_id={payload.get('request_id') or '-'} "
        f"thread_id={payload.get('thread_id') or '-'} prompt_len={len((payload.get('prompt') or ''))} "
        f"direct_ingest={COACH_DIRECT_INGEST} topic={COACH_EVENT_TOPIC}"
    )

    sent_to_kafka = False
    try:
        from core.kafka_producer import produce
        sent_to_kafka = produce(COACH_EVENT_TOPIC, payload, key=payload["user_id"])
        logger.info(
            f"coach_events: kafka dispatch result sent_to_kafka={sent_to_kafka} "
            f"user={payload['user_id']} req_id={payload.get('request_id') or '-'} topic={COACH_EVENT_TOPIC}"
        )
    except Exception as e:
        logger.warning(f"coach_events: kafka produce failed ({e.__class__.__name__}: {e})")
        sent_to_kafka = False

    # Direct-ingest when explicitly enabled (dev) OR when Kafka fell back to
    # Redis (no live consumer guaranteed). This guarantees the event is
    # processed exactly once in dev and avoids double-ingest in prod where
    # COACH_DIRECT_INGEST=false and a real consumer drains the topic.
    if COACH_DIRECT_INGEST or not sent_to_kafka:
        logger.info(
            f"coach_events: submitting direct ingest user={payload['user_id']} "
            f"req_id={payload.get('request_id') or '-'} reason={'config' if COACH_DIRECT_INGEST else 'kafka_fallback'}"
        )
        _spawn_direct_ingest(payload)
    else:
        logger.info(
            f"coach_events: kafka-only mode user={payload['user_id']} req_id={payload.get('request_id') or '-'}"
        )
