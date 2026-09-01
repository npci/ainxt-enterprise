# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Wave 5 — workflow selection (pure)
# ============================================================

from cil.state import ConversationState, Score
from workflow.selector import (
    CODING_CHAT,
    DATA_ANALYSIS,
    DOCUMENT,
    QA,
    SDLC,
    select_workflow,
)


def _state(**kw):
    st = ConversationState()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_default_is_qa():
    assert select_workflow(_state()) == QA


def test_document_output_format():
    assert select_workflow(_state(output_format="document")) == DOCUMENT


def test_data_output_format():
    assert select_workflow(_state(output_format="data")) == DATA_ANALYSIS


def test_code_domain_shallow_is_coding_chat():
    assert select_workflow(_state(domain="code", task_complexity="medium")) == CODING_CHAT


def test_code_domain_deep_is_sdlc():
    assert select_workflow(_state(domain="code", task_complexity="deep")) == SDLC


def test_code_via_tool_tags():
    st = _state(task_complexity="solution", tool_need=Score(score=0.8, tags=["vcs"]))
    assert select_workflow(st) == SDLC


def test_document_outranks_code():
    # explicit document format wins even in code domain
    st = _state(output_format="document", domain="code", task_complexity="deep")
    assert select_workflow(st) == DOCUMENT


def test_summarize_intent_routes_to_summarize():
    from workflow.selector import SUMMARIZE
    assert select_workflow(_state(intent="summarize")) == SUMMARIZE
    assert select_workflow(_state(intent="please summarise this")) == SUMMARIZE


def test_document_format_outranks_summarize_intent():
    from workflow.selector import DOCUMENT
    st = _state(output_format="document", intent="summarize")
    assert select_workflow(st) == DOCUMENT


def test_str_enum_compares_as_string():
    assert select_workflow(_state()) == "qa"


def test_never_raises():
    class Empty:
        pass
    assert select_workflow(Empty()) == QA
