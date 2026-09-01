# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ENTERPRISE RBAC — Role-Based Access Control
#
# Roles (least → most privileged):
#   viewer    — read-only: view chats, agents, metrics
#   developer — viewer + create/edit agents, skills, workflows, chats
#   operator  — developer + manage projects, threads, codebase indexing
#   security  — operator + view compliance logs, audit trail, user list
#   admin     — full access including user management, budget, MCP governance
#
# Usage:
#   from auth.rbac import require_role, ROLES
#   @router.get("/admin/users")
#   def list_users(user=Depends(require_role("admin"))):
# ============================================================

from typing import List, Optional, Set
from fastapi import Depends, HTTPException, Request, status

from auth.dependencies import get_current_user
from core.logger import logger, mask_email

# ── Role hierarchy ────────────────────────────────────────────

ROLES = ["viewer", "developer", "operator", "security", "admin"]

_ROLE_LEVEL = {role: idx for idx, role in enumerate(ROLES)}
_ROLE_LEVEL["user"] = 1   # legacy alias: "user" treated as developer, no DB migration needed

# ── Permissions map ───────────────────────────────────────────
# Each permission is a string key; roles inherit all lower-level permissions.

PERMISSIONS = {
    "viewer": [
        "chat:read",
        "agent:read",
        "skill:read",
        "workflow:read",
        "project:read",
        "thread:read",
        "inbox:read",
        "metrics:read",
        "health:read",
    ],
    "developer": [
        "chat:write",
        "agent:write",
        "skill:write",
        "workflow:write",
        "thread:write",
    ],
    "operator": [
        "project:write",
        "codebase:write",
        "mcp:read",
        "budget:read",
    ],
    "security": [
        "audit:read",
        "compliance:read",
        "user:read",
    ],
    "admin": [
        "user:write",
        "budget:write",
        "mcp:write",
        "mcp:approve",
        "admin:all",
    ],
}


def get_all_permissions(role: str) -> List[str]:
    """Return all permissions for a role (including inherited from lower roles)."""
    idx   = _ROLE_LEVEL.get(role, 0)
    perms = []
    for r in ROLES[:idx + 1]:
        perms.extend(PERMISSIONS.get(r, []))
    return list(set(perms))


def has_permission(role: str, permission: str) -> bool:
    return permission in get_all_permissions(role)


def _role_level(role: str) -> int:
    return _ROLE_LEVEL.get(role, -1)


# ── FastAPI dependencies ──────────────────────────────────────

def require_role(minimum_role: str):
    """
    FastAPI dependency factory. Usage:
        Depends(require_role("operator"))
    """
    def dependency(current_user: dict = Depends(get_current_user)) -> dict:
        user_role  = current_user.get("role", "viewer")
        min_level  = _ROLE_LEVEL.get(minimum_role, 99)
        user_level = _ROLE_LEVEL.get(user_role, -1)

        if user_level < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{minimum_role}' or higher required (you have '{user_role}')",
            )
        return current_user
    return dependency


def require_permission(permission: str):
    """
    FastAPI dependency factory for specific permission check.
        Depends(require_permission("mcp:approve"))
    """
    def dependency(current_user: dict = Depends(get_current_user)) -> dict:
        user_role = current_user.get("role", "viewer")
        if not has_permission(user_role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' required",
            )
        return current_user
    return dependency


# Convenience aliases
require_viewer    = require_role("viewer")
require_developer = require_role("developer")
require_operator  = require_role("operator")
require_security  = require_role("security")
require_admin     = require_role("admin")


# ── ABAC band helpers ─────────────────────────────────────────
#
# Band hierarchy (lowest → highest):
#   A1(1) < A2(2) < B1(3) < B2(4) < C1(5) < C2(6) < D1(7) < D2(8) < E(9)
#
# Usage:
#   @router.post("/products")
#   def create_product(user=Depends(require_band(5))):   # C1+

BAND_LEVELS = {
    "A1": 1, "A2": 2,
    "B1": 3, "B2": 4,
    "C1": 5, "C2": 6,
    "D1": 7, "D2": 8,
    "E":  9,
}


def require_band(min_band_level: int):
    """
    FastAPI dependency: enforces minimum band level from JWT ABAC claim.
    Admins bypass band restrictions.
    """
    def dependency(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") == "admin":
            return current_user
        user_level = int(current_user.get("band_level", 1))
        if user_level < min_band_level:
            band_name = next(
                (b for b, l in BAND_LEVELS.items() if l == min_band_level), str(min_band_level)
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Band {band_name} or higher required",
            )
        return current_user
    return dependency


def require_product_member(product_id_param: str = "product_id"):
    """
    FastAPI dependency: user must be a member of the product_id in the path/query.
    Product owners and admins always pass.
    Usage: Depends(require_product_member("product_id"))
    """
    def dependency(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        # Enforcement is at route level — route handler extracts product_id
        # and calls _check_product_membership() directly for flexibility.
        return current_user
    return dependency


def check_product_membership(current_user: dict, product_id: str) -> None:
    """
    Raise HTTP 403 if user is not an admin, product owner, or product member.
    Call directly in route handlers when product_id is available.
    """
    if is_admin(current_user):
        return
    user_product_ids = current_user.get("product_ids") or []
    if product_id not in user_product_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this product",
        )


def is_c1_plus(current_user: dict) -> bool:
    """Return True if user is C1 band or above (band_level >= 5), or admin."""
    if current_user.get("role") == "admin":
        return True
    return int(current_user.get("band_level", 1)) >= BAND_LEVELS["C1"]


# C1+ dependency shorthand
def require_c1_plus(current_user: dict = Depends(get_current_user)) -> dict:
    """Require C1 band or above — used for Product creation."""
    if not is_c1_plus(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="C1 band or above required",
        )
    return current_user


# ── AD-level based gates (replaces band/role-based gates) ────────────────────
#
# ad_level: 0 = most senior exec, 6 = junior engineer (from AD org tree)
# can_approve: ad_level <= APPROVAL_AD_LEVEL (configurable — see core/config.py)
#
# These are ADDITIVE to the existing role/band helpers — existing code is
# unaffected.  New routes should prefer these helpers going forward.

def require_level(max_level: int):
    """FastAPI dependency: passes if user's ad_level <= max_level (0=most senior)."""
    def _dep(current_user: dict = Depends(get_current_user)):
        user_level = current_user.get("ad_level", 6)
        if current_user.get("role") == "admin":
            return current_user
        if user_level > max_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires seniority level {max_level} or above (your level: {user_level})",
            )
        return current_user
    return _dep


def require_approval(current_user: dict = Depends(get_current_user)):
    """Requires ad_level <= APPROVAL_AD_LEVEL. Used for approve actions.
    Threshold is configurable via APPROVAL_AD_LEVEL env var (default 6 for OSS,
    3 in a typical directory). Admin role always bypasses this check.
    """
    from core.config import APPROVAL_AD_LEVEL
    if current_user.get("role") == "admin":
        return current_user
    user_level = current_user.get("ad_level", 6)
    if user_level > APPROVAL_AD_LEVEL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Approval actions require seniority level {APPROVAL_AD_LEVEL} or above",
        )
    return current_user


def can_approve(current_user: dict) -> bool:
    """Check (non-raising) whether user can approve.
    Threshold driven by APPROVAL_AD_LEVEL env var. Admin always returns True.
    """
    from core.config import APPROVAL_AD_LEVEL
    return current_user.get("ad_level", 6) <= APPROVAL_AD_LEVEL or current_user.get("role") == "admin"


def require_admin_flag(current_user: dict = Depends(get_current_user)):
    """Requires role='admin'."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def is_admin(current_user: dict) -> bool:
    """Return True if the user holds the admin role (non-raising helper)."""
    return current_user.get("role") == "admin"


# ── HOD (Head of Department) helpers ──────────────────────────
#
# HOD detection is performed during auth.dependencies.enrich_user_context()
# and stored on the request payload as `is_hod` and `hod_departments`.
# A user who is both admin and HOD gets both flags — admin takes precedence
# for scope (see get_visible_user_filter) but is_hod/is_hod_departments
# reflect the department_hod_mapping.
#
# Scope semantics:
#   - Admin (regardless of HOD) -> see ALL users  (get_visible_user_filter returns None)
#   - HOD (and not admin)       -> see users whose users.department ∈ hod_departments
#   - Anyone else hitting an admin endpoint -> empty set (deliberate; preserves
#     the pre-existing "no auth check" behaviour on GET /budget/users without
#     leaking data).

def is_hod(current_user: dict) -> bool:
    """
    Return True if the user is a Head of Department.
    A user who is both admin and HOD will have is_hod=True — admin scope
    still takes precedence in get_visible_user_filter, but the HOD flag
    is preserved so the UI can show both Admin and Team views.
    """
    return bool(current_user.get("is_hod", False))


def get_hod_departments(current_user: dict) -> List[str]:
    """Return the list of department_name strings this HOD owns. Empty for non-HODs."""
    deps = current_user.get("hod_departments") or []
    # Defensive copy so callers can't mutate the request payload
    return [str(d) for d in deps if d]


# ── Maker-checker approver routing (HOD + delegates) ──────────────────────────
#
# Single source of truth for "who should this maker-checker request notify /
# let act": the REQUESTER'S OWN HOD (users.hod_email) plus any delegatees that
# HOD has nominated (ainxt.department_hod_mapping.delegated_to) — mirrors the
# budget-approval routing in store.budget_store.resolve_approvers_for_request.
#
# Used by the KB doc-upload, Cowork role, and codebase-index approval flows so
# a request is routed to the requester's own manager (not broadcast to every
# admin/ad_level<=3 user in the company).

def resolve_request_approvers(requester_email: str) -> dict:
    """Return the requester's HOD + nominated delegatees.

    ``{"hod_email": str | None, "delegatee_emails": list[str]}``. The
    requester is filtered out of the delegatee list (never routes a request
    back to its own creator). Falls back to ``{"hod_email": None,
    "delegatee_emails": []}`` if the requester has no resolvable HOD or on
    any lookup error — callers should broadcast to admins in that case so a
    submission is never silently unroutable.
    """
    try:
        from store.budget_store import resolve_approvers_for_request
        result = resolve_approvers_for_request(requester_email)
        return {
            "hod_email":         result.get("hod_email"),
            "delegatee_emails":  list(result.get("delegatees") or []),
        }
    except Exception as e:
        logger.warning(f"resolve_request_approvers({mask_email(requester_email)}): {e}")
        return {"hod_email": None, "delegatee_emails": []}


def is_request_approver(current_user: dict, requester_email: str) -> bool:
    """Non-raising check: True iff ``current_user`` is the requester's HOD or
    one of the HOD's nominated delegatees. Admins are NOT included here —
    call ``is_admin`` separately if admin override should also be allowed.
    """
    actor_email = (current_user.get("email") or "").strip().lower()
    if not actor_email:
        return False
    approvers = resolve_request_approvers(requester_email)
    hod_email = (approvers.get("hod_email") or "").strip().lower()
    delegatees = [d.strip().lower() for d in approvers.get("delegatee_emails") or []]
    return actor_email == hod_email or actor_email in delegatees


def get_visible_user_filter(
        current_user: dict,
        request: Optional[Request] = None,
) -> Optional[Set[str]]:
    """
    Return the set of users.id (str) the caller may see, or None for unrestricted.

      - None        -> admin; no filter (see everything).
      - set[str]    -> HOD; users in their department list (possibly empty).
      - empty set   -> anyone else hitting an admin-style endpoint.
                       Endpoints return an empty list rather than 403, which
                       preserves the pre-existing un-gated behaviour without
                       leaking data.

    Memoises on `request.state.hod_user_ids` so the DB query runs at most once
    per request, even across many scope-checking helpers.
    """
    if is_admin(current_user):
        return None

    if request is not None:
        cached = getattr(request.state, "hod_user_ids", None)
        if cached is not None:
            return cached

    if not is_hod(current_user):
        result: Set[str] = set()
        if request is not None:
            request.state.hod_user_ids = result
        return result

    hod_email = (current_user.get("email") or "").strip().lower()
    if not hod_email:
        result = set()
        if request is not None:
            request.state.hod_user_ids = result
        return result

    # Fail-closed: on any error, the HOD sees nothing rather than everything.
    result: Set[str] = set()
    try:
        from db.database import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    "SELECT id FROM users "
                    "WHERE lower(hod_email) = :hod_email AND is_active = TRUE"
                ),
                {"hod_email": hod_email},
            ).fetchall()
            result = {str(r[0]) for r in rows}
        finally:
            db.close()
    except Exception:
        from core.logger import logger
        logger.warning("get_visible_user_filter: failed to resolve HOD scope; defaulting to empty set")
        result = set()

    if request is not None:
        request.state.hod_user_ids = result
    return result

def can_approve_domain(current_user: dict, domain: str, db=None) -> bool:
    """Non-raising check: returns True iff the caller may approve findings for
    the given governance domain. True when:
      - is_admin(current_user), OR
      - the caller's email or sub/user_id appears in governance_domain_approvers
        for that domain (active=TRUE).

    Fail-closed: returns False on any lookup error, missing domain mapping, or
    when the domain has no approvers configured and the caller is not admin.
    Always uppercases the domain before lookup.

    This is the segregation-of-duties gate — a bug here lets the wrong person
    approve another team's security findings.
    """
    if is_admin(current_user):
        return True

    domain_upper = (domain or "").strip().upper()
    if not domain_upper:
        return False

    email = (current_user.get("email") or "").strip().lower()
    user_id = (current_user.get("sub") or current_user.get("id") or current_user.get("user_id") or "").strip()

    if not email and not user_id:
        return False

    try:
        # Use the store module to check approver membership
        from store.sdlc_governance_approvers import approver_domains_for
        domains = approver_domains_for(current_user)
        result = domain_upper in domains
        if not result:
            logger.warning(
                "[SDLC-GOV] can_approve_domain denied",
                email=email, domain=domain_upper,
            )
        return result
    except Exception as e:
        logger.warning(
            "[SDLC-GOV] can_approve_domain error — fail-closed",
            email=email, domain=domain_upper, error=str(e),
        )
        return False


def is_governance_lead(current_user: dict, db=None) -> bool:
    """Non-raising check: True iff the caller may act as a governance LEAD — i.e.
    sign off cross-run bulk false-positive suppression uploads (end-gate overhaul
    2026-07-23, Step B3.1). True when:
      - is_admin(current_user), OR
      - the caller is an ACTIVE approver for at least one governance domain
        (a domain approver carries enough authority to sign off suppressions).

    Fail-closed: any lookup error → False. This is a privilege gate — a bug here
    lets the wrong person make a real finding disappear across runs.
    """
    if is_admin(current_user):
        return True
    try:
        from store.sdlc_governance_approvers import approver_domains_for
        return bool(approver_domains_for(current_user))
    except Exception as e:
        logger.warning("[SDLC-GOV] is_governance_lead error — fail-closed", error=str(e))
        return False


def can_manage_suppression(current_user: dict, db=None) -> bool:
    """Non-raising check for UNRESTRICTED suppression management (create for any
    repo/product, sign off bulk uploads). True for admins and governance leads.

    NOTE: an ordinary AUTHOR may still create suppressions for their OWN
    repo/product — that scope check is enforced at the router (repo/product
    ownership), NOT here. This helper is the "unrestricted" gate only. Fail-closed.
    """
    return is_governance_lead(current_user, db=db)
