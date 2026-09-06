# SPDX-License-Identifier: MIT
# ============================================================
# RETRIEVAL STATUS — cross-module signal for surfacing embed_svc /
# reranker infrastructure problems to the end user, not just server logs.
#
# Problem this fixes: models/hybrid_search.py::pgvector_search and
# models/hybrid_retriever.py::_rerank_via_svc both already catch every
# embed_svc failure and degrade gracefully (empty results / RRF fallback)
# so a single flaky request never crashes a chat turn — but the ONLY
# record of *why* was a server-side logger.warning() call. A user asking
# a Workspace/Codebase question saw either a generic "no context found"
# note or nothing at all, with zero indication that the actual cause was
# a misconfigured/unreachable embed_svc rather than "nothing relevant
# exists". agents/orchestrator.py reads this back at the point it already
# composes its "no codebase context" warning, to make that warning
# specific instead of generic when the root cause is known.
#
# Context-local (contextvars, not a plain module global) so concurrent
# requests handled by the same gateway worker process never see each
# other's warnings — each async task / thread gets its own copy.
# ============================================================

import contextvars

_retrieval_warning: contextvars.ContextVar[str] = contextvars.ContextVar(
    "retrieval_warning", default=""
)


def set_retrieval_warning(message: str) -> None:
    """Record a user-facing note about degraded/unavailable retrieval
    infrastructure (embed_svc unreachable, provider misconfigured, reranker
    fell back to RRF) for THIS request only. Last problem in a request
    wins if more than one occurs."""
    _retrieval_warning.set(message)


def get_and_clear_retrieval_warning() -> str:
    """Read back the note (if any) and reset it, so a stale warning from
    an earlier retrieval call in the same request/context never leaks into
    a later, unrelated one (e.g. a second retrieval pass that succeeds)."""
    msg = _retrieval_warning.get()
    if msg:
        _retrieval_warning.set("")
    return msg


def describe_embed_svc_error(exc: Exception) -> str:
    """Turn an httpx exception from calling embed_svc's /embed or /rerank
    into the clearest available one-line reason.

    Prefers the FastAPI HTTPException 'detail' body embed_svc itself
    already writes for known-misconfiguration cases (e.g. "Nomic embedder
    not ready — set NOMIC_EMBED_URL in .env and restart the embed
    service") over httpx's own str(exc), which never includes the
    response body — without this, that carefully-written detail message
    is lost the moment a caller catches the exception.
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("detail")
            if detail:
                return str(detail)
        except Exception:
            pass
        body = (exc.response.text or "")[:300]
        return f"embed service returned HTTP {exc.response.status_code}" + (f": {body}" if body else "")
    if isinstance(exc, httpx.ConnectError):
        return (
            "embed service is unreachable (connection refused) — is the "
            "embed-svc container running, and is EMBED_SVC_URL set correctly?"
        )
    if isinstance(exc, httpx.TimeoutException):
        return "embed service timed out — it may be overloaded or still starting up"
    return str(exc)
