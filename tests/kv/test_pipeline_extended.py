# SPDX-License-Identifier: Apache-2.0
# ============================================================
# KVPipeline extended-surface conformance.
#
# Covers Gap #11 — sadd / zadd / setex in pipelines on both backends.
# The existing test_conformance.py::test_pipeline_batch only exercises
# set / incr_by; this file exercises everything else the ABC promises.
# ============================================================

from __future__ import annotations

import pytest

from core.kv import KVClient


def test_pipeline_setex_writes_ttl_keys(kv: KVClient, key_prefix):
    """Batched setex must land all keys with TTL > 0."""
    with kv.pipeline() as pipe:
        pipe.setex(key_prefix + "p1", 60, "a")
        pipe.setex(key_prefix + "p2", 120, "b")
        pipe.setex(key_prefix + "p3", 180, "c")
        pipe.execute()
    for suffix, expected_max in (("p1", 60), ("p2", 120), ("p3", 180)):
        k = key_prefix + suffix
        assert kv.get(k) is not None
        ttl = kv.ttl(k)
        assert 0 < ttl <= expected_max, (
            f"{k}: ttl={ttl} out of (0, {expected_max}]"
        )


def test_pipeline_sadd_creates_set(kv: KVClient, key_prefix):
    """Batched sadd writes every member."""
    k = key_prefix + "tags"
    with kv.pipeline() as pipe:
        pipe.sadd(k, "python", "grpc")
        pipe.sadd(k, "redis", "kv")
        pipe.execute()
    assert kv.smembers(k) == {"python", "grpc", "redis", "kv"}
    assert kv.scard(k) == 4


def test_pipeline_zadd_creates_sorted_set(kv: KVClient, key_prefix):
    """Batched zadd writes every (member, score) pair."""
    k = key_prefix + "scores"
    with kv.pipeline() as pipe:
        pipe.zadd(k, {"alice": 100.0, "bob": 200.0})
        pipe.zadd(k, {"carol": 50.0})
        pipe.execute()
    assert kv.zcard(k) == 3
    # zrange returns ascending by score → carol, alice, bob.
    assert kv.zrange(k, 0, -1) == ["carol", "alice", "bob"]


def test_pipeline_mixed_operations(kv: KVClient, key_prefix):
    """A single pipeline can combine setex + sadd + zadd + incr_by."""
    str_key = key_prefix + "mix:str"
    set_key = key_prefix + "mix:set"
    zset_key = key_prefix + "mix:zset"
    counter = key_prefix + "mix:counter"

    with kv.pipeline() as pipe:
        pipe.setex(str_key, 60, "hello")
        pipe.sadd(set_key, "x", "y")
        pipe.zadd(zset_key, {"m1": 1.0})
        pipe.incr_by(counter, 7)
        pipe.execute()

    assert kv.get(str_key) == "hello"
    assert kv.smembers(set_key) == {"x", "y"}
    assert kv.zrange(zset_key, 0, -1) == ["m1"]
    assert kv.get(counter) == "7"
