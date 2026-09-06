# SPDX-License-Identifier: MIT
# ============================================================
# DISTRIBUTED SEMAPHORE — KV-backed (Redis),
# cross-process LLM rate limiter.
#
# Uses a Lua script for atomic acquire (evict expired + check
# count + add token in one round-trip). No WATCH/MULTI needed.
# Lua execution goes through the backend-agnostic
# core.kv.KVClient.register_script (SPEC §6.7 / redis EVALSHA).
#
# Usage:
#   from core.kv import get_kv
#   from core.config import RDB_QUEUE
#   sem = DistributedSemaphore(get_kv(RDB_QUEUE), "llm_global", capacity=40)
#   token = sem.acquire(timeout=30)   # blocking, returns token str or None
#   sem.release(token)
# ============================================================

import time
import uuid
from core.logger import logger

# Atomic acquire: evict expired entries, check capacity, insert token.
# Args:   KEYS[1]=zset_key, ARGV[1]=now_ms, ARGV[2]=exp_ms, ARGV[3]=token, ARGV[4]=capacity
# Returns 1 on success, 0 if at capacity.
_ACQUIRE_LUA = """
local key      = KEYS[1]
local now_ms   = tonumber(ARGV[1])
local exp_ms   = tonumber(ARGV[2])
local token    = ARGV[3]
local capacity = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms)
local count = redis.call('ZCARD', key)
if count < capacity then
    redis.call('ZADD', key, exp_ms, token)
    return 1
end
return 0
"""


class DistributedSemaphore:
    """
    Cross-process counting semaphore backed by Redis sorted set.
    Each slot expires automatically after ttl_ms — crashed workers
    release their slot without any cleanup code.
    """

    def __init__(self, kv_client, name: str, capacity: int, ttl_ms: int = 60_000):
        # ``kv_client`` is a core.kv.KVClient (Redis) — the
        # script handle and all data operations route through the same
        # backend-agnostic interface.
        self._r        = kv_client
        self._key      = f"dsem:{name}"
        self._capacity = capacity
        self._ttl_ms   = ttl_ms
        self._script   = kv_client.register_script(_ACQUIRE_LUA)

    def acquire(self, timeout: float = 30.0) -> str | None:
        """
        Block until a slot is available or timeout expires.
        Returns an opaque token on success, None on timeout.
        """
        deadline = time.monotonic() + timeout
        token    = str(uuid.uuid4())

        while True:
            now_ms = int(time.time() * 1000)
            exp_ms = now_ms + self._ttl_ms

            result = self._script(
                keys=[self._key],
                args=[now_ms, exp_ms, token, self._capacity],
            )
            if result == 1 or result == b"1":
                return token

            if time.monotonic() >= deadline:
                logger.warning(f"DistributedSemaphore {self._key}: timeout after {timeout}s")
                return None

            time.sleep(0.1)

    def release(self, token: str) -> None:
        if token:
            self._r.zrem(self._key, token)

    def current_count(self) -> int:
        now_ms = int(time.time() * 1000)
        self._r.zremrangebyscore(self._key, 0, now_ms)
        return self._r.zcard(self._key)
