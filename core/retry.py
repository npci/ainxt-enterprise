# SPDX-License-Identifier: Apache-2.0
# ============================================================
# RETRY LOGIC — tenacity-based exponential backoff for LLM calls
# ============================================================

import time
from typing import Callable, Any

from core.logger import logger

# Try to import tenacity; fall back to simple retry loop
try:
    from tenacity import (
        retry, stop_after_attempt, wait_exponential,
        retry_if_exception_type, RetryError,
    )
    _TENACITY = True
except ImportError:
    _TENACITY = False
    logger.warning("retry: tenacity not installed — using simple retry loop")

# Exceptions that warrant a retry
_RETRYABLE_MSGS = (
    "rate limit", "rate_limit", "ratelimit",
    "timeout", "timed out",
    "connection", "connection error",
    "503", "502", "429",
    "overloaded",
)


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _RETRYABLE_MSGS)


def retry_llm(fn: Callable, *args, max_attempts: int = 3, base_delay: float = 1.0, **kwargs) -> Any:
    """
    Call fn(*args, **kwargs) with exponential-backoff retry on transient errors.

    Delays: 1s → 2s → 4s (doubles each attempt).
    Only retries on rate-limit / timeout / connection errors.
    """
    if _TENACITY:
        from tenacity import Retrying, stop_after_attempt, wait_exponential, retry_if_exception

        retryer = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=base_delay, min=base_delay, max=base_delay * 8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        for attempt in retryer:
            with attempt:
                return fn(*args, **kwargs)
    else:
        # Simple manual retry
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if not _is_retryable(e):
                    raise
                last_exc = e
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"retry_llm: attempt {attempt}/{max_attempts} failed "
                    f"({e}), retrying in {delay:.1f}s"
                )
                time.sleep(delay)
        raise last_exc
