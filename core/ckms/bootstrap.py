# SPDX-License-Identifier: MIT
# ============================================================
# core.ckms.bootstrap — one-shot, fail-fast boot sequence
#
# Implements the 5-step sequence in
# hsm_client_integration_requirement.md §"Boot Sequence":
#
#   1. Load active rows from keys_table.
#   2. Load key_type_mapping.
#   3. Unwrap (or base64-decode) each clear DEK; cache in KeyService.
#   4. AES-GCM-decrypt every protected env var; write plaintext back
#      to os.environ so downstream os.getenv() calls keep working.
#   5. Mark KeyService.loaded = True (idempotent re-entry returns early).
#
# Any failure is fatal: log one structured line (no key material) and
# exit with SystemExit(1).
#
# NOTE on os.environ mutation: requirement §4 says "no os.environ
# mutation"; that constraint is relaxed here intentionally so the
# ~50 downstream readers that already call os.getenv() keep working
# unchanged. KeyService.decrypt_env() remains the canonical pattern
# for new code. Decision documented in the integration plan.
# ============================================================

from __future__ import annotations

import base64
import binascii
import os
import sys
from typing import Dict, Iterable, Tuple

from core.ckms.hsm_gateway import HSMGateway
from core.ckms.key_service import KeyService, KeyServiceError

# ------------------------------------------------------------------
# Protected env-var inventory (Tier 1 + Tier 2 from the requirement).
#
# Every variable listed here MUST be present in os.environ at boot as
# AES-256-GCM ciphertext. The default key_type is KEY_CREDS; the
# right-hand value here is informational only — actual key_type
# resolution comes from ainxt.key_type_mapping at runtime.
# ------------------------------------------------------------------
PROTECTED_ENV_VARS: Tuple[str, ...] = (
    # Tier 1 — master keys
    "FERNET_KEY",
    "VAULT_ENCRYPTION_KEY",
    "LOGIN_ENCRYPT_KEY",
    "AUDIT_SIGNING_KEY",
    "JWT_SECRET",
    "SECRET_KEY",
    # Tier 1 — database credentials
    "POSTGRES_PASSWORD",
    "POSTGRES_READ_PASSWORD",
    "POSTGRES_MIGRATE_PASSWORD",
    "PGVECTOR_PASSWORD",
    "PGVECTOR_READ_PASSWORD",
    "REDIS_PASSWORD",
    # Tier 1 — directory / mail
    "LDAP_BIND_PASSWORD",
    "AINXT_SMTP_PASSWORD",
    # Tier 1 — LLM / embedding provider keys
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "NOMIC_EMBED_API_KEY",
    # Tier 1 — source-control PATs
    "GITLAB_TOKEN",
    "GITHUB_TOKEN",
    "ADMIN_GIT_TOKEN",
    # Tier 1 — Atlassian / OAuth
    "JIRA_API_TOKEN",
    "CONFLUENCE_API_TOKEN",
    "KEYCLOAK_CLIENT_SECRET",
    "AZURE_AD_CLIENT_SECRET",
    "ZOHO_CLIENT_SECRET",
    "ZOHO_REFRESH_TOKEN",
    # Tier 1 — webhook signing secrets
    "JIRA_WEBHOOK_SECRET",
    "GITLAB_WEBHOOK_SECRET",
    "SLACK_SIGNING_SECRET",
    # Tier 1 — chat-platform bot tokens
    "SLACK_BOT_TOKEN",
    "TEAMS_BOT_SECRET",
    "WHATSAPP_ACCESS_TOKEN",
    # Tier 1 — inter-service bearers
    "LLM_PROXY_TOKEN",
    "PLATFORM_SERVICE_TOKEN",
    "ORG_SYNC_TOKEN",
    # Tier 1 — object store
    "MINIO_SECRET_KEY",
    "MINIO_ACCESS_KEY",
    # Tier 1 — code-scan / automation
    "SONAR_TOKEN",
    "CHECKMARX_CLIENT_SECRET",
    "N8N_API_KEY",
    # Tier 2 — recommended
    "ZOHO_ACCESS_TOKEN",
    "SEED_ADMIN_PASSWORD",
    "SEED_USER_PASSWORD",
    "SANDBOX_MAVEN_REPO_PWD",
    "AINXT_PASS",
    "PRESENTON_PASSWORD",
    "AINXT_TOKEN",
    "LOCAL_LLM_API_KEY",
    "LITELLM_API_KEY",
    # Tier 1 — enterprise spend tracking (added 2026-06-17)
    # Admin-scoped provider keys used only by services/llm_spend/fetchers/*.
    # Storage / wire format identical to the other LLM provider keys above.
    "OPENAI_ADMIN_API_KEY",
    "ANTHROPIC_ADMIN_API_KEY",
    "GCP_BILLING_SA_JSON",
    # Tier 1 — FIMI provider keys (added 2026-08-14)
    "FIMI_OPENAI_API_KEY",
    "FIMI_ANTHROPIC_API_KEY",
)

_BASE_PREFIX = "BASE:"

# Env-var values must be prefixed with this string to be treated as
# AES-GCM ciphertext. Plain values (no prefix) pass through unchanged —
# this is the backward-compatibility escape hatch so a partially-rolled-out
# .env (some vars encrypted, some still plaintext) keeps working.
# Wire shape after the prefix is:  <b64(iv)>:<b64(ciphertext||gcm_tag)>
_ENC_PREFIX = "ENC:"

# ------------------------------------------------------------------
# DB-connectivity env vars — decrypted in Step 0 using the env-sourced
# bootstrap DEK, BEFORE we try to read keys_table (which itself needs
# POSTGRES_PASSWORD). Chicken-and-egg breaker.
# ------------------------------------------------------------------
DB_BOOTSTRAP_ENV_VARS: Tuple[str, ...] = (
    "POSTGRES_PASSWORD",
    "POSTGRES_READ_PASSWORD",
    "POSTGRES_MIGRATE_PASSWORD",
    "PGVECTOR_PASSWORD",
    "PGVECTOR_READ_PASSWORD",
)


def _logger():
    """Lazy logger import so this module can be imported very early in boot."""
    try:
        from core.logger import logger  # type: ignore

        return logger
    except Exception:  # pragma: no cover - fallback if logging not ready
        import logging

        return logging.getLogger("ckms.bootstrap")


def _resolve_clear_dek(key_name: str, dek: str, kek: str, gw: HSMGateway) -> bytes:
    """Step 3 inner: decode a BASE row, else call HSM."""
    if dek.startswith(_BASE_PREFIX):
        try:
            return base64.b64decode(dek[len(_BASE_PREFIX):], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyServiceError(
                f"keys_table.dek for key_type={key_name} is BASE:-prefixed "
                f"but the base64 payload is invalid"
            ) from exc

    # Non-BASE rows require HSM.
    if not kek:
        raise KeyServiceError(
            f"keys_table.kek is empty for key_type={key_name} (HSM unwrap requires KEK_LMK)"
        )
    return gw.unwrap_dek(dek, kek)


def _build_cache(rows: Iterable, gw: HSMGateway) -> Dict[str, bytes]:
    cache: Dict[str, bytes] = {}
    for row in rows:
        cache[row.key_name] = _resolve_clear_dek(row.key_name, row.dek, row.kek, gw)
    return cache


def _bootstrap_decrypt_db_vars() -> int:
    """Step 0 — decrypt DB-connectivity env vars with the bootstrap DEK.

    Runs BEFORE keys_table is read, so the DB driver in core.ckms.repository
    has a usable password to dial Postgres with.

    Behaviour:
      - If no DB-connectivity var is ENC:-prefixed → no-op (returns 0).
      - If any is ENC:-prefixed → bootstrap DEK MUST be set in env;
        decrypt each ENC: var and write plaintext back to os.environ.
      - Non-ENC (legacy plaintext) vars pass through untouched — same
        contract as the main Step 4 loop.

    Returns the number of vars decrypted (for the boot log).
    Raises KeyServiceError on any failure (caller exits non-zero).
    """
    # Local import: keeps the public ``core.ckms`` import surface narrow
    # and avoids a hard dependency at module load time.
    from core.ckms.bootstrap_dek import (
        ENV_BOOTSTRAP_DEK,
        resolve_bootstrap_dek,
    )
    from core.ckms.crypto import aes_gcm_decrypt

    # Cheap pre-scan — if no DB var is ENC:-prefixed, the bootstrap DEK is
    # not required (legacy plaintext deployments keep working).
    enc_vars = [
        v for v in DB_BOOTSTRAP_ENV_VARS
        if (os.environ.get(v) or "").startswith(_ENC_PREFIX)
    ]
    if not enc_vars:
        return 0

    dek = resolve_bootstrap_dek()
    if dek is None:
        raise KeyServiceError(
            f"{ENV_BOOTSTRAP_DEK} is not set, but at least one DB-connectivity "
            f"env var is ENC:-prefixed (count={len(enc_vars)}). Provision the "
            f"bootstrap DEK in the process environment or revert those vars "
            f"to legacy plaintext."
        )

    for env_var in enc_vars:
        ct = os.environ[env_var][len(_ENC_PREFIX):]
        try:
            pt = aes_gcm_decrypt(ct, dek)
        except Exception as exc:
            raise KeyServiceError(
                f"bootstrap AES-GCM decryption failed for env_var={env_var}"
            ) from exc
        os.environ[env_var] = pt

    return len(enc_vars)


def load_at_boot() -> None:
    """Run the CKMS boot sequence exactly once for this process.

    Safe to call multiple times — subsequent calls are no-ops.

    When ``CKMS_ENABLED=false`` (OSS / plaintext mode): the boot sequence is
    skipped entirely. ``KeyService`` is marked as loaded so all downstream
    callers that check ``svc.loaded`` continue to work. Secrets are read
    directly from environment variables as plaintext — no HSM, no
    ``keys_table``, no CKMS service is required.

    When ``CKMS_ENABLED=true`` (HSM mode): the full 5-step sequence
    runs. Any failure is fatal: log one structured line and ``SystemExit(1)``
    (fail-fast, no partial / degraded boot — requirement §"Failure Policy").
    """
    svc = KeyService.instance()
    if svc.loaded:
        return

    # ── OSS / plaintext mode ──────────────────────────────────────────────
    # When CKMS is disabled, skip the entire HSM/DB boot sequence.
    # Install an empty cache so KeyService.loaded becomes True and all
    # downstream svc.loaded checks pass. All os.getenv() calls will read
    # plaintext values directly from the .env file — no decryption needed.
    ckms_enabled = os.getenv("CKMS_ENABLED", "false").lower() == "true"
    if not ckms_enabled:
        log = _logger()
        svc.install(cache={}, mapping={})
        log.info(
            "ckms.load_at_boot.skipped",
            reason="CKMS_ENABLED=false — plaintext env-var mode active",
        )
        return

    log = _logger()

    try:
        # Step 0 — decrypt DB-connectivity env vars with the env-sourced
        # bootstrap DEK. This MUST happen BEFORE core.ckms.repository is
        # imported, because that import path will eventually open a
        # SQLAlchemy engine that reads POSTGRES_PASSWORD.
        db_bootstrap_count = _bootstrap_decrypt_db_vars()

        # Late imports keep `from core.ckms import load_at_boot` cheap and
        # avoid pulling SQLAlchemy in until the first real call.
        from core.ckms.repository import load_active_keys, load_env_var_mapping

        # Step 1 + 2 — DB reads.
        active_rows = load_active_keys()
        if not active_rows:
            raise KeyServiceError("keys_table has no active rows (status='A')")

        mapping = load_env_var_mapping()

        # Step 3 — unwrap / decode. Reuse one HSM TCP connection only if at
        # least one row actually needs HSM (avoids opening a connection in
        # all-BASE dev environments).
        needs_hsm = any(not r.dek.startswith(_BASE_PREFIX) for r in active_rows)
        if needs_hsm:
            with HSMGateway() as gw:
                cache = _build_cache(active_rows, gw)
        else:
            # No HSM call — pass a never-used sentinel via a stub object that
            # raises if its unwrap_dek is invoked.
            cache = _build_cache(active_rows, _NoopGateway())

        # Install into the singleton BEFORE step 4 — decrypt_env() needs it.
        svc.install(cache=cache, mapping=mapping)

        # Step 4 — decrypt every protected env var and write plaintext back.
        #
        # Backward-compatibility contract:
        #   - Value missing            → skip silently (optional integration).
        #   - Value starts with ENC:   → strip prefix, AES-GCM decrypt.
        #   - Value has no ENC: prefix → leave as-is (legacy plaintext).
        #
        # This lets ops roll out ciphertext one variable at a time without
        # a flag-day. A typo in a non-prefixed value cannot brick the boot —
        # it will fail later at its consumer (Postgres reject, JWT verify,
        # provider 401, …) with a clear, narrow error.
        loaded_count = 0
        plaintext_count = 0
        for env_var in PROTECTED_ENV_VARS:
            raw = os.environ.get(env_var)
            if raw is None:
                continue
            if not raw.startswith(_ENC_PREFIX):
                # Legacy plaintext — leave untouched. No log line per var to
                # avoid leaking the inventory of un-rotated secrets.
                plaintext_count += 1
                continue
            ct = raw[len(_ENC_PREFIX):]
            try:
                pt = svc.decrypt(env_var, ct)
            except Exception as exc:
                raise KeyServiceError(
                    f"AES-GCM decryption failed for env_var={env_var}"
                ) from exc
            os.environ[env_var] = pt
            loaded_count += 1

        log.info(
            "ckms.load_at_boot.ok",
            key_types=sorted(cache.keys()),
            db_bootstrap_decrypted=db_bootstrap_count,
            decrypted_env_vars=loaded_count,
            plaintext_env_vars=plaintext_count,
            mapping_size=len(mapping),
        )

    except KeyServiceError:
        log.error("ckms.load_at_boot.failed")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception:  # last-resort safety net
        log.error("ckms.load_at_boot.unexpected")
        sys.exit(1)


class _NoopGateway:
    """Stand-in used when no row needs HSM. Raises if accidentally called."""

    def unwrap_dek(self, dek_kek_hex: str, kek_lmk_hex: str) -> bytes:
        raise KeyServiceError(
            "internal error: HSM unwrap requested in BASE-only boot path"
        )
