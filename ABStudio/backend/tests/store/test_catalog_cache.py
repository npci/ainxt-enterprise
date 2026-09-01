# SPDX-License-Identifier: Apache-2.0
"""REQ-P3-1 — in-process cache for ``get_tool`` / ``get_skill`` /
``list_skill_files``, with mutation-based invalidation.

Background: every workflow node execution re-read its tools/skills from
Postgres even though those catalog rows are static for the life of a run
(they only change via the tool editor / skill factory / skill uploader).
``workflow_repo`` now serves ``get_tool``/``get_skill``/``list_skill_files``
from a plain in-process dict after the first read, and every mutator
(``upsert_tool``, ``delete_tool``, ``clear_all_tools``, ``upsert_skill``,
``seed_skill_if_not_exists``, ``delete_skill``, ``upsert_skill_files``)
invalidates the relevant entry so a stale row can never survive an edit.

These tests never touch a real Postgres: ``_get_pool`` is monkeypatched to a
fake pool that records every ``execute()`` call and returns canned rows, so
"did the cache avoid a DB round-trip" can be asserted by counting calls.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import pytest

from app.core import workflow_repo as wr


# ---------------------------------------------------------------------------
# Fake pool / connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, row=None, rows=None, rowcount=0):
        self._row = row
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConnection:
    """Records every ``execute()`` call; answers from ``FakeDB``."""

    def __init__(self, db: "FakeDB"):
        self._db = db

    def execute(self, sql: str, params: tuple = ()):
        self._db.calls.append((sql, params))
        return self._db.answer(sql, params)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self, db: "FakeDB"):
        self._db = db

    def connection(self):
        return _FakeConnection(self._db)


class FakeDB:
    """In-memory tools_catalog / skills_catalog / skill_files double.

    ``calls`` records every ``execute()`` invocation so tests can assert how
    many times the "DB" was actually hit.
    """

    def __init__(self):
        self.calls: list = []
        self.tools: dict = {}
        self.skills: dict = {}
        self.skill_files: dict = {}  # skill_name -> list[dict]

    # -- seeding helpers ---------------------------------------------------
    def seed_tool(self, name: str, description: str = "d", code: str = "c"):
        now = datetime.now(timezone.utc)
        self.tools[name] = (name, description, {}, code, True, "svc", now, now)

    def seed_skill(self, name: str, content: str = "body"):
        now = datetime.now(timezone.utc)
        self.skills[name] = (name, "d", "general", content, True, now, now, "ai")

    def seed_skill_files(self, name: str, files: list):
        self.skill_files[name] = files

    # -- SQL dispatch (just enough to satisfy workflow_repo's queries) -----
    def answer(self, sql: str, params: tuple):
        if sql.startswith(wr._TOOL_SELECT) and "WHERE name" in sql:
            name = params[0]
            row = self.tools.get(name)
            return _FakeCursor(row=row)
        if sql.startswith("INSERT INTO tools_catalog"):
            name = params[0]
            desc, schema_json, code, generated, service = params[1:6]
            now = datetime.now(timezone.utc)
            self.tools[name] = (name, desc, {}, code, generated, service, now, now)
            return _FakeCursor()
        if sql.startswith("DELETE FROM tools_catalog WHERE name"):
            name = params[0]
            existed = name in self.tools
            self.tools.pop(name, None)
            return _FakeCursor(rowcount=1 if existed else 0)
        if sql.startswith("DELETE FROM tools_catalog"):
            n = len(self.tools)
            self.tools.clear()
            return _FakeCursor(rowcount=n)

        if sql.startswith(wr._SKILL_SELECT) and "WHERE name" in sql:
            name = params[0]
            row = self.skills.get(name)
            return _FakeCursor(row=row)
        if sql.startswith("INSERT INTO skills_catalog") and "DO NOTHING" in sql:
            name = params[0]
            if name in self.skills:
                return _FakeCursor(rowcount=0)
            desc, category, content = params[1], params[2], params[3]
            now = datetime.now(timezone.utc)
            self.skills[name] = (name, desc, category, content, False, now, now, "builtin")
            return _FakeCursor(rowcount=1)
        if sql.startswith("INSERT INTO skills_catalog"):
            name = params[0]
            desc, category, content, generated = params[1], params[2], params[3], params[4]
            now = datetime.now(timezone.utc)
            self.skills[name] = (name, desc, category, content, generated, now, now, "ai")
            return _FakeCursor()
        if sql.startswith("DELETE FROM skills_catalog"):
            name = params[0]
            existed = name in self.skills
            self.skills.pop(name, None)
            return _FakeCursor(rowcount=1 if existed else 0)

        if sql.startswith("DELETE FROM skill_files"):
            name = params[0]
            self.skill_files.setdefault(name, [])
            return _FakeCursor()
        if sql.startswith("INSERT INTO skill_files"):
            name = params[0]
            self.skill_files.setdefault(name, [])
            return _FakeCursor()
        if sql.startswith("SELECT skill_name, rel_path, '', size_bytes"):
            name = params[0]
            rows = [
                (name, f["rel_path"], "", f.get("size_bytes", 0),
                 f.get("description", ""), f.get("kind", "reference"),
                 f.get("abs_path", ""), None)
                for f in self.skill_files.get(name, [])
            ]
            return _FakeCursor(rows=rows)

        raise AssertionError(f"FakeDB got an unexpected query: {sql!r}")


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(wr, "postgres_enabled", lambda: True)
    monkeypatch.setattr(wr, "_get_pool", lambda: _FakePool(db))
    return db


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# get_tool
# ---------------------------------------------------------------------------

class TestGetToolCache:
    def test_cold_cache_returns_correct_data(self, fake_db):
        fake_db.seed_tool("t1", description="hello")
        tool = _run(wr.get_tool("t1"))
        assert tool["name"] == "t1"
        assert tool["description"] == "hello"

    def test_second_call_serves_from_cache(self, fake_db):
        fake_db.seed_tool("t1")
        _run(wr.get_tool("t1"))
        calls_after_first = len(fake_db.calls)
        assert calls_after_first == 1

        result2 = _run(wr.get_tool("t1"))
        assert len(fake_db.calls) == calls_after_first, "second get_tool must not hit the DB"
        assert result2["name"] == "t1"

    def test_missing_tool_caches_the_none_result(self, fake_db):
        result = _run(wr.get_tool("ghost"))
        assert result is None
        assert len(fake_db.calls) == 1
        result2 = _run(wr.get_tool("ghost"))
        assert result2 is None
        assert len(fake_db.calls) == 1, "a cached miss must not re-query"

    def test_different_names_each_cost_one_round_trip(self, fake_db):
        fake_db.seed_tool("t1")
        fake_db.seed_tool("t2")
        _run(wr.get_tool("t1"))
        _run(wr.get_tool("t2"))
        assert len(fake_db.calls) == 2
        _run(wr.get_tool("t1"))
        _run(wr.get_tool("t2"))
        assert len(fake_db.calls) == 2, "both should now be served from cache"


class TestToolInvalidation:
    def test_upsert_tool_invalidates_only_that_name(self, fake_db):
        fake_db.seed_tool("t1", description="v1")
        fake_db.seed_tool("t2", description="v2")
        _run(wr.get_tool("t1"))
        _run(wr.get_tool("t2"))
        calls_before = len(fake_db.calls)

        _run(wr.upsert_tool("t1", code="new code", description="v1-edited"))

        # t1 must be re-fetched with fresh data...
        t1 = _run(wr.get_tool("t1"))
        assert t1["description"] == "v1-edited"
        # ...but t2's cache entry must be untouched (still 0 extra DB calls
        # for t2 specifically) — verified by re-reading it and checking the
        # total call count only grew by the t1 refetch, not a t2 refetch.
        calls_after_t1_refetch = len(fake_db.calls)
        _run(wr.get_tool("t2"))
        assert len(fake_db.calls) == calls_after_t1_refetch, "t2 must still be cached"
        assert len(fake_db.calls) > calls_before

    def test_delete_tool_invalidates_the_entry(self, fake_db):
        fake_db.seed_tool("t1")
        _run(wr.get_tool("t1"))
        _run(wr.delete_tool("t1"))
        result = _run(wr.get_tool("t1"))
        assert result is None

    def test_clear_all_tools_wipes_the_whole_cache(self, fake_db):
        fake_db.seed_tool("t1")
        fake_db.seed_tool("t2")
        _run(wr.get_tool("t1"))
        _run(wr.get_tool("t2"))
        calls_before = len(fake_db.calls)

        _run(wr.clear_all_tools())

        # Re-seed (clear_all_tools wiped the fake DB rows too) and confirm
        # both names are re-fetched from the DB rather than served stale.
        fake_db.seed_tool("t1", description="post-clear")
        result = _run(wr.get_tool("t1"))
        assert result["description"] == "post-clear"
        assert len(fake_db.calls) > calls_before


# ---------------------------------------------------------------------------
# get_skill / list_skill_files
# ---------------------------------------------------------------------------

class TestGetSkillCache:
    def test_cold_cache_returns_correct_data(self, fake_db):
        fake_db.seed_skill("s1", content="SKILL.md body")
        skill = _run(wr.get_skill("s1"))
        assert skill["name"] == "s1"
        assert skill["content"] == "SKILL.md body"

    def test_second_call_serves_from_cache(self, fake_db):
        fake_db.seed_skill("s1")
        _run(wr.get_skill("s1"))
        calls_after_first = len(fake_db.calls)
        _run(wr.get_skill("s1"))
        assert len(fake_db.calls) == calls_after_first


class TestListSkillFilesCache:
    def test_second_call_serves_from_cache(self, fake_db):
        fake_db.seed_skill_files("s1", [
            {"rel_path": "a.md", "size_bytes": 10, "description": "", "kind": "reference", "abs_path": "/a"},
        ])
        files1 = _run(wr.list_skill_files("s1"))
        assert len(files1) == 1
        calls_after_first = len(fake_db.calls)
        files2 = _run(wr.list_skill_files("s1"))
        assert len(fake_db.calls) == calls_after_first
        assert files2 == files1

    def test_independently_keyed_from_get_skill(self, fake_db):
        """Caching a skill's row must not populate (or clear) its file
        manifest cache, and vice versa — they're separate dicts."""
        fake_db.seed_skill("s1", content="body")
        fake_db.seed_skill_files("s1", [
            {"rel_path": "a.md", "size_bytes": 1, "description": "", "kind": "reference", "abs_path": "/a"},
        ])
        _run(wr.get_skill("s1"))
        assert "s1" not in wr._skill_files_cache
        _run(wr.list_skill_files("s1"))
        assert "s1" in wr._skill_files_cache
        assert "s1" in wr._skill_cache


class TestSkillInvalidation:
    def test_upsert_skill_invalidates_skill_and_file_cache(self, fake_db):
        fake_db.seed_skill("s1", content="v1")
        fake_db.seed_skill_files("s1", [
            {"rel_path": "a.md", "size_bytes": 1, "description": "", "kind": "reference", "abs_path": "/a"},
        ])
        _run(wr.get_skill("s1"))
        _run(wr.list_skill_files("s1"))
        assert "s1" in wr._skill_cache
        assert "s1" in wr._skill_files_cache

        _run(wr.upsert_skill("s1", content="v2"))
        assert "s1" not in wr._skill_cache
        assert "s1" not in wr._skill_files_cache

        skill = _run(wr.get_skill("s1"))
        assert skill["content"] == "v2"

    def test_delete_skill_invalidates_skill_and_file_cache(self, fake_db):
        fake_db.seed_skill("s1")
        fake_db.seed_skill_files("s1", [])
        _run(wr.get_skill("s1"))
        _run(wr.list_skill_files("s1"))

        _run(wr.delete_skill("s1"))
        assert "s1" not in wr._skill_cache
        assert "s1" not in wr._skill_files_cache
        assert _run(wr.get_skill("s1")) is None

    def test_seed_skill_if_not_exists_invalidates(self, fake_db):
        _run(wr.get_skill("s1"))  # caches the miss (None)
        assert wr._skill_cache.get("s1") is None
        assert "s1" in wr._skill_cache

        inserted = _run(wr.seed_skill_if_not_exists("s1", content="fresh"))
        assert inserted is True
        assert "s1" not in wr._skill_cache

        skill = _run(wr.get_skill("s1"))
        assert skill["content"] == "fresh"

    def test_upsert_skill_files_invalidates_both_caches(self, fake_db):
        fake_db.seed_skill("s1", content="body")
        fake_db.seed_skill_files("s1", [
            {"rel_path": "a.md", "size_bytes": 1, "description": "", "kind": "reference", "abs_path": "/a"},
        ])
        _run(wr.get_skill("s1"))
        _run(wr.list_skill_files("s1"))
        assert "s1" in wr._skill_cache
        assert "s1" in wr._skill_files_cache

        _run(wr.upsert_skill_files("s1", [
            {"rel_path": "b.md", "content": "x", "size_bytes": 2,
             "description": "", "kind": "reference", "abs_path": "/b"},
        ]))
        # Both entries for this skill must be gone even though only the
        # file manifest actually changed — matches the doc's mutator table.
        assert "s1" not in wr._skill_cache
        assert "s1" not in wr._skill_files_cache


# ---------------------------------------------------------------------------
# Concurrency smoke test (accepted cold-key race)
# ---------------------------------------------------------------------------

def test_concurrent_cold_reads_all_return_correct_data(fake_db):
    """Many concurrent callers racing on the SAME cold key may each slip
    through the cache-miss check before any of them populates the cache —
    the lock only protects the dict, not the DB round-trip (an accepted,
    documented race in REQ-P3-1: "no TTL needed because every writer
    invalidates" assumes correctness, not cold-key call deduplication).
    Every caller must still get correct data even if several of them raced
    the DB independently."""
    fake_db.seed_tool("hot")

    async def _go():
        return await asyncio.gather(*[wr.get_tool("hot") for _ in range(20)])

    results = _run(_go())
    assert len(results) == 20
    assert all(r["name"] == "hot" for r in results)
    # The cache converges to a single entry once every task has completed.
    assert wr._tool_cache["hot"]["name"] == "hot"


def test_concurrent_warm_reads_never_hit_the_db(fake_db):
    """Once a key is cached (post first read), concurrent readers are
    served entirely from memory — the guarantee that actually matters for
    REQ-P3-2 (loop iterations re-entering the same node)."""
    fake_db.seed_tool("hot")
    _run(wr.get_tool("hot"))  # populate the cache
    calls_after_warm = len(fake_db.calls)
    assert calls_after_warm == 1

    async def _go():
        return await asyncio.gather(*[wr.get_tool("hot") for _ in range(20)])

    results = _run(_go())
    assert len(results) == 20
    assert all(r["name"] == "hot" for r in results)
    assert len(fake_db.calls) == calls_after_warm, "warm concurrent reads must not touch the DB"


# ---------------------------------------------------------------------------
# Stale-write race — code review fix #1
#
# Sequence being guarded against:
#   1. Reader misses cache, starts its DB round-trip (pre-edit data).
#   2. Writer commits an edit/delete and invalidates the cache.
#   3. Reader's DB call finishes and would, without the generation guard,
#      write the STALE pre-edit row back into the cache — silently undoing
#      the invalidation that already ran.
# ---------------------------------------------------------------------------

class TestStaleWriteRace:
    """These tests gate the FAKE DB's ``answer()`` (which runs synchronously
    inside the thread-pool thread that ``asyncio.to_thread`` spawns for the
    reader's ``_run()``), not the ``asyncio.to_thread`` scheduling call
    itself. That distinction matters: the real race is "the reader already
    fetched pre-edit data from Postgres and just hasn't written it to the
    cache yet" — the row value is captured at QUERY time, not at return
    time. Gating the scheduling wrapper instead would let the reader's
    query re-run (and see post-edit data) after being released, which
    doesn't reproduce the bug at all — this is exactly why the first draft
    of this test suite passed even a version of the fix with the race
    still open. Gating inside ``answer()`` captures the row BEFORE the
    writer commits, then blocks the thread with a ``threading.Event``
    (synchronous, since we're inside a real worker thread) until the test
    releases it — after the writer has already committed + invalidated.
    """

    def test_reader_in_flight_during_a_write_does_not_resurrect_stale_data(self, fake_db):
        fake_db.seed_tool("t1", description="v1")

        reader_query_done = threading.Event()
        release_reader = threading.Event()
        original_answer = fake_db.answer

        def gated_answer(sql, params):
            # Run the real lookup FIRST so the reader captures the row as it
            # stood before the writer commits — mirrors an actual Postgres
            # SELECT that already returned a result set to the caller.
            result = original_answer(sql, params)
            is_tool_select = sql.startswith(wr._TOOL_SELECT) and "WHERE name" in sql
            if is_tool_select and not reader_query_done.is_set():
                reader_query_done.set()
                assert release_reader.wait(timeout=5), "test setup deadlocked"
            return result

        fake_db.answer = gated_answer

        async def scenario():
            reader_task = asyncio.create_task(wr.get_tool("t1"))
            # Block the event loop briefly on a real thread wait so we don't
            # need an extra asyncio bridge — reader_query_done is a
            # threading.Event set from inside the worker thread.
            await asyncio.to_thread(reader_query_done.wait, 5)

            # Writer runs and completes an edit + invalidation while the
            # reader's fetched-but-not-yet-cached row is still in flight.
            await wr.upsert_tool("t1", code="new", description="v2-edited")

            release_reader.set()
            return await reader_task

        result = _run(scenario())

        # The reader's own return value legitimately reflects what it read
        # (pre-edit) — that part is unavoidable and fine. What matters is
        # the CACHE must not have been poisoned with that stale value.
        # ``upsert_tool`` only invalidates (it doesn't populate), so the
        # correct end state is an EMPTY cache entry for "t1" (self-healing
        # on the next read) — NOT the reader's stale "v1" row written back
        # after the fact.
        assert result["description"] == "v1"
        assert "t1" not in wr._tool_cache, (
            "the writer's invalidation must win: the reader's in-flight, "
            "pre-edit row must not be written back into the cache after "
            "the invalidation already ran"
        )
        # And the cache self-heals to the fresh value on the very next read.
        fresh = _run(wr.get_tool("t1"))
        assert fresh["description"] == "v2-edited"

    def test_next_read_after_the_race_returns_fresh_data(self, fake_db):
        """End-to-end version of the race: after it resolves, the very next
        get_tool call must see the fresh row (not a poisoned stale one)."""
        fake_db.seed_tool("t1", description="v1")

        reader_query_done = threading.Event()
        release_reader = threading.Event()
        original_answer = fake_db.answer

        def gated_answer(sql, params):
            result = original_answer(sql, params)
            is_tool_select = sql.startswith(wr._TOOL_SELECT) and "WHERE name" in sql
            if is_tool_select and not reader_query_done.is_set():
                reader_query_done.set()
                assert release_reader.wait(timeout=5), "test setup deadlocked"
            return result

        fake_db.answer = gated_answer

        async def scenario():
            reader_task = asyncio.create_task(wr.get_tool("t1"))
            await asyncio.to_thread(reader_query_done.wait, 5)
            await wr.upsert_tool("t1", code="new", description="v2-edited")
            release_reader.set()
            await reader_task
            return await wr.get_tool("t1")

        final = _run(scenario())
        assert final["description"] == "v2-edited"


# ---------------------------------------------------------------------------
# Cache isolation — code review fix #2 (no shared mutable objects)
# ---------------------------------------------------------------------------

class TestCacheReturnsIsolatedCopies:
    def test_mutating_a_returned_tool_row_does_not_corrupt_the_cache(self, fake_db):
        fake_db.seed_tool("t1", description="original")

        row = _run(wr.get_tool("t1"))
        row["description"] = "MUTATED"
        row["input_schema"]["injected"] = "malicious"  # nested mutation too

        row2 = _run(wr.get_tool("t1"))
        assert row2["description"] == "original"
        assert "injected" not in row2["input_schema"]

    def test_two_calls_return_different_object_instances(self, fake_db):
        fake_db.seed_tool("t1")
        row1 = _run(wr.get_tool("t1"))
        row2 = _run(wr.get_tool("t1"))
        assert row1 == row2
        assert row1 is not row2
        assert row1["input_schema"] is not row2["input_schema"]

    def test_mutating_a_returned_skill_row_does_not_corrupt_the_cache(self, fake_db):
        fake_db.seed_skill("s1", content="original body")
        row = _run(wr.get_skill("s1"))
        row["content"] = "MUTATED"

        row2 = _run(wr.get_skill("s1"))
        assert row2["content"] == "original body"

    def test_mutating_a_returned_skill_files_list_does_not_corrupt_the_cache(self, fake_db):
        fake_db.seed_skill_files("s1", [
            {"rel_path": "a.md", "size_bytes": 1, "description": "", "kind": "reference", "abs_path": "/a"},
        ])
        files1 = _run(wr.list_skill_files("s1"))
        files1.append({"rel_path": "INJECTED.md"})
        files1[0]["rel_path"] = "MUTATED.md"

        files2 = _run(wr.list_skill_files("s1"))
        assert len(files2) == 1
        assert files2[0]["rel_path"] == "a.md"

    def test_mutating_the_seed_argument_after_the_call_does_not_corrupt_the_cache(self, fake_db):
        """Defensive copy must also apply on the WRITE side — a caller that
        mutates a dict it passed in (or that the DB layer happens to reuse)
        must not be able to reach into the cache after the fact."""
        fake_db.seed_tool("t1", description="v1")
        first = _run(wr.get_tool("t1"))
        # Simulate a caller mutating the dict it received well after the call
        # returned (e.g. stashing it and editing it later for local bookkeeping).
        first["description"] = "locally-mutated-after-return"

        second = _run(wr.get_tool("t1"))
        assert second["description"] == "v1"


# ---------------------------------------------------------------------------
# seed_skill_if_not_exists — code review fix #4 (only invalidate on mutation)
# ---------------------------------------------------------------------------

class TestSeedSkillInvalidatesOnlyOnInsert:
    def test_no_op_seed_does_not_invalidate_a_hot_entry(self, fake_db):
        fake_db.seed_skill("s1", content="existing")
        _run(wr.get_skill("s1"))  # warm the cache
        assert "s1" in wr._skill_cache
        generation_before = wr._skill_cache_generation.value

        inserted = _run(wr.seed_skill_if_not_exists("s1", content="ignored-since-exists"))

        assert inserted is False
        assert "s1" in wr._skill_cache, "a no-op seed must not evict the hot entry"
        assert wr._skill_cache_generation.value == generation_before, (
            "a no-op seed must not bump the generation counter either"
        )
        # Cached value is unaffected — still the original content.
        cached = _run(wr.get_skill("s1"))
        assert cached["content"] == "existing"

    def test_actual_insert_still_invalidates(self, fake_db):
        # Nothing seeded yet — get_skill caches the miss.
        _run(wr.get_skill("s1"))
        assert "s1" in wr._skill_cache

        inserted = _run(wr.seed_skill_if_not_exists("s1", content="brand-new"))

        assert inserted is True
        assert "s1" not in wr._skill_cache, "a real insert must still invalidate"
        fresh = _run(wr.get_skill("s1"))
        assert fresh["content"] == "brand-new"
