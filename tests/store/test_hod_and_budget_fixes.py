# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Regression tests for the fix/backend-startup-errors branch fixes:
#
#   Issue 1 — resolve_hod_for_request() must resolve automatically from
#             users.department -> department_hod_mapping, NOT
#             users.hod_email (which has no automatic write path).
#             Plus (see test_request_and_approve_budget_increase_end_to_end
#             below): store.budget_store.request_budget_increase() and
#             approve_budget_request() must actually be able to write to
#             hod_allocation_ledger / budget_configs — on a DB where Part
#             OSS1 (department_hod_mapping/hod_allocation_ledger) and Part L
#             (budget_configs) had already run with their ORIGINAL column
#             sets, before either table was later extended with the
#             request-lifecycle / base+extra+winner columns, those two
#             tables silently drift out of sync with db/models.py and every
#             real "Request Increase" submission 500s with
#             ``UndefinedColumn: column "justification" does not exist``
#             (filing) or ``UndefinedColumn: column "base_cost_usd" does not
#             exist`` (approval) despite the HOD itself resolving correctly.
#             Part OSS10 (db/migrate.py) ALTERs both tables to add the
#             missing columns; this test proves the whole request->approve
#             flow works against whatever schema Part OSS10 produces.
#   Issue 2 — GET /budget/users must fall back to Postgres when Redis
#             has no data for a user, instead of silently reporting
#             zero usage (the old raw rc.hgetall() bug).
#   Issue 4 — GET /endpoint-mgmt/hods must exclude HODs whose users row
#             is deactivated (is_active = FALSE).
#
# All rows created here use a random uuid-suffixed department name /
# email so they can never collide with real data, and are removed in a
# fixture teardown regardless of test outcome.
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
def hod_fixture(db_session):
    """Create a throwaway (department, hod) mapping + a requester user in
    that department, and a second, deactivated HOD mapping for the
    is_active filter test. Cleans up all rows on teardown."""
    suffix = _uid_suffix()
    dept = f"__test_dept_{suffix}"
    hod_email = f"hod_{suffix}@test.invalid"
    hod_name = f"Test HOD {suffix}"
    requester_email = f"requester_{suffix}@test.invalid"

    inactive_dept = f"__test_dept_inactive_{suffix}"
    inactive_hod_email = f"inactive_hod_{suffix}@test.invalid"

    db_session.execute(
        text(
            'INSERT INTO department_hod_mapping (department_name, hod_email, hod_name) '
            'VALUES (:dept, :hod_email, :hod_name), (:idept, :ihod_email, :ihod_name)'
        ),
        {
            "dept": dept, "hod_email": hod_email, "hod_name": hod_name,
            "idept": inactive_dept, "ihod_email": inactive_hod_email, "ihod_name": "Inactive HOD",
        },
    )
    # Requester: active user in `dept`.
    db_session.execute(
        text(
            "INSERT INTO users (id, email, name, role, hashed_password, is_active, department) "
            "VALUES (gen_random_uuid(), :email, :name, 'default', 'x', TRUE, :dept)"
        ),
        {"email": requester_email, "name": "Test Requester", "dept": dept},
    )
    # The HOD user account itself, active, so /budget/me's join resolves a name.
    db_session.execute(
        text(
            "INSERT INTO users (id, email, name, role, hashed_password, is_active, department) "
            "VALUES (gen_random_uuid(), :email, :name, 'default', 'x', TRUE, :dept)"
        ),
        {"email": hod_email, "name": hod_name, "dept": dept},
    )
    # The inactive HOD's own user account, is_active = FALSE.
    db_session.execute(
        text(
            "INSERT INTO users (id, email, name, role, hashed_password, is_active, department) "
            "VALUES (gen_random_uuid(), :email, :name, 'default', 'x', FALSE, :idept)"
        ),
        {"email": inactive_hod_email, "name": "Inactive HOD", "idept": inactive_dept},
    )
    db_session.commit()

    yield {
        "dept": dept, "hod_email": hod_email, "hod_name": hod_name,
        "requester_email": requester_email,
        "inactive_dept": inactive_dept, "inactive_hod_email": inactive_hod_email,
    }

    db_session.execute(
        text("DELETE FROM users WHERE email IN (:e1, :e2, :e3)"),
        {"e1": requester_email, "e2": hod_email, "e3": inactive_hod_email},
    )
    db_session.execute(
        text("DELETE FROM department_hod_mapping WHERE department_name IN (:d1, :d2)"),
        {"d1": dept, "d2": inactive_dept},
    )
    db_session.commit()


# ---------------------------------------------------------------------------
# Issue 1 — automatic HOD resolution via department_hod_mapping
# ---------------------------------------------------------------------------

def test_resolve_hod_for_request_uses_department_mapping(hod_fixture):
    from store.budget_store import resolve_hod_for_request

    resolved = resolve_hod_for_request(hod_fixture["requester_email"])
    assert resolved == hod_fixture["hod_email"].lower()


def test_resolve_hod_for_request_ignores_users_hod_email_column(hod_fixture, db_session):
    """Even if users.hod_email is set to something else/garbage, the
    resolution must still come from department_hod_mapping, proving
    users.hod_email is no longer consulted."""
    from store.budget_store import resolve_hod_for_request

    db_session.execute(
        text("UPDATE users SET hod_email = 'someone-else@bogus.invalid' WHERE email = :e"),
        {"e": hod_fixture["requester_email"]},
    )
    db_session.commit()

    resolved = resolve_hod_for_request(hod_fixture["requester_email"])
    assert resolved == hod_fixture["hod_email"].lower()
    assert resolved != "someone-else@bogus.invalid"


def test_resolve_hod_for_request_returns_none_when_no_department(db_session):
    from store.budget_store import resolve_hod_for_request

    suffix = _uid_suffix()
    email = f"nodept_{suffix}@test.invalid"
    db_session.execute(
        text(
            "INSERT INTO users (id, email, name, role, hashed_password, is_active, department) "
            "VALUES (gen_random_uuid(), :email, 'No Dept', 'default', 'x', TRUE, NULL)"
        ),
        {"email": email},
    )
    db_session.commit()
    try:
        assert resolve_hod_for_request(email) is None
    finally:
        db_session.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})
        db_session.commit()


def test_resolve_hod_for_request_returns_none_for_unknown_email():
    from store.budget_store import resolve_hod_for_request
    assert resolve_hod_for_request("nobody-such-user@test.invalid") is None


def test_resolve_hod_for_request_returns_none_for_empty_input():
    from store.budget_store import resolve_hod_for_request
    assert resolve_hod_for_request("") is None
    assert resolve_hod_for_request(None) is None


def test_request_and_approve_budget_increase_end_to_end(hod_fixture, db_session):
    """Full "Request Increase" -> HOD approval round trip against the real
    hod_allocation_ledger and budget_configs tables (Part OSS10 schema).

    This is the actual bug the user hit: department -> HOD resolution
    (tested above) succeeded, but the ledger INSERT (missing
    request-lifecycle columns) and later the approval's budget_configs read
    (missing base/extra/winner columns) both 500'd on a DB where those two
    tables predated the schema additions. Runs the real store functions
    used by routers/budget_router.py end-to-end, not a mock.
    """
    from store.budget_store import (
        request_budget_increase, resolve_approvers_for_request,
        approve_budget_request, get_budget,
    )

    requester_email = hod_fixture["requester_email"]
    hod_email = hod_fixture["hod_email"]

    # Requester needs an id for the FK-less target_user_id column.
    uid = db_session.execute(
        text("SELECT id FROM users WHERE email = :e"), {"e": requester_email},
    ).scalar()
    assert uid is not None

    approvers = resolve_approvers_for_request(requester_email)
    assert approvers["hod_email"] == hod_email.lower()

    req = request_budget_increase(
        user_id=str(uid),
        requested_extra_cost_usd=7.5,
        justification="unit test: end-to-end request/approve",
        requester_email=requester_email,
        requester_name="Test Requester",
        requester_department=hod_fixture["dept"],
        hod_emails=[approvers["hod_email"]],
        delegatee_emails=approvers["delegatees"],
    )
    request_id = req["request_id"]

    try:
        result = approve_budget_request(
            request_id, hod_email, "Test HOD", check_hod_cap=False,
        )
        assert result["success"] is True
        assert result["approved_by"] == hod_email.lower()
        assert result["new_extra_cost_usd"] == pytest.approx(7.5)

        budget = get_budget(str(uid))
        assert budget["extra_cost_usd"] == pytest.approx(7.5)
    finally:
        db_session.execute(
            text("DELETE FROM hod_allocation_ledger WHERE request_id = :rid"),
            {"rid": request_id},
        )
        db_session.execute(
            text("DELETE FROM budget_configs WHERE user_id = :uid"), {"uid": str(uid)},
        )
        db_session.commit()


# ---------------------------------------------------------------------------
# Issue 4 — /endpoint-mgmt/hods excludes deactivated HODs
# ---------------------------------------------------------------------------

def test_endpoint_mgmt_hods_excludes_deactivated_hod(hod_fixture):
    """Reuses the SQL from routers/endpoint_mgmt_router.py's list_hods()
    directly, since the endpoint itself is behind admin auth (out of
    scope for a unit test) -- this proves the query-level fix."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                'SELECT lower(dhm."hod_email") AS email, '
                '       MAX(dhm."hod_name")    AS hod_name, '
                '       array_agg(DISTINCT dhm."department_name") AS departments '
                'FROM ainxt.department_hod_mapping dhm '
                'LEFT JOIN users u ON lower(u.email) = lower(dhm."hod_email") '
                'WHERE dhm."hod_email" IS NOT NULL AND dhm."hod_email" <> \'\' '
                '  AND (u.is_active IS NULL OR u.is_active = TRUE) '
                'GROUP BY lower(dhm."hod_email") '
                'ORDER BY lower(dhm."hod_email")'
            )
        ).fetchall()
    finally:
        db.close()

    emails = {r[0] for r in rows}
    assert hod_fixture["hod_email"].lower() in emails
    assert hod_fixture["inactive_hod_email"].lower() not in emails


# ---------------------------------------------------------------------------
# Issue 2 — GET /budget/users falls back to Postgres, not raw Redis-only reads
# ---------------------------------------------------------------------------

def test_budget_users_roster_falls_back_to_postgres_on_redis_miss(monkeypatch):
    """Simulate a user with NO Redis budget/usage keys at all (e.g. Redis
    was flushed, or the user's data only ever landed in Postgres) and
    confirm the roster-building code path (get_budget/get_usage_total/
    get_usage_today) still returns their real Postgres-backed totals
    instead of a false zero."""
    from store import budget_store as _bs

    uid = f"pgfallback-{_uid_suffix()}"

    # Redis reports nothing for this uid.
    class _EmptyRedis:
        def hgetall(self, key):
            return {}

    monkeypatch.setattr(_bs, "_get_redis", lambda: _EmptyRedis())
    monkeypatch.setattr(_bs, "_pg_get_budget", lambda user_id: {
        "user_id": user_id,
        "max_tokens_per_day": 0, "max_requests_per_day": 0, "max_cost_usd_per_day": 0.0,
        "max_tokens_total": 500_000, "max_requests_total": 1_000,
        "max_cost_usd_total": 50.0, "base_cost_usd": 50.0, "extra_cost_usd": 0.0,
        "winner_extra_usd": 0.0, "winner_origin_period": None, "model_limits": {},
    })
    monkeypatch.setattr(_bs, "_pg_get_usage", lambda user_id: {
        "tokens_used": 12345, "requests_made": 42, "cost_usd_spent": 3.21,
    })

    budget = _bs.get_budget(uid)
    usage = _bs.get_usage_total(uid)

    assert budget is not None
    assert budget["max_cost_usd_total"] == 50.0
    # This is the crux of the regression: the OLD code (raw rc.hgetall())
    # would have returned tokens_used=0 here purely because Redis had no
    # key -- it never looked at Postgres at all.
    assert usage["tokens_used"] == 12345
    assert usage["cost_usd_spent"] == 3.21


# ---------------------------------------------------------------------------
# Issue 5 — evals relevance-prompt format-string bug
# ---------------------------------------------------------------------------

def test_relevance_prompt_formats_the_actual_answer_text():
    from core.evals import _RELEVANCE_PROMPT

    rendered = _RELEVANCE_PROMPT.format(
        repo_context="some repo context",
        question="How do I do X?",
        answer="You should call foo() because bar.",
    )
    assert "You should call foo() because bar." in rendered
    # Regression guard: the old bug rendered the literal Python
    # expression text instead of the answer.
    assert "__name__" not in rendered
    assert "type(answer)" not in rendered
