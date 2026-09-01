# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SKILL LOOP STORE — successful run signatures for the self-improving
# skill loop. Sibling of store/learning_store.py.
#
# Storage: Redis db=1 (trace store), 7-day window (matches learning_store).
#   skill_loop:sig:{dept}     — ZSET member=signature score=occurrence_count
#   skill_loop:meta:{sig}     — HASH {representative_prompt, tool_sequence,
#                                     source, department, last_seen}
#
# Capture is O(1) and inline-safe: one ZINCRBY + one HSET. NO LLM, NO
# clustering — detection/synthesis happens out-of-band in
# workers/skill_loop_worker.py. PII is redacted by the CALLER before the
# prompt reaches this store (we never store raw prompts), but we also
# defensively cap lengths here.
# ============================================================
from __future__ import annotations

import hashlib
import json
import time
from typing import List

import redis

from core.config import REDIS_HOST, REDIS_PORT
from core.logger import logger

_TTL          = 86400 * 7    # 7 days
_MAX_PROMPT   = 2000         # cap stored representative prompt
_DEFAULT_DEPT = "_global"

# Lightweight English stop-words for intent normalization. Kept small on
# purpose — the goal is to collapse trivial phrasing differences, not to do
# real NLP. Genuinely-repeated tasks collide; one-off prose doesn't.
_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "at", "by",
    "is", "are", "be", "with", "this", "that", "it", "as", "from", "please",
    "can", "you", "i", "we", "me", "my", "our", "us", "do", "does", "will",
}


def _redis() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=1,
        decode_responses=True,
        socket_connect_timeout=1,
    )


def _normalize_intent(redacted_prompt: str) -> str:
    """Lowercase, drop stop-words, sort the remaining token shingle so that
    'create the UPI report' and 'UPI report create' collapse to one key.
    Truncated to keep the signature stable for long prose."""
    toks = [t for t in "".join(
        c.lower() if (c.isalnum() or c.isspace()) else " " for c in (redacted_prompt or "")
    ).split() if t and t not in _STOP and len(t) > 1]
    # Sorted unique token set keeps the signature order-invariant.
    return " ".join(sorted(set(toks)))[:400]


def compute_signature(key: str, redacted_prompt: str) -> str:
    """sha256(source_key + '|' + normalized_intent). `key` is the agent name or
    the scheduled task_id — a stable per-source anchor."""
    basis = f"{(key or '').strip().lower()}|{_normalize_intent(redacted_prompt)}"
    # This digest is a dedup/grouping signature, never an integrity or
    # authentication control. sha256 is used instead of sha1 to avoid the
    # static-analysis false positive; it does not change the intended purpose.
    return hashlib.sha256(basis.encode("utf-8", "ignore")).digest().hex()


def record_run_signature(
    source: str,
    key: str,
    redacted_prompt: str,
    tool_history: List[str] | None = None,
    department: str = "",
) -> None:
    """Record ONE successful run occurrence. Inline-safe (O(1), never raises).

    Args:
      source           — 'agent_run' | 'cowork_task' | 'code_run'
      key              — stable per-source anchor (agent name / task_id)
      redacted_prompt  — PII-REDACTED trigger text (caller redacts; we cap length)
      tool_history     — observed tool names, in order
      department       — scoping bucket (falls back to a global bucket)
    """
    try:
        dept = (department or "").strip() or _DEFAULT_DEPT
        sig = compute_signature(key, redacted_prompt)
        r = _redis()

        zkey = f"skill_loop:sig:{dept}"
        mkey = f"skill_loop:meta:{sig}"

        pipe = r.pipeline()
        pipe.zincrby(zkey, 1, sig)
        pipe.expire(zkey, _TTL)
        pipe.hset(mkey, mapping={
            "representative_prompt": (redacted_prompt or "")[:_MAX_PROMPT],
            "tool_sequence":         json.dumps(list(tool_history or []))[:1000],
            "source":                source or "",
            "department":            dept,
            "key":                   (key or "")[:255],
            "last_seen":             str(time.time()),
        })
        pipe.expire(mkey, _TTL)
        pipe.execute()
        logger.debug(f"[SkillLoop] signature recorded sig={sig[:8]} dept={dept} source={source}")
    except Exception as e:
        # Capture must NEVER break a run — fail silently like learning_store.
        logger.debug(f"[SkillLoop] record_run_signature skipped: {e}")


def iter_hot_signatures(threshold: int, window_seconds: int = _TTL) -> List[dict]:
    """Return signatures whose occurrence_count >= threshold across all dept
    buckets, newest-seen first. Each item:
      {signature, department, count, source, representative_prompt, tool_sequence, key}

    `window_seconds` bounds freshness via the meta hash `last_seen` (the ZSET
    itself is count-based, not time-based)."""
    out: List[dict] = []
    try:
        r = _redis()
        cutoff = time.time() - window_seconds
        for zkey in r.scan_iter("skill_loop:sig:*", count=100):
            dept = zkey.split("skill_loop:sig:", 1)[-1]
            # Members at or above the threshold score.
            for sig, score in r.zrangebyscore(zkey, threshold, "+inf", withscores=True):
                meta = r.hgetall(f"skill_loop:meta:{sig}") or {}
                try:
                    last_seen = float(meta.get("last_seen", 0))
                except (TypeError, ValueError):
                    last_seen = 0.0
                if last_seen < cutoff:
                    continue
                try:
                    tool_seq = json.loads(meta.get("tool_sequence", "[]"))
                except Exception:
                    tool_seq = []
                out.append({
                    "signature":            sig,
                    "department":           dept if dept != _DEFAULT_DEPT else "",
                    "count":                int(score),
                    "source":               meta.get("source", ""),
                    "representative_prompt": meta.get("representative_prompt", ""),
                    "tool_sequence":        tool_seq,
                    "key":                  meta.get("key", ""),
                    "last_seen":            last_seen,
                })
        out.sort(key=lambda d: d.get("last_seen", 0), reverse=True)
    except Exception as e:
        logger.warning(f"[SkillLoop] iter_hot_signatures failed: {e}")
    return out


def clear_signature(signature: str, department: str = "") -> None:
    """Reset a signature's bucket after it has been turned into a proposal, so
    it doesn't immediately re-fire. Removes the ZSET member + meta hash."""
    try:
        r = _redis()
        dept = (department or "").strip() or _DEFAULT_DEPT
        pipe = r.pipeline()
        pipe.zrem(f"skill_loop:sig:{dept}", signature)
        pipe.delete(f"skill_loop:meta:{signature}")
        pipe.execute()
    except Exception as e:
        logger.debug(f"[SkillLoop] clear_signature skipped: {e}")
