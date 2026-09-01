# SPDX-License-Identifier: Apache-2.0
"""
agents/sdlc_baseline_gate.py — WS-2: the baseline build gate (preflight).

See docs/planning/SDLC_AGENTIC_LOOP_RFD.md §4 WS-2 and §5.3.

WHY
---
Runs start on repos that may not build at HEAD; the failure surfaces *late* (at
TESTING, after CODING/REVIEWING/CROSS_MODEL/FIXING) and gets mis-attributed to
the agent's diff. WS-2 builds HEAD as-is as the FINAL preflight step, so a repo
broken before any change suspends *before* CLASSIFYING with a clear reason and a
user-fix choice. A green baseline (or a warm SHA-keyed cache hit)
advances to CLASSIFYING.

CONTRACTS HONOURED (RFD §3)
---------------------------
* Suspend-not-fail: a persistent baseline-build failure suspends the run at
  ``BASELINE_BUILD`` (``sdlc_runs.state`` is free-text VARCHAR — no migration,
  RFD §5.3). It never marks the run FAILED.
* Flag-gated default-off (``SDLC_ENABLE_BASELINE_GATE``). Off ⇒ this gate is a
  no-op and the pipeline behaves byte-for-byte as today.
* ``add_run_event`` actors are plain strings.

TESTABILITY
-----------
The actual build (workspace clone + Docker/Maven compile) only runs on Ubuntu,
so this module performs NO build itself. The orchestration — flag check, Redis
SHA cache, retry policy, suspend, baseline-vs-diff telemetry — is pure and every
boundary (``build_fn``, ``redis_client``, ``suspend_fn``, ``event_fn``,
``context_patch_fn``) is injected, so it is fully unit-tested on Windows.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from core.logger import logger

_CACHE_KEY_PREFIX = "sdlc:baseline_build"


# ── env helpers (read at call time) ─────────────────────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        logger.warning(f"[baseline-gate] invalid int for {name}={raw!r} — using {default}")
        return default


def baseline_gate_enabled() -> bool:
    """Master switch for WS-2 (RFD §10). Default OFF."""
    return _env_flag("SDLC_ENABLE_BASELINE_GATE", False)


def _retries() -> int:
    return max(0, _env_int("SDLC_BASELINE_BUILD_RETRIES", 2))


def _cache_ttl() -> int:
    return _env_int("SDLC_BASELINE_CACHE_TTL_SECS", 604800)  # 7d


def _cache_key(repo: str, sha: str) -> str:
    return f"{_CACHE_KEY_PREFIX}:{repo}:{sha}"


# ── telemetry helper ─────────────────────────────────────────────────────────

def baseline_failure_class(run_context: Optional[dict]) -> str:
    """Classify a build failure for telemetry (RFD §4 WS-2.5).

    After a green baseline gate we stamp ``run["context"]["baseline_build"]`` so
    every later build failure can be labelled ``diff`` (the agent's change broke
    it) rather than ``baseline`` (the repo was already broken). When the gate was
    never run / never went green, a build failure is ``baseline`` by default —
    keeping the §1.2 "most failures are baseline" assumption falsifiable.
    """
    bb = (run_context or {}).get("baseline_build") or {}
    return "diff" if bb.get("status") == "green" else "baseline"


# ── the gate ──────────────────────────────────────────────────────────────────

def run_baseline_gate(
    *,
    run_id: str,
    repo: str,
    head_sha: str,
    build_fn: Callable[[], dict],
    redis_client=None,
    suspend_fn: Optional[Callable[[str], None]] = None,
    event_fn: Optional[Callable[[str, str, dict], None]] = None,
    context_patch_fn: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run the baseline build gate.

    Args:
      run_id, repo: identity. ``repo`` is the namespace/project slug.
      head_sha: the resolved HEAD commit SHA for the target branch. Used only as
        the cache key; empty string disables caching (always builds).
      build_fn: () -> dict with keys {success: bool, transient: bool,
        errors: list[str], output: str}. Builds HEAD as-is. Server-side only.
      redis_client: optional redis.Redis (decode_responses=True). None disables
        caching. Any cache error is non-fatal (log + continue).
      suspend_fn: (reason) -> None. Transitions the run to SUSPENDED at
        BASELINE_BUILD. Required for a real suspend; if None the gate still
        returns status="suspended" so the caller can stop.
      event_fn: (actor, message, data) -> None. Run-event sink (actor is a
        plain string).
      context_patch_fn: (patch_dict) -> None. Persists a context marker on green.

    Returns:
      {"status": "skipped"|"green"|"suspended", "from_cache": bool, "reason": str,
       "sha": str}
    """
    emit = event_fn or (lambda actor, msg, data: None)

    if not baseline_gate_enabled():
        return {"status": "skipped", "from_cache": False, "reason": "flag off", "sha": head_sha}

    # ── 1. Warm-cache short-circuit (SHA-keyed) ──────────────────────────────
    if redis_client is not None and head_sha:
        try:
            cached = redis_client.get(_cache_key(repo, head_sha))
            if cached:
                payload = _loads(cached)
                if payload.get("success"):
                    emit("baseline-gate",
                         f"baseline green (cache hit) repo={repo} sha={head_sha[:12]}",
                         {"from_cache": True, "sha": head_sha})
                    _mark_green(context_patch_fn, head_sha, from_cache=True)
                    return {"status": "green", "from_cache": True,
                            "reason": "warm cache", "sha": head_sha}
        except Exception as e:  # cache is best-effort
            logger.warning(f"[baseline-gate {run_id}] cache read failed (non-fatal): {e}")

    # ── 2. Build HEAD as-is, auto-retrying transient failures ────────────────
    retries = _retries()
    last: dict = {}
    for attempt in range(retries + 1):
        try:
            last = build_fn() or {}
        except Exception as e:
            # A build-machinery crash is treated as a transient infra failure
            # (retryable) so a flake never hard-suspends a healthy repo.
            logger.warning(f"[baseline-gate {run_id}] build_fn raised (attempt {attempt}): {e}")
            last = {"success": False, "transient": True, "errors": [str(e)], "output": str(e)}

        if last.get("success"):
            emit("baseline-gate",
                 f"baseline green repo={repo} sha={head_sha[:12]} attempt={attempt}",
                 {"from_cache": False, "sha": head_sha, "attempt": attempt})
            _cache_green(redis_client, run_id, repo, head_sha)
            _mark_green(context_patch_fn, head_sha, from_cache=False)
            return {"status": "green", "from_cache": False,
                    "reason": "built green", "sha": head_sha}

        transient = bool(last.get("transient"))
        if transient and attempt < retries:
            emit("baseline-gate",
                 f"baseline build transient failure — retry {attempt + 1}/{retries}",
                 {"build_failure_class": "baseline", "attempt": attempt, "transient": True})
            continue
        break  # genuine failure, or retries exhausted

    # ── 3. Persistent failure ────────────────────────────────────────────────
    errors = last.get("errors") or []
    detail = "; ".join(str(e) for e in errors[:5]) or (last.get("output") or "unknown")[:500]
    reason = f"repo does not build at HEAD (sha={head_sha[:12]}): {detail}"

    # ── 3b. Suspend (not fail) ────────────────────────────────────────────────
    emit("baseline-gate", f"SUSPEND BASELINE_BUILD: {reason}",
         {"build_failure_class": "baseline", "sha": head_sha})
    if suspend_fn is not None:
        try:
            suspend_fn(reason)
        except Exception as e:
            logger.error(f"[baseline-gate {run_id}] suspend_fn failed: {e}")
    return {"status": "suspended", "from_cache": False, "reason": reason, "sha": head_sha}


# ── internals ─────────────────────────────────────────────────────────────────

def _loads(raw) -> dict:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cache_green(redis_client, run_id: str, repo: str, sha: str) -> None:
    if redis_client is None or not sha:
        return
    try:
        redis_client.set(
            _cache_key(repo, sha),
            json.dumps({"success": True, "sha": sha}),
            ex=_cache_ttl(),
        )
    except Exception as e:
        logger.warning(f"[baseline-gate {run_id}] cache write failed (non-fatal): {e}")


def _mark_green(context_patch_fn, sha: str, from_cache: bool) -> None:
    if context_patch_fn is None:
        return
    try:
        context_patch_fn({"baseline_build": {"status": "green", "sha": sha,
                                             "from_cache": from_cache}})
    except Exception as e:
        logger.warning(f"[baseline-gate] context marker write failed (non-fatal): {e}")
