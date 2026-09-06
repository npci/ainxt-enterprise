# SPDX-License-Identifier: MIT
"""Job-scoped logging context for background workers.

Wraps the existing per-thread setters in ``core/logger.py`` so a worker job
entrypoint can bind ``job_id`` / ``chat_id`` / ``user_id`` / ``request_id``
once and have every subsequent log line in that job carry those fields.

Without this, RQ workers log under the empty defaults the ``_context_processor``
falls back to (``user_id="-", chat_id="-", task_id=""``), making it impossible
to follow a single job through the gateway log.

Usage:

    from core.log_job_context import job_log_context

    def generate_doc_job(payload):
        with job_log_context(
            job_id=payload["job_id"],
            user_id=payload.get("user_id", ""),
            chat_id=payload.get("chat_id", ""),
            request_id=payload.get("request_id", ""),
        ):
            ...  # every logger.info() in here carries the bound fields
"""

import re
from contextlib import contextmanager

from core.logger import (
    bind_context,
    clear_chat_context,
    get_agent_id,
    get_chat_id,
    get_correlation_id,
    get_job_kind,
    get_request_id,
    get_task_id,
    get_user_id,
    set_chat_context,
    set_correlation_id,
    set_job_kind,
    set_request_id,
)

_ID_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_id(value: str, limit: int = 128) -> str:
    if not value:
        return ""
    return _ID_UNSAFE_RE.sub("", str(value))[:limit]


@contextmanager
def job_log_context(
        *,
        job_id: str,
        user_id: str = "",
        chat_id: str = "",
        request_id: str = "",
        agent_id: str = "doc_worker",
        correlation_id: str = "",
        job_kind: str = "",
):
    """Bind job-scoped fields onto the logger's thread-local context.

    On exit, restore the prior context so worker process re-use across jobs
    doesn't leak fields from a previous job into the next one.
    """
    # Snapshot current values so we can restore them. The RQ work-horse is a
    # subprocess that reuses threads across jobs; without restore we'd carry
    # the last job's chat_id into the next job's first few log lines.
    # NOTE: logger.py was migrated from threading.local (_log_context) to
    # contextvars.ContextVar — use the exported getter functions instead.
    prev = {
        "request_id":     get_request_id(),
        "user_id":        get_user_id(),
        "chat_id":        get_chat_id(),
        "agent_id":       get_agent_id(),
        "task_id":        get_task_id(),
        "correlation_id": get_correlation_id(),
        "job_kind":       get_job_kind(),
    }

    _s_request_id = _sanitize_id(request_id)
    _s_correlation_id = _sanitize_id(correlation_id)
    _s_job_id = _sanitize_id(job_id)
    _s_user_id = _sanitize_id(user_id)
    _s_chat_id = _sanitize_id(chat_id)

    _corr = _s_correlation_id or _s_request_id or _s_job_id or "-"
    set_request_id(_s_request_id or _s_job_id or "-")
    set_chat_context(_s_user_id or "-", _s_chat_id or "-")
    set_correlation_id(_corr)
    set_job_kind(_sanitize_id(job_kind, limit=64))
    bind_context(agent_id=agent_id, task_id=_s_job_id)

    try:
        yield
    finally:
        set_request_id(prev["request_id"])
        if prev["user_id"] == "-" and prev["chat_id"] == "-":
            clear_chat_context()
        else:
            set_chat_context(prev["user_id"], prev["chat_id"])
        set_correlation_id(prev["correlation_id"])
        set_job_kind(prev["job_kind"])
        bind_context(agent_id=prev["agent_id"], task_id=prev["task_id"])
