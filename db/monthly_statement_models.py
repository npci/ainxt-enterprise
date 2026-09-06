# SPDX-License-Identifier: MIT
# ============================================================
# Monthly Usage Statement — ORM models
#
# Kept in a dedicated module (rather than appended to db/models.py) so
# adding the statement feature is purely additive and does not touch any
# existing model definitions.
#
# SQLAlchemy auto-registers these classes on the shared Base.metadata as
# soon as this module is imported, so the only requirement is that
# something in the app import path imports this file before tables are
# queried (the monthly_statement_router does so).
# ============================================================

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey,
    Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# MONTHLY STATEMENT ARCHIVE
# ============================================================

class MonthlyStatement(Base):
    """
    One archived monthly statement per (user_id, billing_month, billing_year).

    statement_html  : fully-rendered Jinja2 output (served as-is for in-app view).
    statement_json  : structured summary — kept for audits and future re-rendering.
    sent_at         : NULL means the statement was generated but the email
                      delivery has not yet succeeded (or was not requested).
    """
    __tablename__ = "monthly_statements"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "billing_month", "billing_year",
            name="uq_monthly_statements_user_period",
        ),
        Index("idx_monthly_statements_user", "user_id"),
        Index("idx_monthly_statements_period", "billing_year", "billing_month"),
    )

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id         = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    billing_month   = Column(SmallInteger, nullable=False)   # 1-12
    billing_year    = Column(SmallInteger, nullable=False)
    statement_html  = Column(Text, nullable=False)
    statement_json  = Column(JSONB, nullable=False, default=dict)
    total_cost      = Column(Numeric(12, 4), nullable=False, default=0)
    total_tokens    = Column(BigInteger, nullable=False, default=0)
    total_requests  = Column(Integer, nullable=False, default=0)
    sent_at         = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, nullable=False, default=_now)


# ============================================================
# USER NOTIFICATION PREFERENCES
# ============================================================

class UserNotificationPreference(Base):
    """
    Per-user opt-in / opt-out for platform notifications.

    Currently only `monthly_statement_enabled` is consumed; the table is
    designed so additional notification toggles can be added later as
    columns (e.g. weekly_digest_enabled, anomaly_alerts_enabled) without
    a schema explosion of separate tables.
    """
    __tablename__ = "user_notification_preferences"

    user_id                     = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    monthly_statement_enabled   = Column(Boolean, nullable=False, default=True)
    email_override              = Column(String(255), nullable=True)
    updated_at                  = Column(DateTime, nullable=False, default=_now, onupdate=_now)
