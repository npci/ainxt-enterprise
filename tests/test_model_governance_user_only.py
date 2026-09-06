# SPDX-License-Identifier: MIT
# ============================================================
# Regression tests for issue 9: Model Governance is now purely user-level.
#
# The admin UI (ai-ui/src/components/ModelGovernance.jsx) no longer has a
# Department dropdown or a "Department Access" list -- it shows a single
# "Models" list where picking a model lets an admin grant/restrict it for
# individual users directly, with no department axis involved.
#
# This exercises the new department-independent backend surface that UI
# calls:
#   GET  /model-governance/users            -- list all active users
#   GET  /model-governance/user-permissions -- ALL user-level overrides
#   POST /model-governance/user             -- set/update a user override
#                                              (department now optional,
#                                              server resolves it from the
#                                              target user's own record)
#   DELETE /model-governance/user/{uid}/{model_id}
#
# All rows created here use a random uuid-suffixed user so they can never
# collide with real data, and are removed in fixture teardown regardless
# of test outcome.
# ============================================================

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from db.database import SessionLocal


def _uid_suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def gov_user(db_session):
    """A throwaway active user to exercise user-level model governance
    against. Cleans up the user row and any permission rows it accrues."""
    suffix = _uid_suffix()
    email = f"gov_user_{suffix}@test.invalid"
    dept = f"__test_gov_dept_{suffix}"

    row = db_session.execute(
        text(
            "INSERT INTO users (id, email, name, role, hashed_password, is_active, department) "
            "VALUES (gen_random_uuid(), :email, 'Gov Test User', 'default', 'x', TRUE, :dept) "
            "RETURNING id"
        ),
        {"email": email, "dept": dept},
    ).fetchone()
    user_id = str(row[0])
    db_session.commit()

    yield {"user_id": user_id, "email": email, "dept": dept}

    db_session.execute(
        text("DELETE FROM user_model_permissions WHERE user_id = :uid"),
        {"uid": user_id},
    )
    db_session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
    db_session.commit()


def test_get_all_users_is_department_independent(gov_user):
    """GET /model-governance/users style query (exercised directly via the
    same SQL the router runs) returns the test user with no department
    filter applied -- there is no {dept} path segment any more."""
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT id, email, name, role, ad_level, department "
                "FROM users WHERE is_active = TRUE ORDER BY name"
            )
        ).fetchall()
    finally:
        db.close()
    emails = {r.email for r in rows}
    assert gov_user["email"] in emails


def test_set_user_model_permission_resolves_department_when_omitted(gov_user):
    """POST /model-governance/user's UserPermissionBody.department is now
    optional -- when the UI omits it, the endpoint must resolve it from the
    target user's own users.department rather than requiring the caller to
    know/send it."""
    from routers.model_governance_router import UserPermissionBody, set_user_model_permission

    db = SessionLocal()
    try:
        body = UserPermissionBody(
            user_id=gov_user["user_id"],
            model_id="claude-sonnet-4-6",
            allowed=False,
            web_search_allowed=False,
        )
        # department intentionally omitted -- must default to None and be
        # resolved server-side, not raise a validation error.
        assert body.department is None

        result = set_user_model_permission(
            body=body,
            admin={"sub": "test-admin", "email": "admin@test.invalid"},
            db=db,
        )
        assert result["ok"] is True
        assert result["user_id"] == gov_user["user_id"]
        assert result["allowed"] is False

        stored = db.execute(
            text(
                "SELECT department, allowed FROM user_model_permissions "
                "WHERE user_id = :uid AND model_id = :mid"
            ),
            {"uid": gov_user["user_id"], "mid": "claude-sonnet-4-6"},
        ).fetchone()
        assert stored is not None
        # Resolved from the user's own department, not left blank/NULL.
        assert stored.department == gov_user["dept"]
        assert stored.allowed is False
    finally:
        db.close()


def test_get_all_user_permissions_has_no_department_filter(gov_user):
    """GET /model-governance/user-permissions must return a user's override
    regardless of department -- there's no {dept} segment in this route."""
    from routers.model_governance_router import (
        UserPermissionBody, set_user_model_permission, get_all_user_permissions,
    )

    db = SessionLocal()
    try:
        set_user_model_permission(
            body=UserPermissionBody(
                user_id=gov_user["user_id"], model_id="gpt-5.4", allowed=False,
            ),
            admin={"sub": "test-admin"},
            db=db,
        )
        result = get_all_user_permissions(_admin={"sub": "test-admin"}, db=db)
        matches = [
            p for p in result["permissions"]
            if p["user_id"] == gov_user["user_id"] and p["model_id"] == "gpt-5.4"
        ]
        assert len(matches) == 1
        assert matches[0]["allowed"] is False
    finally:
        db.close()


def test_delete_user_model_permission_removes_override(gov_user):
    """DELETE /model-governance/user/{uid}/{model_id} removes the override
    row so the user reverts to the fail-open default (allowed)."""
    from routers.model_governance_router import (
        UserPermissionBody, set_user_model_permission, delete_user_model_permission,
    )

    db = SessionLocal()
    try:
        set_user_model_permission(
            body=UserPermissionBody(
                user_id=gov_user["user_id"], model_id="gpt-5-mini", allowed=False,
            ),
            admin={"sub": "test-admin"},
            db=db,
        )
        before = db.execute(
            text(
                "SELECT count(*) FROM user_model_permissions "
                "WHERE user_id = :uid AND model_id = :mid"
            ),
            {"uid": gov_user["user_id"], "mid": "gpt-5-mini"},
        ).scalar()
        assert before == 1

        delete_user_model_permission(
            user_id=gov_user["user_id"], model_id="gpt-5-mini",
            admin={"sub": "test-admin"}, db=db,
        )
        after = db.execute(
            text(
                "SELECT count(*) FROM user_model_permissions "
                "WHERE user_id = :uid AND model_id = :mid"
            ),
            {"uid": gov_user["user_id"], "mid": "gpt-5-mini"},
        ).scalar()
        assert after == 0
    finally:
        db.close()


def test_filter_allowed_models_still_honours_user_level_override(gov_user):
    """The runtime enforcement path (filter_allowed_models, used by
    gateway.py and ABStudio) must still respect a user-level 'not allowed'
    override after this refactor -- department is now optional at the API
    layer, but the underlying resolution logic (user beats dept, absent
    beats to allowed) is unchanged."""
    from routers.model_governance_router import (
        UserPermissionBody, set_user_model_permission, filter_allowed_models,
    )

    db = SessionLocal()
    try:
        set_user_model_permission(
            body=UserPermissionBody(
                user_id=gov_user["user_id"], model_id="claude-opus-4-7", allowed=False,
            ),
            admin={"sub": "test-admin"},
            db=db,
        )
        allowed = filter_allowed_models(
            ["claude-opus-4-7", "gpt-5.4"], gov_user["user_id"], gov_user["dept"], db,
        )
        assert "claude-opus-4-7" not in allowed
        assert "gpt-5.4" in allowed
    finally:
        db.close()
