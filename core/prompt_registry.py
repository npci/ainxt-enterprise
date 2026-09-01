# SPDX-License-Identifier: Apache-2.0
"""
core/prompt_registry.py — P10: Prompt versioning + A/B testing.

Provides a registry for versioned prompts with:
  - Version management (register, activate, rollback)
  - A/B testing (deterministic session-based routing)
  - Redis caching (TTL=300s) to avoid DB hits on every request
  - Auto-rollback when variant eval_score drops >20% vs control

DESIGN
------
- Prompts are stored in the prompt_versions table (db/models.py)
- Active version is served by default; A/B test routes % of traffic to variant
- Session routing is deterministic: hash(session_id) % 100 < variant_pct → variant
- Redis cache key: prompt_cache:{prompt_key} → JSON {version, content}
- Auto-rollback: checked by core/evals.py after storing EvalResult

WHAT IS NOT BUILT
-----------------
- Multi-armed bandit auto-routing
- Prompt diff/merge
- Git-based prompt versioning
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from core.logger import logger

_CACHE_TTL = int(os.getenv("PROMPT_CACHE_TTL_SEC", "300"))
_CACHE_PFX = "prompt_cache:"
_AB_ENABLED = os.getenv("PROMPT_AB_TEST_ENABLED", "false").lower() in ("1", "true", "yes")

# Auto-rollback threshold: if variant score drops >20% vs control, rollback
_AUTO_ROLLBACK_THRESHOLD = 0.20


class PromptRegistry:
    """
    Registry for versioned prompts with A/B testing support.
    """

    def __init__(self):
        self._redis = None

    def _get_redis(self):
        if self._redis is None:
            try:
                from core.kv import get_kv
                from core.config import RDB_CACHE
                self._redis = get_kv(RDB_CACHE, decode_responses=True)
            except Exception:
                pass
        return self._redis

    def _get_db(self):
        from db.database import SessionLocal
        return SessionLocal()

    def get(self, key: str, session_id: Optional[str] = None) -> Optional[str]:
        """
        Get the active prompt content for key.

        If A/B test is active and PROMPT_AB_TEST_ENABLED=true:
          - Deterministically route session_id to control or variant
          - hash(session_id) % 100 < variant_pct → serve variant

        Returns None if no active version exists.
        """
        # Try Redis cache first
        cache_key = f"{_CACHE_PFX}{key}"
        try:
            redis = self._get_redis()
            if redis:
                cached = redis.get(cache_key)
                if cached:
                    data = json.loads(cached)
                    # A/B routing on cached data
                    if _AB_ENABLED and session_id and data.get("ab_test"):
                        return self._ab_route(key, session_id, data)
                    return data.get("content")
        except Exception:
            pass

        # Load from DB
        try:
            db = self._get_db()
            try:
                from db.models import PromptVersion
                from sqlalchemy import and_

                # Get all active versions for this key
                versions = (
                    db.query(PromptVersion)
                    .filter(and_(
                        PromptVersion.prompt_key == key,
                        PromptVersion.is_active == True,
                    ))
                    .all()
                )

                if not versions:
                    return None

                # Find control and variant
                control = next((v for v in versions if v.is_control), None)
                variant = next((v for v in versions if not v.is_control), None)

                if not control:
                    control = versions[0]

                # Build cache payload
                cache_data: dict = {
                    "content": control.content,
                    "version": control.version,
                }

                if _AB_ENABLED and variant and session_id:
                    cache_data["ab_test"] = {
                        "control_content":  control.content,
                        "variant_content":  variant.content,
                        "variant_pct":      variant.traffic_pct,
                        "control_version":  control.version,
                        "variant_version":  variant.version,
                    }

                # Cache for TTL seconds
                try:
                    redis = self._get_redis()
                    if redis:
                        redis.setex(cache_key, _CACHE_TTL, json.dumps(cache_data))
                except Exception:
                    pass

                if _AB_ENABLED and session_id and cache_data.get("ab_test"):
                    return self._ab_route(key, session_id, cache_data)

                return control.content

            finally:
                db.close()
        except Exception as e:
            logger.warning(f"PromptRegistry.get failed for key={key!r}: {e}")
            return None

    def _ab_route(self, key: str, session_id: str, cache_data: dict) -> str:
        """Deterministically route session_id to control or variant."""
        ab = cache_data.get("ab_test", {})
        if not ab:
            return cache_data.get("content", "")
        variant_pct = float(ab.get("variant_pct", 0))
        # Deterministic: hash(session_id + key) % 100
        h = int(hashlib.sha256(f"{session_id}:{key}".encode()).hexdigest(), 16) % 100
        if h < variant_pct:
            logger.debug(f"PromptRegistry A/B: session={session_id[:8]} → variant (key={key})")
            return ab.get("variant_content", cache_data.get("content", ""))
        return ab.get("control_content", cache_data.get("content", ""))

    def register(self, key: str, content: str, author: str = "system") -> int:
        """
        Register a new version of a prompt.
        Auto-increments version number. Does NOT activate automatically.
        Returns the new version number.
        """
        try:
            db = self._get_db()
            try:
                from db.models import PromptVersion
                from sqlalchemy import func

                # Get max version for this key
                max_ver = (
                    db.query(func.max(PromptVersion.version))
                    .filter(PromptVersion.prompt_key == key)
                    .scalar()
                ) or 0

                new_version = max_ver + 1
                pv = PromptVersion(
                    prompt_key=key,
                    version=new_version,
                    content=content,
                    is_active=False,
                    is_control=False,
                    author=author,
                )
                db.add(pv)
                db.commit()
                logger.info(f"PromptRegistry: registered {key} v{new_version} by {author}")
                return new_version
            finally:
                db.close()
        except Exception as e:
            logger.error(f"PromptRegistry.register failed: {e}")
            raise

    def activate(self, key: str, version: int) -> None:
        """
        Activate a specific version of a prompt.
        Deactivates all other versions for this key.
        Clears Redis cache.
        """
        try:
            db = self._get_db()
            try:
                from db.models import PromptVersion

                # Deactivate all versions for this key
                db.query(PromptVersion).filter(
                    PromptVersion.prompt_key == key
                ).update({"is_active": False, "is_control": False})

                # Activate the requested version as control
                db.query(PromptVersion).filter(
                    PromptVersion.prompt_key == key,
                    PromptVersion.version == version,
                ).update({"is_active": True, "is_control": True})

                db.commit()
                self._invalidate_cache(key)
                logger.info(f"PromptRegistry: activated {key} v{version}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"PromptRegistry.activate failed: {e}")
            raise

    def rollback(self, key: str) -> int:
        """
        Rollback to the previous active version.
        Returns the version number rolled back to.
        """
        try:
            db = self._get_db()
            try:
                from db.models import PromptVersion

                # Get current active version
                current = (
                    db.query(PromptVersion)
                    .filter(PromptVersion.prompt_key == key, PromptVersion.is_active == True)
                    .first()
                )
                if not current:
                    raise ValueError(f"No active version for key={key!r}")

                # Find the previous version (highest version < current)
                prev = (
                    db.query(PromptVersion)
                    .filter(
                        PromptVersion.prompt_key == key,
                        PromptVersion.version < current.version,
                    )
                    .order_by(PromptVersion.version.desc())
                    .first()
                )
                if not prev:
                    raise ValueError(f"No previous version to rollback to for key={key!r}")

                self.activate(key, prev.version)
                logger.info(f"PromptRegistry: rolled back {key} from v{current.version} to v{prev.version}")
                return prev.version
            finally:
                db.close()
        except Exception as e:
            logger.error(f"PromptRegistry.rollback failed: {e}")
            raise

    def start_ab_test(
        self,
        key: str,
        control_version: int,
        variant_version: int,
        variant_pct: float = 10.0,
    ) -> None:
        """
        Start an A/B test between control and variant versions.
        Both versions are marked active; variant gets traffic_pct % of traffic.
        """
        if not _AB_ENABLED:
            logger.warning("PromptRegistry.start_ab_test: PROMPT_AB_TEST_ENABLED=false — skipping")
            return
        try:
            db = self._get_db()
            try:
                from db.models import PromptVersion

                # Deactivate all
                db.query(PromptVersion).filter(
                    PromptVersion.prompt_key == key
                ).update({"is_active": False, "is_control": False, "traffic_pct": 0.0})

                # Activate control
                db.query(PromptVersion).filter(
                    PromptVersion.prompt_key == key,
                    PromptVersion.version == control_version,
                ).update({"is_active": True, "is_control": True, "traffic_pct": 100.0 - variant_pct})

                # Activate variant
                db.query(PromptVersion).filter(
                    PromptVersion.prompt_key == key,
                    PromptVersion.version == variant_version,
                ).update({"is_active": True, "is_control": False, "traffic_pct": variant_pct})

                db.commit()
                self._invalidate_cache(key)
                logger.info(
                    f"PromptRegistry: A/B test started for {key} "
                    f"control=v{control_version} variant=v{variant_version} "
                    f"variant_pct={variant_pct}%"
                )
            finally:
                db.close()
        except Exception as e:
            logger.error(f"PromptRegistry.start_ab_test failed: {e}")
            raise

    def record_eval_score(self, key: str, version: int, score: float) -> None:
        """
        Record an eval score for a prompt version.
        Triggers auto-rollback if variant score drops >20% vs control.
        """
        try:
            db = self._get_db()
            try:
                from db.models import PromptVersion
                from sqlalchemy import and_

                db.query(PromptVersion).filter(
                    PromptVersion.prompt_key == key,
                    PromptVersion.version == version,
                ).update({"eval_score": score})
                db.commit()

                # Auto-rollback check: compare variant vs control
                if _AB_ENABLED:
                    versions = (
                        db.query(PromptVersion)
                        .filter(and_(
                            PromptVersion.prompt_key == key,
                            PromptVersion.is_active == True,
                        ))
                        .all()
                    )
                    control = next((v for v in versions if v.is_control), None)
                    variant = next((v for v in versions if not v.is_control), None)

                    if (
                        control and variant
                        and control.eval_score is not None
                        and variant.eval_score is not None
                    ):
                        drop = (control.eval_score - variant.eval_score) / max(control.eval_score, 0.001)
                        if drop > _AUTO_ROLLBACK_THRESHOLD:
                            logger.warning(
                                f"PromptRegistry: auto-rollback triggered for {key} "
                                f"variant v{variant.version} score={variant.eval_score:.3f} "
                                f"dropped {drop:.0%} vs control v{control.version} "
                                f"score={control.eval_score:.3f}"
                            )
                            self.activate(key, control.version)
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"PromptRegistry.record_eval_score failed (non-fatal): {e}")

    def _invalidate_cache(self, key: str) -> None:
        """Delete the Redis cache entry for a prompt key."""
        try:
            redis = self._get_redis()
            if redis:
                redis.delete(f"{_CACHE_PFX}{key}")
        except Exception:
            pass


# Module-level singleton
prompt_registry = PromptRegistry()
