# SPDX-License-Identifier: Apache-2.0
# ============================================================
# PRODUCTS ROUTER
#
# Access control — two layers:
#   Layer 1: Department   — user.department (from org_tree via JWT)
#   Layer 2: Product      — dept_product_mappings links products ↔ departments
#
# If dept_product_mappings.department == user.department → user sees the product.
# NO manual product_members / product_owners UUID lists.
# "Who has access" is always live from org_tree (dynamic, not a static list).
#
# Endpoints:
#   GET    /products                      — list (dept-scoped; admin sees all)
#   POST   /products                      — create (any user; level>3 → PENDING_APPROVAL)
#   GET    /products/pending              — pending approvals (level≤3 / admin)
#   GET    /products/departments          — distinct depts from org_tree
#   GET    /products/{id}                 — detail (dept-member only)
#   PATCH  /products/{id}                 — update URLs/departments (level≤3 in dept / admin)
#                                           departments change → PENDING_APPROVAL (4-eyes)
#   DELETE /products/{id}                 — soft-delete (level≤3 / admin)
#   POST   /products/{id}/approve         — approve pending (level≤3 / admin)
#   POST   /products/{id}/reject          — reject pending (level≤3 / admin)
#   POST   /products/{id}/repos           — add repo (level≤3 in dept / admin)
#   DELETE /products/{id}/repos/{name}    — remove repo (level≤3 in dept / admin)
#
# NOTE: /products/{id}/dept-mappings POST/DELETE are DISABLED (410).
#       Department changes must go through PATCH + approval workflow.
# ============================================================

import re as _re
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from auth.dependencies import get_current_user
from auth.rbac import can_approve
from core.logger import logger
from core.pii_crypto import encrypt_pii
from core.security_validation import (
    validate_create_product_request,
    validate_update_product_request,
    validate_add_repo_request,
    sanitize_input,
)

router = APIRouter(prefix="/products", tags=["products"])


def _parse_jira_key(url: str) -> Optional[str]:
    """Extract Jira project key from a URL or raw key string."""
    if not url:
        return None
    s = url.strip()
    # Already a raw project key (e.g. "RUPAY")
    if _re.match(r'^[A-Z][A-Z0-9_]{0,19}$', s, _re.I):
        return s.upper()
    # /projects/KEY (Jira Cloud)
    m = _re.search(r'/projects/([A-Z][A-Z0-9_]+)', s, _re.I)
    if m:
        return m.group(1).upper()
    # /browse/KEY or /browse/KEY-123
    m = _re.search(r'/browse/([A-Z][A-Z0-9_]+)(?:-\d+)?', s, _re.I)
    if m:
        return m.group(1).upper()
    return None


def _parse_confluence_space(url: str) -> Optional[str]:
    """Extract Confluence space key from a URL or raw key string."""
    if not url:
        return None
    s = url.strip()
    # Already a raw space key
    if _re.match(r'^~?[A-Z][A-Z0-9_]{0,19}$', s, _re.I):
        return s.upper()
    # /spaces/KEY
    m = _re.search(r'/spaces/(~?[A-Z][A-Z0-9_]+)', s, _re.I)
    if m:
        return m.group(1).upper()
    return None


def _notify_approvers_product(product_id: str, product_name: str,
                               submitter_email: str, departments: list) -> None:
    """Notify the recipients who should approve this product submission.

    Routing (mirrors budget-approval routing — auth.rbac.resolve_request_approvers):
      - the submitter's own HOD (users.hod_email), plus any delegatees that
        HOD has nominated (department_hod_mapping.delegated_to). A product
        may span several target departments the submitter has no reporting
        relationship with — approval authority is scoped to the submitter's
        own manager, not to every approver across the target departments.
      - falls back to every active admin/ad_level<=3 user (optionally
        narrowed to the target departments) when the submitter has no
        resolvable HOD, so a submission is never left unrouted.
    """
    try:
        from store.inbox_store import publish_inbox_item
        from db.database import SessionLocal
        from db.models import User
        from sqlalchemy import or_, func
        from datetime import datetime as _dtnow, timedelta as _tdnow
        from auth.rbac import resolve_request_approvers
        _ist_now = (_dtnow.utcnow() + _tdnow(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")

        approvers = resolve_request_approvers(submitter_email or "")
        hod_email = approvers.get("hod_email")
        delegatee_emails = approvers.get("delegatee_emails") or []
        recipient_emails = ([hod_email] if hod_email else []) + delegatee_emails

        db = SessionLocal()
        try:
            if recipient_emails:
                recipients = db.query(User).filter(
                    func.lower(User.email).in_([e.lower() for e in recipient_emails]),
                    User.is_active == True).all()
            else:
                # Fallback: no resolvable HOD — notify configurable approval level
                from core.config import APPROVAL_AD_LEVEL as _APPROVAL_LEVEL
                q = db.query(User).filter(
                    or_(User.ad_level <= _APPROVAL_LEVEL, User.role == "admin"),
                    User.is_active == True,
                )
                if departments:
                    q = q.filter(User.department.in_(departments))
                recipients = q.all()
            for u in recipients:
                publish_inbox_item(
                    user_id   = str(u.id),
                    type      = "product_approval",
                    title     = f"[Product] New product pending: {product_name}",
                    body      = f"**{submitter_email}** submitted product **{product_name}** for approval on {_ist_now}.",
                    source_id = product_id,
                    metadata  = {
                        "entity_type":  "product",
                        "product_id":   product_id,
                        "product_name": product_name,
                        "submitted_by": submitter_email,
                        "action":       "submit",
                        "departments":  departments or [],
                        "hod_email":    hod_email,
                        "delegatee_emails": delegatee_emails,
                    },
                )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"ProductsRouter: failed to notify approvers for {product_name}: {e}")


def _notify_submitter_product(product_id: str, product_name: str,
                               submitter_email: str, approved: bool,
                               note: str = "", reviewer_email: str = "") -> None:
    """Notify the submitter when their product is approved or rejected."""
    try:
        from store.inbox_store import publish_inbox_item
        from db.database import SessionLocal
        from db.models import User
        from datetime import datetime as _dt, timedelta as _td
        _ist = (_dt.utcnow() + _td(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")
        _reviewer_str = f" by `{reviewer_email}`" if reviewer_email else ""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == submitter_email).first()
            if not user:
                return
            action = "approved" if approved else "rejected"
            _body = f"Your product **{product_name}** was **{action}**{_reviewer_str} on {_ist}."
            if not approved and note:
                _body += f"\n\n**Reason:** {note}"
            publish_inbox_item(
                user_id   = str(user.id),
                type      = "product_result",
                title     = f"[Product] {product_name} {action}",
                body      = _body,
                source_id = product_id,
                metadata  = {
                    "entity_type":   "product",
                    "product_id":    product_id,
                    "product_name":  product_name,
                    "action":        action,
                    "reviewed_by":   reviewer_email,
                },
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"ProductsRouter: failed to notify submitter for {product_name}: {e}")


# ── helpers ───────────────────────────────────────────────────

def _product_or_404(db, product_id: str):
    from db.models import Product
    p = db.query(Product).filter(Product.id == product_id, Product.is_active == True).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    return p


def _get_product_depts(db, product_id: str) -> List[str]:
    from db.models import DeptProductMapping
    return [
        d.department for d in
        db.query(DeptProductMapping).filter(DeptProductMapping.product_id == product_id).all()
    ]


def _require_dept_approver_or_admin(db, current_user: dict, product_id: str):
    """Level≤3 user whose department is mapped to this product, or admin."""
    from auth.rbac import is_admin
    if is_admin(current_user):
        return
    if not can_approve(current_user):
        raise HTTPException(403, "Approver (ad_level ≤ 3) required")
    user_dept = current_user.get("department", "")
    depts = _get_product_depts(db, product_id)
    if user_dept not in depts:
        raise HTTPException(403, "You are not in a department mapped to this product")


def _resolve_email(db, user_id: str) -> str:
    from sqlalchemy import text
    try:
        row = db.execute(
            text("SELECT email FROM users WHERE id::text = :uid"),
            {"uid": str(user_id)},
        ).fetchone()
        return row[0] if row else user_id
    except Exception:
        return user_id


# ── request models ────────────────────────────────────────────

class RepoEntry(BaseModel):
    repo_name: str
    branch:    str = "main"


class CreateProductRequest(BaseModel):
    name:             str
    code:             str
    description:      Optional[str] = None
    jira_url:         Optional[str] = None
    confluence_url:   Optional[str] = None
    departments:      List[str] = []
    repos:            List[RepoEntry] = []


class UpdateProductRequest(BaseModel):
    name:             Optional[str] = None
    description:      Optional[str] = None
    jira_url:         Optional[str] = None
    confluence_url:   Optional[str] = None
    departments:      Optional[List[str]] = None  # if set → product back to PENDING_APPROVAL


class AddRepoRequest(BaseModel):
    repo_name: str
    branch:    str = "main"


class AddDeptMappingRequest(BaseModel):
    department: str


# ── LIST — dept-scoped ────────────────────────────────────────

@router.get("")
def list_products(current_user: dict = Depends(get_current_user)):
    from db.database import SessionLocal
    from db.models import Product, DeptProductMapping
    from auth.rbac import is_admin
    from sqlalchemy import or_, and_

    db = SessionLocal()
    try:
        q = db.query(Product).filter(Product.is_active == True)

        if not is_admin(current_user):
            user_dept  = current_user.get("department", "")
            caller_sub = current_user.get("sub", "")
            if not user_dept and not caller_sub:
                return {"total": 0, "products": []}

            # Active products in user's department
            dept_product_ids = [
                r.product_id for r in
                db.query(DeptProductMapping.product_id).filter(
                    DeptProductMapping.department == user_dept
                ).all()
            ] if user_dept else []

            # User sees: ACTIVE products in their dept  OR  their own PENDING/REJECTED submissions
            q = q.filter(
                or_(
                    and_(Product.status == "ACTIVE", Product.id.in_(dept_product_ids)),
                    and_(Product.status.in_(["PENDING_APPROVAL", "REJECTED"]), Product.created_by == caller_sub),
                )
            )
        else:
            # Admin sees everything (ACTIVE + PENDING + REJECTED)
            q = q.filter(Product.status.in_(["ACTIVE", "PENDING_APPROVAL", "REJECTED"]))

        products = q.order_by(Product.name).all()
        return {
            "total": len(products),
            "products": [
                {
                    "id":               str(p.id),
                    "name":             p.name,
                    "code":             p.code,
                    "status":           p.status,
                    "description":      p.description,
                    "jira_project_key": p.jira_project_key,
                    "confluence_space": p.confluence_space,
                    "jira_url":         p.jira_url,
                    "confluence_url":   p.confluence_url,
                    "created_by":       _resolve_email(db, p.created_by),
                    "created_at":       p.created_at.isoformat() if p.created_at else None,
                    "requested_by":     p.requested_by or "",
                    "review_note":      p.review_note or "",
                    "reviewed_by":      p.reviewed_by or "",
                }
                for p in products
            ],
        }
    finally:
        db.close()


# ── CREATE ────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_product(
    body: CreateProductRequest,
    current_user: dict = Depends(get_current_user),
):
    from db.database import SessionLocal
    from db.models import Product, DeptProductMapping
    from auth.rbac import is_admin

    # Validate all inputs
    is_valid, field_errors, sanitized = validate_create_product_request(body)
    if not is_valid:
        # Flatten errors into a single message
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(400, "; ".join(error_messages))

    code = sanitized["code"]

    # 4-eyes principle: only admin auto-approves; everyone else (including approvers) → PENDING
    _status = "ACTIVE" if is_admin(current_user) else "PENDING_APPROVAL"

    db = SessionLocal()
    try:
        from db.models import Product as P
        if db.query(P).filter(P.code == code, P.is_active == True).first():
            raise HTTPException(409, f"Product code '{code}' already exists")
        if db.query(P).filter(P.name == sanitized["name"], P.is_active == True).first():
            raise HTTPException(409, f"Product name '{sanitized['name']}' already exists")

        product = Product(
            name             = sanitized["name"],
            code             = code,
            description      = sanitized["description"],
            jira_url         = sanitized["jira_url"],
            jira_project_key = _parse_jira_key(sanitized["jira_url"]),
            confluence_url   = sanitized["confluence_url"],
            confluence_space = _parse_confluence_space(sanitized["confluence_url"]),
            created_by       = current_user["sub"],
            requested_by     = current_user.get("email") or current_user["sub"],
            status           = _status,
        )
        db.add(product)
        db.flush()

        for dept in sanitized["departments"]:
            db.add(DeptProductMapping(product_id=product.id, department=dept))

        for r in sanitized["repos"]:
            if r["repo_name"]:
                from db.models import ProductRepo
                db.add(ProductRepo(
                    product_id=product.id,
                    repo_name=r["repo_name"],
                    branch=r["branch"],
                    added_by=current_user["sub"],
                ))

        db.commit()
        db.refresh(product)
        product_id  = str(product.id)
        product_name = product.name
        result = {
            "id":          product_id,
            "name":        product_name,
            "code":        product.code,
            "status":      product.status,
            "departments": sanitized["departments"],
        }
    finally:
        db.close()

    if _status == "PENDING_APPROVAL":
        _notify_approvers_product(
            product_id     = product_id,
            product_name   = product_name,
            submitter_email= current_user.get("email", ""),
            departments    = sanitized["departments"],
        )

    return result


# ── PENDING (approver inbox) ──────────────────────────────────

@router.get("/pending")
def list_pending_products(current_user: dict = Depends(get_current_user)):
    from db.database import SessionLocal
    from db.models import Product, DeptProductMapping
    from auth.rbac import is_admin

    if not is_admin(current_user) and not can_approve(current_user):
        raise HTTPException(403, "Approver access required (ad_level ≤ 3)")

    caller_sub = current_user.get("sub", "")
    user_dept  = current_user.get("department", "")

    db = SessionLocal()
    try:
        q = db.query(Product).filter(Product.is_active == True, Product.status == "PENDING_APPROVAL")

        if not is_admin(current_user):
            # Approver only sees pending products in their own department,
            # and never their own submissions (4-eyes: cannot approve what you created)
            dept_product_ids = [
                r.product_id for r in
                db.query(DeptProductMapping.product_id).filter(
                    DeptProductMapping.department == user_dept
                ).all()
            ] if user_dept else []

            q = q.filter(
                Product.id.in_(dept_product_ids),
                Product.created_by != caller_sub,   # self-approval blocked here too
            )

        products = q.order_by(Product.created_at.desc()).all()
        return {
            "products": [
                {
                    "id":           str(p.id),
                    "name":         p.name,
                    "code":         p.code,
                    "description":  p.description,
                    "requested_by": p.requested_by,
                    "created_at":   p.created_at.isoformat() if p.created_at else None,
                }
                for p in products
            ]
        }
    finally:
        db.close()


# ── DEPARTMENTS (from org_tree) ───────────────────────────────

@router.get("/departments")
def list_departments(current_user: dict = Depends(get_current_user)):
    from db.database import SessionLocal
    from db.models import OrgTree

    db = SessionLocal()
    try:
        depts = (
            db.query(OrgTree.department)
            .distinct()
            .filter(OrgTree.department.isnot(None))
            .all()
        )
        return {"departments": sorted([d[0] for d in depts])}
    finally:
        db.close()


# ── APPROVE / REJECT ──────────────────────────────────────────

@router.post("/{product_id}/approve")
def approve_product(product_id: str, current_user: dict = Depends(get_current_user)):
    """Approve a PENDING_APPROVAL product.

    Authorised: admin, OR the submitter's own HOD, OR one of the HOD's
    nominated delegatees. A product may span several target departments the
    submitter has no reporting relationship with, so approval authority is
    scoped to the submitter's own manager (not to every approver across the
    target departments) — unlike the plain department-membership gate used
    elsewhere in this router.
    """
    from db.database import SessionLocal
    from auth.rbac import is_admin, is_request_approver
    from datetime import datetime

    caller_sub   = current_user.get("sub", "")
    caller_email = current_user.get("email") or caller_sub

    db = SessionLocal()
    try:
        p = _product_or_404(db, product_id)

        if not (is_admin(current_user) or is_request_approver(current_user, p.requested_by or "")):
            raise HTTPException(403, "Only the submitter's HOD (or their delegate) or an admin can approve products")

        # 4-eyes: cannot approve your own submission
        if p.created_by == caller_sub or p.requested_by == caller_email:
            raise HTTPException(403, "Cannot approve your own product submission (4-eyes principle)")

        if p.status != "PENDING_APPROVAL":
            raise HTTPException(400, f"Product is already '{p.status}'")

        p.status      = "ACTIVE"
        p.reviewed_by = caller_email
        p.reviewed_at = datetime.utcnow()
        submitter     = p.requested_by or ""
        product_name  = p.name
        db.commit()
    finally:
        db.close()

    _notify_submitter_product(product_id, product_name, submitter, approved=True, reviewer_email=caller_email)
    return {"id": product_id, "status": "ACTIVE"}


@router.post("/{product_id}/reject")
def reject_product(
    product_id: str,
    note: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Reject a PENDING_APPROVAL product.

    Authorised: admin, OR the submitter's own HOD, OR one of the HOD's
    nominated delegatees (see approve_product for the rationale).
    """
    from db.database import SessionLocal
    from auth.rbac import is_admin, is_request_approver
    from datetime import datetime

    caller_sub   = current_user.get("sub", "")
    caller_email = current_user.get("email") or caller_sub

    db = SessionLocal()
    try:
        p = _product_or_404(db, product_id)

        if not (is_admin(current_user) or is_request_approver(current_user, p.requested_by or "")):
            raise HTTPException(403, "Only the submitter's HOD (or their delegate) or an admin can reject products")

        # 4-eyes: cannot reject your own submission
        if p.created_by == caller_sub or p.requested_by == caller_email:
            raise HTTPException(403, "Cannot reject your own product submission")

        p.status      = "REJECTED"
        p.reviewed_by = caller_email
        p.reviewed_at = datetime.utcnow()
        p.review_note = note or ""
        submitter     = p.requested_by or ""
        product_name  = p.name
        db.commit()
    finally:
        db.close()

    _notify_submitter_product(product_id, product_name, submitter, approved=False, note=note or "", reviewer_email=caller_email)
    return {"id": product_id, "status": "REJECTED"}


# ── GET DETAIL ────────────────────────────────────────────────

@router.get("/{product_id}")
def get_product(product_id: str, current_user: dict = Depends(get_current_user)):
    from db.database import SessionLocal
    from db.models import DeptProductMapping, OrgTree
    from auth.rbac import is_admin

    db = SessionLocal()
    try:
        p = _product_or_404(db, product_id)

        # Access check: user's dept must be in the product's dept mappings (or admin)
        depts = _get_product_depts(db, product_id)
        if not is_admin(current_user) and current_user.get("department") not in depts:
            raise HTTPException(403, "Your department does not have access to this product")

        # People with access — live from org_tree for all mapped departments
        # Grouped by department, ordered by level (most senior first)
        people = (
            db.query(OrgTree)
            .filter(OrgTree.department.in_(depts), OrgTree.mail.isnot(None))
            .order_by(OrgTree.department, OrgTree.level, OrgTree.display_name)
            .all()
        )

        from core.config import APPROVAL_AD_LEVEL as _APPROVAL_LEVEL
        people_list = [
            {
                "name":       encrypt_pii(o.display_name),
                "email":      encrypt_pii(o.mail),
                "level":      o.level,
                "title":      o.title,
                "department": o.department,
                "can_approve": o.level <= _APPROVAL_LEVEL,
            }
            for o in people
        ]

        # Repos: merge product_repos (explicit links) + index_requests (indexed repos for this product)
        from db.models import IndexRequest
        repo_map = {}
        for r in p.repos:
            repo_map[r.repo_name] = {"repo_name": r.repo_name, "branch": r.branch, "source": "linked"}
        indexed = (
            db.query(IndexRequest)
            .filter(IndexRequest.product_id == product_id, IndexRequest.status == "done")
            .all()
        )
        for ir in indexed:
            if ir.repo_name not in repo_map:
                repo_map[ir.repo_name] = {"repo_name": ir.repo_name, "branch": ir.branch, "source": "indexed"}
        repos_list = list(repo_map.values())

        return {
            "id":               str(p.id),
            "name":             p.name,
            "code":             p.code,
            "description":      p.description,
            "jira_project_key": p.jira_project_key,
            "confluence_space": p.confluence_space,
            "jira_url":         p.jira_url,
            "confluence_url":   p.confluence_url,
            "status":           p.status,
            "departments":      depts,
            "created_by":       _resolve_email(db, p.created_by),
            "requested_by":     p.requested_by,
            "created_at":       p.created_at.isoformat() if p.created_at else None,
            "repos":            repos_list,
            "people":           people_list,   # replaces owners+members — live from org_tree
        }
    finally:
        db.close()


# ── UPDATE (level≤3 in dept / admin) ─────────────────────────

@router.patch("/{product_id}")
def update_product(
    product_id: str,
    body: UpdateProductRequest,
    current_user: dict = Depends(get_current_user),
):
    from db.database import SessionLocal
    from db.models import DeptProductMapping

    # Validate all inputs
    is_valid, field_errors, sanitized = validate_update_product_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(400, "; ".join(error_messages))

    db = SessionLocal()
    try:
        _require_dept_approver_or_admin(db, current_user, product_id)
        p = _product_or_404(db, product_id)

        if "name" in sanitized: p.name = sanitized["name"]
        if "description" in sanitized: p.description = sanitized["description"]
        if "jira_url" in sanitized:
            p.jira_url         = sanitized["jira_url"]
            p.jira_project_key = _parse_jira_key(sanitized["jira_url"])
        if "confluence_url" in sanitized:
            p.confluence_url   = sanitized["confluence_url"]
            p.confluence_space = _parse_confluence_space(sanitized["confluence_url"])

        # Department changes are structural — require re-approval (4-eyes)
        dept_changed = False
        if "departments" in sanitized:
            new_depts = sanitized["departments"]
            db.query(DeptProductMapping).filter(
                DeptProductMapping.product_id == product_id
            ).delete()
            for dept in new_depts:
                db.add(DeptProductMapping(product_id=product_id, department=dept))
            p.status      = "PENDING_APPROVAL"
            p.reviewed_by = None
            p.reviewed_at = None
            p.review_note = None
            dept_changed  = True

        product_name   = p.name
        submitter_email = current_user.get("email", "")
        db.commit()
    finally:
        db.close()

    if dept_changed:
        _notify_approvers_product(
            product_id      = product_id,
            product_name    = product_name,
            submitter_email = submitter_email,
            departments     = sanitized["departments"],
        )
        return {"id": product_id, "status": "PENDING_APPROVAL",
                "message": "Department change submitted for approval"}

    return {"id": product_id, "name": product_name}


# ── DELETE (level≤3 in dept / admin) ─────────────────────────

@router.delete("/{product_id}", status_code=204)
def delete_product(product_id: str, current_user: dict = Depends(get_current_user)):
    from db.database import SessionLocal

    db = SessionLocal()
    try:
        _require_dept_approver_or_admin(db, current_user, product_id)
        p = _product_or_404(db, product_id)
        p.is_active = False
        db.commit()
    finally:
        db.close()


# ── REPOS ─────────────────────────────────────────────────────

@router.post("/{product_id}/repos", status_code=201)
def add_repo(
    product_id: str,
    body: AddRepoRequest,
    current_user: dict = Depends(get_current_user),
):
    from db.database import SessionLocal
    from db.models import ProductRepo

    # Validate input
    is_valid, errors, sanitized = validate_add_repo_request(body)
    if not is_valid:
        raise HTTPException(400, "; ".join(errors))

    db = SessionLocal()
    try:
        _require_dept_approver_or_admin(db, current_user, product_id)
        _product_or_404(db, product_id)

        existing = (
            db.query(ProductRepo)
            .filter(ProductRepo.product_id == product_id, ProductRepo.repo_name == sanitized["repo_name"])
            .first()
        )
        if existing:
            raise HTTPException(409, "Repo already linked to this product")

        db.add(ProductRepo(
            product_id=product_id,
            repo_name=sanitized["repo_name"],
            branch=sanitized["branch"],
            added_by=current_user["sub"],
        ))
        db.commit()
        return {"product_id": product_id, "repo_name": sanitized["repo_name"], "branch": sanitized["branch"]}
    finally:
        db.close()


# ── DEPT MAPPINGS ─────────────────────────────────────────────

@router.get("/{product_id}/dept-mappings")
def list_dept_mappings(product_id: str, current_user: dict = Depends(get_current_user)):
    """List department mappings for a product."""
    from db.database import SessionLocal
    from db.models import DeptProductMapping
    db = SessionLocal()
    try:
        rows = db.query(DeptProductMapping).filter(DeptProductMapping.product_id == product_id).all()
        return {"departments": [r.department for r in rows]}
    finally:
        db.close()


@router.post("/{product_id}/dept-mappings")
def add_dept_mapping(product_id: str, body: AddDeptMappingRequest, current_user: dict = Depends(get_current_user)):
    """Disabled — department changes must go through PATCH /products/{id} → approval flow."""
    raise HTTPException(
        status_code=410,
        detail="Direct department mapping is disabled. Edit the product via PATCH /products/{id} "
               "with a 'departments' list — this triggers the approval workflow.",
    )


@router.delete("/{product_id}/dept-mappings/{department}")
def remove_dept_mapping(product_id: str, department: str, current_user: dict = Depends(get_current_user)):
    """Disabled — department changes must go through PATCH /products/{id} → approval flow."""
    raise HTTPException(
        status_code=410,
        detail="Direct department mapping is disabled. Edit the product via PATCH /products/{id} "
               "with a 'departments' list — this triggers the approval workflow.",
    )


@router.delete("/{product_id}/repos/{repo_name}", status_code=204)
def remove_repo(
    product_id: str,
    repo_name: str,
    current_user: dict = Depends(get_current_user),
):
    from db.database import SessionLocal
    from db.models import ProductRepo

    db = SessionLocal()
    try:
        _require_dept_approver_or_admin(db, current_user, product_id)

        row = (
            db.query(ProductRepo)
            .filter(ProductRepo.product_id == product_id, ProductRepo.repo_name == repo_name)
            .first()
        )
        if not row:
            raise HTTPException(404, "Repo not found")
        db.delete(row)
        db.commit()
    finally:
        db.close()
