# SPDX-License-Identifier: Apache-2.0
# ============================================================
# EPISODIC MEMORY STORE
# Cross-session, cross-agent persistent key-value memory.
# Backed by PostgreSQL (agent_memory table).
# Falls back to in-process dict if DB unavailable.
# ============================================================

import uuid
from datetime import datetime
from typing import List, Optional

from core.logger import logger

# In-process fallback
_mem: dict = {}   # (agent_name, key) → record


# ── DB session ────────────────────────────────────────────────

def _get_session():
    try:
        from db.database import SessionLocal
        return SessionLocal()
    except Exception:
        return None


# ── REMEMBER ──────────────────────────────────────────────────

def remember(
    agent_name: str,
    key:        str,
    value:      str,
    tags:       Optional[List[str]] = None,
) -> dict:
    """
    Upsert a memory entry. Creates or overwrites the value for (agent_name, key).
    Returns the memory record dict.
    """
    tags = tags or []
    now  = datetime.utcnow().isoformat()
    record_id = str(uuid.uuid4())

    record = {
        "id":         record_id,
        "agent_name": agent_name,
        "key":        key,
        "value":      value,
        "tags":       tags,
        "created_at": now,
        "updated_at": now,
    }

    # Update in-process cache
    existing = _mem.get((agent_name, key))
    if existing:
        record["id"]         = existing["id"]
        record["created_at"] = existing["created_at"]
    _mem[(agent_name, key)] = record

    # Persist to DB
    session = _get_session()
    if session:
        try:
            from db.models import AgentMemory
            db_rec = session.query(AgentMemory).filter(
                AgentMemory.agent_name == agent_name,
                AgentMemory.key == key,
            ).first()
            if db_rec:
                db_rec.value      = value
                db_rec.tags       = tags
                db_rec.updated_at = datetime.utcnow()
                record["id"]         = str(db_rec.id)
                record["created_at"] = db_rec.created_at.isoformat()
            else:
                db_rec = AgentMemory(
                    id=record["id"],
                    agent_name=agent_name,
                    key=key,
                    value=value,
                    tags=tags,
                )
                session.add(db_rec)
            session.commit()
        except Exception as e:
            logger.warning(f"episodic_memory: DB upsert failed → {e}")
            session.rollback()
        finally:
            session.close()

    return record


# ── RECALL ────────────────────────────────────────────────────

def recall(agent_name: str, key: str) -> Optional[str]:
    """Return the value for (agent_name, key), or None."""
    session = _get_session()
    if session:
        try:
            from db.models import AgentMemory
            rec = session.query(AgentMemory).filter(
                AgentMemory.agent_name == agent_name,
                AgentMemory.key == key,
            ).first()
            if rec:
                return rec.value
        except Exception as e:
            logger.warning(f"episodic_memory: recall failed → {e}")
        finally:
            session.close()
    # fallback
    rec = _mem.get((agent_name, key))
    return rec["value"] if rec else None


# ── RECALL ALL ────────────────────────────────────────────────

def recall_all(agent_name: str) -> dict:
    """Return all memory entries for an agent as {key: value}."""
    session = _get_session()
    if session:
        try:
            from db.models import AgentMemory
            recs = session.query(AgentMemory).filter(
                AgentMemory.agent_name == agent_name
            ).all()
            return {r.key: r.value for r in recs}
        except Exception as e:
            logger.warning(f"episodic_memory: recall_all failed → {e}")
        finally:
            session.close()
    # fallback
    return {
        k: v["value"]
        for (a, k), v in _mem.items()
        if a == agent_name
    }


# ── RECALL BY TAGS ────────────────────────────────────────────

def recall_by_tags(agent_name: str, tags: List[str]) -> list:
    """Return all memory records for agent_name whose tags overlap with given tags."""
    session = _get_session()
    results = []
    if session:
        try:
            from db.models import AgentMemory
            recs = session.query(AgentMemory).filter(
                AgentMemory.agent_name == agent_name
            ).all()
            for r in recs:
                rtags = r.tags or []
                if any(t in rtags for t in tags):
                    results.append({
                        "key":   r.key,
                        "value": r.value,
                        "tags":  rtags,
                    })
            return results
        except Exception as e:
            logger.warning(f"episodic_memory: recall_by_tags failed → {e}")
        finally:
            session.close()
    # fallback
    for (a, k), v in _mem.items():
        if a == agent_name:
            rtags = v.get("tags", [])
            if any(t in rtags for t in tags):
                results.append({"key": k, "value": v["value"], "tags": rtags})
    return results


# ── FORGET ────────────────────────────────────────────────────

def forget(agent_name: str, key: str) -> bool:
    """Delete a memory entry. Returns True if it existed."""
    existed = False
    _mem.pop((agent_name, key), None)

    session = _get_session()
    if session:
        try:
            from db.models import AgentMemory
            deleted = session.query(AgentMemory).filter(
                AgentMemory.agent_name == agent_name,
                AgentMemory.key == key,
            ).delete()
            session.commit()
            existed = deleted > 0
        except Exception as e:
            logger.warning(f"episodic_memory: forget failed → {e}")
            session.rollback()
        finally:
            session.close()

    return existed
