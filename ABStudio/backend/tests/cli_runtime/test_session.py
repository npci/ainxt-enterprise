# SPDX-License-Identifier: Apache-2.0
"""Run sessions: token authentication, scope, expiry, and the event bus.

A session token is the ONLY thing that lets an anonymous subprocess act as a
specific user, so these are security tests as much as unit tests.
"""

from __future__ import annotations

import time

import asyncio

from app.cli_runtime.session import (
    TOOL_EVENT_RESULT,
    TOOL_EVENT_START,
    SessionRegistry,
    ToolEvent,
)


class TestAuthentication:
    def test_correct_token_resolves_the_session(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1", user_id="u1")
        found, reason = registry.authenticate("r1", session.token)
        assert found is session and reason == ""

    def test_wrong_token_is_rejected(self):
        registry = SessionRegistry()
        registry.register(run_id="r1")
        found, reason = registry.authenticate("r1", "not-the-token")
        assert found is None and reason

    def test_unknown_run_is_rejected(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        found, _ = registry.authenticate("other-run", session.token)
        assert found is None

    def test_failure_reasons_are_indistinguishable(self):
        """A prober must not be able to tell a bad token from a missing run,
        or it could enumerate which runs are live."""
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        _, bad_token = registry.authenticate("r1", "wrong")
        _, no_run = registry.authenticate("nope", session.token)
        assert bad_token == no_run

    def test_empty_token_never_authenticates(self):
        registry = SessionRegistry()
        registry.register(run_id="r1")
        assert registry.authenticate("r1", "")[0] is None

    def test_tokens_are_unique_and_long(self):
        registry = SessionRegistry()
        tokens = {registry.register(run_id=f"r{i}").token for i in range(50)}
        assert len(tokens) == 50
        assert all(len(t) >= 32 for t in tokens)

    def test_revoked_session_stops_authenticating(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        registry.revoke("r1")
        assert registry.authenticate("r1", session.token)[0] is None

    def test_revoke_is_idempotent(self):
        registry = SessionRegistry()
        registry.register(run_id="r1")
        assert registry.revoke("r1") is not None
        assert registry.revoke("r1") is None

    def test_expired_session_is_rejected_and_dropped(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1", ttl_seconds=60)
        session.expires_at = time.time() - 1
        assert registry.authenticate("r1", session.token)[0] is None
        assert registry.get("r1") is None


class TestScope:
    def test_only_attached_tools_are_allowed(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1", allowed_tools=["gitlab_read_file"])
        assert session.allows_tool("gitlab_read_file") is True
        assert session.allows_tool("jira_create_issue") is False

    def test_a_session_with_no_tools_allows_nothing(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        assert session.allows_tool("anything") is False
        assert session.allows_tool("") is False

    def test_identity_is_carried_for_credential_resolution(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1", user_id="u-9", email="a@b.c")
        assert (session.user_id, session.email) == ("u-9", "a@b.c")


class TestEventBus:
    def test_events_drain_in_order(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="t"))
        session.publish(ToolEvent(kind=TOOL_EVENT_RESULT, tool_name="t"))
        drained = session.drain_events()
        assert [e.kind for e in drained] == [TOOL_EVENT_START, TOOL_EVENT_RESULT]

    def test_draining_twice_yields_nothing_the_second_time(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="t"))
        assert len(session.drain_events()) == 1
        assert session.drain_events() == []

    def test_publishing_never_raises(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.close_events()
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="t"))  # must not raise

    def test_generated_files_are_deduplicated_by_disk_name(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.record_files([{"filename": "a.pptx", "disk_name": "a_1.pptx"}])
        session.record_files([{"filename": "a.pptx", "disk_name": "a_1.pptx"}])
        session.record_files([{"filename": "b.pdf", "disk_name": "b_2.pdf"}])
        assert len(session.generated_files) == 2

    # ── ARCH-F-ABS1-005: bounded queue + drop-oldest overflow handling ──────

    def test_event_bus_is_bounded_not_unbounded(self):
        """The queue must have a real maxsize (previously unbounded)."""
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        assert session.events.maxsize > 0

    def test_overflow_drops_oldest_and_keeps_newest(self):
        """When the queue is full, publish() must drop the OLDEST queued
        event (not raise, not drop the new one) so the most recent tool
        activity is always what survives."""
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.events = asyncio.Queue(maxsize=2)  # shrink for a fast test

        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="oldest"))
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="middle"))
        assert session.events_dropped == 0

        # Queue is now full (maxsize=2) — this publish must evict "oldest".
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="newest"))

        drained = session.drain_events()
        assert [e.tool_name for e in drained] == ["middle", "newest"]
        assert session.events_dropped == 1

    def test_overflow_never_raises(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.events = asyncio.Queue(maxsize=1)
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="a"))
        # Must not raise even though the queue is already full.
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="b"))
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="c"))

    def test_close_events_overflow_still_enqueues_sentinel(self):
        """close_events() must always get its sentinel in, even if that means
        evicting a queued event — a drain loop must terminate."""
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.events = asyncio.Queue(maxsize=1)
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="a"))
        session.close_events()

        drained = session.drain_events()
        # "a" was evicted to make room for the sentinel; drain_events() stops
        # at the sentinel (None) and returns whatever preceded it (nothing).
        assert drained == []
        assert session.events_dropped == 1

    def test_events_dropped_counter_persists_across_multiple_overflows(self):
        registry = SessionRegistry()
        session = registry.register(run_id="r1")
        session.events = asyncio.Queue(maxsize=1)
        session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name="a"))
        for i in range(5):
            session.publish(ToolEvent(kind=TOOL_EVENT_START, tool_name=f"t{i}"))
        assert session.events_dropped == 5


class TestHousekeeping:
    def test_sweep_removes_only_expired_sessions(self):
        registry = SessionRegistry()
        live = registry.register(run_id="live", ttl_seconds=600)
        dead = registry.register(run_id="dead", ttl_seconds=600)
        dead.expires_at = time.time() - 1
        assert registry.sweep_expired() == 1
        assert registry.get("live") is live
        assert registry.get("dead") is None

    def test_clear_revokes_everything(self):
        registry = SessionRegistry()
        for i in range(3):
            registry.register(run_id=f"r{i}")
        assert registry.active_count() == 3
        registry.clear()
        assert registry.active_count() == 0
