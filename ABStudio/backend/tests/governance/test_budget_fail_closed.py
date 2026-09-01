# SPDX-License-Identifier: Apache-2.0
"""Budget-check fail-CLOSED tests (unbounded-spend-during-outage fix).

Background: ``governance.check_budget_allowed`` used to return
``{"allowed": True, "reason": "budget-check-unavailable (fail-open)"}`` on any
Redis/Postgres error. During a backing-store outage that left cloud-LLM spend
completely ungoverned — a hostile user could drive arbitrary cost precisely
when we were blind to it.

It now fails closed. The paid run is refused, and the verdict carries a
``fallback_model`` (a zero-cost local/in-house model) so callers can downgrade
the run instead of hard-failing. When no local model is configured the run is
denied outright — spend is never allowed to continue unmetered.

Note ``store.budget_store.check_budget`` has its OWN internal fail-open and
returns the sentinel reason rather than raising, so the sentinel path is
covered explicitly: without that detection the gate would never engage.
"""
from __future__ import annotations

import importlib

import pytest


_USER = "user-budget-test"
_SENTINEL = "budget-check-unavailable (fail-open)"


@pytest.fixture()
def gov(monkeypatch):
    """Import governance with budget enforcement ON and a known local model."""
    monkeypatch.setenv("ABSTUDIO_BUDGET_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("LOCAL_SIMPLE_MODELS", "kimi-k2.7-code,glm-5.2")
    monkeypatch.delenv("ABSTUDIO_FALLBACK_LLM_MODEL", raising=False)
    import app.core.governance as g
    importlib.reload(g)
    return g


def _patch_store(monkeypatch, *, raises=None, returns=None):
    """Patch store.budget_store.check_budget (imported inside the function)."""
    import store.budget_store as bs

    def _fake(user_id):
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(bs, "check_budget", _fake)


# ---------------------------------------------------------------------------
# Fallback model resolution
# ---------------------------------------------------------------------------

class TestFallbackModelResolution:
    def test_configured_local_model_is_used(self, gov, monkeypatch):
        """The deployment's existing auto-fallback model is the downgrade target.
        .env pins ABSTUDIO_FALLBACK_LLM_MODEL=kimi-k2.7-code."""
        monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "kimi-k2.7-code")
        assert gov._resolve_budget_failure_fallback_model() == "kimi-k2.7-code"

    def test_paid_model_is_rejected(self, gov, monkeypatch):
        """A paid model as the outage fallback would defeat the whole fix —
        spend must not continue while the budget store is blind."""
        monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "claude-sonnet-4-6")
        assert gov._resolve_budget_failure_fallback_model() == ""

    def test_unset_var_yields_no_fallback(self, gov, monkeypatch):
        """Unset → no no-cost path → callers hard-deny rather than spend."""
        monkeypatch.delenv("ABSTUDIO_FALLBACK_LLM_MODEL", raising=False)
        assert gov._resolve_budget_failure_fallback_model() == ""

    def test_local_prefixed_value_is_accepted(self, gov, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "local:glm-5.2")
        assert gov._resolve_budget_failure_fallback_model() == "local:glm-5.2"


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_store_exception_denies_the_run(self, gov, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "kimi-k2.7-code")
        _patch_store(monkeypatch, raises=RuntimeError("redis down"))
        res = gov.check_budget_allowed(_USER)
        assert res["allowed"] is False, "must FAIL CLOSED on a store exception"
        assert res["degraded"] is True
        assert res["code"] == "BUDGET_STORE_UNAVAILABLE"
        assert res["fallback_model"] == "kimi-k2.7-code"

    def test_store_internal_fail_open_sentinel_is_converted_to_deny(self, gov, monkeypatch):
        """budget_store swallows a total outage and returns its own fail-open
        sentinel instead of raising. Without detecting it the gate never fires."""
        monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "kimi-k2.7-code")
        _patch_store(monkeypatch, returns={"allowed": True, "reason": _SENTINEL})
        res = gov.check_budget_allowed(_USER)
        assert res["allowed"] is False, "sentinel must be converted to fail-closed"
        assert res["degraded"] is True
        assert res["fallback_model"] == "kimi-k2.7-code"

    def test_paid_fallback_config_is_a_hard_deny(self, gov, monkeypatch):
        """Fallback var pointing at a PAID model → no zero-cost path, so the
        run is refused outright rather than spending untracked."""
        monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "claude-sonnet-4-6")
        _patch_store(monkeypatch, raises=RuntimeError("postgres down"))
        res = gov.check_budget_allowed(_USER)
        assert res["allowed"] is False
        assert res["fallback_model"] == ""
        assert "blocked" in res["reason"].lower()

    def test_unset_fallback_config_is_a_hard_deny(self, gov, monkeypatch):
        monkeypatch.delenv("ABSTUDIO_FALLBACK_LLM_MODEL", raising=False)
        _patch_store(monkeypatch, raises=RuntimeError("redis down"))
        res = gov.check_budget_allowed(_USER)
        assert res["allowed"] is False
        assert res["fallback_model"] == ""

    def test_degraded_reason_is_user_facing(self, gov, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "kimi-k2.7-code")
        _patch_store(monkeypatch, raises=RuntimeError("redis down"))
        reason = gov.check_budget_allowed(_USER)["reason"]
        assert "temporarily unavailable" in reason.lower()
        assert "kimi-k2.7-code" in reason
        # The old fail-open wording must be gone.
        assert "fail-open" not in reason.lower()


# ---------------------------------------------------------------------------
# Non-outage paths must be unchanged
# ---------------------------------------------------------------------------

class TestNormalPathsUnchanged:
    def test_normal_allow_passes_through(self, gov, monkeypatch):
        _patch_store(monkeypatch, returns={"allowed": True, "reason": "ok"})
        res = gov.check_budget_allowed(_USER)
        assert res["allowed"] is True
        assert res.get("degraded") is None

    def test_genuine_budget_exceeded_passes_through(self, gov, monkeypatch):
        _patch_store(
            monkeypatch,
            returns={"allowed": False, "reason": "Total spend limit reached"},
        )
        res = gov.check_budget_allowed(_USER)
        assert res["allowed"] is False
        # NOT degraded — this is a real denial, so callers must not downgrade
        # the user onto a free local model and let them bypass their limit.
        assert res.get("degraded") is None
        assert gov.budget_degraded_fallback_model(res) == ""

    def test_enforcement_disabled_still_allows(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_BUDGET_ENFORCEMENT_ENABLED", "false")
        import app.core.governance as g
        importlib.reload(g)
        res = g.check_budget_allowed(_USER)
        assert res["allowed"] is True
        assert res.get("degraded") is None


# ---------------------------------------------------------------------------
# The caller-facing helper
# ---------------------------------------------------------------------------

class TestBudgetDegradedFallbackModel:
    def test_returns_model_for_degraded_verdict(self, gov):
        res = {"allowed": False, "degraded": True, "fallback_model": "local:glm-5.2"}
        assert gov.budget_degraded_fallback_model(res) == "local:glm-5.2"

    def test_empty_for_non_degraded_denial(self, gov):
        res = {"allowed": False, "reason": "Total spend limit reached"}
        assert gov.budget_degraded_fallback_model(res) == ""

    def test_empty_when_degraded_but_no_model(self, gov):
        res = {"allowed": False, "degraded": True, "fallback_model": ""}
        assert gov.budget_degraded_fallback_model(res) == ""

    def test_tolerates_non_dict(self, gov):
        assert gov.budget_degraded_fallback_model(None) == ""


# ---------------------------------------------------------------------------
# The shared deny-payload contract
# ---------------------------------------------------------------------------

class TestBudgetDeniedDetail:
    """``budget_denied_detail`` is the single source of truth for the deny
    contract, replacing five hand-rolled copies of the same dict literal."""

    def test_degraded_verdict_is_retryable(self, gov):
        """A store outage is transient — the client should be told to retry.
        Conflating it with a real over-limit denial would hide the outage."""
        detail = gov.budget_denied_detail(
            {"allowed": False, "degraded": True, "reason": "store down"}
        )
        assert detail == {
            "code": "BUDGET_STORE_UNAVAILABLE",
            "message": "store down",
            "degraded": True,
            "retryable": True,
        }

    def test_genuine_denial_is_not_retryable(self, gov):
        """A user genuinely out of funds must NOT be advertised as retryable —
        that would invite a retry storm against a limit that will not lift."""
        detail = gov.budget_denied_detail(
            {"allowed": False, "reason": "Total spend limit reached"}
        )
        assert detail["code"] == "BUDGET_EXCEEDED"
        assert "degraded" not in detail
        assert "retryable" not in detail

    def test_explicit_code_wins(self, gov):
        """check_budget_allowed already sets `code`; it must not be overridden."""
        detail = gov.budget_denied_detail(
            {"degraded": True, "code": "CUSTOM_CODE", "reason": "r"}
        )
        assert detail["code"] == "CUSTOM_CODE"

    def test_missing_reason_falls_back_to_generic_message(self, gov):
        assert gov.budget_denied_detail({})["message"] == "Budget limit reached"

    def test_tolerates_non_dict(self, gov):
        """Never raise from an error path — a crash here would turn a 429 into
        an unhandled 500 and lose the deny reason entirely."""
        detail = gov.budget_denied_detail(None)
        assert detail["code"] == "BUDGET_EXCEEDED"
        assert detail["message"] == "Budget limit reached"


class TestBudgetDegradedAllowed:
    def test_reason_names_the_applied_model(self, gov):
        """The reason is what surfaces to the user as the downgrade notice, so
        it must name the model that actually ran."""
        res = gov.budget_degraded_allowed("kimi-k2.7-code")
        assert res["allowed"] is True
        assert "kimi-k2.7-code" in res["reason"]
        # Must NOT carry `degraded` — the downgrade already resolved the outage
        # for this run, so downstream deny checks have to see a clean allow.
        assert "degraded" not in res


# ---------------------------------------------------------------------------
# The shared HTTP preflight
# ---------------------------------------------------------------------------

pytest.importorskip("fastapi", reason="deps.py is a FastAPI module")


@pytest.fixture()
def deps(gov, monkeypatch):
    """Import the shared preflight with a known-degraded budget store."""
    monkeypatch.setenv("ABSTUDIO_FALLBACK_LLM_MODEL", "kimi-k2.7-code")
    import app.api.deps as d
    importlib.reload(d)
    return d


def _audit_spy(deps, monkeypatch):
    """Capture audit_event calls made by the preflight."""
    calls = []
    monkeypatch.setattr(deps, "audit_event", lambda **kw: calls.append(kw))
    return calls


def _verdict(deps, monkeypatch, verdict):
    """Force check_budget_allowed's return value for the preflight under test."""
    monkeypatch.setattr(deps, "check_budget_allowed", lambda uid: dict(verdict))


class TestEnforceBudgetOrDowngrade:
    """The five endpoint copies of this policy collapsed into one function, so
    these tests are the only place the fail-closed contract is pinned."""

    @pytest.mark.asyncio
    async def test_allow_does_not_downgrade(self, deps, monkeypatch):
        _verdict(deps, monkeypatch, {"allowed": True, "reason": "ok"})
        called = []
        decision = await deps.enforce_budget_or_downgrade(
            user_id=_USER, endpoint="ep", skip_check=False,
            downgrade=lambda m: called.append(m) or True,
        )
        assert called == []            # never touch a run that is within budget
        assert decision.notice == ""   # nothing to tell the user
        assert decision.downgraded is False

    @pytest.mark.asyncio
    async def test_skip_check_bypasses_the_store_entirely(self, deps, monkeypatch):
        """All-local runs incur no spend, so blocking them would be a pure
        false positive — and must not even hit the (possibly down) store."""
        def _boom(uid):
            raise AssertionError("check_budget_allowed must not be called")
        monkeypatch.setattr(deps, "check_budget_allowed", _boom)
        decision = await deps.enforce_budget_or_downgrade(
            user_id=_USER, endpoint="ep", skip_check=True,
            downgrade=lambda m: True,
        )
        assert decision.downgraded is False

    @pytest.mark.asyncio
    async def test_degraded_downgrade_success_allows_the_run(self, deps, monkeypatch):
        """Store down + downgrade holds ⇒ run proceeds locally, user is told."""
        _verdict(deps, monkeypatch, {
            "allowed": False, "degraded": True, "reason": "store down",
            "fallback_model": "kimi-k2.7-code", "code": "BUDGET_STORE_UNAVAILABLE",
        })
        audits = _audit_spy(deps, monkeypatch)
        decision = await deps.enforce_budget_or_downgrade(
            user_id=_USER, endpoint="ep", skip_check=False,
            downgrade=lambda m: True,
        )
        assert decision.fallback_model == "kimi-k2.7-code"
        assert decision.notice == "store down"   # caller MUST surface this
        assert decision.config is None           # True ⇒ mutated in place
        assert [a["action"] for a in audits] == ["budget_degraded_downgrade"]

    @pytest.mark.asyncio
    async def test_async_downgrade_config_is_returned(self, deps, monkeypatch):
        """The agent paths' downgrade is async and returns a config dict."""
        _verdict(deps, monkeypatch, {
            "allowed": False, "degraded": True, "reason": "store down",
            "fallback_model": "kimi-k2.7-code",
        })
        _audit_spy(deps, monkeypatch)

        async def _downgrade(model):
            return {"model": model}

        decision = await deps.enforce_budget_or_downgrade(
            user_id=_USER, endpoint="ep", skip_check=False, downgrade=_downgrade,
        )
        assert decision.config == {"model": "kimi-k2.7-code"}

    @pytest.mark.asyncio
    async def test_degraded_downgrade_failure_denies(self, deps, monkeypatch):
        """THE core fail-closed case: if the downgrade cannot be applied we must
        deny, never fall through onto the paid model while the store is blind."""
        _verdict(deps, monkeypatch, {
            "allowed": False, "degraded": True, "reason": "store down",
            "fallback_model": "kimi-k2.7-code", "code": "BUDGET_STORE_UNAVAILABLE",
        })
        audits = _audit_spy(deps, monkeypatch)
        with pytest.raises(deps.HTTPException) as exc:
            await deps.enforce_budget_or_downgrade(
                user_id=_USER, endpoint="ep", skip_check=False,
                downgrade=lambda m: None,        # cannot downgrade
            )
        assert exc.value.status_code == 429
        assert exc.value.detail["code"] == "BUDGET_STORE_UNAVAILABLE"
        assert exc.value.detail["retryable"] is True
        assert [a["action"] for a in audits] == ["budget_denied"]

    @pytest.mark.asyncio
    async def test_genuine_denial_never_downgrades(self, deps, monkeypatch):
        """A user out of funds must not be handed a free local re-run — that
        would let them bypass the limit they just hit."""
        _verdict(deps, monkeypatch, {
            "allowed": False, "reason": "Total spend limit reached",
        })
        audits = _audit_spy(deps, monkeypatch)
        called = []
        with pytest.raises(deps.HTTPException) as exc:
            await deps.enforce_budget_or_downgrade(
                user_id=_USER, endpoint="ep", skip_check=False,
                downgrade=lambda m: called.append(m) or True,
            )
        assert called == []                      # downgrade never attempted
        assert exc.value.detail["code"] == "BUDGET_EXCEEDED"
        assert "retryable" not in exc.value.detail
        assert [a["action"] for a in audits] == ["budget_denied"]

    @pytest.mark.asyncio
    async def test_audit_kwargs_are_forwarded(self, deps, monkeypatch):
        """Endpoint-specific identifiers must reach the audit trail, otherwise
        a denial cannot be attributed to a workflow/thread."""
        _verdict(deps, monkeypatch, {"allowed": False, "reason": "nope"})
        audits = _audit_spy(deps, monkeypatch)
        with pytest.raises(deps.HTTPException):
            await deps.enforce_budget_or_downgrade(
                user_id=_USER, endpoint="abstudio.workflow.run", skip_check=False,
                downgrade=lambda m: True,
                audit_kwargs=dict(workflow_id="wf-1", thread_id="th-1"),
            )
        assert audits[0]["workflow_id"] == "wf-1"
        assert audits[0]["thread_id"] == "th-1"
        assert audits[0]["endpoint"] == "abstudio.workflow.run"
