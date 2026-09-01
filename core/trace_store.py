# SPDX-License-Identifier: Apache-2.0
# ============================================================
# PRODUCTION TRACE STORE
# Redis-backed, thread-safe, scalable
# ============================================================

import json
from datetime import datetime
from core.config import RDB_TRACE
from core.kv import get_kv
from core.logger import logger


# ============================================================
# KV CONFIG (DB=1, trace store)
# Backend selected via REDIS_CLIENT_CONFIG_DB1.
# ============================================================

redis_client = get_kv(RDB_TRACE, decode_responses=True)

TRACE_TTL = 86400  # 24 hours


# ============================================================
# ADD TRACE
# ============================================================

def add_trace(request_id: str, message: str):

    try:

        if not request_id:
            return

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "message": message
        }

        key = f"trace:{request_id}"

        redis_client.rpush(
            key,
            json.dumps(entry)
        )

        redis_client.expire(
            key,
            TRACE_TTL
        )

    except Exception as e:

        logger.error(f"Trace store failed: {e}")


# ============================================================
# GET TRACE
# ============================================================

def get_trace(request_id: str):

    try:

        key = f"trace:{request_id}"

        entries = redis_client.lrange(
            key,
            0,
            -1
        )

        return [
            json.loads(e)
            for e in entries
        ]

    except Exception as e:

        logger.error(f"Trace fetch failed: {e}")

        return []


# ============================================================
# DELETE TRACE (optional cleanup)
# ============================================================

def delete_trace(request_id: str):

    try:

        redis_client.delete(
            f"trace:{request_id}"
        )

    except Exception as e:

        logger.error(f"Trace delete failed: {e}")


logger.info("Trace store initialized")