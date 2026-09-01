# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MemoryService — one facade over the seven memory stores
# ============================================================
#
# docs/architecture/07-memory-architecture.md §7.8 (M1, M2). Today memory access
# is scattered across seven stores with seven APIs. This facade gives one
# read(scope)/write(scope)/forget(scope) surface AND enforces the sensitivity
# gate (§7.7): `restricted`/`confidential` content must never persist into
# cross-chat durable memory.
#
# The store operations are INJECTED (callables) so this module stays pure and
# importable in a bare env; production wiring passes the real
# postgres_memory/redis_memory functions. The sensitivity gate is a pure
# decision, fully testable here.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class Scope(str, Enum):
    SESSION = "session"       # redis, per-chat
    WORKING = "working"       # redis, per-task
    DURABLE = "durable"       # postgres, cross-chat (the sensitive one)
    ORG = "org"               # cowork prefs


# sensitivity ordering (mirrors profiles.routing / core.rag_acl intent)
_SENSITIVITY_ORDER = ["public", "internal", "confidential", "restricted"]


def _rank(tier: str) -> int:
    try:
        return _SENSITIVITY_ORDER.index((tier or "internal").lower())
    except ValueError:
        return 1  # default to 'internal'


@dataclass
class MemoryService:
    """Facade over the memory stores. Store ops are injected callables so the
    facade is pure/testable; production passes real store functions.

    max_sensitivity_to_store comes from the Domain Profile (memory policy). A
    write whose content sensitivity EXCEEDS this floor is REFUSED for DURABLE
    scope (returns False) — the right-to-not-persist-secrets guarantee.
    """

    reader: Optional[Callable[[str, str], Any]] = None   # (scope, key) -> value
    writer: Optional[Callable[[str, str, Any], bool]] = None  # (scope, key, value) -> ok
    forgetter: Optional[Callable[[str, str], bool]] = None    # (scope, key) -> ok
    max_sensitivity_to_store: str = "internal"

    def can_store_durable(self, sensitivity: str) -> bool:
        """Pure gate: may content of this sensitivity persist to durable memory?"""
        return _rank(sensitivity) <= _rank(self.max_sensitivity_to_store)

    def read(self, scope: Scope, key: str) -> Any:
        if self.reader is None:
            return None
        try:
            return self.reader(scope.value, key)
        except Exception:  # noqa: BLE001 — reads never fail a turn
            return None

    def write(self, scope: Scope, key: str, value: Any, *, sensitivity: str = "internal") -> bool:
        """Write to a store. DURABLE writes are sensitivity-gated; over-sensitive
        content is silently refused (returns False) rather than persisted."""
        if scope == Scope.DURABLE and not self.can_store_durable(sensitivity):
            return False  # refuse to persist confidential/restricted cross-chat
        if self.writer is None:
            return False
        try:
            return bool(self.writer(scope.value, key, value))
        except Exception:  # noqa: BLE001 — writes are best-effort
            return False

    def forget(self, scope: Scope, key: str) -> bool:
        if self.forgetter is None:
            return False
        try:
            return bool(self.forgetter(scope.value, key))
        except Exception:  # noqa: BLE001
            return False
