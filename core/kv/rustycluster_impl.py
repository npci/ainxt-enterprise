# SPDX-License-Identifier: Apache-2.0
# ============================================================
# RustyClusterKVClient — KVClient implementation backed by
# rustycluster.get_client.
#
# Each logical DB (0..7) maps to a named cluster ("DB0".."DB7")
# defined in rustycluster.yaml. The rustycluster client handles
# replication, sharding, failover, and authentication.
#
# Method names on the RC client (SPEC §6) differ slightly from
# redis-py; this wrapper normalizes them onto the KVClient ABC.
# ============================================================

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from .base import KVClient, KVPipeline, KVScript
from .errors import KVError, KVPermanent, KVTransient
from .metrics import observe as _kv_observe


# Import lazily so the codebase can still run in REDIS-only mode
# without the rustycluster package installed.
def _get_rc():
    try:
        import rustycluster  # noqa: F401
        return rustycluster
    except ImportError as exc:
        raise KVPermanent(
            "py-rustycluster-client is not installed; "
            "either `pip install py-rustycluster-client>=1.1.4` or set "
            "REDIS_CLIENT_CONFIG=REDIS"
        ) from exc


# ── Env-var expansion for rustycluster.yaml ────────────────────────────────
# The upstream rustycluster library does NOT expand ${VAR} / ${VAR:-default}
# placeholders when loading YAML — it passes raw strings to Pydantic, which
# then rejects them as not 'host:port'. We pre-load the YAML here, expand
# shell-style placeholders against os.environ, build a RustyClusterSettings,
# and pass it explicitly into get_client(..., settings=...). Cached after
# first load so cost is one-time.

_YAML_SEARCH_PATHS = (
    "rustycluster.yaml",
    "config/rustycluster.yaml",
    "~/.rustycluster.yaml",
)

# Matches ${VAR} and ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_settings_cache = None  # type: Optional[Any]


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} / ${VAR:-default} in strings."""
    if isinstance(value, str):
        def _sub(match: "re.Match[str]") -> str:
            var, default = match.group(1), match.group(2)
            return os.environ.get(var, default if default is not None else "")
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _load_settings_with_env() -> Any:
    """Load rustycluster.yaml with ${VAR:-default} expansion, then build
    a RustyClusterSettings. Cached after first call."""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache

    import yaml
    from rustycluster.config import RustyClusterSettings

    yaml_path: Optional[Path] = None
    for candidate in _YAML_SEARCH_PATHS:
        p = Path(candidate).expanduser()
        if p.exists():
            yaml_path = p
            break
    if yaml_path is None:
        # Let rustycluster's own auto-discovery raise its native error.
        return None

    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    expanded = _expand_env(raw)

    # RustyClusterSettings.from_yaml expects a path, so emit the expanded
    # YAML to a sibling temp file and load from there. This preserves the
    # library's own validation / defaults-merging logic.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tf:
        yaml.safe_dump(expanded, tf, sort_keys=False)
        tmp_path = tf.name

    try:
        _settings_cache = RustyClusterSettings.from_yaml(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return _settings_cache


def _wrap(fn):
    """Decorator: convert rustycluster errors into core.kv.errors.* and
    record kv_call_total / kv_call_latency_seconds metrics."""
    op_name = fn.__name__

    def _inner(self, *args, **kwargs):
        with _kv_observe(self.backend, op_name, self.db):
            try:
                return fn(self, *args, **kwargs)
            except KVError:
                raise
            except Exception as exc:  # pragma: no cover — exact RC exception types vary
                # Best-effort classification by name. The RC SDK raises
                # ConnectionError / TimeoutError / AuthError subclasses; if
                # any unknown exception slips through, treat it as KVError.
                cls_name = type(exc).__name__.lower()
                if "auth" in cls_name or "permission" in cls_name:
                    raise KVPermanent(str(exc)) from exc
                if "timeout" in cls_name or "connection" in cls_name or "unavailable" in cls_name:
                    raise KVTransient(str(exc)) from exc
                raise KVError(str(exc)) from exc
    _inner.__name__ = fn.__name__
    _inner.__doc__ = fn.__doc__
    return _inner


class _RustyClusterScript(KVScript):
    """Lua script handle backed by RustyCluster SPEC §6.7 load_script/eval_sha.

    Source is uploaded once (`load_script` returns a SHA); subsequent calls
    use `eval_sha`. If the SDK lacks either method we fall back to a
    Python-side cache keyed by the source string and raise on call.
    """

    def __init__(self, client, source: str):
        self._client = client
        self._source = source
        self._sha: Optional[str] = None
        loader = getattr(client, "load_script", None)
        if callable(loader):
            try:
                self._sha = loader(source)
            except Exception as exc:
                raise KVPermanent(f"load_script failed: {exc}") from exc
        else:
            # SDK without scripting — record the source so __call__ can
            # raise with a clear message instead of obscure AttributeError.
            self._sha = None

    def __call__(self, *, keys: list, args: list):
        runner = getattr(self._client, "eval_sha", None)
        if self._sha is None or not callable(runner):
            raise KVPermanent(
                "RustyCluster SDK lacks load_script/eval_sha — cannot run "
                "Lua scripts on this backend. Upgrade py-rustycluster-client "
                "or route this DB to REDIS."
            )
        try:
            return runner(self._sha, keys=list(keys), args=list(args))
        except Exception as exc:  # pragma: no cover — RC exception names vary
            cls = type(exc).__name__.lower()
            if "auth" in cls or "permission" in cls:
                raise KVPermanent(str(exc)) from exc
            if "timeout" in cls or "connection" in cls or "unavailable" in cls:
                raise KVTransient(str(exc)) from exc
            raise KVError(str(exc)) from exc


class _RustyClusterPipeline(KVPipeline):
    """Adapter over rustycluster batch() per SPEC §7."""

    backend = "RUSTYCLUSTER"

    def __init__(self, client, db: int = -1):
        self._client = client
        self.db = db
        # rustycluster supports .batch() returning a builder object; if
        # the installed version differs, we fall back to a Python-side
        # buffer that replays at execute() time.
        self._ops: list[tuple[str, tuple, dict]] = []

    def _add(self, op: str, *args, **kwargs):
        self._ops.append((op, args, kwargs))
        return self

    def set(self, key, value, *, ex=None):
        if ex is not None:
            return self._add("set_ex", key, value, ex)
        return self._add("set", key, value)

    def setex(self, key, ttl, value):
        return self._add("set_ex", key, value, ttl)

    def delete(self, *keys):
        return self._add("del_multiple", *keys)

    def sadd(self, key, *members):
        if not members:
            return self
        return self._add("sadd", key, *members)

    def zadd(self, key, mapping):
        if not mapping:
            return self
        return self._add("zadd", key, dict(mapping))

    def incr(self, key, amount=1):
        return self._add("incr_by", key, amount)

    def incr_by(self, key, amount=1):
        return self._add("incr_by", key, amount)

    def incrbyfloat(self, key, amount):
        return self._add("incr_by_float", key, amount)

    def hset(self, key, field, value):
        return self._add("hset", key, field, value)

    def hincrby(self, key, field, amount=1):
        return self._add("hincr_by", key, field, amount)

    def zincrby(self, key, amount, member):
        # SPEC: rustycluster zadd is the upsert; zincrby is exposed as a
        # dedicated method. If not present in this SDK version, fall back
        # via a read-modify-write at replay time.
        return self._add("zincr_by", key, amount, member)

    def expire(self, key, ttl):
        return self._add("set_expiry", key, ttl)

    @_wrap
    def execute(self):
        # Prefer native batch() if the SDK exposes it; otherwise replay.
        batch = getattr(self._client, "batch", None)
        if callable(batch):
            try:
                b = batch()
                for op, args, kwargs in self._ops:
                    getattr(b, op)(*args, **kwargs)
                results = b.execute()
                self._ops.clear()
                return results
            except (TypeError, AttributeError):
                pass  # SDK shape differs — fall through to replay
        results = []
        for op, args, kwargs in self._ops:
            results.append(getattr(self._client, op)(*args, **kwargs))
        self._ops.clear()
        return results

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # If the caller exited cleanly without calling .execute(), the
        # queued ops would be silently lost. Match redis-py's behaviour by
        # logging a warning — explicit .execute() remains required.
        if exc_type is None and self._ops:
            try:
                from core.logger import logger as _logger
                _logger.warning(
                    "rustycluster pipeline exited with %d un-executed op(s) — "
                    "call .execute() before leaving the context",
                    len(self._ops),
                )
            except Exception:
                pass
        self._ops.clear()


class RustyClusterKVClient(KVClient):
    """KVClient backed by rustycluster.get_client(f'DB{db}')."""

    def __init__(self, db: int, *, decode_responses: bool = True):
        self._db = db
        self._decode = decode_responses
        rc = _get_rc()
        settings = _load_settings_with_env()
        if settings is not None:
            self._client = rc.get_client(f"DB{db}", settings=settings)
        else:
            # No YAML found locally — fall back to library auto-discovery
            # so the original error path is preserved.
            self._client = rc.get_client(f"DB{db}")

    # ---- introspection ----
    @property
    def backend(self) -> str:
        return "RUSTYCLUSTER"

    @property
    def db(self) -> int:
        return self._db

    @_wrap
    def ping(self) -> bool:
        # SPEC §8 — system ops; the client exposes ping() or returns True
        # when the connection is healthy. Defensive: try both shapes.
        if hasattr(self._client, "ping"):
            return bool(self._client.ping())
        # Fallback: a cheap get on a sentinel key
        self._client.get("__kv_ping__")
        return True

    def close(self) -> None:
        try:
            if hasattr(self._client, "close"):
                self._client.close()
        except Exception:
            pass

    # ---- helpers ----
    @staticmethod
    def _to_str(v):
        if v is None:
            return None
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8")
            except UnicodeDecodeError:
                return v
        return v

    # ---- Strings ----
    @_wrap
    def get(self, key):
        return self._to_str(self._client.get(key))

    @_wrap
    def set(self, key, value, *, ex=None, nx=False):
        if nx and ex is not None:
            ok = self._client.set_nx(key, value)
            if ok:
                self._client.set_expiry(key, ex)
            return bool(ok)
        if nx:
            return bool(self._client.set_nx(key, value))
        if ex is not None:
            return bool(self._client.set_ex(key, value, ex))
        return bool(self._client.set(key, value))

    @_wrap
    def setex(self, key, ttl, value):
        return bool(self._client.set_ex(key, value, ttl))

    @_wrap
    def setnx(self, key, value):
        return bool(self._client.set_nx(key, value))

    @_wrap
    def delete(self, *keys):
        if not keys:
            return 0
        if len(keys) == 1:
            return int(bool(self._client.delete(keys[0])))
        return int(self._client.del_multiple(*keys))

    @_wrap
    def exists(self, *keys):
        if not keys:
            return 0
        count = 0
        for k in keys:
            if self._client.exists(k):
                count += 1
        return count

    @_wrap
    def expire(self, key, ttl):
        return bool(self._client.set_expiry(key, ttl))

    @_wrap
    def ttl(self, key):
        return int(self._client.ttl(key))

    @_wrap
    def mget(self, *keys):
        if not keys:
            return []
        return [self._to_str(v) for v in self._client.mget(*keys)]

    @_wrap
    def keys(self, pattern):
        return [self._to_str(k) for k in self._client.keys(pattern)]

    @_wrap
    def incr(self, key, amount=1):
        return int(self._client.incr_by(key, amount))

    @_wrap
    def decr(self, key, amount=1):
        return int(self._client.decr_by(key, amount))

    @_wrap
    def incrbyfloat(self, key, amount):
        return float(self._client.incr_by_float(key, amount))

    # ---- Hashes ----
    @_wrap
    def hset(self, key, field=None, value=None, *, mapping=None):
        # SPEC: hset returns bool / int depending on call. KVClient
        # contract returns int (count of fields newly added).
        if mapping is not None:
            # hmset is the bulk variant — set everything atomically.
            extra = dict(mapping)
            if field is not None:
                extra[field] = value
            ok = self._client.hmset(key, extra)
            return int(len(extra)) if ok else 0
        return int(bool(self._client.hset(key, field, value)))

    @_wrap
    def hget(self, key, field):
        return self._to_str(self._client.hget(key, field))

    @_wrap
    def hmget(self, key, *fields):
        return [self._to_str(v) for v in self._client.hmget(key, *fields)]

    @_wrap
    def hgetall(self, key):
        raw = self._client.hget_all(key)
        return {self._to_str(k): self._to_str(v) for k, v in dict(raw).items()}

    @_wrap
    def hmset(self, key, mapping):
        if not mapping:
            return True
        return bool(self._client.hmset(key, dict(mapping)))

    @_wrap
    def hexists(self, key, field):
        return bool(self._client.hexists(key, field))

    @_wrap
    def hdel(self, key, *fields):
        if not fields:
            return 0
        return int(self._client.hdel(key, *fields))

    @_wrap
    def hlen(self, key):
        return int(self._client.hlen(key))

    @_wrap
    def hsetnx(self, key, field, value):
        return bool(self._client.hsetnx(key, field, value))

    @_wrap
    def hincrby(self, key, field, amount=1):
        return int(self._client.hincr_by(key, field, amount))

    @_wrap
    def hincrbyfloat(self, key, field, amount):
        # SPEC §6.2 — RC SDK exposes hincr_by_float.
        return float(self._client.hincr_by_float(key, field, amount))

    @_wrap
    def hscan(self, key, cursor=0, match=None, count=None):
        next_cursor, mapping = self._client.hscan(
            key, cursor=cursor, pattern=match, count=count,
        )
        return int(next_cursor), {
            self._to_str(k): self._to_str(v) for k, v in dict(mapping).items()
        }

    # ---- Sets ----
    @_wrap
    def sadd(self, key, *members):
        if not members:
            return 0
        return int(self._client.sadd(key, *members))

    @_wrap
    def srem(self, key, *members):
        if not members:
            return 0
        return int(self._client.srem(key, *members))

    @_wrap
    def smembers(self, key):
        return {self._to_str(m) for m in self._client.smembers(key)}

    @_wrap
    def sismember(self, key, member):
        return bool(self._client.sismember(key, member))

    @_wrap
    def scard(self, key):
        return int(self._client.scard(key))

    # ---- Sorted Sets ----
    @_wrap
    def zadd(self, key, mapping):
        if not mapping:
            return 0
        return int(self._client.zadd(key, dict(mapping)))

    @_wrap
    def zrem(self, key, *members):
        if not members:
            return 0
        return int(self._client.zrem(key, *members))

    @_wrap
    def zremrangebyscore(self, key, min_, max_):
        # SPEC §6.4 — RC SDK exposes zremrangebyscore. Defensive fallback:
        # iterate and zrem if the method is missing (older SDK versions).
        fn = getattr(self._client, "zremrangebyscore", None)
        if callable(fn):
            return int(fn(key, min_, max_))
        victims = self._client.zrangebyscore(key, min_, max_)
        if not victims:
            return 0
        return int(self._client.zrem(key, *victims))

    @_wrap
    def zrange(self, key, start, end, *, withscores=False):
        res = self._client.zrange(key, start, end, withscores=withscores)
        if withscores:
            return [(self._to_str(m), float(s)) for m, s in res]
        return [self._to_str(m) for m in res]

    @_wrap
    def zrevrange(self, key, start, end, *, withscores=False):
        # Prefer a dedicated method if the SDK exposes it; otherwise
        # reverse the ascending range client-side.
        fn = getattr(self._client, "zrevrange", None)
        if callable(fn):
            res = fn(key, start, end, withscores=withscores)
        else:
            asc = self._client.zrange(key, 0, -1, withscores=withscores)
            res = list(reversed(asc))[start:None if end == -1 else end + 1]
        if withscores:
            return [(self._to_str(m), float(s)) for m, s in res]
        return [self._to_str(m) for m in res]

    @_wrap
    def zincrby(self, key, amount, member):
        # SPEC §6.4 — RC SDK exposes zincr_by; if not, fall back to a
        # read-modify-write using zscore + zadd. Not atomic in the
        # fallback path; callers should treat the result as best-effort
        # under concurrent contention.
        fn = getattr(self._client, "zincr_by", None) or getattr(self._client, "zincrby", None)
        if callable(fn):
            return float(fn(key, amount, member))
        current = self._client.zscore(key, member) or 0.0
        new_score = float(current) + float(amount)
        self._client.zadd(key, {member: new_score})
        return new_score

    @_wrap
    def zrangebyscore(self, key, min_, max_, *, withscores=False):
        res = self._client.zrangebyscore(key, min_, max_, withscores=withscores)
        if withscores:
            return [(self._to_str(m), float(s)) for m, s in res]
        return [self._to_str(m) for m in res]

    @_wrap
    def zscore(self, key, member):
        v = self._client.zscore(key, member)
        return float(v) if v is not None else None

    @_wrap
    def zcard(self, key):
        return int(self._client.zcard(key))

    # ---- Lists ----
    @_wrap
    def lpush(self, key, *values):
        if not values:
            return 0
        return int(self._client.lpush(key, *values))

    @_wrap
    def rpush(self, key, *values):
        if not values:
            return 0
        return int(self._client.rpush(key, *values))

    @_wrap
    def lpop(self, key):
        return self._to_str(self._client.lpop(key))

    @_wrap
    def rpop(self, key):
        return self._to_str(self._client.rpop(key))

    @_wrap
    def blpop(self, keys, timeout=0):
        res = self._client.blpop(list(keys), timeout=timeout)
        if not res:
            return None
        return (self._to_str(res[0]), self._to_str(res[1]))

    @_wrap
    def brpop(self, keys, timeout=0):
        res = self._client.brpop(list(keys), timeout=timeout)
        if not res:
            return None
        return (self._to_str(res[0]), self._to_str(res[1]))

    @_wrap
    def lrange(self, key, start, end):
        return [self._to_str(v) for v in self._client.lrange(key, start, end)]

    @_wrap
    def ltrim(self, key, start, end):
        # RC SDK exposes ltrim per SPEC §6.5. Defensive: if missing,
        # emulate via lrange + delete + rpush (best-effort).
        fn = getattr(self._client, "ltrim", None)
        if callable(fn):
            return bool(fn(key, start, end))
        items = self._client.lrange(key, start, end)
        self._client.delete(key)
        if items:
            self._client.rpush(key, *items)
        return True

    @_wrap
    def llen(self, key):
        return int(self._client.llen(key))

    # ---- Streams ----
    @_wrap
    def xadd(self, key, fields, *, maxlen=None, approximate=True):
        kwargs = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
        return self._to_str(self._client.xadd(key, dict(fields), **kwargs))

    @_wrap
    def xread(self, streams, *, count=None, block=None):
        kwargs = {}
        if count is not None:
            kwargs["count"] = count
        if block is not None:
            kwargs["block_ms"] = block
        raw = self._client.xread(dict(streams), **kwargs) or []
        out = []
        for stream_name, entries in raw:
            converted = []
            for entry_id, entry_fields in entries:
                converted.append((
                    self._to_str(entry_id),
                    {self._to_str(k): self._to_str(v) for k, v in dict(entry_fields).items()},
                ))
            out.append((self._to_str(stream_name), converted))
        return out

    @_wrap
    def xlen(self, key):
        return int(self._client.xlen(key))

    @_wrap
    def xdel(self, key, *ids):
        if not ids:
            return 0
        return int(self._client.xdel(key, *ids))

    # ---- Server-side scripting ----
    @_wrap
    def register_script(self, source: str) -> KVScript:
        return _RustyClusterScript(self._client, source)

    # ---- Pipeline ----
    def pipeline(self) -> KVPipeline:
        return _RustyClusterPipeline(self._client, db=self._db)
