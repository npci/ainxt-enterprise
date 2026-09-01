# SPDX-License-Identifier: Apache-2.0
# ============================================================
# API KEY AUTH — IDE Bearer token resolution
#
# Raw key format:  {username_slug}-{uuid4}
#   e.g.  kannan-f47ac10b-58a2-4b3c-9d2e-1a2b3c4d5e6f
#
# Only the SHA-256 hex digest is stored in the DB.
# The plaintext key is shown ONCE at generation time and never stored.
# ============================================================

import hashlib
import os
from datetime import datetime
from typing import Optional


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_api_key(token: str) -> bool:
    """
    Distinguish an IDE API key from a JWT.
    JWTs have exactly 3 base64-encoded segments separated by dots.
    API keys match  {slug}-{uuid4}  — at least one hyphen, fewer than 2 dots.
    """
    return token.count(".") < 2 and "-" in token


_EXPIRING_SOON_DAYS = int(os.getenv("API_KEY_EXPIRING_SOON_DAYS", "15"))


def _notify_if_expiring_soon(api_key_row, user_email: str) -> None:
    """Send inbox notification + email if the key expires within 15 days.

    Gates on `last_expiry_notified_at` so the user gets at most one round
    of notifications per key. Best-effort — never raises.
    """
    if not api_key_row.expires_at:
        return
    remaining = (api_key_row.expires_at - datetime.utcnow()).total_seconds()
    if not (0 < remaining <= _EXPIRING_SOON_DAYS * 86400):
        return
    # Already notified — skip
    if api_key_row.last_expiry_notified_at:
        return

    days_left = int(remaining // 86400)
    subject = f"AiNxt API key expiring in {days_left} day(s)"
    body = (
        f"Your API key ({api_key_row.key_prefix}) will expire on "
        f"{api_key_row.expires_at.strftime('%d %B %Y')} and will stop "
        f"working for your IDE integration.\n\n"
        f"Go to Settings → API Keys and generate a new key "
        f"before the expiration date."
    )

    # 1. In-app inbox notification (bell icon — web UI)
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            str(api_key_row.user_id),
            "api_key_expiring",
            subject,
            body,
            source_id=f"api_key_expire:{api_key_row.id}",
            metadata={
                "kind": "api_key_expiring",
                "key_prefix": api_key_row.key_prefix,
                "expires_at": api_key_row.expires_at.isoformat(),
                "days_left": days_left,
            },
        )
    except Exception:
        pass

    # 2. Email (reaches users on any screen)
    try:
        from services.smtp_service import send_html_email
        # Built from PLATFORM_BASE_URL, not hardcoded: this link previously
        # pointed every recipient at https://ainxt.npci.org.in/... , an internal
        # internal host that no external deployment can reach.
        from core.config import PLATFORM_BASE_URL as _pbu
        _settings_url = f"{(_pbu or '').rstrip('/')}/settings/api-keys"
        send_html_email(
            to=[user_email],
            subject=subject,
            html_body=(
                f"<p>Your AiNxt API key (<b>{api_key_row.key_prefix}</b>) will expire on "
                f"<b>{api_key_row.expires_at.strftime('%d %B %Y')}</b> and will stop "
                f"working for your IDE integration.</p>"
                f"<p>Please generate a new key from "
                f"<a href='{_settings_url}'>Settings → API Keys</a> "
                f"before the expiration date.</p>"
            ),
            text_body=body,
        )
    except Exception:
        pass

    # Stamp notified_at so we don't notify again
    api_key_row.last_expiry_notified_at = datetime.utcnow()


def resolve_api_key(raw_key: str) -> Optional[dict]:
    """
    Validate a raw API key and return a JWT-compatible payload dict, or None.

    Steps:
    1. SHA-256 hash the raw key.
    2. Lookup user_api_keys WHERE key_hash = <hash> AND is_active = TRUE.
    3. Fetch the user row and confirm the account is active.
    4. Stamp last_used_at (best-effort — never fails auth on error).
    5. Return a payload dict that matches the JWT payload contract
       (same keys: sub, email, role, ad_level, department, can_approve, etc.)
       plus auth_method = "api_key" so callers can distinguish if needed.
    """
    key_hash = _sha256_hex(raw_key)

    try:
        from db.database import SessionLocal
        from db.models import UserAPIKey, User

        db = SessionLocal()
        try:
            api_key_row = (
                db.query(UserAPIKey)
                .filter(
                    UserAPIKey.key_hash == key_hash,
                    UserAPIKey.is_active == True,  # noqa: E712
                )
                .first()
            )
            if not api_key_row:
                return None
            # Reject expired keys
            from datetime import datetime
            if api_key_row.expires_at and datetime.utcnow() > api_key_row.expires_at:
                return None

            user = db.query(User).filter(User.id == api_key_row.user_id).first()
            if not user or not getattr(user, "is_active", True) is True:
                return None

            # Stamp last_used_at — best-effort, never block auth on failure
            try:
                api_key_row.last_used_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()

            # Notify if key is expiring within 15 days — best-effort, once per key
            try:
                _notify_if_expiring_soon(api_key_row, user.email or "")
                db.commit()
            except Exception:
                pass

            ad_level = int(user.ad_level) if user.ad_level is not None else 6
            return {
                "sub":              str(user.id),
                "email":            user.email or "",
                "name":             getattr(user, "name", "") or "",
                "role":             user.role or "user",
                "org_id":           getattr(user, "org_id", "") or "",
                "ad_level":         ad_level,
                "department":       getattr(user, "department", "") or "",
                "is_security_team": bool(getattr(user, "is_security_team", False)),
                "can_approve":      ad_level <= int(os.getenv("APPROVAL_AD_LEVEL", "6")) or (user.role or "") == "admin",
                "auth_method":      "api_key",
                "api_key_id":       str(api_key_row.id),
            }
        finally:
            db.close()
    except Exception:
        return None