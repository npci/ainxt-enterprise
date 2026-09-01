# SPDX-License-Identifier: Apache-2.0
# ============================================================
# core.ckms.key_service — process-memory singleton holding clear DEKs
#
# Lifecycle:
#   1. core.ckms.bootstrap.load_at_boot() populates the singleton.
#   2. After load, the cache is read-only for the rest of the process.
#   3. No runtime rotation (deferred — see requirement §"Open Points").
#
# Concurrency: a lock guards the load-once flow; once loaded, reads are
# lock-free (plain dict reads are atomic in CPython).
# ============================================================

from __future__ import annotations

import os
import threading
from typing import Dict, Optional


class KeyServiceError(Exception):
    """Any CKMS failure surfaced to ``bootstrap.load_at_boot``."""


_DEFAULT_KEY_TYPE = "KEY_CREDS"


class KeyService:
    """Singleton holding ``{key_type: clear_DEK_bytes}`` and the env→key_type map."""

    _instance: Optional["KeyService"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # Internal mutable state; written only inside _load_lock during load.
        self._cache: Dict[str, bytes] = {}
        self._mapping: Dict[str, str] = {}
        self._loaded: bool = False
        self._load_lock = threading.Lock()

    # -- singleton accessors ------------------------------------

    @classmethod
    def instance(cls) -> "KeyService":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the singleton — tests only."""
        with cls._instance_lock:
            cls._instance = None

    # -- load (called by bootstrap) -----------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    def install(
        self,
        cache: Dict[str, bytes],
        mapping: Dict[str, str],
    ) -> None:
        """Atomically install the clear-DEK cache and env-var mapping.

        Idempotent: a second call with ``loaded=True`` is a no-op.
        """
        with self._load_lock:
            if self._loaded:
                return
            # Defensive copies — callers must not be able to mutate our state.
            self._cache = dict(cache)
            self._mapping = dict(mapping)
            self._loaded = True

    # -- accessors ----------------------------------------------

    def key_type_for(self, env_var: str) -> str:
        """Return the key_type for ``env_var`` (default ``KEY_CREDS``)."""
        return self._mapping.get(env_var, _DEFAULT_KEY_TYPE)

    def clear_dek(self, key_type: str) -> bytes:
        """Return the clear DEK bytes for ``key_type``."""
        try:
            return self._cache[key_type]
        except KeyError as exc:
            raise KeyServiceError(
                f"no active DEK loaded for key_type={key_type}"
            ) from exc

    def decrypt(self, env_var: str, ciphertext: str) -> str:
        """AES-GCM-decrypt ``ciphertext`` using the DEK for ``env_var``'s key_type."""
        # Local import to avoid a hard dependency cycle and to keep the
        # surface of this module minimal.
        from core.ckms.crypto import aes_gcm_decrypt

        key_type = self.key_type_for(env_var)
        key = self.clear_dek(key_type)
        return aes_gcm_decrypt(ciphertext, key)

    def decrypt_env(self, env_var: str) -> str:
        """Read ``os.environ[env_var]`` and decrypt it. Raises if missing."""
        try:
            ct = os.environ[env_var]
        except KeyError as exc:
            raise KeyServiceError(
                f"required env var not set: {env_var}"
            ) from exc
        return self.decrypt(env_var, ct)
