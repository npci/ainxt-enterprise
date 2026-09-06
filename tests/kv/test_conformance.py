# SPDX-License-Identifier: MIT
# ============================================================
# KV conformance suite
#
# Every method on KVClient is exercised against both backends.
# A green run on both columns is the contract for Phase 0.
# ============================================================

from __future__ import annotations

import time

import pytest

from core.kv import KVClient


# ---------------- Strings -----------------------------------------------------

def test_set_get_delete(kv: KVClient, key_prefix):
    k = key_prefix + "str"
    assert kv.set(k, "hello") is True
    assert kv.get(k) == "hello"
    assert kv.delete(k) == 1
    assert kv.get(k) is None


def test_set_ex_and_ttl(kv: KVClient, key_prefix):
    k = key_prefix + "ex"
    assert kv.set(k, "v", ex=30) is True
    ttl = kv.ttl(k)
    assert 0 < ttl <= 30
    assert kv.get(k) == "v"


def test_setex_alias(kv: KVClient, key_prefix):
    k = key_prefix + "setex"
    assert kv.setex(k, 60, "v") is True
    assert kv.ttl(k) > 0


def test_setnx_only_first_wins(kv: KVClient, key_prefix):
    k = key_prefix + "nx"
    assert kv.setnx(k, "first") is True
    assert kv.setnx(k, "second") is False
    assert kv.get(k) == "first"


def test_exists_and_expire(kv: KVClient, key_prefix):
    k = key_prefix + "ex"
    kv.set(k, "v")
    assert kv.exists(k) == 1
    assert kv.expire(k, 60) is True
    assert kv.ttl(k) > 0


def test_mget(kv: KVClient, key_prefix):
    kv.set(key_prefix + "a", "1")
    kv.set(key_prefix + "b", "2")
    res = kv.mget(key_prefix + "a", key_prefix + "missing", key_prefix + "b")
    assert res == ["1", None, "2"]


def test_incr_decr(kv: KVClient, key_prefix):
    k = key_prefix + "counter"
    assert kv.incr(k, 5) == 5
    assert kv.incr(k, 3) == 8
    assert kv.decr(k, 2) == 6


def test_incrbyfloat(kv: KVClient, key_prefix):
    k = key_prefix + "balance"
    assert kv.incrbyfloat(k, 1.5) == pytest.approx(1.5)
    assert kv.incrbyfloat(k, 2.25) == pytest.approx(3.75)


# ---------------- Hashes ------------------------------------------------------

def test_hset_hget_hgetall(kv: KVClient, key_prefix):
    k = key_prefix + "h"
    kv.hset(k, "name", "Alice")
    kv.hset(k, "role", "admin")
    assert kv.hget(k, "name") == "Alice"
    assert kv.hgetall(k) == {"name": "Alice", "role": "admin"}


def test_hmget_hexists_hdel_hlen(kv: KVClient, key_prefix):
    k = key_prefix + "h2"
    kv.hmset(k, {"a": "1", "b": "2", "c": "3"})
    assert kv.hmget(k, "a", "missing", "c") == ["1", None, "3"]
    assert kv.hexists(k, "b") is True
    assert kv.hlen(k) == 3
    assert kv.hdel(k, "a", "b") == 2
    assert kv.hlen(k) == 1


def test_hincrby(kv: KVClient, key_prefix):
    k = key_prefix + "stats"
    assert kv.hincrby(k, "logins", 1) == 1
    assert kv.hincrby(k, "logins", 4) == 5


# ---------------- Sets --------------------------------------------------------

def test_sadd_smembers_sismember(kv: KVClient, key_prefix):
    k = key_prefix + "tags"
    kv.sadd(k, "python", "grpc", "redis")
    assert kv.scard(k) == 3
    assert kv.sismember(k, "python") is True
    assert kv.sismember(k, "rust") is False
    assert kv.smembers(k) == {"python", "grpc", "redis"}
    kv.srem(k, "grpc")
    assert kv.scard(k) == 2


# ---------------- Sorted sets -------------------------------------------------

def test_zadd_zrange_zscore(kv: KVClient, key_prefix):
    k = key_prefix + "z"
    kv.zadd(k, {"a": 1.0, "b": 2.0, "c": 3.0})
    assert kv.zcard(k) == 3
    assert kv.zscore(k, "b") == 2.0
    assert kv.zrange(k, 0, -1) == ["a", "b", "c"]


# ---------------- Lists -------------------------------------------------------

def test_lpush_rpush_lrange(kv: KVClient, key_prefix):
    k = key_prefix + "l"
    kv.rpush(k, "first", "second", "third")
    assert kv.llen(k) == 3
    assert kv.lrange(k, 0, -1) == ["first", "second", "third"]
    assert kv.lpop(k) == "first"
    assert kv.rpop(k) == "third"


# ---------------- Streams -----------------------------------------------------

def test_xadd_xlen_xread(kv: KVClient, key_prefix):
    k = key_prefix + "stream"
    id1 = kv.xadd(k, {"token": "hello"})
    assert id1
    id2 = kv.xadd(k, {"token": "world"})
    assert kv.xlen(k) == 2

    result = kv.xread({k: "0-0"}, count=10)
    assert len(result) == 1
    stream_name, entries = result[0]
    assert stream_name == k
    assert len(entries) == 2
    assert entries[0][1]["token"] == "hello"
    assert entries[1][1]["token"] == "world"


# ---------------- Pipeline ----------------------------------------------------

def test_pipeline_batch(kv: KVClient, key_prefix):
    with kv.pipeline() as pipe:
        pipe.set(key_prefix + "p1", "a")
        pipe.set(key_prefix + "p2", "b")
        pipe.incr_by(key_prefix + "p3", 5)
        pipe.execute()
    assert kv.get(key_prefix + "p1") == "a"
    assert kv.get(key_prefix + "p2") == "b"
    assert kv.get(key_prefix + "p3") == "5"


# ---------------- Server-side scripting --------------------------------------

_NOOP_LUA = """
return tonumber(ARGV[1])
"""


def test_register_script_executes(kv: KVClient, key_prefix):
    script = kv.register_script(_NOOP_LUA)
    result = script(keys=[key_prefix + "_sx"], args=[42])
    # Both backends return an integer (or stringified int via EVAL on Redis).
    try:
        result = int(result)
    except (TypeError, ValueError):
        pass
    assert result == 42


def test_zremrangebyscore(kv: KVClient, key_prefix):
    k = key_prefix + "zrs"
    kv.zadd(k, {"a": 1.0, "b": 5.0, "c": 10.0})
    # Remove members with score in [0, 7] — should drop "a" and "b".
    removed = kv.zremrangebyscore(k, 0, 7)
    assert removed == 2
    assert kv.zcard(k) == 1


# ---------------- Introspection ----------------------------------------------

def test_backend_attribute(kv: KVClient):
    assert kv.backend in ("REDIS",)


def test_ping_succeeds(kv: KVClient):
    assert kv.ping() is True
