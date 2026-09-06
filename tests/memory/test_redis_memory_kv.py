# SPDX-License-Identifier: MIT
# ============================================================
# memory/redis_memory.py KV-contract tests (Phase 4).
#
# Risk closed: conversation ordering, user-scoped key isolation,
# 7-day TTL on conversations, agent run + workflow run round-trip,
# and delete semantics must match on both backends.
#
# Each test uses a unique session_id / run_id so parallel tests
# don't collide. RedisMemory is constructed against DB=9.
# ============================================================

from __future__ import annotations

import uuid

import pytest

from memory.redis_memory import RedisMemory


@pytest.fixture
def mem(kv):
    """Build a RedisMemory bound to DB9 via the parametrized kv fixture.

    The constructor calls get_kv(db) internally; we pass db=9 so the
    sandboxed test DB is used. The `kv` parameter is here just to
    inherit its parametrization (so this fixture runs once per backend).
    """
    m = RedisMemory(db=9)
    yield m
    # No persistent cleanup — keys are unique per test via uuid.


def _sid():
    return f"sess-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Conversation messages
# ---------------------------------------------------------------------------

def test_save_and_get_conversation_preserves_order(mem):
    session_id = _sid()
    for i, role in enumerate(["user", "assistant", "user", "assistant", "user"]):
        mem.save_message(session_id, role, f"msg-{i}")
    msgs = mem.get_conversation(session_id, limit=10)
    assert len(msgs) == 5
    assert [m["content"] for m in msgs] == [f"msg-{i}" for i in range(5)]


def test_get_conversation_respects_limit(mem):
    session_id = _sid()
    for i in range(5):
        mem.save_message(session_id, "user", f"msg-{i}")
    # limit=2 → last 2 only.
    msgs = mem.get_conversation(session_id, limit=2)
    assert len(msgs) == 2
    assert msgs[0]["content"] == "msg-3"
    assert msgs[1]["content"] == "msg-4"


def test_user_scoped_keys_are_isolated(mem):
    session_id = _sid()
    mem.save_message(session_id, "user", "hello-a", user_id="user_a")
    # Different user, same session_id → empty.
    other = mem.get_conversation(session_id, user_id="user_b")
    assert other == []
    # Anonymous bucket also empty.
    anon = mem.get_conversation(session_id)
    assert anon == []
    # Original bucket still has the message.
    own = mem.get_conversation(session_id, user_id="user_a")
    assert len(own) == 1 and own[0]["content"] == "hello-a"


def test_conversation_ttl_within_7_days(mem):
    session_id = _sid()
    user_id = f"u-{uuid.uuid4().hex[:6]}"
    mem.save_message(session_id, "user", "x", user_id=user_id)
    key = mem._conv_key(session_id, user_id)
    ttl = mem.client.ttl(key)
    seven_days = 7 * 24 * 3600
    assert 0 < ttl <= seven_days


def test_delete_conversation_removes_key(mem):
    session_id = _sid()
    mem.save_message(session_id, "user", "x")
    assert mem.get_conversation(session_id) != []
    mem.delete_conversation(session_id)
    assert mem.get_conversation(session_id) == []


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

def test_save_and_get_agent_run(mem):
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    mem.save_agent_run(
        run_id=run_id,
        agent_name="test_agent",
        question="2+2?",
        answer="4",
        tool_history=["calculator"],
        compliance_flags=[],
    )
    record = mem.get_agent_run(run_id)
    assert record is not None
    assert record["agent_name"] == "test_agent"
    assert record["answer"] == "4"


def test_list_agent_runs_returns_recent_ids(mem):
    agent_name = f"agent-{uuid.uuid4().hex[:6]}"
    ids = []
    for i in range(3):
        rid = f"run-{uuid.uuid4().hex[:8]}"
        ids.append(rid)
        mem.save_agent_run(
            run_id=rid, agent_name=agent_name, question="q",
            answer=f"a{i}", tool_history=[], compliance_flags=[],
        )
    runs = mem.list_agent_runs(agent_name)
    fetched_ids = [r["run_id"] for r in runs]
    assert set(ids) <= set(fetched_ids)


# ---------------------------------------------------------------------------
# Workflow runs
# ---------------------------------------------------------------------------

def test_save_and_get_workflow_run(mem):
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    mem.save_workflow_run(
        workflow_id=workflow_id,
        workflow_name="test_flow",
        steps=[{"name": "step1", "status": "ok"}],
        status="success",
    )
    rec = mem.get_workflow_run(workflow_id)
    assert rec is not None
    assert rec["status"] == "success"
    assert rec["steps"][0]["name"] == "step1"
