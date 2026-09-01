# SPDX-License-Identifier: Apache-2.0
"""
ABStudio Governance Adapter
============================
Wraps platform-level audit, budget, and tool-policy services with
ABStudio-specific context labels.

Design principles
-----------------
* Fail-open for audit/storage unavailability — a DB hiccup must never
  break a user's workflow run.
* Fail-closed for explicit tool-policy denies — a denied tool call
  returns a structured error to the LLM; it does NOT crash the workflow.
* Reuse platform implementations (core/request_audit.py,
  store/budget_store.py, core/model_registry.py) — no duplication.

Config toggles (env vars)
-------------------------
ABSTUDIO_GOVERNANCE_AUDIT_ENABLED        default true
ABSTUDIO_BUDGET_ENFORCEMENT_ENABLED      default true
ABSTUDIO_TOOL_POLICY_ENFORCEMENT_ENABLED default true
ABSTUDIO_BUDGET_PRODUCT_ID               default "abstudio"
ABSTUDIO_BLOCKED_TOOLS                   comma-separated tool names, default ""
ABSTUDIO_SENSITIVE_TOOLS                 comma-separated tool names, default ""
"""
from __future__ import annotations


import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from core.logger import logger
# ---------------------------------------------------------------------------
# Config toggles
# ---------------------------------------------------------------------------

def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


AUDIT_ENABLED           = _flag("ABSTUDIO_GOVERNANCE_AUDIT_ENABLED",       True)
BUDGET_ENFORCEMENT      = _flag("ABSTUDIO_BUDGET_ENFORCEMENT_ENABLED",      True)
TOOL_POLICY_ENFORCEMENT = _flag("ABSTUDIO_TOOL_POLICY_ENFORCEMENT_ENABLED", True)

BUDGET_PRODUCT_ID = os.getenv("ABSTUDIO_BUDGET_PRODUCT_ID", "abstudio")


def _csv_set(name: str) -> Set[str]:
    raw = os.getenv(name, "")
    return {t.strip() for t in raw.split(",") if t.strip()}


BLOCKED_TOOLS   = _csv_set("ABSTUDIO_BLOCKED_TOOLS")
SENSITIVE_TOOLS = _csv_set("ABSTUDIO_SENSITIVE_TOOLS")

# Hierarchy-based tool restrictions.
# ABSTUDIO_RESTRICTED_TOOLS_MID — tools blocked for mid-level users (ad_level 4–5).
#   These are typically destructive or privileged operations.
#   Example: delete_file,drop_table,execute_sql,shell_exec,code_exec
# ABSTUDIO_READONLY_TOOLS — allowlist for junior users (ad_level 6).
#   Only tools in this list are permitted. Empty = all tools blocked for juniors.
#   Example: read_file,search_web,get_jira_issue,get_confluence_page
RESTRICTED_TOOLS_MID = _csv_set("ABSTUDIO_RESTRICTED_TOOLS_MID")
READONLY_TOOLS        = _csv_set("ABSTUDIO_READONLY_TOOLS")


# ---------------------------------------------------------------------------
# Token / cost estimation helpers
# ---------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4  # rough approximation when exact usage is unavailable


def _estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


# Cache of the in-house/local model IDs (lowercased). Populated lazily from
# the local gateway's discovered catalogue + the LOCAL_* env allowlists, then
# reused so budget preflights don't pay a gateway round-trip per request.
_LOCAL_MODEL_IDS_CACHE: Optional[Set[str]] = None
_LOCAL_MODEL_IDS_TS: float = 0.0
_LOCAL_MODEL_IDS_TTL_S = 300.0  # re-discover at most every 5 minutes

# DEFERRED (ARCH-F-022 / EA Finding 3):
# Concurrent coroutines racing on the shared cache globals above could all
# read a stale cache simultaneously and each trigger a duplicate gateway HTTP
# call (cache stampede). The fix is to introduce an asyncio.Lock that
# serialises cache refreshes so only one coroutine fetches while others wait.
#
# This requires making _local_model_ids() async-safe and propagating async
# through every call site:
#   _is_local_model → estimate_model_cost → _estimate_cost
#   _resolve_budget_failure_fallback_model → check_budget_allowed
#   RunUsageTracker.observe_event / finalize
# ...and their callers in api/execution.py, api/deps.py, api/factories.py,
# services/trigger_scheduler.py, and engine/native_engine.py.
#
# That cross-cutting async propagation is out of scope for this security-fix
# branch. All call sites continue to use the synchronous _local_model_ids()
# directly until the follow-up is completed. The race condition is unchanged
# from the pre-branch baseline — no regression introduced here.


def _env_local_model_ids() -> Set[str]:
    """Local model IDs declared via env vars (no network needed).

    Covers the same env allowlists the local gateway and model registry read:
    LOCAL_VISION_MODELS, LOCAL_SIMPLE/MEDIUM/COMPLEX_MODELS, and the single
    LOCAL_LLM_MODEL_NAME / LOCAL_LLM_MODEL default.
    """
    ids: Set[str] = set()
    for var in (
        "LOCAL_VISION_MODELS",
        "LOCAL_SIMPLE_MODELS",
        "LOCAL_MEDIUM_MODELS",
        "LOCAL_COMPLEX_MODELS",
    ):
        for m in os.getenv(var, "").split(","):
            m = m.strip().lower()
            if m:
                ids.add(m)
    for var in ("LOCAL_LLM_MODEL_NAME", "LOCAL_LLM_MODEL"):
        m = (os.getenv(var) or "").strip().lower()
        if m:
            ids.add(m)
    return ids


def _local_model_ids() -> Set[str]:
    """Authoritative set of in-house/local model IDs (lowercased), cached.

    Sourced from the live local gateway catalogue (``list_models()`` — the same
    discovery that feeds the Agent/Workflow model picker's "Local (In-house)"
    group) merged with the LOCAL_* env allowlists. Falls back to just the env
    IDs when the gateway is unavailable so detection still works offline.
    """
    global _LOCAL_MODEL_IDS_CACHE, _LOCAL_MODEL_IDS_TS
    now = time.monotonic()
    if _LOCAL_MODEL_IDS_CACHE is not None and (now - _LOCAL_MODEL_IDS_TS) < _LOCAL_MODEL_IDS_TTL_S:
        return _LOCAL_MODEL_IDS_CACHE

    ids = _env_local_model_ids()
    env_count = len(ids)
    gateway_ok = False
    try:
        from gateway_local_llm import get_local_gateway  # type: ignore
        for mid in (get_local_gateway().list_models() or []):
            mid = (mid or "").strip().lower()
            if mid and mid != "local":
                ids.add(mid)
        gateway_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[AGENT] _local_model_ids: local gateway unavailable — env-only: {exc}")

    _LOCAL_MODEL_IDS_CACHE = ids
    _LOCAL_MODEL_IDS_TS = now
    logger.info(
        f"[AGENT] _local_model_ids: refreshed local model catalogue "
        f"(env={env_count}, gateway_ok={gateway_ok}, total={len(ids)}): {sorted(ids)}"
    )
    return ids


def _is_local_model(model_name: str) -> bool:
    """Return True for in-house/local models that are cost-exempt.

    Local models in this platform are identified by the in-house gateway
    catalogue (e.g. ``Kimi-k2.7``, ``glm-5.2``) — names that carry no obvious
    "local"/"llama" token — so a pure substring heuristic misses them. We check
    the authoritative gateway + env allowlist first, then fall back to the
    substring/prefix heuristic for ``local:``-prefixed ids and ollama/llama
    names.
    """
    if not model_name:
        logger.debug("[AGENT] _is_local_model: empty model_name → False")
        return False
    lower = model_name.lower()
    # ``local:<id>`` pinned ids (CHAT_FALLBACK_CHAIN style) — strip the prefix
    # before matching against the catalogue.
    bare = lower.split(":", 1)[1] if lower.startswith("local:") else lower
    if lower.startswith("local:"):
        logger.debug(f"[AGENT] _is_local_model: model={model_name!r} matched local: prefix → True")
        return True
    catalogue = _local_model_ids()
    if bare in catalogue:
        logger.debug(f"[AGENT] _is_local_model: model={model_name!r} found in local catalogue → True")
        return True
    # Fallback name heuristic for common local families.
    heuristic = (
        "local" in bare
        or "ollama" in bare
        or "llama" in bare
        or bare.startswith("kimi")
        or bare.startswith("glm-")
        or bare.startswith("qwen")
        or bare.startswith("mistral")
        or bare.startswith("mixtral")
    )
    logger.debug(
        f"[AGENT] _is_local_model: model={model_name!r} not in catalogue "
        f"(size={len(catalogue)}) → heuristic={heuristic}"
    )
    return heuristic


def estimate_model_cost(model_name: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate USD cost using the platform model registry pricing table."""
    if _is_local_model(model_name):
        return 0.0
    try:
        from core.model_registry import MODEL_COST_PER_1M
        pricing = MODEL_COST_PER_1M.get(model_name)
        if pricing:
            in_cost, out_cost = pricing
            return (tokens_in * in_cost + tokens_out * out_cost) / 1_000_000
    except Exception:
        pass
    return 0.0


def _estimate_cost(model_name: str, tokens_in: int, tokens_out: int) -> float:
    return estimate_model_cost(model_name, tokens_in, tokens_out)


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def audit_event(
    *,
    user_id: str,
    endpoint: str,
    action: str,
    workflow_id: str = "",
    workflow_name: str = "",
    thread_id: str = "",
    request_id: str = "",
    email: str = "",
    department: str = "",
    model_used: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    error: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a best-effort ABStudio audit event via the platform audit helper.

    Never raises — audit failures must not affect the user response path.
    The ``endpoint`` field carries the ABStudio-specific label
    (e.g. ``abstudio.workflow.run``) and ``action`` carries the sub-action
    (e.g. ``start``, ``success``, ``error``).
    """
    if not AUDIT_ENABLED:
        return

    # Build a short question-like summary for the audit log's question_hash.
    summary_parts = [f"action={action}"]
    if workflow_id:
        summary_parts.append(f"workflow={workflow_id}")
    if thread_id:
        summary_parts.append(f"thread={thread_id}")
    if extra:
        for k, v in list(extra.items())[:3]:
            summary_parts.append(f"{k}={v}")
    summary = " ".join(summary_parts)

    try:
        from core.request_audit import record_audit
        record_audit(
            user_id=user_id or "anonymous",
            client_source=f"abstudio.{action}",
            endpoint=endpoint,
            request_id=request_id or "",
            email=email or "",
            department=department or "",
            question=summary,
            model_used=model_used or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            cache_hit="none",
            compliance_blocked=False,
            error=error or "",
        )
    except Exception as exc:
        logger.debug(f'[AGENT] ABStudio audit_event failed (non-fatal): {exc}')


# ---------------------------------------------------------------------------
# Budget helpers
# ---------------------------------------------------------------------------

# ``store.budget_store.check_budget`` swallows a total Redis+Postgres outage
# and returns this exact reason instead of raising. We must recognise it or the
# fail-closed gate below would never engage.
_BUDGET_UNAVAILABLE_SENTINEL = "budget-check-unavailable (fail-open)"


def _resolve_budget_failure_fallback_model() -> str:
    """Resolve the zero-cost model paid runs are downgraded to during a
    budget-store outage.

    Uses the platform's existing auto-fallback model,
    ``ABSTUDIO_FALLBACK_LLM_MODEL`` (``.env`` pins this to a local/in-house
    model, e.g. ``kimi-k2.7-code``) — the same var ``llm_handler`` already uses
    for its transparent primary→fallback switch, so a budget-outage downgrade
    lands on the model the deployment has already chosen for that role.

    The value is still validated with ``_is_local_model``: a *paid* model here
    would defeat the purpose (spend must not continue while the budget store is
    blind), so a non-local value is rejected and ``""`` is returned — which
    callers turn into a hard deny rather than untracked cloud spend.
    """
    configured = (os.getenv("ABSTUDIO_FALLBACK_LLM_MODEL") or "").strip()
    if not configured:
        logger.error(
            '[AGENT] ABSTUDIO_FALLBACK_LLM_MODEL is not set — no no-cost fallback is '
            'available, so paid runs will be denied while the budget store is down.'
        )
        return ""
    if not _is_local_model(configured):
        logger.error(
            f'[AGENT] ABSTUDIO_FALLBACK_LLM_MODEL={configured!r} is not a local/in-house '
            f'model — refusing to use a paid model as the budget-outage fallback '
            f'(that would keep spending while the budget store is blind). Paid runs '
            f'will be denied until the budget store recovers.'
        )
        return ""
    return configured


def check_budget_allowed(user_id: str) -> Dict[str, Any]:
    """Check whether the user has remaining budget.

    Returns ``{"allowed": True, "reason": "..."}`` or
    ``{"allowed": False, "reason": "..."}``.

    Fail-CLOSED on budget-store unavailability
    ------------------------------------------
    This used to fail *open* (``allowed: True``) on any Redis/Postgres error,
    which left cloud-LLM spend completely ungoverned for the duration of an
    outage — a hostile user could drive arbitrary cost precisely when we were
    blind to it.

    It now fails closed: the paid run is refused. To keep the platform usable
    the verdict carries a downgrade hint — ``fallback_model``, a zero-cost
    local/in-house model — so callers can re-run the request locally instead of
    hard-failing. When no local model is configured the run is denied outright
    (``fallback_model: ""``); spend is never allowed to continue unmetered.

    Extra keys present only on a degraded verdict:
      * ``degraded``       – True when the store was unavailable
      * ``fallback_model`` – local model to downgrade to, or "" for hard deny
      * ``code``           – ``BUDGET_STORE_UNAVAILABLE``
    """
    if not BUDGET_ENFORCEMENT:
        return {"allowed": True, "reason": "budget enforcement disabled"}

    def _degraded(detail: str) -> Dict[str, Any]:
        fallback = _resolve_budget_failure_fallback_model()
        if fallback:
            reason = (
                "Budget service is temporarily unavailable, so paid (cloud) models "
                f"are blocked. Continuing on the no-cost local model '{fallback}'."
            )
        else:
            reason = (
                "Budget service is temporarily unavailable and no local (no-cost) "
                "model is configured, so this run was blocked to prevent untracked "
                "spend. Please retry shortly."
            )
        logger.error(
            f'[AGENT] ABStudio check_budget_allowed: budget store unavailable — '
            f'FAIL-CLOSED for user={user_id} ({detail}); '
            f"fallback_model={fallback or '<none — hard deny>'}"
        )
        return {
            "allowed": False,
            "reason": reason,
            "degraded": True,
            "fallback_model": fallback,
            "code": "BUDGET_STORE_UNAVAILABLE",
        }

    try:
        from store.budget_store import check_budget
        result = check_budget(user_id)
    except Exception as exc:
        return _degraded(f"{type(exc).__name__}: {exc}")

    # Convert budget_store's own internal fail-open sentinel into a
    # fail-closed degraded verdict.
    if (
        isinstance(result, dict)
        and result.get("allowed")
        and str(result.get("reason", "")).strip() == _BUDGET_UNAVAILABLE_SENTINEL
    ):
        return _degraded("budget_store returned its internal fail-open sentinel")

    return result


def budget_degraded_fallback_model(budget_result: Dict[str, Any]) -> str:
    """Return the local model a denied-but-degraded run should downgrade to.

    ``""`` means "do not downgrade — deny the run". That covers both a normal
    budget denial (user genuinely out of funds; a free local re-run would let
    them bypass their limit) and a degraded verdict with no local model
    configured.
    """
    if not isinstance(budget_result, dict):
        return ""
    if not budget_result.get("degraded"):
        return ""
    return str(budget_result.get("fallback_model") or "")


def budget_denied_detail(budget_result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the structured payload describing a budget denial.

    Single source of truth for the deny *contract*, so every entry point — the
    HTTP endpoints (as an ``HTTPException`` 429 detail) and the trigger
    scheduler (as audit ``extra`` + a persisted execution error) — reports the
    same ``code`` and retryability for the same verdict.

    ``degraded`` (budget store unreachable) is distinguished from a genuine
    over-limit denial: the former is transient and safe for the client to
    retry, the latter is not, and conflating them would either hide an outage
    or invite a retry storm against a user who is legitimately out of funds.

    Deliberately fastapi-free — this module is a service-layer adapter and must
    stay importable by non-HTTP callers such as the trigger scheduler.
    """
    if not isinstance(budget_result, dict):
        budget_result = {}
    degraded = bool(budget_result.get("degraded"))
    return {
        "code": budget_result.get(
            "code", "BUDGET_STORE_UNAVAILABLE" if degraded else "BUDGET_EXCEEDED"
        ),
        "message": budget_result.get("reason", "Budget limit reached"),
        **({"degraded": True, "retryable": True} if degraded else {}),
    }


def budget_degraded_allowed(fallback_model: str) -> Dict[str, Any]:
    """The synthetic allow-verdict that a successful degraded downgrade yields.

    After a caller has re-pointed the run at ``fallback_model`` there is no
    remaining spend to govern, so the original degraded *denial* is replaced
    with this allow verdict and the run proceeds locally. Centralised so the
    ``reason`` string stays identical across entry points.
    """
    return {"allowed": True, "reason": f"degraded: local model {fallback_model}"}


def increment_budget_usage(
    user_id: str,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    requests: int = 1,
) -> None:
    """Record ABStudio request/token/cost usage in the platform budget store.

    Best-effort — never raises.
    """
    if not BUDGET_ENFORCEMENT:
        return
    total_tokens = tokens_in + tokens_out
    try:
        from store.budget_store import increment_usage
        increment_usage(
            user_id=user_id,
            tokens=total_tokens,
            requests=requests,
            cost_usd=cost_usd,
            product_id=BUDGET_PRODUCT_ID,
        )
    except Exception as exc:
        logger.warning(f'[AGENT] ABStudio increment_budget_usage failed (non-fatal): {exc}')


# ---------------------------------------------------------------------------
# Per-run usage tracker
# ---------------------------------------------------------------------------

@dataclass
class RunUsageTracker:
    """Tracks token/cost/latency for a single workflow or agent run.

    Usage
    -----
    1. Create at the start of a run: ``tracker = RunUsageTracker(user_id, ...)``.
    2. Call ``observe_event(payload)`` for each SSE payload dict from the engine.
    3. Call ``finalize(status)`` at the end (success or error) to write audit
       and budget records.
    """
    user_id: str
    endpoint: str
    workflow_id: str = ""
    workflow_name: str = ""
    thread_id: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    email: str = ""
    department: str = ""
    model_used: str = ""

    _start_time: float = field(default_factory=time.monotonic, init=False)
    _tokens_in: int = field(default=0, init=False)
    _tokens_out: int = field(default=0, init=False)
    _cost_usd: float = field(default=0.0, init=False)
    _finalized: bool = field(default=False, init=False)

    def observe_event(self, payload: Dict[str, Any]) -> None:
        """Extract usage signals from an engine SSE payload dict."""
        etype = payload.get("event") or payload.get("type") or ""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        model = data.get("model") or data.get("model_used") or ""
        if model and not self.model_used:
            self.model_used = model
        # Count only agent_usage events; agent_complete/complete usage blocks
        # are display summaries and would double-count the same LLM turns.
        if etype == "agent_usage":
            usage = data.get("usage") or {}
            tokens_in = int(
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or usage.get("tokens_in")
                or 0
            )
            tokens_out = int(
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or usage.get("tokens_out")
                or 0
            )
            self._tokens_in += tokens_in
            self._tokens_out += tokens_out
            cost = float(usage.get("cost_usd") or 0.0)
            if not cost and (tokens_in or tokens_out):
                cost = estimate_model_cost(model or self.model_used, tokens_in, tokens_out)
            self._cost_usd += cost
        # Track thread_id from complete events
        tid = data.get("thread_id") or ""
        if tid and not self.thread_id:
            self.thread_id = tid

    def add_tokens(self, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0) -> None:
        """Manually add token/cost estimates (e.g. from prompt length)."""
        self._tokens_in  += tokens_in
        self._tokens_out += tokens_out
        self._cost_usd   += cost_usd

    def finalize(
        self,
        status: str,
        *,
        error: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write audit event and increment budget usage. Idempotent."""
        if self._finalized:
            return
        self._finalized = True

        latency_ms = int((time.monotonic() - self._start_time) * 1000)

        # If we have no token data from the engine, estimate from model
        if not self._tokens_in and not self._tokens_out and self.model_used:
            pass  # leave at 0 — no prompt text available here

        # Estimate cost if not already set
        if not self._cost_usd and (self._tokens_in or self._tokens_out):
            self._cost_usd = _estimate_cost(
                self.model_used, self._tokens_in, self._tokens_out
            )

        audit_event(
            user_id=self.user_id,
            endpoint=self.endpoint,
            action=status,
            workflow_id=self.workflow_id,
            workflow_name=self.workflow_name,
            thread_id=self.thread_id,
            request_id=self.request_id,
            email=self.email,
            department=self.department,
            model_used=self.model_used,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            cost_usd=self._cost_usd,
            latency_ms=latency_ms,
            error=error,
            extra=extra,
        )

        if status not in ("start", "budget_denied"):
            increment_budget_usage(
                self.user_id,
                tokens_in=self._tokens_in,
                tokens_out=self._tokens_out,
                cost_usd=self._cost_usd,
                requests=1,
            )


# ---------------------------------------------------------------------------
# Tool access policy
# ---------------------------------------------------------------------------

class ToolPolicyDenied(Exception):
    """Raised when a tool call is denied by policy."""
    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"Tool '{tool_name}' denied: {reason}")


def check_tool_access(
    tool_name: str,
    *,
    user_id: str = "",
    is_admin: bool = False,
    # Hierarchy fields from ExecutionContext
    ad_level: int = 6,
    is_hod: bool = False,
    is_security_team: bool = False,
    # Node/workflow-level policy inputs
    node_data: Optional[Dict[str, Any]] = None,
    available_tools: Optional[List[str]] = None,   # tools actually attached/surfaced
    allowed_tools: Optional[List[str]] = None,     # explicit allowlist (if set)
    blocked_tools: Optional[List[str]] = None,     # node/workflow-level blocked list
    # Audit context
    endpoint: str = "abstudio.tool.execute",
    workflow_id: str = "",
    thread_id: str = "",
    email: str = "",
    department: str = "",
) -> Optional[str]:
    """Check whether a tool call is permitted by policy.

    Returns ``None`` when the call is allowed.
    Returns a denial reason string when the call is denied.

    Policy evaluation order (first match wins):
    1. Empty tool name -> denied.
    2. Global ABSTUDIO_BLOCKED_TOOLS -> denied.
    3. Node/workflow blocked_tools -> denied.
    4. Explicit allowed_tools allowlist (when present) -> denied if not in list.
    5. available_tools attachment check (when present) -> denied if not attached.
    6. Sensitive tools requiring HITL for non-admin users (best-effort).
    7. Hierarchy-based access (ad_level) -- admins and security team bypass.
    8. Allowed.

    Never raises -- returns a reason string on deny, None on allow.
    """
    if not TOOL_POLICY_ENFORCEMENT:
        return None

    if not tool_name:
        return "empty tool name"

    # 1. Global blocked tools (env-configured)
    if tool_name in BLOCKED_TOOLS:
        return f"tool '{tool_name}' is globally blocked by platform policy"

    # 2. Node/workflow-level blocked tools
    _node_blocked: Set[str] = set()
    if blocked_tools:
        _node_blocked = {t for t in blocked_tools if t}
    elif node_data:
        raw = node_data.get("blocked_tools") or node_data.get("blockedTools") or []
        if isinstance(raw, list):
            _node_blocked = {t for t in raw if t}
        elif isinstance(raw, str):
            _node_blocked = {t.strip() for t in raw.split(",") if t.strip()}
    if tool_name in _node_blocked:
        return f"tool '{tool_name}' is blocked by workflow/node policy"

    # 3. Explicit allowed_tools allowlist
    _allowed: Optional[List[str]] = allowed_tools
    if _allowed is None and node_data:
        raw = node_data.get("allowed_tools") or node_data.get("allowedTools")
        if isinstance(raw, list) and raw:
            _allowed = raw
    if _allowed is not None:
        if tool_name not in _allowed:
            return f"tool '{tool_name}' is not in the allowed_tools list for this node"

    # 4. Available (attached) tools check
    _available: Optional[List[str]] = available_tools
    if _available is not None:
        if tool_name not in _available:
            return f"tool '{tool_name}' is not attached to this agent/node"

    # 5. Sensitive tools -- require HITL / admin for non-admin users
    if tool_name in SENSITIVE_TOOLS and not is_admin:
        # Soft check: log a warning but do not hard-deny unless HITL is off.
        # The engine's HITL gate handles the actual pause; this is a backstop.
        logger.info(f"[AGENT] ABStudio tool_policy: sensitive tool '{tool_name}' called by non-admin user={user_id}")

    # 6. Hierarchy-based access (ad_level)
    # Admins and security team members bypass all hierarchy restrictions.
    # is_hod (Head of Department) is treated as ad_level 3 if their actual
    # ad_level is higher -- HODs always get manager-level tool access.
    if not is_admin and not is_security_team:
        effective_level = min(ad_level, 3) if is_hod else ad_level

        if effective_level <= 2:
            # Senior executives (ad_level 0-2) -- all tools allowed, no restriction.
            pass

        elif effective_level == 3:
            # HOD / Managers (ad_level 3, or is_hod=True) -- full access including
            # sensitive tools. No additional restriction beyond levels 1-5 above.
            pass

        elif effective_level in (4, 5):
            # Mid-level employees (ad_level 4-5) -- blocked from destructive /
            # privileged tools listed in ABSTUDIO_RESTRICTED_TOOLS_MID.
            if RESTRICTED_TOOLS_MID and tool_name in RESTRICTED_TOOLS_MID:
                return (
                    f"tool '{tool_name}' is not permitted for your access level "
                    f"(mid-level users cannot use destructive or privileged tools)"
                )

        else:
            # Junior employees (ad_level 6+) -- only tools in ABSTUDIO_READONLY_TOOLS
            # are permitted. If the allowlist is empty, all tools are blocked.
            if not READONLY_TOOLS:
                return (
                    f"tool '{tool_name}' is not permitted for your access level "
                    f"(no read-only tools configured -- contact your administrator)"
                )
            if tool_name not in READONLY_TOOLS:
                return (
                    f"tool '{tool_name}' is not permitted for your access level "
                    f"(junior users may only use read-only tools)"
                )

    return None  # allowed


def tool_policy_denied_result(tool_name: str, reason: str) -> str:
    """Return a JSON-serialised tool_policy_denied result for the LLM."""
    import json
    return json.dumps({
        "error": "tool_policy_denied",
        "detail": reason,
    })
