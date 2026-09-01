# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BROADCAST ROUTER — Allowlist-gated email broadcast feature
#
# All endpoints under /broadcast require:
#   1. require_broadcast_user  (JWT email ∈ BROADCAST_ALLOWED_EMAILS)
#   2. enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)
#
# Allowlist is configured in the backend .env:
#   BROADCAST_ALLOWED_EMAILS=alice@example.com,bob@example.com
# Case-insensitive, comma-separated. If the var is empty/unset, access is
# denied for everyone (fail-closed).
#
# Endpoints:
#   GET    /broadcast/access                 — is the caller on the allowlist?
#   POST   /broadcast/templates/suggest      — LLM-generated HTML template
#   POST   /broadcast/preview                — render {{name}} with sample
#   GET    /broadcast/departments            — distinct User.department values
#   POST   /broadcast/recipients/resolve     — preview the targeted audience
#   POST   /broadcast/attachments            — upload one file
#   DELETE /broadcast/attachments/{id}       — remove an uploaded file
#   POST   /broadcast/send                   — enqueue broadcast for sending
#   GET    /broadcast                        — list past broadcasts
#   GET    /broadcast/{id}                   — broadcast detail
#   GET    /broadcast/{id}/recipients        — paginated recipient list
#   POST   /broadcast/{id}/cancel            — stop in-flight broadcast
# ============================================================

from __future__ import annotations

import html as _html
import os
import re
import uuid as _uuid_mod
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import and_ as _sa_and, func as _sa_func, or_ as _sa_or
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user
from core.file_validator import validate_upload
from core.logger import logger
from core.pii_crypto import encrypt_pii, decrypt_pii
from core.rate_limiter import SENSITIVE_ADMIN, enforce_rate_limit_with_behaviour
from core.security_validation import validate_broadcast_send_request, _flatten_errors
from db.database import get_db
from workers.broadcast_worker import submit_broadcast_recipient


router = APIRouter(prefix="/broadcast", tags=["broadcast"])


# ── Configuration ─────────────────────────────────────────────────────────────

_BROADCAST_ATTACHMENT_DIR = os.getenv(
    "BROADCAST_ATTACHMENT_DIR",
    "/var/lib/ainxt/broadcast_attachments",
)
_MAX_RECIPIENTS_PER_SEND = int(
    os.getenv("BROADCAST_MAX_RECIPIENTS_PER_SEND", "5000")
)
_ATTACHMENT_ALLOWED_EXTENSIONS = frozenset({
    "pdf", "docx", "png", "jpg", "jpeg", "txt", "csv", "xlsx",
})
_ATTACHMENT_MAX_SIZE_BYTES = 25 * 1024 * 1024   # 25 MB
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _load_allowed_emails() -> frozenset[str]:
    """Parse BROADCAST_ALLOWED_EMAILS from the environment.

    Read at call time (not import time) so ops can rotate the allowlist via
    a process restart without touching code. Returns a lowercased frozenset
    for O(1) membership checks. An empty/missing var → empty set → access
    denied to everyone (fail-closed).
    """
    raw = os.getenv("BROADCAST_ALLOWED_EMAILS", "") or ""
    return frozenset(
        part.strip().lower()
        for part in raw.split(",")
        if part.strip()
    )


def _is_broadcast_allowed(user: dict) -> bool:
    """Non-raising allowlist check used by the /access endpoint and the UI gate."""
    email = (user.get("email") or "").strip().lower()
    if not email:
        return False
    return email in _load_allowed_emails()


def require_broadcast_user(current_user: dict = Depends(get_current_user)) -> dict:
    """Authorise the caller against the BROADCAST_ALLOWED_EMAILS allowlist.

    Replaces the previous admin-role guard. Configure recipients in .env:
        BROADCAST_ALLOWED_EMAILS=alice@x.com,bob@y.com
    """
    if not _is_broadcast_allowed(current_user):
        raise HTTPException(
            status_code=403,
            detail="Email broadcast access is restricted to allow-listed users.",
        )
    return current_user


def _ensure_attachment_dir() -> str:
    """Create BROADCAST_ATTACHMENT_DIR with mode 0700 on first use."""
    try:
        os.makedirs(_BROADCAST_ATTACHMENT_DIR, mode=0o700, exist_ok=True)
        # Best-effort chmod for the case where the directory pre-existed
        # with looser permissions.
        try:
            os.chmod(_BROADCAST_ATTACHMENT_DIR, 0o700)
        except Exception:
            pass
    except Exception as exc:
        logger.error(
            f"broadcast_router: cannot create attachment dir "
            f"{_BROADCAST_ATTACHMENT_DIR!r}: {exc}"
        )
        raise HTTPException(500, "Attachment storage is not available")
    return _BROADCAST_ATTACHMENT_DIR


# ── Pydantic models ───────────────────────────────────────────────────────────

class _TemplateSuggestReq(BaseModel):
    intent: str = Field(..., min_length=3, max_length=4000)
    tone:   Optional[str] = Field(None, max_length=60)


class _TemplateSuggestRes(BaseModel):
    html:  str
    model: str


class _PreviewReq(BaseModel):
    html:        str = Field(..., min_length=1, max_length=200_000)
    sample_name: str = Field("Priyadharshan", min_length=1, max_length=120)
    enrich_name: bool = True


class _PreviewRes(BaseModel):
    html: str


class _ResolveTargetingReq(BaseModel):
    all:           bool          = False
    departments:   List[str]     = Field(default_factory=list)
    max_ad_level:  Optional[int] = None
    user_ids:      List[str]     = Field(default_factory=list)
    emails:        List[str]     = Field(default_factory=list)


class _ResolveSampleRow(BaseModel):
    user_id:    Optional[str] = None
    email:      str
    name:       Optional[str] = None
    department: Optional[str] = None
    ad_level:   Optional[int] = None


class _ResolveRes(BaseModel):
    count:  int
    sample: List[_ResolveSampleRow]


class _SendReq(BaseModel):
    subject:        str  = Field(..., min_length=1, max_length=998)
    html_body:      str  = Field(..., min_length=1, max_length=500_000)
    text_body:      Optional[str] = Field(None, max_length=200_000)
    enrich_name:    bool = False
    targeting:      _ResolveTargetingReq
    attachment_ids: List[str] = Field(default_factory=list)


class _SendRes(BaseModel):
    broadcast_id: str
    total_count:  int


class _AttachmentRes(BaseModel):
    id:         str
    filename:   str
    size_bytes: int
    mimetype:   str


class _BroadcastSummary(BaseModel):
    id:                 str
    subject:            str
    status:             str
    total_count:        int
    success_count:      int
    failure_count:      int
    enrich_name:        bool
    compliance_blocked: bool
    model_used:         Optional[str]
    created_at:         str
    created_by_email:   Optional[str] = None


class _BroadcastDetail(_BroadcastSummary):
    html_body:      str
    text_body:      Optional[str]
    targeting_json: Dict[str, Any]
    failed_sample:  List[Dict[str, Any]]


class _RecipientRow(BaseModel):
    id:         str
    email:      str
    name:       Optional[str] = None
    status:     str
    error_text: Optional[str] = None
    sent_at:    Optional[str] = None


class _RecipientPage(BaseModel):
    total: int
    items: List[_RecipientRow]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compliance_check(text: str, *, label: str) -> None:
    """Raise HTTP 400 (with detail) if compliance blocks the text."""
    try:
        from agents.compliance_engine import compliance_engine as _ce
        result = _ce.validate_input(text or "")
    except Exception as exc:
        logger.warning(f"broadcast_router: compliance check failed ({label}, fail-open): {exc}")
        return
    if result.get("blocked"):
        raise HTTPException(
            status_code=400,
            detail={
                "error":         "compliance_blocked",
                "label":         label,
                "blocked_types": result.get("blocked_types") or [],
            },
        )


def _audit(
    db: Session,
    *,
    broadcast_id: Optional[str],
    actor: dict,
    action: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one row to email_broadcast_audit_log. Never raises."""
    from db.models import EmailBroadcastAuditLog
    try:
        db.add(EmailBroadcastAuditLog(
            broadcast_id=broadcast_id,
            actor_user_id=actor.get("sub") or actor.get("user_id"),
            actor_email=(actor.get("email") or "").lower() or None,
            action=action,
            detail_json=detail or {},
        ))
        db.commit()
    except Exception as exc:
        logger.warning(f"broadcast_router: audit write failed action={action}: {exc}")
        try:
            db.rollback()
        except Exception:
            pass


def _resolve_recipients_query(db: Session, targeting: _ResolveTargetingReq):
    """
    Build a query against the `users` table that returns all matching active
    users for the given targeting payload. Uses ONLY the User table (no
    org_tree join). Returns (User_query, raw_email_set).
    """
    from db.models import User
    q = db.query(
        User.id, User.email, User.name, User.department, User.ad_level,
    ).filter(User.is_active == True)  # noqa: E712

    if targeting.all:
        return q, set()

    # Department + AD-level form a single "filter group" combined with AND
    # (e.g. dept = Engineering AND ad_level ≤ 3). Individuals (user_ids / emails)
    # are always additive (OR) on top of that group.
    group_conditions = []

    if targeting.departments:
        deps = [d for d in targeting.departments if d and isinstance(d, str)]
        if deps:
            group_conditions.append(User.department.in_(deps))

    if targeting.max_ad_level is not None:
        try:
            lvl = int(targeting.max_ad_level)
            group_conditions.append(User.ad_level <= lvl)
        except (TypeError, ValueError):
            pass

    conditions = []
    if group_conditions:
        conditions.append(_sa_and(*group_conditions))

    if targeting.user_ids:
        ids = [u for u in targeting.user_ids if u and isinstance(u, str)]
        if ids:
            conditions.append(User.id.in_(ids))

    raw_emails: set[str] = set()
    if targeting.emails:
        for e in targeting.emails:
            # May echo an encrypted value from a prior /recipients/resolve
            # sample row — decrypt before matching (no-op if disabled).
            e = decrypt_pii(e)
            if isinstance(e, str) and _EMAIL_RE.match(e.strip().lower()):
                raw_emails.add(e.strip().lower())
        if raw_emails:
            conditions.append(_sa_func.lower(User.email).in_(list(raw_emails)))

    if not conditions:
        # No targeting at all and not "all" → empty result
        return q.filter(False), raw_emails

    return q.filter(_sa_or(*conditions)), raw_emails


def _resolve_recipient_list(db: Session, targeting: _ResolveTargetingReq) -> List[Dict[str, Any]]:
    """
    Materialise the full deduplicated recipient list (by lowercased email).
    Returns a list of dicts: {user_id, email, name, department, ad_level}.
    Includes raw email entries (no User row) when supplied via targeting.emails.
    """
    q, raw_emails = _resolve_recipients_query(db, targeting)
    seen: Dict[str, Dict[str, Any]] = {}
    for uid, email, name, department, ad_level in q.all():
        if not email:
            continue
        key = email.strip().lower()
        if key in seen:
            continue
        seen[key] = {
            "user_id":    uid,
            "email":      email,
            "name":       name,
            "department": department,
            "ad_level":   ad_level,
        }

    # Add raw emails that didn't match a User row.
    matched_lower = {row["email"].strip().lower() for row in seen.values()}
    for em in raw_emails:
        if em in matched_lower:
            continue
        seen[em] = {
            "user_id":    None,
            "email":      em,
            "name":       None,
            "department": None,
            "ad_level":   None,
        }

    return list(seen.values())


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/access")
def broadcast_access(current_user: dict = Depends(get_current_user)):
    """Lightweight probe used by the UI to decide whether to show the feature.

    Does NOT raise 403 — returns {"allowed": false} for non-allowlisted users
    so the sidebar can simply hide the menu item. Real authorisation still
    happens on every other endpoint via `require_broadcast_user`.
    """
    return {"allowed": _is_broadcast_allowed(current_user)}


@router.post("/templates/suggest", response_model=_TemplateSuggestRes)
def templates_suggest(
    body: _TemplateSuggestReq,
    request: Request,
    _user: dict = Depends(require_broadcast_user),
):
    """Generate a single HTML email template from a plain-English intent.

    Compliance runs on BOTH the intent (input) and the model output before
    returning. Returns the HTML and the model label used.
    """
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    intent = (body.intent or "").strip()
    if not intent:
        raise HTTPException(400, "intent is required")

    # Compliance on the LLM input
    _compliance_check(intent, label="intent")

    tone = (body.tone or "professional").strip()[:60]

    system_prompt = (
        "You are an expert email designer at AiNxt (a payments regulator).\n"
        "Write ONE complete, self-contained HTML email body suitable for an internal\n"
        "company-wide announcement. Output ONLY the HTML — no markdown fences,\n"
        "no commentary.\n\n"
        "STRICT RULES (do not violate):\n"
        "  1. Output a single <div>…</div> wrapper with inline CSS only — no\n"
        "     external stylesheets, no <html>/<head>/<body> tags, no <script>,\n"
        "     no <iframe>, no <form>, no remote URLs other than https://.\n"
        "  2. Use an attractive, clean, professional, executive-briefing tone.\n"
        "  3. Include the literal placeholder {{name}} exactly once in the\n"
        "     greeting (e.g. 'Hi {{name}},'). Do NOT replace it with a value.\n"
        "  4. Keep total HTML under 20,000 characters.\n"
        "  5. Do not include images unless absolutely necessary; if used, use a\n"
        "     placeholder alt text only.\n"
        "  6. Do not include any signature for the email\n"
        "  7. Do not include any copyright text. At the end just mention the below line. - AiNxt · AiNxt Autonomous Agentic Engineering Platform\n"
        "     Please do not reply — this is a system-generated email."
    )

    user_prompt = (
        f"Tone: {tone}\n\n"
        f"Intent (what the email should communicate):\n{intent}\n\n"
        "Produce the HTML email body now."
    )

    full_prompt = f"{system_prompt}\n\n{user_prompt}"

    try:
        from models.model_router import model_router
        result = model_router.generate(full_prompt, model_hint="claude", return_meta=True)
    except Exception as exc:
        logger.error(f"broadcast_router: model_router.generate failed: {exc}")
        raise HTTPException(502, "Template generation failed")

    if isinstance(result, dict):
        html = (result.get("text") or "").strip()
        meta = result.get("meta") or {}
    else:
        html = str(result or "").strip()
        meta = {}

    if not html:
        raise HTTPException(502, "Template generation returned empty output")

    # Strip accidental markdown fences if the model added them.
    html = re.sub(r"^```[a-zA-Z]*\s*", "", html)
    html = re.sub(r"\s*```\s*$", "", html.strip())

    # Compliance on the LLM output before returning.
    _compliance_check(html, label="template_output")

    model_used = str(meta.get("model") or "claude")
    return _TemplateSuggestRes(html=html, model=model_used)


@router.post("/preview", response_model=_PreviewRes)
def preview(
    body: _PreviewReq,
    request: Request,
    _user: dict = Depends(require_broadcast_user),
):
    """Substitute {{name}} with the given sample name (HTML-escaped)."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    rendered = body.html
    if body.enrich_name:
        first = (body.sample_name.strip().split() or ["there"])[0]
        safe  = _html.escape(first, quote=True)
        rendered = body.html.replace("{{name}}", safe)
    return _PreviewRes(html=rendered)


@router.get("/departments")
def list_departments(
    request: Request,
    _user: dict = Depends(require_broadcast_user),
    db: Session = Depends(get_db),
):
    """Distinct department names from the `users` table (active users only)."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    from db.models import User
    rows = (
        db.query(User.department)
        .filter(User.department.isnot(None), User.is_active == True)  # noqa: E712
        .distinct()
        .order_by(User.department.asc())
        .all()
    )
    departments = [r[0] for r in rows if r[0]]
    return {"departments": departments}


@router.post("/recipients/resolve", response_model=_ResolveRes)
def resolve_recipients(
    body: _ResolveTargetingReq,
    request: Request,
    _user: dict = Depends(require_broadcast_user),
    db: Session = Depends(get_db),
):
    """Return total recipient count + the first 50 matched rows as a sample."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    rows = _resolve_recipient_list(db, body)
    sample = [
        _ResolveSampleRow(
            user_id=r["user_id"],
            email=encrypt_pii(r["email"]),
            name=encrypt_pii(r["name"]),
            department=r["department"],
            ad_level=r["ad_level"],
        )
        for r in rows[:50]
    ]
    return _ResolveRes(count=len(rows), sample=sample)


@router.post("/attachments", response_model=_AttachmentRes)
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    _user: dict = Depends(require_broadcast_user),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Upload a single attachment. Validated with the standard allow-list and a
    25 MB cap. Persists bytes to BROADCAST_ATTACHMENT_DIR/<uuid>__<safe>.
    The attachment is not linked to a broadcast yet; broadcast_id is set when
    POST /broadcast/send references this attachment id.
    """
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    content = await file.read()
    vr = validate_upload(
        filename=file.filename or "attachment.bin",
        content=content,
        allowed_extensions=_ATTACHMENT_ALLOWED_EXTENSIONS,
        max_size_bytes=_ATTACHMENT_MAX_SIZE_BYTES,
        caller="broadcast_router/attachments",
    )
    if not vr.valid:
        raise HTTPException(status_code=415, detail=vr.error or "Invalid attachment")

    dir_path = _ensure_attachment_dir()
    att_id   = str(_uuid_mod.uuid4())
    storage_name = f"{att_id}__{vr.safe_filename}"
    storage_path = os.path.join(dir_path, storage_name)

    try:
        with open(storage_path, "wb") as fh:
            fh.write(content)
        try:
            os.chmod(storage_path, 0o600)
        except Exception:
            pass
    except Exception as exc:
        logger.error(f"broadcast_router: attachment write failed: {exc}")
        raise HTTPException(500, "Could not persist attachment")

    from db.models import EmailBroadcastAttachment
    mimetype = file.content_type or "application/octet-stream"
    row = EmailBroadcastAttachment(
        id=att_id,
        broadcast_id=None,
        uploaded_by=current_user.get("sub") or current_user.get("user_id"),
        filename=vr.original_filename,
        mimetype=mimetype,
        size_bytes=vr.size_bytes,
        storage_path=storage_path,
    )
    db.add(row)
    db.commit()

    _audit(db, broadcast_id=None, actor=current_user, action="attachment_uploaded",
           detail={"attachment_id": att_id, "filename": vr.original_filename,
                   "size_bytes": vr.size_bytes})

    return _AttachmentRes(
        id=att_id,
        filename=vr.original_filename,
        size_bytes=vr.size_bytes,
        mimetype=mimetype,
    )


@router.delete("/attachments/{attachment_id}")
def delete_attachment(
    attachment_id: str,
    request: Request,
    _user: dict = Depends(require_broadcast_user),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete an attachment only if it has not been linked to a sent broadcast."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    from db.models import EmailBroadcast, EmailBroadcastAttachment

    row = (
        db.query(EmailBroadcastAttachment)
        .filter(EmailBroadcastAttachment.id == attachment_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Attachment not found")

    if row.broadcast_id:
        bc = db.query(EmailBroadcast).filter(EmailBroadcast.id == row.broadcast_id).first()
        if bc and bc.status not in ("draft", "cancelled"):
            raise HTTPException(409, "Attachment is linked to an active or sent broadcast")

    # Remove from disk first, then DB (best-effort on disk).
    try:
        if row.storage_path and os.path.exists(row.storage_path):
            os.remove(row.storage_path)
    except Exception as exc:
        logger.warning(f"broadcast_router: file remove failed for {row.storage_path!r}: {exc}")

    db.delete(row)
    db.commit()
    _audit(db, broadcast_id=row.broadcast_id, actor=current_user,
           action="attachment_deleted", detail={"attachment_id": attachment_id})
    return {"status": "deleted", "id": attachment_id}


@router.post("/send", response_model=_SendRes)
def send_broadcast(
    body: _SendReq,
    request: Request,
    _user: dict = Depends(require_broadcast_user),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Enqueue a broadcast for sending.

    Pipeline:
      1. compliance.validate_input on subject AND html_body.
      2. Resolve recipients against the User table (de-dupe by lowercased email).
      3. Create EmailBroadcast row (status=queued).
      4. Bulk-insert EmailBroadcastRecipient rows (status=pending).
      5. Link any referenced attachments to this broadcast.
      6. Submit one task per recipient to the in-process broadcast
         ThreadPoolExecutor (8 threads). Returns immediately.
      7. Write 'created' + 'queued' audit rows.
    """
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # subject gets a CRLF/header-injection check, html_body is scanned for
    # script/iframe/event-handler/javascript: constructs only (it's a
    # legitimate rich-text email body), text_body is XSS-checked free text.
    is_valid, field_errors, sanitized = validate_broadcast_send_request(body)
    if not is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(field_errors))
    body.subject = sanitized["subject"]
    body.html_body = sanitized["html_body"]
    body.text_body = sanitized["text_body"]

    # ── 1. Compliance on subject + body ──────────────────────────────────────
    try:
        _compliance_check(body.subject, label="subject")
        _compliance_check(body.html_body, label="html_body")
    except HTTPException as exc:
        # Audit the compliance block with NO recipients created.
        _audit(db, broadcast_id=None, actor=current_user, action="compliance_blocked",
               detail={"label": getattr(exc, "detail", {}).get("label", "unknown")
                       if isinstance(exc.detail, dict) else "unknown",
                       "subject_preview": body.subject[:120]})
        raise

    # ── 2. Resolve recipients ────────────────────────────────────────────────
    recipients = _resolve_recipient_list(db, body.targeting)
    if not recipients:
        raise HTTPException(400, "Targeting matched zero recipients")
    if len(recipients) > _MAX_RECIPIENTS_PER_SEND:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Recipient count {len(recipients)} exceeds the "
                f"BROADCAST_MAX_RECIPIENTS_PER_SEND limit ({_MAX_RECIPIENTS_PER_SEND})"
            ),
        )

    # ── 3. Create EmailBroadcast row ─────────────────────────────────────────
    from db.models import (
        EmailBroadcast,
        EmailBroadcastAttachment,
        EmailBroadcastRecipient,
    )

    broadcast_id = str(_uuid_mod.uuid4())
    bc = EmailBroadcast(
        id=broadcast_id,
        created_by=current_user.get("sub") or current_user.get("user_id"),
        subject=body.subject,
        html_body=body.html_body,
        text_body=body.text_body,
        enrich_name=bool(body.enrich_name),
        targeting_json=body.targeting.model_dump(),
        status="queued",
        total_count=len(recipients),
        success_count=0,
        failure_count=0,
        compliance_blocked=False,
    )
    db.add(bc)

    # ── 4. Bulk-insert recipient rows ────────────────────────────────────────
    recipient_rows: List[EmailBroadcastRecipient] = []
    for r in recipients:
        rcpt_id = str(_uuid_mod.uuid4())
        recipient_rows.append(EmailBroadcastRecipient(
            id=rcpt_id,
            broadcast_id=broadcast_id,
            user_id=r.get("user_id"),
            email=r["email"],
            name=r.get("name"),
            status="pending",
        ))
    db.add_all(recipient_rows)

    # ── 5. Link attachments to this broadcast (validate ownership/existence) ─
    if body.attachment_ids:
        att_rows = (
            db.query(EmailBroadcastAttachment)
            .filter(EmailBroadcastAttachment.id.in_(body.attachment_ids))
            .all()
        )
        found_ids = {a.id for a in att_rows}
        missing = [aid for aid in body.attachment_ids if aid not in found_ids]
        if missing:
            raise HTTPException(400, f"Unknown attachment_ids: {missing}")
        for a in att_rows:
            if a.broadcast_id and a.broadcast_id != broadcast_id:
                raise HTTPException(
                    409,
                    f"Attachment {a.id} is already linked to a different broadcast",
                )
            a.broadcast_id = broadcast_id

    db.commit()

    # ── 6. Audit: created ────────────────────────────────────────────────────
    _audit(db, broadcast_id=broadcast_id, actor=current_user, action="created",
           detail={
               "subject":        body.subject,
               "total_count":    len(recipients),
               "enrich_name":    bool(body.enrich_name),
               "attachment_ids": list(body.attachment_ids or []),
           })

    # ── 7. Submit one task per recipient to the in-process threadpool ────────
    # Fire-and-forget: submit_broadcast_recipient returns immediately. The
    # 8 worker threads pull jobs off the executor's internal queue and call
    # send_broadcast_recipient, which is thread-safe by construction (own
    # SessionLocal, atomic SQL counter UPDATEs, race-safe finalisation).
    enqueued = 0
    for rcpt in recipient_rows:
        try:
            submit_broadcast_recipient(
                {"broadcast_id": broadcast_id, "recipient_id": rcpt.id}
            )
            enqueued += 1
        except Exception as exc:
            # Submit can only fail if the executor was already shut down
            # (gateway in the middle of stopping). Mark this specific
            # recipient failed so we never lose track of it.
            logger.error(
                f"broadcast_router: submit failed for recipient {rcpt.id}: {exc}"
            )
            db.query(EmailBroadcastRecipient).filter(
                EmailBroadcastRecipient.id == rcpt.id
            ).update({
                "status":     "failed",
                "error_text": f"Submit failed: {exc}"[:2000],
            })
            db.query(EmailBroadcast).filter(EmailBroadcast.id == broadcast_id).update({
                "failure_count": EmailBroadcast.failure_count + 1,
                "updated_at":    datetime.utcnow(),
            })
            db.commit()

    _audit(db, broadcast_id=broadcast_id, actor=current_user, action="queued",
           detail={"enqueued": enqueued, "total_count": len(recipients)})

    return _SendRes(broadcast_id=broadcast_id, total_count=len(recipients))


# ── Listing / detail / cancel ────────────────────────────────────────────────

def _summary_from(row, creator_email: Optional[str]) -> _BroadcastSummary:
    return _BroadcastSummary(
        id=row.id,
        subject=row.subject,
        status=row.status,
        total_count=row.total_count,
        success_count=row.success_count,
        failure_count=row.failure_count,
        enrich_name=row.enrich_name,
        compliance_blocked=row.compliance_blocked,
        model_used=row.model_used,
        created_at=(row.created_at.isoformat() if row.created_at else ""),
        created_by_email=encrypt_pii(creator_email),
    )


@router.get("")
def list_broadcasts(
    request: Request,
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0,  ge=0),
    _user: dict = Depends(require_broadcast_user),
    db: Session = Depends(get_db),
):
    """List broadcasts, most-recent first. Admin sees all."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    from db.models import EmailBroadcast, User

    q = (
        db.query(EmailBroadcast, User.email)
        .outerjoin(User, User.id == EmailBroadcast.created_by)
        .order_by(EmailBroadcast.created_at.desc())
    )
    total = q.count()
    rows  = q.offset(offset).limit(limit).all()
    items = [_summary_from(bc, email).model_dump() for (bc, email) in rows]
    return {"total": total, "items": items}


@router.get("/{broadcast_id}", response_model=_BroadcastDetail)
def get_broadcast(
    broadcast_id: str,
    request: Request,
    _user: dict = Depends(require_broadcast_user),
    db: Session = Depends(get_db),
):
    """Detail view: full broadcast row + first 20 failed recipients."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    from db.models import EmailBroadcast, EmailBroadcastRecipient, User

    row = db.query(EmailBroadcast).filter(EmailBroadcast.id == broadcast_id).first()
    if row is None:
        raise HTTPException(404, "Broadcast not found")

    creator_email = None
    if row.created_by:
        creator_email = (
            db.query(User.email).filter(User.id == row.created_by).scalar()
        )

    failed = (
        db.query(EmailBroadcastRecipient)
        .filter(
            EmailBroadcastRecipient.broadcast_id == broadcast_id,
            EmailBroadcastRecipient.status == "failed",
        )
        .order_by(EmailBroadcastRecipient.created_at.asc())
        .limit(20)
        .all()
    )
    failed_sample = [
        {
            "id":         f.id,
            "email":      encrypt_pii(f.email),
            "name":       encrypt_pii(f.name),
            "error_text": f.error_text,
        }
        for f in failed
    ]

    summary = _summary_from(row, creator_email)
    return _BroadcastDetail(
        **summary.model_dump(),
        html_body=row.html_body,
        text_body=row.text_body,
        targeting_json=dict(row.targeting_json or {}),
        failed_sample=failed_sample,
    )


@router.get("/{broadcast_id}/recipients", response_model=_RecipientPage)
def list_recipients(
    broadcast_id: str,
    request: Request,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0,   ge=0),
    _user: dict = Depends(require_broadcast_user),
    db: Session = Depends(get_db),
):
    """Paginated recipients list, optionally filtered by status."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    from db.models import EmailBroadcast, EmailBroadcastRecipient

    bc = db.query(EmailBroadcast).filter(EmailBroadcast.id == broadcast_id).first()
    if bc is None:
        raise HTTPException(404, "Broadcast not found")

    q = db.query(EmailBroadcastRecipient).filter(
        EmailBroadcastRecipient.broadcast_id == broadcast_id
    )
    if status_filter:
        q = q.filter(EmailBroadcastRecipient.status == status_filter)
    total = q.count()
    rows  = (
        q.order_by(EmailBroadcastRecipient.created_at.asc())
        .offset(offset).limit(limit).all()
    )
    items = [
        _RecipientRow(
            id=r.id,
            email=encrypt_pii(r.email),
            name=encrypt_pii(r.name),
            status=r.status,
            error_text=r.error_text,
            sent_at=(r.sent_at.isoformat() if r.sent_at else None),
        )
        for r in rows
    ]
    return _RecipientPage(total=total, items=items)


@router.post("/{broadcast_id}/cancel")
def cancel_broadcast(
    broadcast_id: str,
    request: Request,
    _user: dict = Depends(require_broadcast_user),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a broadcast: status=cancelled, pending recipients → skipped."""
    enforce_rate_limit_with_behaviour(request, SENSITIVE_ADMIN)

    from db.models import EmailBroadcast, EmailBroadcastRecipient

    bc = db.query(EmailBroadcast).filter(EmailBroadcast.id == broadcast_id).first()
    if bc is None:
        raise HTTPException(404, "Broadcast not found")
    if bc.status in ("completed", "cancelled", "failed"):
        return {"status": bc.status}

    bc.status = "cancelled"
    bc.updated_at = datetime.utcnow()

    skipped = (
        db.query(EmailBroadcastRecipient)
        .filter(
            EmailBroadcastRecipient.broadcast_id == broadcast_id,
            EmailBroadcastRecipient.status == "pending",
        )
        .update({"status": "skipped",
                 "error_text": "Broadcast cancelled"})
    )
    db.commit()
    _audit(db, broadcast_id=broadcast_id, actor=current_user, action="cancelled",
           detail={"skipped": int(skipped)})
    return {"status": "cancelled", "skipped": int(skipped)}
