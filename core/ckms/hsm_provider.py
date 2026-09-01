# SPDX-License-Identifier: Apache-2.0
# ============================================================
# core.ckms.hsm_provider — pluggable HSM backend contract
#
# WHY THIS EXISTS
# ---------------
# CKMS previously bound directly to one vendor's client library
# (`py-hsm-client`), which is not published on PyPI. That made HSM-backed key
# management structurally unavailable to anyone outside the originating
# organisation: the only way to use a different HSM was to patch core code.
#
# This module turns the HSM into a *configured capability*. The key-management
# logic (envelope encryption, DEK cache, boot sequence) is generic and stays in
# core; only the transport to a specific HSM is pluggable.
#
# SELECTING A PROVIDER
# --------------------
#   HSM_PROVIDER=py-hsm-client        # default -- the built-in vendor adapter
#   HSM_PROVIDER=mypkg.hsm:MyProvider # any dotted path to an HSMProvider
#
# The default is deliberately the previous behaviour, so nothing changes for an
# existing deployment unless it opts in.
#
# IMPLEMENTING A PROVIDER
# -----------------------
# Implement three methods (see HSMProvider below) and point HSM_PROVIDER at your
# class. You do not need to modify this repository:
#
#     class MyProvider:
#         def __init__(self, config_path=None): ...
#         def open(self) -> None: ...            # establish the connection
#         def close(self) -> None: ...           # best-effort teardown
#         def unwrap_dek(self, dek_kek_hex: str, kek_lmk_hex: str) -> bytes: ...
#
# Contract requirements:
#   * `unwrap_dek` returns the CLEAR DEK as raw bytes, suitable directly as an
#     AES-256-GCM key. Do not return hex.
#   * Every failure -- transport, malformed response, rejection -- must raise
#     KeyServiceError, so the boot sequence fails fast and uniformly.
#   * Never log, echo or include key material in an exception message.
# ============================================================

from __future__ import annotations

import os
from typing import Optional

from core.ckms.key_service import KeyServiceError

#: Env var selecting the backend. Default preserves prior behaviour exactly.
HSM_PROVIDER_ENV = "HSM_PROVIDER"
DEFAULT_PROVIDER = "py-hsm-client"

#: Env var for the backend's own config file, passed through unchanged.
HSM_CONFIG_ENV = "HSM_CONFIG_PATH"


class HSMProvider:
    """The contract a HSM backend must satisfy.

    Deliberately a plain base class rather than a ``typing.Protocol`` so that a
    provider can subclass it and inherit the ``NotImplementedError`` guards,
    and so ``isinstance`` checks remain available to operators debugging a
    misconfigured plugin.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def unwrap_dek(self, dek_kek_hex: str, kek_lmk_hex: str) -> bytes:
        raise NotImplementedError


class PyHsmClientProvider(HSMProvider):
    """Built-in adapter for the ``py-hsm-client`` library.

    Behaviour is intentionally byte-for-byte the same as the previous inline
    implementation in ``hsm_gateway``: same import points, same error
    translation, same handling of the ``outputformat: "text"`` response.

    The library is imported lazily inside each method so that test code (which
    substitutes the whole gateway) and any deployment using a different
    provider never require ``py-hsm-client`` to be installed. It is an optional
    extra (``pip install -e ".[hsm]"``) and is **not** published on PyPI.
    """

    name = "py-hsm-client"

    def __init__(self, config_path: Optional[str] = None):
        super().__init__(config_path)
        self._client = None

    def open(self) -> None:
        try:
            from py_hsm_client.core import get_client  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise KeyServiceError(
                "py-hsm-client is not installed; cannot unwrap non-BASE rows. "
                "Install the optional extra, or set HSM_PROVIDER to a provider "
                "for your own HSM."
            ) from exc

        try:
            self._client = get_client(config_path=self.config_path).__enter__()
        except FileNotFoundError as exc:
            raise KeyServiceError(
                f"HSM config file not found (HSM_CONFIG_PATH={self.config_path!r})"
            ) from exc
        except Exception as exc:  # py-hsm-client connect errors
            # The library aggregates per-node failures into the exception
            # message. We surface that opaquely; never log key material.
            raise KeyServiceError(f"HSM connect failed: {exc}") from exc

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.__exit__(None, None, None)
            except Exception:  # pragma: no cover - best-effort close
                pass

    def unwrap_dek(self, dek_kek_hex: str, kek_lmk_hex: str) -> bytes:
        if self._client is None:
            raise KeyServiceError("HSM provider used before open()")

        from py_hsm_client.command.decrypt_dek import DecryptDEK  # type: ignore
        from py_hsm_client.hsm_exceptions import (  # type: ignore
            HSMConnectionError,
            HSMInvalidResponse,
        )

        cmd = DecryptDEK(dek_kek_hex, kek_lmk_hex)
        try:
            raw = self._client.send(cmd.build())
            resp = cmd.parse_response(raw)
        except (HSMConnectionError, HSMInvalidResponse) as exc:
            raise KeyServiceError(f"HSM unwrap transport failure: {exc}") from exc

        if not resp.get("success"):
            status = resp.get("status", "??")
            raise KeyServiceError(f"HSM unwrap rejected: status={status}")

        clear_dek = resp.get("data")
        if not clear_dek:
            raise KeyServiceError("HSM unwrap returned empty data")

        # HSM is configured with outputformat: "text" — `data` is the clear DEK
        # as a 32-char ASCII alphanumeric string, NOT hex. Encode straight to
        # bytes (no hex decode) for use as the AES-256-GCM key.
        if isinstance(clear_dek, bytes):
            return clear_dek
        return clear_dek.encode("ascii")

#: Built-in providers, by short name.
_BUILTIN = {PyHsmClientProvider.name: PyHsmClientProvider}


def _load_dotted(spec: str):
    """Import ``module:ClassName`` (or ``module.ClassName``) and return it."""
    if ":" in spec:
        module_name, _, attr = spec.partition(":")
    else:
        module_name, _, attr = spec.rpartition(".")
    if not module_name or not attr:
        raise KeyServiceError(
            f"HSM_PROVIDER={spec!r} is not a known provider name or an "
            f"importable 'module:ClassName' path"
        )
    try:
        import importlib

        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise KeyServiceError(
            f"HSM_PROVIDER={spec!r}: cannot import {module_name!r}: {exc}"
        ) from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise KeyServiceError(
            f"HSM_PROVIDER={spec!r}: {module_name!r} has no attribute {attr!r}"
        ) from exc


def resolve_provider(config_path: Optional[str] = None,
                     provider: Optional[str] = None) -> HSMProvider:
    """Instantiate the configured provider.

    Resolution order: explicit ``provider`` argument, then ``HSM_PROVIDER``,
    then the built-in default. An unknown short name is an error rather than a
    silent fallback -- quietly using a different key backend than the operator
    asked for would be a security-relevant surprise.
    """
    spec = (provider or os.getenv(HSM_PROVIDER_ENV) or DEFAULT_PROVIDER).strip()
    cls = _BUILTIN.get(spec)
    if cls is None:
        if "." not in spec and ":" not in spec:
            raise KeyServiceError(
                "HSM_PROVIDER=%r is not a built-in provider (%s) and is not an "
                "importable path. Refusing to fall back to a different key "
                "backend than requested." % (spec, ", ".join(sorted(_BUILTIN)))
            )
        cls = _load_dotted(spec)

    instance = cls(config_path=config_path)
    for method in ("open", "close", "unwrap_dek"):
        if not callable(getattr(instance, method, None)):
            raise KeyServiceError(
                f"HSM_PROVIDER={spec!r} does not implement {method}() -- see "
                f"core/ckms/hsm_provider.py for the contract"
            )
    return instance
