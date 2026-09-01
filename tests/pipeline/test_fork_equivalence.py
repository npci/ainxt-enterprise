# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Phase 3 Step B — fork driving equivalence & behavior change
# ============================================================
#
# Proves the property the gateway relies on: the driving decision (_decide_fork,
# used at gateway.py fork line ~4079) preserves today's behavior for the
# common/off cases and only PROMOTES genuinely-agentic no-repo turns.
#
# The gateway condition is:
#   fast_path if (not repo and not project and not force_orch)
# where force_orch = (flag on and conv_state and no-repo and
#                     decide_fork(...) == "orchestrator")
# We test decide_fork + that logic here (pure).
# ============================================================

from cil.state import ConversationState, Score
from pipeline.dispatch import FORK_GENERAL, FORK_ORCHESTRATOR, decide_fork


def _state(**kw):
    st = ConversationState()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _gateway_fork(*, repo, project, flag_on, conv_state):
    """Mirror the gateway.py fork logic (line ~4079) for equivalence testing."""
    force_orch = False
    if flag_on and conv_state is not None and not repo and not project:
        if decide_fork(conv_state, repo_filter=repo, project_id=project) == "orchestrator":
            force_orch = True
    # gateway: fast path iff (not repo and not project and not force_orch)
    return "general" if (not repo and not project and not force_orch) else "orchestrator"


# ── flag OFF → byte-identical to today (repo/project → orch; else general) ──

def test_flag_off_trivial_is_general_like_today():
    assert _gateway_fork(repo=None, project=None, flag_on=False, conv_state=_state()) == "general"


def test_flag_off_repo_is_orchestrator_like_today():
    assert _gateway_fork(repo="r", project=None, flag_on=False, conv_state=_state()) == "orchestrator"


def test_flag_off_agentic_still_general_today():
    # even a high-tool-need turn stays fast-path when the flag is OFF (today's behavior)
    st = _state(tool_need=Score(score=0.9))
    assert _gateway_fork(repo=None, project=None, flag_on=False, conv_state=st) == "general"


def test_flag_off_no_conv_state_identical():
    assert _gateway_fork(repo=None, project=None, flag_on=False, conv_state=None) == "general"
    assert _gateway_fork(repo="r", project=None, flag_on=False, conv_state=None) == "orchestrator"


# ── flag ON → real behavior change, but only promotes agentic no-repo turns ─

def test_flag_on_trivial_still_general():
    # no behavior change for ordinary turns
    assert _gateway_fork(repo=None, project=None, flag_on=True, conv_state=_state()) == "general"


def test_flag_on_agentic_promoted_to_orchestrator():
    # THE behavior change: a high-tool-need no-repo turn now goes agentic
    st = _state(tool_need=Score(score=0.9))
    assert _gateway_fork(repo=None, project=None, flag_on=True, conv_state=st) == "orchestrator"


def test_flag_on_deep_promoted():
    assert _gateway_fork(repo=None, project=None, flag_on=True,
                         conv_state=_state(task_complexity="deep")) == "orchestrator"


def test_flag_on_never_diverts_repo():
    # repo always orchestrator regardless of flag/shape (safety invariant preserved)
    st = _state(task_complexity="simple")
    assert _gateway_fork(repo="r", project=None, flag_on=True, conv_state=st) == "orchestrator"


def test_flag_on_retrieval_only_stays_general():
    # RAG-only turns stay fast-path (retrieval runs inside _general_stream)
    st = _state(retrieval_need=Score(score=0.8))
    assert _gateway_fork(repo=None, project=None, flag_on=True, conv_state=st) == "general"
