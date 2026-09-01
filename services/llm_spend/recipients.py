# SPDX-License-Identifier: Apache-2.0
# ============================================================
# services.llm_spend.recipients
#
# Digest recipients are now routed PER CADENCE via dedicated env vars —
# each cadence's digest goes ONLY to the addresses in its own env CSV:
#
#   daily      -> LLM_SPEND_DAILY_TO
#   weekly     -> LLM_SPEND_WEEKLY_TO
#   monthly    -> LLM_SPEND_MONTHLY_TO
#   quarterly  -> LLM_SPEND_QUARTERLY_TO
#
# There is no Cc/Bcc and no users-table admin lookup for digests anymore.
# If a cadence's env var is empty/unset, that digest is skipped and the
# orchestrator fires a misconfiguration alert to LLM_SPEND_ALERT_EMAILS.
#
# The legacy exec/admin envelope resolvers (resolve_exec_recipients,
# resolve_admin_cc, resolve_digest_envelope) are retained below for
# backwards-compat / ad-hoc use but are NO LONGER on the digest path.
# ============================================================

from __future__ import annotations

import os
from typing import List, Set, Tuple

from sqlalchemy import text

from core.logger import logger
from db.database import SessionLocal


def _parse_email_csv(raw: str) -> List[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _norm(addr: str) -> str:
    return addr.strip().lower()


def _dedupe(addrs: List[str]) -> List[str]:
    seen: Set[str] = set()
    ordered: List[str] = []
    for a in addrs:
        n = _norm(a)
        if not n or n in seen:
            continue
        seen.add(n)
        ordered.append(a.strip())
    return ordered


# ── per-cadence digest recipients (To:) ───────────────────────────────────
#
# Each cadence resolves its To: list from its OWN env var. No Cc/Bcc, no
# users-table lookup. An empty/unset var yields [] — the orchestrator
# treats that as a misconfiguration (skips the digest + alerts on-call).

_CADENCE_TO_ENV = {
    "daily":     "LLM_SPEND_DAILY_TO",
    "weekly":    "LLM_SPEND_WEEKLY_TO",
    "monthly":   "LLM_SPEND_MONTHLY_TO",
    "quarterly": "LLM_SPEND_QUARTERLY_TO",
}


def cadence_to_env_var(cadence: str) -> str:
    """Return the env var name that holds the To: CSV for `cadence`."""
    return _CADENCE_TO_ENV.get(cadence, "")


# ── per-cadence digest BCC (Bcc:) ──────────────────────────────────────────
#
# Each cadence resolves an OPTIONAL blind-copy list from its own env var
# (LLM_SPEND_{DAILY,WEEKLY,MONTHLY,QUARTERLY}_BCC). Unlike To:, an empty/unset
# BCC is normal — the digest still sends with no BCC and no misconfiguration
# alert. BCC addresses ride only in the SMTP envelope, never in headers.

_CADENCE_BCC_ENV = {
    "daily":     "LLM_SPEND_DAILY_BCC",
    "weekly":    "LLM_SPEND_WEEKLY_BCC",
    "monthly":   "LLM_SPEND_MONTHLY_BCC",
    "quarterly": "LLM_SPEND_QUARTERLY_BCC",
}


def cadence_bcc_env_var(cadence: str) -> str:
    """Return the env var name that holds the Bcc: CSV for `cadence`."""
    return _CADENCE_BCC_ENV.get(cadence, "")


def resolve_digest_bcc(cadence: str) -> List[str]:
    """Return the de-duped Bcc: list for `cadence` from its env CSV.

    BCC is optional: an empty/unset var (or an unknown cadence) yields [] with
    NO warning and NO misconfiguration alert — the digest sends without a blind
    copy. This differs from resolve_digest_to, where an empty To: aborts.
    """
    env_name = _CADENCE_BCC_ENV.get(cadence)
    if not env_name:
        return []
    return _dedupe(_parse_email_csv(os.getenv(env_name, "")))


def resolve_digest_to(cadence: str) -> List[str]:
    """Return the de-duped To: list for `cadence` from its env CSV.

    Routing is per-cadence (LLM_SPEND_{DAILY,WEEKLY,MONTHLY,QUARTERLY}_TO).
    Returns [] when the var is empty/unset or the cadence is unknown — the
    caller decides how to handle an empty audience (we skip + alert).
    """
    env_name = _CADENCE_TO_ENV.get(cadence)
    if not env_name:
        logger.error(
            f"[llm_spend.recipients] unknown cadence {cadence!r}; no To: env mapping"
        )
        return []
    to = _dedupe(_parse_email_csv(os.getenv(env_name, "")))
    if not to:
        logger.warning(
            f"[llm_spend.recipients] {env_name} is empty — {cadence} digest "
            f"has no recipients and will be skipped"
        )
    return to


# ── exec recipients (To:) — LEGACY, no longer on the digest path ──────────

def resolve_exec_recipients() -> List[str]:
    """Return de-duped exec addresses from EXEC_REPORT_EMAILS env CSV."""
    addrs = _parse_email_csv(os.getenv("EXEC_REPORT_EMAILS", ""))
    seen: Set[str] = set()
    ordered: List[str] = []
    for a in addrs:
        n = _norm(a)
        if not n or n in seen:
            continue
        seen.add(n)
        ordered.append(a.strip())
    if not ordered:
        logger.warning("[llm_spend.recipients] EXEC_REPORT_EMAILS is empty — digest will be skipped")
    return ordered


# ── platform-admin recipients (Cc:) ───────────────────────────────────────

_ADMIN_SQL = text(
    """
    SELECT email
    FROM ainxt.users
    WHERE is_active = TRUE
      AND role      = 'admin'
      AND email IS NOT NULL
      AND email <> ''
    """
)


def resolve_admin_cc(exclude: List[str]) -> List[str]:
    """Platform admins (users.role='admin') for the Cc: line.

    `exclude` is the already-resolved To: list — we drop any admin who is
    already on To: so a single user never receives two envelopes.
    """
    excluded_norms = {_norm(a) for a in exclude}
    seen: Set[str] = set()
    ordered: List[str] = []
    try:
        with SessionLocal() as session:
            rows = session.execute(_ADMIN_SQL).fetchall()
    except Exception as e:
        logger.error(f"[llm_spend.recipients] admin lookup failed: {e}")
        return []
    for (email,) in rows:
        n = _norm(email)
        if not n or n in seen or n in excluded_norms:
            continue
        seen.add(n)
        ordered.append(email.strip())
    return ordered


def resolve_digest_envelope() -> Tuple[List[str], List[str]]:
    """Convenience: returns (to, cc) for an exec digest."""
    to = resolve_exec_recipients()
    cc = resolve_admin_cc(to)
    return to, cc


# ── on-call alert addresses (unchanged) ───────────────────────────────────

_DEFAULT_ALERT_EMAILS = ""  # empty — set LLM_SPEND_ALERT_EMAILS in .env

def resolve_alert_recipients() -> List[str]:
    """On-call addresses for missing-fetch alerts.
    Configure via LLM_SPEND_ALERT_EMAILS (comma-separated) in .env.
    Returns empty list when not configured — alerts are silently skipped.
    """
    raw = os.getenv("LLM_SPEND_ALERT_EMAILS", _DEFAULT_ALERT_EMAILS)
    return _parse_email_csv(raw)
