# SPDX-License-Identifier: Apache-2.0
# ============================================================
# KV health probe — used by /health and any other endpoint that
# needs a per-DB reachability summary.
#
# Extracted from gateway.py so the logic can be unit-tested
# without importing the full FastAPI app.
# ============================================================

from __future__ import annotations

from typing import Dict


def kv_health_status() -> Dict[str, dict]:
    """
    Ping every logical KV DB through its configured backend.

    Returns a mapping::

        {
            "DB0": {"backend": "REDIS", "ok": True},
            "DB2": {"backend": "REDIS", "ok": False, "error": "..."},
            ...
        }

    The dictionary is ordered DB0 → DB(KV_DB_COUNT-1). Callers decide
    whether any failure should escalate to ``unhealthy``; the gateway
    does not, because a Redis outage is already accounted for by its
    legacy ``redis`` probe.
    """
    from .factory import get_kv, kv_backend_map

    out: Dict[str, dict] = {}
    for db, backend in kv_backend_map().items():
        try:
            get_kv(db).ping()
            out[f"DB{db}"] = {"backend": backend, "ok": True}
        except Exception as exc:
            out[f"DB{db}"] = {
                "backend": backend,
                "ok": False,
                "error": str(exc),
            }
    return out
