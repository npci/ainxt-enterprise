# SPDX-License-Identifier: MIT
# ============================================================
# CIRCUIT BREAKER — simple in-memory version for llm_proxy
#
# Intentionally has no Redis dependency.
# The main project's core/circuit_breaker.py is Redis-backed;
# this one is process-local (fine for a single-process proxy).
#
# Same public API:  get_breaker(name, ...) → breaker
#   breaker.is_open          — True when circuit is tripped
#   breaker.call(fn, *args)  — call fn; trips breaker on failures
# ============================================================

import time
import threading
from core.logger import logger


class _CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int, recovery_timeout: int):
        self.name              = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._failures         = 0
        self._opened_at        = None
        self._lock             = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.time() - self._opened_at >= self.recovery_timeout:
                # Half-open: allow one attempt
                self._opened_at = None
                self._failures  = 0
                logger.info(f"CircuitBreaker({self.name}): half-open — allowing retry")
                return False
            return True

    def call(self, fn, *args, **kwargs):
        if self.is_open:
            raise RuntimeError(f"CircuitBreaker({self.name}) is OPEN")
        try:
            result = fn(*args, **kwargs)
            with self._lock:
                self._failures = 0   # success resets counter
            return result
        except Exception as exc:
            with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._opened_at = time.time()
                    logger.warning(
                        f"CircuitBreaker({self.name}): OPENED after "
                        f"{self._failures} failures"
                    )
            raise

    async def async_call(self, coro_fn, *args, **kwargs):
        if self.is_open:
            raise RuntimeError(f"CircuitBreaker({self.name}) is OPEN")
        try:
            result = await coro_fn(*args, **kwargs)
            with self._lock:
                self._failures = 0
            return result
        except Exception as exc:
            with self._lock:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._opened_at = time.time()
                    logger.warning(
                        f"CircuitBreaker({self.name}): OPENED after "
                        f"{self._failures} failures"
                    )
            raise


_breakers: dict[str, _CircuitBreaker] = {}
_lock = threading.Lock()


def get_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: int  = 60,
) -> _CircuitBreaker:
    with _lock:
        if name not in _breakers:
            _breakers[name] = _CircuitBreaker(name, failure_threshold, recovery_timeout)
        return _breakers[name]
