# SPDX-License-Identifier: MIT
# ============================================================
# core.ckms — Centralized Key Management Service (CKMS)
#
# Public surface for the rest of the application. Everything else
# in this package is implementation detail.
#
# Usage:
#     from core.ckms import load_at_boot, KeyService, KeyServiceError
#
#     load_at_boot()                                  # call once, very early
#     plaintext = KeyService.instance().decrypt_env("FERNET_KEY")
#
# See hsm_client_integration_requirement.md and docs/py_hsm_client_SPEC.md
# ============================================================

from core.ckms.bootstrap import load_at_boot
from core.ckms.key_service import KeyService, KeyServiceError

__all__ = ["load_at_boot", "KeyService", "KeyServiceError"]
