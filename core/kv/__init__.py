# SPDX-License-Identifier: MIT
# ============================================================
# core.kv — backend-agnostic key-value layer for AiNxt
#
# Public API:
#   from core.kv import get_kv, close_all_kv, kv_backend_map
#   from core.kv import KVClient, KVError, KVTransient, KVPermanent
#
# Each of the 8 logical DBs picks its backend independently via
# REDIS_CLIENT_CONFIG_DB{n} (or REDIS_CLIENT_CONFIG globally).
# See core.config.
# ============================================================

from .async_base import AsyncKVClient, AsyncKVPipeline
from .base import KVClient, KVPipeline, KVScript
from .errors import KVError, KVPermanent, KVTransient
from .factory import (
    async_close_all_kv,
    async_get_kv,
    close_all_kv,
    get_kv,
    kv_backend_map,
)
from .health import kv_health_status

__all__ = [
    "KVClient",
    "KVPipeline",
    "KVScript",
    "AsyncKVClient",
    "AsyncKVPipeline",
    "KVError",
    "KVTransient",
    "KVPermanent",
    "get_kv",
    "close_all_kv",
    "kv_backend_map",
    "async_get_kv",
    "async_close_all_kv",
    "kv_health_status",
]
