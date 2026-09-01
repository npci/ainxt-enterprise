# SPDX-License-Identifier: Apache-2.0
# ============================================================
# RAG ACCESS CONTROL  (P0-NEW-1 through P0-NEW-4)
# ============================================================
#
# Document-level sensitivity classification + per-chunk access
# filtering + immutable RAG audit log.
#
# Classification tiers (ascending sensitivity):
#   PUBLIC          — open to all authenticated users
#   INTERNAL        — default; all registered employees
#   CONFIDENTIAL    — role-restricted (operator+ or explicit allow-list)
#   RESTRICTED      — explicit user/role allow-list only
#   PCI_SENSITIVE   — PCI/DSS scoped; security + admin only
#
# Every retrieval call must go through check_rag_access().
# The audit log (rag_access_log table) is immutable and never deleted.
# ============================================================

import hashlib
from typing import Optional

from core.logger import logger

# Roles ordered by privilege level (higher index = more access)
# "user" is the current default role (same privilege as legacy "viewer")
_ROLE_LEVEL = {
    "user":      0,   # current default role — same as viewer
    "viewer":    0,
    "developer": 1,
    "operator":  2,
    "security":  3,
    "admin":     4,
}

# Minimum role required for each classification tier
# "code" and "documentation" are used by the index worker for source code / docs — treat as INTERNAL
_MIN_ROLE: dict = {
    "PUBLIC":           "viewer",
    "INTERNAL":         "viewer",
    "CODE":             "viewer",    # source code chunks indexed from repos
    "DOCUMENTATION":    "viewer",    # documentation chunks
    "CONFIDENTIAL":     "operator",
    "RESTRICTED":       "security",  # must also be in allowed_users/roles
    "PCI_SENSITIVE":    "security",
}


def _role_ge(user_role: str, required_role: str) -> bool:
    """Return True if user_role meets or exceeds required_role."""
    return _ROLE_LEVEL.get(user_role, -1) >= _ROLE_LEVEL.get(required_role, 99)


def check_rag_access(
    user_id: str,
    user_role: str,
    org_id: str,
    chunk_metadata: dict,
    session_id: str = "",
    query_text: str = "",
    user_product_ids: list = None,
) -> tuple[bool, str]:
    """
    Decide whether a user may receive a specific RAG chunk.

    Parameters
    ----------
    user_id        Authenticated user ID.
    user_role      Role from JWT ('viewer' … 'admin').
    org_id         Organisation ID from JWT.
    chunk_metadata Dict with keys: classification, org_id, owner_team,
                   allowed_roles (list), allowed_users (list),
                   chunk_id, repo, file_path, uploaded_by.
    session_id     Current session ID (for audit log correlation).
    query_text     Original query text (hashed for audit log).

    Returns
    -------
    (True, "")          if access is granted
    (False, reason_str) if access is denied
    """
    classification  = (chunk_metadata.get("classification") or "INTERNAL").upper()
    chunk_org_id    = chunk_metadata.get("org_id") or ""
    allowed_roles   = chunk_metadata.get("allowed_roles") or []
    allowed_users   = chunk_metadata.get("allowed_users") or []
    chunk_id        = str(chunk_metadata.get("chunk_id") or chunk_metadata.get("id") or "")
    repo            = chunk_metadata.get("repo") or ""
    file_path       = chunk_metadata.get("file_path") or ""

    # 0. Product-scoped isolation — if chunk has a product_id, user must be a member
    chunk_product_id = chunk_metadata.get("product_id") or ""
    if chunk_product_id and user_role != "admin":
        member_product_ids = list(user_product_ids or [])
        if chunk_product_id not in member_product_ids:
            reason = f"product_isolation: chunk_product_id={chunk_product_id} not in user products"
            _write_audit_log(
                user_id, user_role, org_id, query_text,
                chunk_id=str(chunk_metadata.get("chunk_id") or chunk_metadata.get("id") or ""),
                repo=chunk_metadata.get("repo") or "",
                file_path=chunk_metadata.get("file_path") or "",
                classification=(chunk_metadata.get("classification") or "INTERNAL").upper(),
                granted=False, deny_reason=reason, session_id=session_id,
            )
            return False, reason

    # 1. Org isolation — users only see docs from their own org
    #    (empty org_id on chunk means "shared platform docs", always accessible)
    #
    # ARCH-F-CORE-008: the old condition had an empty-string bypass — if
    # org_id was "" (unauthenticated or unscoped user), the whole expression
    # evaluated to False and access was granted to any org-scoped document.
    # These three explicit branches make every deny case auditable.
    _org_denied = False
    _org_deny_reason = ""
    if not chunk_org_id:
        pass  # shared platform doc — always accessible
    elif not org_id:
        _org_denied = True
        _org_deny_reason = "org_isolation_no_user_org"
    elif chunk_org_id != org_id:
        _org_denied = True
        _org_deny_reason = "org_isolation"

    if _org_denied:
        reason = f"{_org_deny_reason}: user_org={org_id} chunk_org={chunk_org_id}"
        _write_audit_log(
            user_id, user_role, org_id, query_text,
            chunk_id, repo, file_path, classification,
            granted=False, deny_reason=reason, session_id=session_id,
        )
        return False, reason

    # 2. Classification tier minimum role check
    # Unknown classification defaults to INTERNAL semantics (all employees), not security
    min_role = _MIN_ROLE.get(classification, "viewer")
    if not _role_ge(user_role, min_role):
        reason = f"classification={classification} requires role>={min_role}, user_role={user_role}"
        _write_audit_log(
            user_id, user_role, org_id, query_text,
            chunk_id, repo, file_path, classification,
            granted=False, deny_reason=reason, session_id=session_id,
        )
        return False, reason

    # 3. Explicit allow-list for RESTRICTED / PCI_SENSITIVE
    if classification in ("RESTRICTED", "PCI_SENSITIVE"):
        in_role_list = any(_role_ge(user_role, r) for r in allowed_roles)
        in_user_list = user_id in allowed_users
        # admin always gets through
        if user_role != "admin" and not in_role_list and not in_user_list:
            reason = (
                f"classification={classification}: user not in allowed_roles "
                f"{allowed_roles} or allowed_users"
            )
            _write_audit_log(
                user_id, user_role, org_id, query_text,
                chunk_id, repo, file_path, classification,
                granted=False, deny_reason=reason, session_id=session_id,
            )
            return False, reason

    # ✅ Access granted
    _write_audit_log(
        user_id, user_role, org_id, query_text,
        chunk_id, repo, file_path, classification,
        granted=True, deny_reason="", session_id=session_id,
    )
    return True, ""


def _write_audit_log(
    user_id: str,
    user_role: str,
    org_id: str,
    query_text: str,
    chunk_id: str,
    repo: str,
    file_path: str,
    classification: str,
    granted: bool,
    deny_reason: str,
    session_id: str = "",
) -> None:
    """
    Write an immutable entry to rag_access_log.
    Errors are logged but never raised — audit failures must not block retrieval.
    """
    try:
        from db.database import SessionLocal
        from db.models import RAGAccessLog
        query_hash = hashlib.sha256(query_text.encode()).hexdigest()[:64]
        db = SessionLocal()
        try:
            entry = RAGAccessLog(
                user_id=user_id,
                user_role=user_role,
                org_id=org_id,
                query_hash=query_hash,
                chunk_id=chunk_id,
                repo=repo,
                file_path=file_path,
                classification=classification,
                access_granted=granted,
                deny_reason=deny_reason[:500] if deny_reason else "",
                session_id=session_id,
            )
            db.add(entry)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        # Audit log failure must never block the retrieval path
        logger.error(f"rag_acl: audit log write failed: {e}")


def filter_chunks_by_acl(
    chunks: list,
    user_id: str,
    user_role: str,
    org_id: str,
    session_id: str = "",
    query_text: str = "",
    user_product_ids: list = None,
) -> list:
    """
    Filter a list of chunk dicts, keeping only those the user may see.
    Each chunk dict must have a 'metadata' sub-dict with classification
    and permission fields as stored in DocumentEmbedding.

    Returns the filtered list (denied chunks silently removed).
    """
    allowed = []
    for chunk in chunks:
        meta = chunk.get("metadata") or chunk  # support flat and nested
        granted, _ = check_rag_access(
            user_id=user_id,
            user_role=user_role,
            org_id=org_id,
            chunk_metadata=meta,
            session_id=session_id,
            query_text=query_text,
            user_product_ids=user_product_ids,
        )
        if granted:
            allowed.append(chunk)
    return allowed
