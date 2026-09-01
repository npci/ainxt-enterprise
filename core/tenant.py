# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MULTI-TENANT — org_id helpers
# ============================================================

DEFAULT_ORG = "default"


def get_org_id(user: dict) -> str:
    """Extract org_id from a user dict. Falls back to DEFAULT_ORG."""
    return user.get("org_id") or DEFAULT_ORG
