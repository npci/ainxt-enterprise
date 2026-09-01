# SPDX-License-Identifier: Apache-2.0
"""Shared FastAPI dependencies and helpers used by all routers."""
import inspect
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from fastapi import Depends, HTTPException, Request, status
from app.models import require_framework_access, AuthenticatedUser
from app.engine import ChainDefinition, ChainEdge, ExecutionContext
from app.core.governance import (
    audit_event,
    budget_degraded_allowed,
    budget_degraded_fallback_model,
    budget_denied_detail,
    check_budget_allowed,
)

# Reuse the shared gateway logging context (core/logger.py) so every AB Studio
# request stamps its structured agent.log lines with the same identifiers the
# rest of the platform uses: request_id, chat_id (AB Studio's thread_id),
# user_id, span_id, client_source. These are thread-local setters — safe under
# uvicorn's worker threads and shared across the handler + its SSE generator
# because asyncio runs both on the same event-loop thread.
from core.logger import (
    set_request_id,
    set_chat_context,
    set_span_id,
    set_client_source,
    clear_chat_context,
)

# Marks AB Studio (Build Studio) as the originating surface in agent.log,
# alongside the existing platform | cli | ide-vscode | ide-jetbrains sources.
ABSTUDIO_CLIENT_SOURCE = "abstudio"


def bind_log_context(
    current_user: Optional[AuthenticatedUser] = None,
    *,
    thread_id: Optional[str] = None,
    request: Optional[Request] = None,
    request_id: Optional[str] = None,
    span: str = "abstudio",
) -> str:
    """Bind the per-request logging context for the current worker thread.

    Populates the ``core.logger`` thread-local so all subsequent ``logger``
    calls in this request carry proper request_id / chat_id / user_id /
    span_id / client_source. Returns the resolved request_id.

    * ``chat_id``    ← AB Studio's ``thread_id`` (its conversation/session id).
    * ``request_id`` ← explicit arg, else an ``X-Request-ID`` header, else a
      freshly minted uuid4 hex so a reused worker thread never inherits a stale
      id from a previous request.

    Call ``clear_log_context()`` in a ``finally`` to avoid leaking context onto
    the next request handled by the same thread.
    """
    rid = request_id
    if not rid and request is not None:
        rid = request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
    if not rid:
        rid = uuid.uuid4().hex
    set_request_id(rid)
    set_chat_context(
        user_id=str(getattr(current_user, "id", "") or "-"),
        chat_id=str(thread_id or "-"),
    )
    set_span_id(span)
    set_client_source(ABSTUDIO_CLIENT_SOURCE)
    return rid


def clear_log_context() -> None:
    """Reset the per-request logging context. Call in a ``finally`` block."""
    clear_chat_context()

# When running inside the AiNxt gateway, wrap the gateway's get_current_user
# (which returns a dict) into an AuthenticatedUser so ABStudio routers can
# use dot-access (current_user.id, current_user.email) without modification.
try:
    from fastapi import Depends as _Depends
    from auth.dependencies import get_current_user as _gateway_auth

    async def _wrapped_gateway_auth(
        _user: dict = _Depends(_gateway_auth),
    ) -> AuthenticatedUser:
        return AuthenticatedUser(
            id=_user.get("userId") or _user.get("id") or _user.get("sub", "unknown"),
            email=_user.get("email", ""),
            full_name=_user.get("name", ""),
            role=_user.get("role", "user"),
            # Department is set server-side in auth/dependencies.py from the
            # users table — required for pgvector PRIVATE-doc ACL filtering
            # in workflow / agent-config KB retrieval.
            department=_user.get("department", "") or "",
            frameworks=["agent-chain"],
            # Hierarchy fields from the enriched JWT payload.
            # ad_level: 0=most senior exec, 6=junior. Default 6 = most restricted.
            ad_level=int(_user.get("ad_level", 6)),
            is_hod=bool(_user.get("is_hod", False)),
            is_security_team=bool(_user.get("is_security_team", False)),
            hod_departments=list(_user.get("hod_departments") or []),
        )

    require_access = _wrapped_gateway_auth
except ImportError:
    require_access = require_framework_access("agent-chain")


async def require_admin(
    current_user: AuthenticatedUser = Depends(require_access),
) -> AuthenticatedUser:
    """Authenticated-user dependency that additionally enforces admin role.

    Reuses ``require_access`` so the same gateway-wrapped auth path applies
    (real JWT in-process, dev-stub admin standalone). Non-admins receive a
    403 — distinct from the 401 ``require_access`` raises on missing auth.
    """
    if (current_user.role or "").lower() != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return current_user


def build_execution_context(
    current_user: AuthenticatedUser,
    *,
    thread_id: Optional[str] = None,
    workflow_id: str = "",
    workflow_name: str = "",
    subagents_enabled: Optional[bool] = None,
    # Loop Engineering (P2) fields. All optional so existing callers that
    # don't know about the loop subsystem keep working unchanged.
    goal_id: Optional[str] = None,
    loop_id: Optional[str] = None,
    loop_run_id: Optional[str] = None,
    budget: Optional[Dict[str, Any]] = None,
    trigger_src: Optional[str] = None,
    run_workspace_dir: Optional[str] = None,
    allowed_connections: Optional[List[str]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> ExecutionContext:
    """Construct an ExecutionContext from the authenticated request user.

    Carries department + admin flag so workflow agent nodes can apply the
    same PUBLIC + user-dept PRIVATE pgvector ACL the chat ``Knowledge``
    toggle uses. ``subagents_enabled`` is the run-level swarm opt-in from
    the chat panel; None = older client / not sent.

    The loop-engineering kwargs (``goal_id`` / ``loop_id`` / ``loop_run_id``
    / ``budget`` / ``trigger_src`` / ``run_workspace_dir`` /
    ``allowed_connections``) flow through to LoopRunner — see PHASE_2 §6.
    """
    return ExecutionContext(
        thread_id=thread_id,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        user_id=current_user.id,
        email=current_user.email,
        department=current_user.department or "",
        is_admin=(current_user.role or "").lower() == "admin",
        ad_level=current_user.ad_level,
        is_hod=current_user.is_hod,
        is_security_team=current_user.is_security_team,
        subagents_enabled=subagents_enabled,
        goal_id=goal_id,
        loop_id=loop_id,
        loop_run_id=loop_run_id,
        budget=budget,
        trigger_src=trigger_src,
        run_workspace_dir=run_workspace_dir,
        allowed_connections=allowed_connections,
        attachments=attachments,
    )


def to_chain(workflow) -> ChainDefinition:
    """Convert Workflow model to engine-agnostic ChainDefinition."""
    edges = [
        ChainEdge(
            source=e.source,
            target=e.target,
            source_handle=e.sourceHandle,
        )
        for e in workflow.edges
    ]
    # ``knowledge`` is optional on older clients — fall through as None.
    return ChainDefinition(
        nodes=workflow.nodes,
        edges=edges,
        knowledge=getattr(workflow, "knowledge", None),
    )


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Budget preflight (shared by every run/chat entry point)
# ---------------------------------------------------------------------------

@dataclass
class BudgetDecision:
    """Outcome of a budget preflight that did NOT deny the run.

    A denial never produces a ``BudgetDecision`` — it raises, so reaching one of
    these means the run is cleared to proceed.

    * ``notice`` – non-empty only when the run was downgraded during a budget
      store outage. Callers MUST surface it (SSE ``agent_fallback`` frame or a
      response field) so the user knows they got a different model than the one
      they configured.
    * ``fallback_model`` – the local model actually applied, "" if no downgrade.
    * ``config`` – whatever the caller's ``downgrade`` callable returned, for
      the agent paths that rebuild a config dict. ``None`` when the callable
      mutates in place (the workflow paths, which return a bool).
    """
    notice: str = ""
    fallback_model: str = ""
    config: Any = None

    @property
    def downgraded(self) -> bool:
        return bool(self.fallback_model)


async def enforce_budget_or_downgrade(
    *,
    user_id: str,
    endpoint: str,
    skip_check: bool,
    downgrade: Callable[[str], Any],
    audit_kwargs: Optional[Dict[str, Any]] = None,
) -> BudgetDecision:
    """Run the budget preflight: check → downgrade-on-degraded → deny with 429.

    This is the single implementation of the fail-closed budget policy for all
    HTTP entry points (workflow run / run-stream / resume-stream, agent chat /
    chat-stream). It previously existed as five near-identical inline copies,
    which is how the non-streaming workflow path silently lost its downgrade
    notice — a security control with five independent edit points will drift.

    Policy, in order:

    1. ``skip_check=True`` bypasses the check entirely. Pass the caller's
       "is this run all-local?" verdict here: local models incur no spend, so
       blocking them on budget would be pure false-positive. Callers are
       expected to fail *safe* (return False → enforce) on any lookup error.
    2. On a degraded verdict (store unreachable) ask ``downgrade`` to re-point
       the run at the no-cost local fallback. A truthy return means the
       downgrade held, and the run continues under a synthetic allow verdict.
       A falsy/``None`` return means the caller could not guarantee an all-local
       run, so we fall through to the denial below rather than continue on the
       paid model — never trade a failed downgrade for untracked spend.
    3. Any remaining denial raises ``HTTPException(429)`` with the shared
       structured detail from ``governance.budget_denied_detail``.

    ``downgrade`` may be sync or async; the result is awaited when awaitable, so
    both ``_downgrade_workflow_to_model`` (sync, returns bool) and
    ``_agent_config_downgraded_to`` (async, returns dict|None) plug in directly.

    Every branch that changes the run's fate is audited: a downgrade emits
    ``budget_degraded_downgrade`` and a denial emits ``budget_denied``, both
    with the caller's ``audit_kwargs`` (workflow/thread identifiers, email,
    department) merged in.

    Raises:
        HTTPException: 429 when the run is denied.
    """
    audit_kwargs = audit_kwargs or {}

    budget_result: Dict[str, Any] = {"allowed": True}
    if not skip_check:
        budget_result = check_budget_allowed(user_id)

    decision = BudgetDecision()
    fallback = budget_degraded_fallback_model(budget_result)
    if fallback:
        applied = downgrade(fallback)
        if inspect.isawaitable(applied):
            applied = await applied
        if applied:
            decision = BudgetDecision(
                notice=budget_result.get("reason", ""),
                fallback_model=fallback,
                # Workflow paths mutate in place and return True — there is no
                # config object to hand back in that case.
                config=None if applied is True else applied,
            )
            audit_event(
                user_id=user_id,
                endpoint=endpoint,
                action="budget_degraded_downgrade",
                error=f"budget store unavailable — downgraded to {fallback}",
                **audit_kwargs,
            )
            budget_result = budget_degraded_allowed(fallback)

    if not budget_result.get("allowed"):
        audit_event(
            user_id=user_id,
            endpoint=endpoint,
            action="budget_denied",
            error=budget_result.get("reason", "budget exceeded"),
            **audit_kwargs,
        )
        raise HTTPException(
            status_code=429,
            detail=budget_denied_detail(budget_result),
        )

    return decision
