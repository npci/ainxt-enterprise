# SPDX-License-Identifier: MIT
# ============================================================
# CIRCUIT BREAKER — Redis-backed, per-provider
# States: CLOSED (normal) → OPEN (failing) → HALF-OPEN (testing)
# ============================================================

import os
import time
from typing import Callable, Any, Optional

from core.logger import logger
from core.config import RDB_QUEUE
from core.kv import get_kv, KVError

# ── KV client (required — no silent in-memory fallback) ───────
#
# All gunicorn workers share circuit state via the queue KV (DB=5).
# Backend selected via REDIS_CLIENT_CONFIG_DB5.
# If the KV is down, the circuit breaker fails OPEN (fast-fail) rather
# than silently allowing each worker to maintain independent state and
# causing a thundering herd when the KV recovers.

_redis = None
_redis_available = False


def _get_redis():
    global _redis, _redis_available
    if _redis is None:
        try:
            c = get_kv(RDB_QUEUE, decode_responses=True)
            c.ping()
            _redis = c
            _redis_available = True
        except KVError as e:
            logger.error(
                f"circuit_breaker: KV backend unavailable ({e}). "
                "All circuit breakers will fail OPEN to prevent thundering herd."
            )
            _redis_available = False
    return _redis if _redis_available else None


# ── States ───────────────────────────────────────────────────

CLOSED    = "CLOSED"
OPEN      = "OPEN"
HALF_OPEN = "HALF_OPEN"


# ── Circuit Breaker ──────────────────────────────────────────

class CircuitBreaker:
    """
    Per-provider circuit breaker with Redis state persistence.

    CLOSED  → normal operation
    OPEN    → all calls rejected immediately (fast-fail)
    HALF_OPEN → one probe call allowed; if it succeeds → CLOSED
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 10,
        recovery_timeout:  int = 30,
    ):
        self.name              = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._state_key        = f"cb:{name}:state"
        self._failures_key     = f"cb:{name}:failures"
        self._opened_at_key    = f"cb:{name}:opened_at"

    # ── State accessors ──────────────────────────────────────

    def _get_state(self) -> str:
        rc = _get_redis()
        if rc:
            return rc.get(self._state_key) or CLOSED
        # Redis unavailable: fail open (safe default prevents split-brain)
        return OPEN

    def _set_state(self, state: str) -> None:
        rc = _get_redis()
        if rc:
            rc.set(self._state_key, state, ex=86400)

    def _get_failures(self) -> int:
        rc = _get_redis()
        if not rc:
            return 0
        val = rc.get(self._failures_key)
        return int(val) if val else 0

    def _incr_failures(self) -> int:
        rc = _get_redis()
        if not rc:
            return 0
        count = rc.incr(self._failures_key)
        rc.expire(self._failures_key, 86400)
        return count

    def _reset_failures(self) -> None:
        rc = _get_redis()
        if rc:
            rc.delete(self._failures_key)

    def _get_opened_at(self) -> float:
        rc = _get_redis()
        if not rc:
            return 0.0
        val = rc.get(self._opened_at_key)
        return float(val) if val else 0.0

    def _set_opened_at(self, ts: float) -> None:
        rc = _get_redis()
        if rc:
            rc.set(self._opened_at_key, ts, ex=86400)

    # ── Public interface ─────────────────────────────────────

    @property
    def is_open(self) -> bool:
        state = self._get_state()
        if state == OPEN:
            # Check if recovery_timeout has elapsed
            opened_at = self._get_opened_at()
            if time.time() - opened_at >= self.recovery_timeout:
                self._set_state(HALF_OPEN)
                logger.info(f"CircuitBreaker[{self.name}]: OPEN → HALF_OPEN (recovery probe)")
                return False
            return True
        return False

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        """
        Execute fn with circuit breaker protection.
        Raises RuntimeError if circuit is OPEN.
        """
        if os.environ.get("CIRCUIT_BREAKER_DISABLED", "").lower() in ("1", "true", "yes"):
            return fn(*args, **kwargs)

        state = self._get_state()

        if state == OPEN:
            # Check recovery timeout
            opened_at = self._get_opened_at()
            if time.time() - opened_at < self.recovery_timeout:
                raise RuntimeError(
                    f"CircuitBreaker[{self.name}] is OPEN — "
                    f"fast-failing until {self.recovery_timeout}s recovery"
                )
            # Transition to HALF_OPEN for probe
            self._set_state(HALF_OPEN)
            logger.info(f"CircuitBreaker[{self.name}]: OPEN → HALF_OPEN (probe)")

        try:
            result = fn(*args, **kwargs)
            # Success → reset
            if self._get_state() in (HALF_OPEN,):
                logger.info(f"CircuitBreaker[{self.name}]: HALF_OPEN → CLOSED (probe succeeded)")
            self._set_state(CLOSED)
            self._reset_failures()
            return result

        except Exception as e:
            failures = self._incr_failures()
            logger.warning(
                f"CircuitBreaker[{self.name}]: failure #{failures} → {e}"
            )
            if failures >= self.failure_threshold:
                self._set_state(OPEN)
                self._set_opened_at(time.time())
                logger.error(
                    f"CircuitBreaker[{self.name}]: CLOSED → OPEN "
                    f"(threshold {self.failure_threshold} reached)"
                )
            raise

    def record_success(self) -> None:
        """Record a successful upstream call without wrapping it in call().

        Used by callers (e.g. ConnectorEngine._execute_with_retry) that need
        fine-grained control over which outcomes count as circuit-breaker
        signal — e.g. a 404/403 "not found"/"forbidden" response reflects the
        request, not upstream health, and should not reset/trip the breaker
        the same way a 5xx or connection error does.
        """
        if self._get_state() in (HALF_OPEN,):
            logger.info(f"CircuitBreaker[{self.name}]: HALF_OPEN → CLOSED (probe succeeded)")
        self._set_state(CLOSED)
        self._reset_failures()

    def record_failure(self, exc: Optional[BaseException] = None) -> None:
        """Record a failed upstream call without wrapping it in call().
        See record_success() for why callers may want this instead of call()."""
        failures = self._incr_failures()
        logger.warning(f"CircuitBreaker[{self.name}]: failure #{failures} → {exc}")
        if failures >= self.failure_threshold:
            self._set_state(OPEN)
            self._set_opened_at(time.time())
            logger.error(
                f"CircuitBreaker[{self.name}]: CLOSED → OPEN "
                f"(threshold {self.failure_threshold} reached)"
            )

    def status(self) -> dict:
        state = self._get_state()
        opened_at = self._get_opened_at()
        return {
            "name":              self.name,
            "state":             state,
            "failures":          self._get_failures(),
            "failure_threshold": self.failure_threshold,
            "recovery_timeout":  self.recovery_timeout,
            "opened_at":         opened_at or None,
        }


# ── Singleton registry ───────────────────────────────────────

_breakers: dict = {}

# Per-provider tuning:
# External APIs (Jira/GitLab/Confluence) are more flaky than LLM APIs —
# lower threshold so we fail fast on Atlassian outages.
# LLM APIs: higher threshold (10) because occasional timeouts are normal.
#
# ARCH-F-007 / ARCH-F-008 (2026-08-26): added the remaining connector
# adapters that route through connectors/engine.py._execute_with_retry()
# (see that method's docstring) — same "external API, fail fast" tuning as
# jira/gitlab/confluence. Connector names match connectors/seed.py's "name"
# field / ConnectorContext.connector_name, not the DB-row auth_type.
_BREAKER_DEFAULTS: dict[str, tuple[int, int]] = {
    # name             failure_threshold  recovery_timeout_s
    "jira":           (5,  30),
    "jira_connector": (5,  30),
    "gitlab":         (5,  30),
    "confluence":     (5,  30),
    "microsoft_365":  (5,  30),
    "gmail":          (5,  30),
    "google_drive":   (5,  30),
    "slack":          (5,  30),
    "github":         (5,  30),
    "zoom":           (5,  30),
    "docusign":       (5,  30),
    "openai":         (10, 30),
    "claude":         (10, 30),
    "gemini":         (10, 30),
    "local":          (8,  20),
    "embed_svc":      (8,  15),
    # ARCH-F-CORE-003 (2026-08-26): the CLI /v1/messages compat router's link
    # to the LLM proxy (routers/messages_compat_router.py). Same threshold as
    # the direct provider breakers above (10, 30) — these calls go through
    # LLM_PROXY_URL / LOCAL_LLM_BASE_URL, one hop further than the direct
    # openai/anthropic/google breakers, but still an LLM API where occasional
    # timeouts are normal, not a sign of an outage.
    "llm_proxy_claude": (10, 30),
    "llm_proxy_openai": (10, 30),
    "llm_proxy_gemini": (10, 30),
    "llm_proxy_local_llm": (10, 30),
}


def get_breaker(name: str, failure_threshold: int = None, recovery_timeout: int = None) -> CircuitBreaker:
    """Return a named CircuitBreaker instance (singleton per name).
    Uses per-provider tuned defaults from _BREAKER_DEFAULTS if no override given.
    """
    if name not in _breakers:
        defaults = _BREAKER_DEFAULTS.get(name, (10, 30))
        ft = failure_threshold if failure_threshold is not None else defaults[0]
        rt = recovery_timeout  if recovery_timeout  is not None else defaults[1]
        _breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=ft,
            recovery_timeout=rt,
        )
    return _breakers[name]


def all_breaker_states() -> list:
    """Return status dicts for all registered circuit breakers."""
    return [b.status() for b in _breakers.values()]
