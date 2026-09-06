# SPDX-License-Identifier: MIT
# ============================================================
# RUNTIME CLIENT — single httpx chokepoint for ainxt-runtimed
#
# Drop-in for ainxt-platform/core/runtime_client.py
#
# Environment variables
# ---------------------
# RUNTIME_URL      = http://127.0.0.1:8080   (loopback only — never expose to browser)
# ENABLE_RUNTIME   = false                    (master flag — set true to activate)
# RUNTIME_TIMEOUT  = 60                       (seconds)
# RUNTIME_PCT      = 0                        (canary %, 0=off 1=1% 25=25% 100=full)
#
# Usage
# -----
# R1 shadow (fire-and-forget after Python answers):
#   from core.runtime_client import shadow_turn, ENABLE_RUNTIME
#   if ENABLE_RUNTIME:
#       import threading
#       threading.Thread(target=_run_shadow, daemon=True).start()
#
# R3 canary (before Python logic):
#   from core.runtime_client import chat_stream_sync, user_in_canary, RUNTIME_PCT
#   if ENABLE_RUNTIME and user_in_canary(user_id, RUNTIME_PCT):
#       for chunk in chat_stream_sync(...):
#           _publish_chunk(stream_key, chunk)
#       _publish_done(stream_key, meta=meta)
#       return
# ============================================================

import hashlib
import json
import os
import time
import threading

import httpx

from core.logger import logger

# ── Configuration ─────────────────────────────────────────────
RUNTIME_URL     = os.getenv("RUNTIME_URL",     "http://127.0.0.1:8080")
ENABLE_RUNTIME  = os.getenv("ENABLE_RUNTIME",  "false").lower() == "true"
RUNTIME_TIMEOUT = int(os.getenv("RUNTIME_TIMEOUT", "60"))
RUNTIME_PCT     = int(os.getenv("RUNTIME_PCT", "0"))   # canary %


# Verify the sidecar's TLS certificate unless it is reached over loopback.
# The sidecar runs on 127.0.0.1 with an ephemeral self-signed cert and there is
# no network path to intercept it; pointing RUNTIME_URL at another host sends
# the traffic off-box, so the certificate is checked instead of blindly trusted
# (CWE-599). Shared with the other loopback services via core.config.
from core.config import loopback_tls_verify as _loopback_tls_verify

RUNTIME_VERIFY_TLS = _loopback_tls_verify(RUNTIME_URL)

# ── Circuit breaker (in-process, shared via module-level state) ──
# The platform already has a Redis-backed circuit breaker (core/circuit_breaker.py)
# for model providers. This is a lightweight in-process one for the runtime
# sidecar — no Redis dependency so it works even if Redis is down.
_cb_lock       = threading.Lock()
_cb_failures   = 0
_cb_open_until = 0.0
_CB_THRESHOLD  = 5    # open after 5 consecutive failures
_CB_RESET_SEC  = 30   # retry after 30 seconds


def _cb_is_open() -> bool:
    with _cb_lock:
        if _cb_open_until and time.monotonic() < _cb_open_until:
            return True
        return False


def _cb_record_failure():
    global _cb_failures, _cb_open_until
    with _cb_lock:
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_open_until = time.monotonic() + _CB_RESET_SEC
            logger.warning(
                f"runtime_client: circuit breaker OPEN — "
                f"runtime unreachable, retrying in {_CB_RESET_SEC}s"
            )


def _cb_record_success():
    global _cb_failures, _cb_open_until
    with _cb_lock:
        _cb_failures   = 0
        _cb_open_until = 0.0


# ── Canary bucket ─────────────────────────────────────────────
def user_in_canary(user_id: str, pct: int) -> bool:
    """
    Stable per-user canary bucket — same user always gets the same path.
    Uses SHA-256 of user_id mod 100 so the split is deterministic across restarts.
    This is bucketing, not a security control, but sha256 avoids the
    static-analysis false positive that MD5 triggers.
    """
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    bucket = int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % 100
    return bucket < pct


# ── Core: synchronous SSE streaming (chat_worker.py is sync/RQ) ──
def chat_stream_sync(
    session: str,
    turn: str,
    message: str,
    data_class: str = "internal",
    caps: list = None,
    department: str = None,
    user_id: str = None,
):
    """
    MODE B (R3) — Synchronous generator that streams text.delta chunks
    from the runtime. Designed for use inside the synchronous RQ job
    (chat_worker._run_pipeline).

    Yields str chunks (the text content of text.delta events).
    Raises RuntimeError on 503 back-pressure so the caller can fall back.

    Example:
        for chunk in chat_stream_sync(session, turn, message, ...):
            _publish_chunk(stream_key, chunk)
        _publish_done(stream_key, meta=meta)
    """
    if not ENABLE_RUNTIME:
        return

    if _cb_is_open():
        logger.debug("runtime_client: circuit breaker open — skipping runtime call")
        return

    payload = {
        "session":    session,
        "turn":       turn,
        "input":      message,
        "data_class": data_class,
        "caps":       caps or ["chat.send"],
    }
    if department:
        payload["department"] = department

    url = f"{RUNTIME_URL}/v1/chat"
    # v2 runtime reads caps + department from X-AInxt-* headers (not just JSON body)
    _caps_str = ",".join(caps or ["chat.send"])
    _actor = user_id or session   # prefer real user_id, fall back to session
    _headers = {
        "Content-Type":       "application/json",
        "X-AInxt-User":       _actor,
        "X-AInxt-Caps":       _caps_str,
    }
    if department:
        _headers["X-AInxt-Department"] = department
    try:
        with httpx.Client(timeout=RUNTIME_TIMEOUT, verify=RUNTIME_VERIFY_TLS) as client:
            with client.stream("POST", url, json=payload, headers=_headers) as resp:
                if resp.status_code == 503:
                    logger.warning(
                        "runtime_client: 503 back-pressure — "
                        "runtime inbox full, falling back to Python"
                    )
                    _cb_record_failure()
                    raise RuntimeError("runtime_503")

                if resp.status_code != 200:
                    logger.warning(
                        f"runtime_client: unexpected {resp.status_code} — "
                        "falling back to Python"
                    )
                    _cb_record_failure()
                    raise RuntimeError(f"runtime_{resp.status_code}")

                _cb_record_success()

                for line in resp.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    ev_type = event.get("type", "")

                    if ev_type == "text.delta":
                        yield event.get("text", "")

                    elif ev_type == "turn.completed":
                        return

                    elif ev_type == "turn.failed":
                        reason = event.get("reason", "unknown")
                        logger.warning(
                            f"runtime_client: turn.failed reason={reason} — "
                            "falling back to Python"
                        )
                        raise RuntimeError(f"runtime_turn_failed:{reason}")

    except RuntimeError:
        raise   # re-raise so caller can fall back to Python

    except httpx.TimeoutException:
        logger.warning(
            f"runtime_client: timeout after {RUNTIME_TIMEOUT}s — "
            "falling back to Python"
        )
        _cb_record_failure()
        raise RuntimeError("runtime_timeout")

    except httpx.ConnectError:
        logger.warning(
            f"runtime_client: cannot connect to {RUNTIME_URL} — "
            "is ainxt-runtimed running? falling back to Python"
        )
        _cb_record_failure()
        raise RuntimeError("runtime_connect_error")

    except Exception as exc:
        logger.warning(f"runtime_client: unexpected error: {exc} — falling back to Python")
        _cb_record_failure()
        raise RuntimeError(f"runtime_error:{exc}")


# ── Shadow mode (R1) ──────────────────────────────────────────
def shadow_turn(
    session: str,
    turn: str,
    message: str,
    python_answer: str,
    data_class: str = "internal",
    caps: list = None,
    department: str = None,
) -> None:
    """
    R1 SHADOW MODE — Fire the same turn at the runtime AFTER Python has answered.
    Output is DISCARDED — never shown to the user.
    Logs a SHADOW_DIFF row for analysis.

    Call from a daemon thread so it never blocks the RQ job:
        t = threading.Thread(
                target=shadow_turn,
                args=(session, turn, message, full_response, data_class, caps, dept),
                daemon=True,
            )
        t.start()
    """
    rt_chunks = []
    try:
        for chunk in chat_stream_sync(session, f"{turn}_shadow", message,
                                      data_class, caps, department):
            rt_chunks.append(chunk)
    except RuntimeError:
        pass  # shadow failure is always silent

    rt_answer = "".join(rt_chunks)
    logger.info(
        "SHADOW_DIFF session=%s turn=%s py_len=%d rt_len=%d match=%s",
        session, turn,
        len(python_answer), len(rt_answer),
        "YES" if python_answer.strip() == rt_answer.strip() else "NO",
    )


# ── Health check ──────────────────────────────────────────────
def health_check() -> bool:
    """
    Returns True if the runtime is reachable and answering.

    NOTE: ainxt-runtimed has no dedicated /health or /ping endpoint.
    /v1/observe requires ?session=<id> (it is a live SSE session tail, not a health check).
    The correct probe is POST /v1/chat with a minimal payload — a 200 or 503 both mean
    the daemon is up (503 = inbox full = alive but busy). Only a connection error = down.

    Use from monitoring or PM2 pre-start:
        python -c "from core.runtime_client import health_check; exit(0 if health_check() else 1)"
    """
    try:
        resp = httpx.post(
            f"{RUNTIME_URL}/v1/chat",
            json={
                "session":    "_health",
                "turn":       "_t0",
                "input":      "ping",
                "data_class": "public",
                "caps":       ["chat.send"],
            },
            timeout=5.0,
            verify=RUNTIME_VERIFY_TLS,
        )
        # 200 = alive and answered; 503 = alive but inbox full (back-pressure)
        # Both mean the daemon is running. Only ConnectError / timeout = down.
        return resp.status_code in (200, 503)
    except Exception:
        return False
