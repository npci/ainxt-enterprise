# SPDX-License-Identifier: MIT
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
import hmac
import os
from datetime import datetime, timezone
from typing import Optional

# Iteration count for the PBKDF2-keyed API-key hash below. Chosen to add a
# meaningful work factor (~ms-scale per call) without materially slowing
# down the synchronous per-request auth path.
_PBKDF2_ITERATIONS = 100_000


def _sha256_hex(value: str) -> str:
    """Legacy, unkeyed digest — kept ONLY to look up API keys issued before
    the HMAC migration (see `_hmac_hash_hex` / `_keyed_hash_hex`). Never
    used for new keys."""
    return hashlib.sha256(value.encode()).hexdigest()


def _hmac_hash_hex(value: str) -> str:
    """HMAC-SHA256 digest of `value`, keyed with JWT_SECRET.

    Intermediate-generation hash, superseded by the PBKDF2 form in
    `_keyed_hash_hex` below. Kept ONLY to look up/lazily-migrate keys that
    were already upgraded to this form before the PBKDF2 migration — never
    used for new keys. (Changing `_keyed_hash_hex`'s algorithm in place
    instead of adding this fallback tier would have locked out every
    already-migrated key: their stored `key_hash` would stop matching
    either the new PBKDF2 digest or the original legacy SHA-256 digest.)
    """
    secret = os.getenv("JWT_SECRET", "")
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _keyed_hash_hex(value: str) -> str:
    """PBKDF2-HMAC-SHA256 digest of `value`, keyed with JWT_SECRET.

    CodeQL (py/weak-sensitive-data-hashing) flags fast, unsalted/unkeyed
    SHA-256 on sensitive data: even though these API keys embed a uuid4
    (high entropy, not practically brute-forceable on its own), a single
    fast hash round still means anyone who steals both the
    `user_api_keys` table AND the server-side secret could verify
    candidate keys extremely quickly. Using PBKDF2 (keyed with JWT_SECRET
    as the salt) adds a deliberate, tunable work factor on top of the
    previous HMAC-only keying, while remaining fully deterministic --
    required here since `key_hash` is looked up via an equality match (an
    index), not verified via a per-row randomized compare like
    bcrypt/argon2 (which would make direct lookup impossible). Same digest
    size (64 hex chars) as the legacy forms, so no schema change is needed.
    """
    secret = os.getenv("JWT_SECRET", "")
    return hashlib.pbkdf2_hmac(
        "sha256", value.encode(), secret.encode(), _PBKDF2_ITERATIONS
    ).hex()


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
    remaining = (api_key_row.expires_at - datetime.now(timezone.utc)).total_seconds()
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
    1. PBKDF2-HMAC-SHA256 hash the raw key (keyed with JWT_SECRET).
    2. Lookup user_api_keys WHERE key_hash = <hash> AND is_active = TRUE.
       Falls back to the intermediate HMAC-SHA256 hash, then the legacy
       unkeyed SHA-256 hash, for keys issued before each respective
       migration, and lazily upgrades the stored hash to the current
       (PBKDF2) form on first successful use of such a key.
    3. Fetch the user row and confirm the account is active.
    4. Stamp last_used_at (best-effort — never fails auth on error).
    5. Return a payload dict that matches the JWT payload contract
       (same keys: sub, email, role, ad_level, department, can_approve, etc.)
       plus auth_method = "api_key" so callers can distinguish if needed.
    """
    key_hash = _keyed_hash_hex(raw_key)
    hmac_hash = _hmac_hash_hex(raw_key)
    legacy_hash = _sha256_hex(raw_key)

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
            _needs_migration = False
            if not api_key_row:
                # Fall back to the intermediate HMAC-SHA256 hash (keys
                # already migrated once, before the PBKDF2 upgrade).
                api_key_row = (
                    db.query(UserAPIKey)
                    .filter(
                        UserAPIKey.key_hash == hmac_hash,
                        UserAPIKey.is_active == True,  # noqa: E712
                    )
                    .first()
                )
                _needs_migration = api_key_row is not None
            if not api_key_row:
                # Fall back to the legacy unkeyed hash (pre-HMAC-migration keys).
                api_key_row = (
                    db.query(UserAPIKey)
                    .filter(
                        UserAPIKey.key_hash == legacy_hash,
                        UserAPIKey.is_active == True,  # noqa: E712
                    )
                    .first()
                )
                _needs_migration = api_key_row is not None
            if not api_key_row:
                return None
            if _needs_migration:
                # Lazily upgrade this row to the keyed hash now that we have
                # the raw key in hand (we never store the raw key itself).
                try:
                    api_key_row.key_hash = key_hash
                    db.commit()
                except Exception:
                    db.rollback()
            # Reject expired keys
            if api_key_row.expires_at and datetime.now(timezone.utc) > api_key_row.expires_at:
                return None

            user = db.query(User).filter(User.id == api_key_row.user_id).first()
            if not user or not getattr(user, "is_active", True) is True:
                return None

            # Stamp last_used_at — best-effort, never block auth on failure
            try:
                api_key_row.last_used_at = datetime.now(timezone.utc)
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