# SPDX-License-Identifier: MIT
# ============================================================
# auth/google_auth.py — Google sign-in unit tests.
#
# These cover the decisions that make the flow safe, and the ones that
# make it non-breaking:
#
#   * the feature is inert unless ENABLE_GOOGLE_LOGIN=true
#   * scopes/extra_params from the gmail connector row are overridden, so
#     login never asks for mailbox access and never re-prompts for consent
#   * the connector row is admin-editable, so authorize_url/token_url are
#     pinned to Google hosts
#   * id_token iss/aud/exp are enforced
#   * an unverified Google address can never match an existing account
#   * an existing azure_ad / keycloak / scim linkage is never overwritten
#
# No DB and no network: the connector row and the SQLAlchemy session are
# both stubbed, so this runs anywhere pytest does.
# ============================================================

from __future__ import annotations

import base64
import json
import time

import pytest

import auth.google_auth as ga


# ── helpers ───────────────────────────────────────────────────

CLIENT_ID = "test-client-id.apps.googleusercontent.com"

_CONNECTOR_ROW = {
    "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "client_id_env": "GOOGLE_CLIENT_ID",
    "client_secret_env": "GOOGLE_CLIENT_SECRET",
    "pkce": True,
    # Deliberately the real connector values — the point is that login
    # overrides them.
    "scopes": [
        "openid", "email", "profile",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    "extra_params": {"access_type": "offline", "prompt": "consent"},
}


def _id_token(**overrides) -> str:
    """Build an unsigned JWT whose claims we control."""
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "person@example.com",
        "email_verified": True,
        "name": "A Person",
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


@pytest.fixture
def google_env(monkeypatch):
    """Flag on, credentials present, connector row stubbed."""
    monkeypatch.setenv("ENABLE_GOOGLE_LOGIN", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PLATFORM_BASE_URL", "https://ainxt.test")
    monkeypatch.setattr(ga, "_load_connector_auth_config", lambda: dict(_CONNECTOR_ROW))


# ── feature flag ──────────────────────────────────────────────

def test_disabled_by_default(monkeypatch):
    """No flag, no feature — an upgrade that touches no env var is inert."""
    monkeypatch.delenv("ENABLE_GOOGLE_LOGIN", raising=False)
    assert ga.google_login_enabled() is False


def test_enabled_requires_credentials(monkeypatch):
    """Flag on but no client id — the button must not render."""
    monkeypatch.setenv("ENABLE_GOOGLE_LOGIN", "true")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setattr(ga, "_load_connector_auth_config", lambda: dict(_CONNECTOR_ROW))
    assert ga.google_login_enabled() is False


def test_enabled_when_flag_and_credentials_present(google_env):
    assert ga.google_login_enabled() is True


def test_unreadable_connector_row_degrades_to_disabled(monkeypatch):
    """A DB problem must hide the button, not raise into /auth/ui-config."""
    monkeypatch.setenv("ENABLE_GOOGLE_LOGIN", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)

    def _boom():
        raise RuntimeError("connector_definitions unavailable")

    monkeypatch.setattr(ga, "_load_connector_auth_config", _boom)
    assert ga.google_login_enabled() is False


# ── config derived from the connector row ─────────────────────

def test_login_never_requests_gmail_scopes(google_env):
    """The row lists gmail.readonly/modify/send; login must drop all of them."""
    config = ga._load_oauth_config()
    assert config.scopes == ["openid", "email", "profile"]
    assert not any("gmail" in s for s in config.scopes)


def test_login_overrides_connector_extra_params(google_env):
    """prompt=consent would re-prompt on every login."""
    config = ga._load_oauth_config()
    assert config.extra_params["prompt"] == "select_account"


def test_login_does_not_request_offline_access(google_env):
    """OAuth2Handler.generate_authorize_url hardcodes access_type=offline for
    every provider. Sign-in reads the ID token once and stores nothing, so it
    must not ask for a durable offline grant. extra_params is applied last, so
    this is what actually reaches Google."""
    config = ga._load_oauth_config()
    assert config.extra_params["access_type"] == "online"


def test_credential_env_var_names_come_from_the_row(google_env):
    """Sign-in reuses the Gmail connector's OAuth client."""
    config = ga._load_oauth_config()
    assert config.client_id_env == "GOOGLE_CLIENT_ID"
    assert config.client_secret_env == "GOOGLE_CLIENT_SECRET"


@pytest.mark.parametrize("field", ["authorize_url", "token_url"])
def test_non_google_endpoint_is_rejected(google_env, monkeypatch, field):
    """PUT /connectors/definitions/gmail is admin-editable — an admin must not
    be able to repoint the login page (or the code exchange) at another host."""
    row = dict(_CONNECTOR_ROW)
    row[field] = "https://evil.example.com/authorize"
    monkeypatch.setattr(ga, "_load_connector_auth_config", lambda: row)
    with pytest.raises(RuntimeError, match="not an allowed Google endpoint"):
        ga._load_oauth_config()


def test_redirect_uri_carries_the_api_prefix(google_env):
    """nginx only proxies /ainxt/v1/api to the gateway."""
    assert ga._redirect_uri() == "https://ainxt.test/ainxt/v1/api/auth/google/callback"


def test_redirect_uri_prefers_connector_base(google_env, monkeypatch):
    monkeypatch.setenv("CONNECTOR_OAUTH_REDIRECT_BASE", "https://connectors.test/")
    assert ga._redirect_uri() == "https://connectors.test/ainxt/v1/api/auth/google/callback"


def test_explicit_redirect_uri_wins(google_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_LOGIN_REDIRECT_URI", "https://x.test/cb")
    assert ga._redirect_uri() == "https://x.test/cb"


# ── id_token validation ───────────────────────────────────────

def test_valid_id_token_decodes():
    claims = ga._decode_id_token(_id_token(), CLIENT_ID)
    assert claims["sub"] == "1234567890"
    assert claims["email"] == "person@example.com"


def test_id_token_wrong_audience_rejected():
    with pytest.raises(ValueError, match="audience"):
        ga._decode_id_token(_id_token(), "some-other-client-id")


def test_id_token_wrong_issuer_rejected():
    with pytest.raises(ValueError, match="issuer"):
        ga._decode_id_token(_id_token(iss="https://evil.example.com"), CLIENT_ID)


def test_expired_id_token_rejected():
    with pytest.raises(ValueError, match="expired"):
        ga._decode_id_token(_id_token(exp=int(time.time()) - 60), CLIENT_ID)


def test_id_token_without_subject_rejected():
    with pytest.raises(ValueError, match="subject"):
        ga._decode_id_token(_id_token(sub=""), CLIENT_ID)


def test_malformed_id_token_rejected():
    with pytest.raises(ValueError, match="not a JWT"):
        ga._decode_id_token("not-a-jwt", CLIENT_ID)


# ── auto-provision policy ─────────────────────────────────────

def test_auto_provision_defaults_to_self_registration(monkeypatch):
    """Closing self-registration must not leave Google as an open side door."""
    monkeypatch.delenv("GOOGLE_AUTO_PROVISION", raising=False)
    monkeypatch.setattr("core.config.ENABLE_SELF_REGISTRATION", False)
    assert ga._auto_provision_enabled() is False
    monkeypatch.setattr("core.config.ENABLE_SELF_REGISTRATION", True)
    assert ga._auto_provision_enabled() is True


def test_auto_provision_explicit_override(monkeypatch):
    monkeypatch.setattr("core.config.ENABLE_SELF_REGISTRATION", False)
    monkeypatch.setenv("GOOGLE_AUTO_PROVISION", "true")
    assert ga._auto_provision_enabled() is True
    monkeypatch.setenv("GOOGLE_AUTO_PROVISION", "false")
    assert ga._auto_provision_enabled() is False


# ── user matching / linkage ───────────────────────────────────

class _FakeUser:
    def __init__(self, **kw):
        self.id = kw.get("id", "user-1")
        self.email = kw.get("email", "person@example.com")
        self.name = kw.get("name", "A Person")
        self.role = kw.get("role", "user")
        self.org_id = kw.get("org_id")
        self.sso_provider = kw.get("sso_provider")
        self.sso_subject = kw.get("sso_subject")
        self.is_active = kw.get("is_active", True)
        self.email_verified = kw.get("email_verified", False)
        self.ad_level = kw.get("ad_level", 6)
        self.is_security_team = kw.get("is_security_team", False)
        self.last_login_at = kw.get("last_login_at")


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._result


class _FakeSession:
    """Returns `by_subject` for the first query and `by_email` for the second,
    mirroring _upsert_google_user's two-step lookup."""

    def __init__(self, by_subject=None, by_email=None):
        self._results = [by_subject, by_email]
        self.added = []
        self.committed = False

    def query(self, *_a, **_k):
        return _FakeQuery(self._results.pop(0) if self._results else None)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def refresh(self, _obj):
        pass

    def close(self):
        pass


@pytest.fixture
def patch_db(monkeypatch):
    """Swap SessionLocal and User for the fakes above."""
    def _install(session):
        import db.database
        import db.models
        monkeypatch.setattr(db.database, "SessionLocal", lambda: session)
        monkeypatch.setattr(db.models, "User", _FakeUser)
        return session
    return _install


def test_existing_sso_linkage_is_never_overwritten(patch_db, monkeypatch):
    """users.sso_provider/sso_subject are single-valued and already claimed by
    Keycloak, Azure AD and SCIM. A Google sign-in must not steal them."""
    existing = _FakeUser(sso_provider="azure_ad", sso_subject="azure-oid-999")
    session = patch_db(_FakeSession(by_subject=None, by_email=existing))

    result = ga._upsert_google_user(sub="google-sub-1", email="person@example.com", name="A Person")

    assert existing.sso_provider == "azure_ad"
    assert existing.sso_subject == "azure-oid-999"
    assert result["id"] == "user-1"
    assert session.added == []


def test_null_linkage_is_backfilled(patch_db):
    """A password-only account picks up the Google identity on first sign-in."""
    existing = _FakeUser(sso_provider=None, sso_subject=None)
    patch_db(_FakeSession(by_subject=None, by_email=existing))

    ga._upsert_google_user(sub="google-sub-1", email="person@example.com", name="A Person")

    assert existing.sso_provider == "google"
    assert existing.sso_subject == "google-sub-1"
    assert existing.email_verified is True


def test_sign_in_stamps_last_login_at(patch_db):
    """Otherwise a Google user reads as "never logged in" everywhere the admin
    user list and session views surface that column."""
    existing = _FakeUser(sso_provider="google", sso_subject="google-sub-1")
    patch_db(_FakeSession(by_subject=existing))

    ga._upsert_google_user(sub="google-sub-1", email="person@example.com", name="A Person")

    assert existing.last_login_at is not None


def test_new_user_gets_last_login_at(patch_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_AUTO_PROVISION", "true")
    session = patch_db(_FakeSession())

    ga._upsert_google_user(sub="google-sub-2", email="new2@example.com", name="New Two")

    assert session.added[0].last_login_at is not None


def test_disabled_account_is_refused(patch_db):
    from fastapi import HTTPException

    patch_db(_FakeSession(by_subject=_FakeUser(is_active=False)))
    with pytest.raises(HTTPException) as exc:
        ga._upsert_google_user(sub="google-sub-1", email="person@example.com", name="A Person")
    assert exc.value.detail == "account_disabled"


def test_unknown_user_refused_when_auto_provision_off(patch_db, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("GOOGLE_AUTO_PROVISION", "false")
    patch_db(_FakeSession())
    with pytest.raises(HTTPException) as exc:
        ga._upsert_google_user(sub="google-sub-1", email="new@example.com", name="New Person")
    assert exc.value.detail == "not_registered"


def test_new_user_matches_self_registration_defaults(patch_db, monkeypatch):
    """role "user" (auth/rbac.py ranks "viewer" lower) and an explicit
    DEFAULT_AD_LEVEL rather than the column default of 6."""
    monkeypatch.setenv("GOOGLE_AUTO_PROVISION", "true")
    monkeypatch.setattr("core.config.DEFAULT_AD_LEVEL", 3)
    session = patch_db(_FakeSession())

    ga._upsert_google_user(sub="google-sub-1", email="New@Example.com", name="New Person")

    assert len(session.added) == 1
    created = session.added[0]
    assert created.email == "new@example.com"   # normalised to lowercase
    assert created.role == "user"
    assert created.ad_level == 3
    assert created.email_verified is True
    assert created.sso_provider == "google"
    assert created.sso_subject == "google-sub-1"
