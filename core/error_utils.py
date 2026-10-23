"""
Shared helpers for safely surfacing exception information to API clients.

CodeQL (py/stack-trace-exposure) flags any code path where raw exception
text - which may contain internal file paths, SQL, stack frames, or other
implementation details - flows into an HTTP response. The helpers below
give callers a single, consistent way to:

  1. Always log the *full* exception (with traceback) server-side so nothing
     is lost for debugging/observability.
  2. Return a client-safe message: a constant string chosen by exception
     type, in every environment, so internals are never leaked to callers.

NOTE: services/llm_proxy/core/error_utils.py is a deliberate copy of this file.
llm_proxy puts its own directory first on sys.path, so its `core` package
shadows this one; keep the two in sync.
"""
from __future__ import annotations

from core.logger import logger

DEFAULT_GENERIC_MESSAGE = "An internal error occurred. Please try again or contact support."


def safe_error_detail(
    e: BaseException,
    context: str = "",
    generic: str = DEFAULT_GENERIC_MESSAGE,
) -> str:
    """
    Log ``e`` with full traceback and return a message safe to send to clients.

    The returned string is ALWAYS a constant, picked from the exception's
    *type* - it never contains any part of the exception message, traceback,
    paths, queries, etc., in any environment. The full detail is only ever
    written to the server-side log (``logger.error(..., exc_info=True)``).

    (Earlier versions returned ``str(e)`` outside production for developer
    convenience. That made every caller a stack-trace exposure in CodeQL's
    eyes, because the exception text flowed through this helper into the
    response. Developers should read the server log instead.)

    Args:
        e: The caught exception.
        context: Optional short description of where/what failed, used only
            in the server-side log line (never returned to the client).
        generic: Optional override for the fallback message.

    Returns:
        A constant string suitable for an HTTP error ``detail``/``error`` field.
    """
    log_msg = f"{context}: {e}" if context else str(e)
    logger.error(log_msg, exc_info=True)
    # Constants only - chosen by type, so no exception content reaches the caller.
    if isinstance(e, (TimeoutError,)):
        return "The operation timed out. Please try again."
    if isinstance(e, PermissionError):
        return "You do not have permission to perform this operation."
    if isinstance(e, (ValueError, TypeError, KeyError, LookupError)):
        return "The request could not be processed because some input was invalid."
    if isinstance(e, (ConnectionError, OSError)):
        return "A dependent service or resource is currently unavailable. Please try again."
    return generic
