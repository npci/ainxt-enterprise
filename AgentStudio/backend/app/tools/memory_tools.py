# SPDX-License-Identifier: MIT
"""
Memory tools — session memory (Redis) and episodic/cross-session memory (Postgres).

Env vars:
  REDIS_URL      — required; Redis connection URL e.g. redis://localhost:6379/0
  DATABASE_URL   — Postgres DSN e.g. postgresql://<user>:<pass>@<host>/<db>
Each tool's `code` string is self-contained and runs in the sandbox subprocess.

NOTE: These tools are marked `"draft": True` — they are present in the catalog
but will NOT be seeded into the database until the memory integration is
configured and the draft flag is removed.
"""

_REDIS_HELPERS = '''
import os, json

def _redis_url():
    url = os.environ.get("REDIS_URL", "")
    if not url:
        raise Exception("REDIS_URL is not set — configure it to your Redis instance URL")
    return url

def _redis():
    import redis as _redis_lib
    return _redis_lib.from_url(_redis_url(), decode_responses=True)
'''

_PG_HELPERS = '''
import os, json

def _db_url():
    return os.environ.get("DATABASE_URL", "")

def _pg_conn():
    import psycopg2
    return psycopg2.connect(_db_url())
'''

MEMORY_TOOLS = [
    # ------------------------------------------------------------------ #
    # memory_save — Redis session memory                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "memory_save",
        "draft": True,
        "description": "Save a conversation message to Redis session memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session identifier"},
                "role":       {"type": "string", "description": "Message role: user | assistant | system"},
                "content":    {"type": "string", "description": "Message content"},
            },
            "required": ["session_id", "role", "content"],
        },
        "code": _REDIS_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        session_id = inputs.get("session_id", "")
        role       = inputs.get("role", "user")
        content    = inputs.get("content", "")
        r          = _redis()
        key        = f"session:{session_id}:messages"
        message    = json.dumps({"role": role, "content": content})
        r.rpush(key, message)
        r.expire(key, 86400)  # 24h TTL
        length = r.llen(key)
        return {"result": f"Message saved to session {session_id} (total: {length} messages).", "length": length}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # memory_get — Redis session memory                                    #
    # ------------------------------------------------------------------ #
    {
        "name": "memory_get",
        "draft": True,
        "description": "Retrieve conversation history for a session from Redis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string",  "description": "Session identifier"},
                "limit":      {"type": "integer", "description": "Max messages to return (most recent)", "default": 50},
            },
            "required": ["session_id"],
        },
        "code": _REDIS_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        session_id = inputs.get("session_id", "")
        limit      = int(inputs.get("limit", 50))
        r          = _redis()
        key        = f"session:{session_id}:messages"
        raw        = r.lrange(key, -limit, -1)
        messages   = [json.loads(m) for m in raw]
        return {"result": f"Retrieved {len(messages)} message(s) for session {session_id}.", "messages": messages}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # memory_remember — Postgres episodic/cross-session memory             #
    # ------------------------------------------------------------------ #
    {
        "name": "memory_remember",
        "draft": True,
        "description": "Store a key-value memory for an agent that persists across sessions (Postgres).",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Name of the agent storing the memory"},
                "key":        {"type": "string", "description": "Memory key"},
                "value":      {"type": "string", "description": "Memory value to store"},
                "tags":       {"type": "array",  "description": "Optional tags for categorisation", "items": {"type": "string"}},
            },
            "required": ["agent_name", "key", "value"],
        },
        "code": _PG_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        agent_name = inputs.get("agent_name", "")
        key        = inputs.get("key", "")
        value      = inputs.get("value", "")
        tags       = inputs.get("tags", [])
        conn       = _pg_conn()
        cur        = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                agent_name TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                tags       JSONB DEFAULT \'[]\',
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (agent_name, key)
            )
        """)
        cur.execute("""
            INSERT INTO agent_memory (agent_name, key, value, tags, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (agent_name, key) DO UPDATE
              SET value = EXCLUDED.value, tags = EXCLUDED.tags, updated_at = NOW()
        """, (agent_name, key, value, json.dumps(tags)))
        conn.commit()
        cur.close()
        conn.close()
        return {"result": f"Memory stored: {agent_name}/{key}"}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # memory_recall — Postgres episodic/cross-session memory               #
    # ------------------------------------------------------------------ #
    {
        "name": "memory_recall",
        "draft": True,
        "description": "Recall a stored cross-session memory value for an agent by key (Postgres).",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Name of the agent"},
                "key":        {"type": "string", "description": "Memory key to recall"},
            },
            "required": ["agent_name", "key"],
        },
        "code": _PG_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        agent_name = inputs.get("agent_name", "")
        key        = inputs.get("key", "")
        conn       = _pg_conn()
        cur        = conn.cursor()
        cur.execute(
            "SELECT value, updated_at FROM agent_memory WHERE agent_name = %s AND key = %s",
            (agent_name, key)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return {"result": f"No memory found for {agent_name}/{key}.", "value": None}
        value, updated_at = row
        return {"result": f"Memory {agent_name}/{key}: {value}", "value": value, "updated_at": str(updated_at)}
    except Exception as e:
        return {"error": str(e)}
''',
    },
]
