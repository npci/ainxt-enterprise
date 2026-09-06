# SPDX-License-Identifier: MIT
# ============================================================
# core.ckms.hsm_gateway — connection lifecycle over a pluggable HSM provider
#
# Responsibilities:
# - Resolve which HSM backend to use (see core.ckms.hsm_provider).
# - Resolve the backend's config file (honours HSM_CONFIG_PATH env override).
# - Reuse a single connection across the burst of boot-time unwraps
#   (spec §6.2 / §10).
# - Guarantee every failure surfaces as KeyServiceError so bootstrap can
#   fail-fast uniformly.
#
# Only DecryptDEK (M2) is exercised at runtime — KEK generation and DEK
# wrapping are out of scope (requirement §"Out of Scope").
#
# The vendor-specific transport used to live inline here. It now lives in
# core.ckms.hsm_provider.PyHsmClientProvider, which remains the DEFAULT so
# existing deployments are unaffected. Point HSM_PROVIDER at your own class to
# use a different HSM without modifying this repository.
# ============================================================

from __future__ import annotations

import os
from types import TracebackType
from typing import Optional, Type

from core.ckms.hsm_provider import HSM_CONFIG_ENV, resolve_provider
from core.ckms.key_service import KeyServiceError

#: Retained for backwards compatibility with anything importing this name.
_HSM_CONFIG_ENV = HSM_CONFIG_ENV


class HSMGateway:
    """Reusable connection to the configured HSM backend.

    Typical boot-time usage::

        with HSMGateway() as gw:
            dek1 = gw.unwrap_dek(dek_kek_hex_1, kek_lmk_hex_1)
            dek2 = gw.unwrap_dek(dek_kek_hex_2, kek_lmk_hex_2)

    A single connection is reused for the whole ``with`` block.

    ``provider`` selects the backend explicitly; when omitted it comes from
    ``HSM_PROVIDER``, defaulting to the built-in ``py-hsm-client`` adapter.
    """

    def __init__(self, config_path: Optional[str] = None,
                 provider: Optional[str] = None):
        self._config_path = config_path or os.getenv(HSM_CONFIG_ENV) or None
        self._provider_spec = provider
        self._provider = None  # resolved and opened in __enter__

    # -- context-manager protocol -------------------------------

    def __enter__(self) -> "HSMGateway":
        provider = resolve_provider(
            config_path=self._config_path, provider=self._provider_spec
        )
        try:
            provider.open()
        except Exception:
            # provider.open() can establish a live connection/socket
            # internally and still raise later in its own handshake/init
            # sequence. On that path `self._provider` is never set, so
            # `__exit__` never runs (the `with` block's body never starts) and
            # the connection would otherwise leak. Close it here, best-effort,
            # before propagating the original error (CWE-772 Improper
            # Resource Shutdown or Release).
            try:
                provider.close()
            except Exception:  # pragma: no cover - best-effort close
                pass
            raise
        self._provider = provider
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        provider = self._provider
        self._provider = None
        if provider is not None:
            try:
                provider.close()
            except Exception:  # pragma: no cover - best-effort close
                pass

    # -- operations ---------------------------------------------

    def unwrap_dek(self, dek_kek_hex: str, kek_lmk_hex: str) -> bytes:
        """Unwrap a DEK via the configured HSM and return the clear DEK bytes.

        The returned value is used directly as the AES-256-GCM key
        (requirement §Background.2), so a provider must return raw key bytes
        rather than a hex string.
        """
        if self._provider is None:
            raise KeyServiceError(
                "HSMGateway.unwrap_dek called outside `with` block"
            )
        return self._provider.unwrap_dek(dek_kek_hex, kek_lmk_hex)
