# SPDX-License-Identifier: Apache-2.0
# ============================================================
# core.ckms.bootstrap_dek
#
# Resolves the *bootstrap* DEK from environment variables.
#
# Why this module exists
# ----------------------
# CKMS normally fetches DEKs from ``ainxt.keys_table`` — but that read
# requires a working DB connection, which in turn requires a decrypted
# ``POSTGRES_PASSWORD``. Classic chicken-and-egg.
#
# To break it, ops provisions a *dedicated* DEK in the process environment
# that is used **only** to decrypt DB-connectivity env vars (Postgres +
# pgvector). Once the DB is reachable, the normal keys_table-driven flow
# takes over for everything else.
#
# Two env vars are honoured (mirror of ``keys_table.dek`` / ``keys_table.kek``
# so the dev/staging vs. prod story is identical):
#
#   CKMS_BOOTSTRAP_DEK   (required if any DB-connectivity var is ENC:-prefixed)
#       - "BASE:<b64-of-32-char-DEK>"   → dev / phased rollout, no HSM call.
#       - "<DEK_KEK_hex>"               → prod, HSM-wrapped. Needs CKMS_BOOTSTRAP_KEK.
#
#   CKMS_BOOTSTRAP_KEK   (required only when CKMS_BOOTSTRAP_DEK is the
#                         HSM-wrapped form). Holds the KEK_LMK hex.
#
# If neither env var is set AND no DB-connectivity var is ENC:-prefixed,
# this module is a silent no-op (back-compat with legacy plaintext .env).
# ============================================================

from __future__ import annotations

import base64
import binascii
import os
from typing import Optional

from core.ckms.key_service import KeyServiceError

ENV_BOOTSTRAP_DEK = "CKMS_BOOTSTRAP_DEK"
ENV_BOOTSTRAP_KEK = "CKMS_BOOTSTRAP_KEK"

_BASE_PREFIX = "BASE:"


def resolve_bootstrap_dek() -> Optional[bytes]:
    """Return the 32-byte clear bootstrap DEK, or ``None`` if not configured.

    Raises ``KeyServiceError`` if the env vars are present but malformed
    (e.g. ``BASE:`` body is not valid base64, or HSM-wrapped form is set
    without ``CKMS_BOOTSTRAP_KEK``, or the HSM call rejects the unwrap).
    """
    dek_env = os.environ.get(ENV_BOOTSTRAP_DEK, "").strip()
    if not dek_env:
        return None

    # ── BASE: dev / phased-rollout form ──────────────────────────
    if dek_env.startswith(_BASE_PREFIX):
        try:
            return base64.b64decode(dek_env[len(_BASE_PREFIX):], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyServiceError(
                f"{ENV_BOOTSTRAP_DEK} is BASE:-prefixed but the base64 payload "
                f"is invalid"
            ) from exc

    # ── HSM-wrapped form ────────────────────────────────────────
    kek_env = os.environ.get(ENV_BOOTSTRAP_KEK, "").strip()
    if not kek_env:
        raise KeyServiceError(
            f"{ENV_BOOTSTRAP_DEK} is HSM-wrapped (no BASE: prefix) but "
            f"{ENV_BOOTSTRAP_KEK} is not set"
        )

    # Local import keeps py-hsm-client out of the dev import path.
    from core.ckms.hsm_gateway import HSMGateway

    try:
        with HSMGateway() as gw:
            return gw.unwrap_dek(dek_env, kek_env)
    except KeyServiceError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise KeyServiceError(
            f"bootstrap DEK unwrap via HSM failed: {exc}"
        ) from exc
