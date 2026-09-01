# SPDX-License-Identifier: Apache-2.0
"""
Award / reject email notifications for TenX and OSS.

Each function fires in a background daemon thread (caller uses threading.Thread)
so it never blocks the HTTP response. All exceptions are caught and logged —
a mail failure must never cause the committee action to fail.

FROM address is configurable per program via env vars so the HR team's address
can be set without a code change:
  TENX_AWARD_FROM_EMAIL   (default: SMTP_FROM)
  TENX_REJECT_FROM_EMAIL  (default: SMTP_FROM)
  OSS_AWARD_FROM_EMAIL    (default: SMTP_FROM)
  OSS_REJECT_FROM_EMAIL   (default: SMTP_FROM)
"""
from __future__ import annotations

import html as _html
import os
from typing import List

from core.config import PLATFORM_NAME
from core.logger import logger, mask_email

# ── FROM address config ───────────────────────────────────────────────────────
_SMTP_FROM = os.getenv("SMTP_FROM", "")

TENX_AWARD_FROM  = os.getenv("TENX_AWARD_FROM_EMAIL",  _SMTP_FROM)
TENX_REJECT_FROM = os.getenv("TENX_REJECT_FROM_EMAIL", _SMTP_FROM)
OSS_AWARD_FROM   = os.getenv("OSS_AWARD_FROM_EMAIL",   _SMTP_FROM)
OSS_REJECT_FROM  = os.getenv("OSS_REJECT_FROM_EMAIL",  _SMTP_FROM)


# ── helpers ───────────────────────────────────────────────────────────────────

def _recipients(sub) -> List[str]:
    """Collect non-empty email addresses from all submission members."""
    return [
        m.email.strip()
        for m in (sub.members or [])
        if m.email and m.email.strip()
    ]


def _member_name(sub) -> str:
    """Best-effort display name for the lead submitter."""
    for m in (sub.members or []):
        if m.is_lead and m.full_name:
            return m.full_name
    return getattr(sub, "full_name", None) or "Team"


def _send(to: List[str], subject: str, html_body: str) -> None:
    """Send via core.notifications.send_email — silently skips if SMTP not configured."""
    if not to:
        logger.warning("award_mailer: no recipient emails found — skipping send")
        return
    try:
        from core.notifications import send_email
        send_email(to=to, subject=subject, body=html_body, html=True)
    except Exception as e:
        logger.error("award_mailer: send_email failed: %s", e)


def _safe(text: str) -> str:
    """HTML-escape committee text and convert newlines to <br/> so multi-line
    reasons render correctly in email clients."""
    return _html.escape(text or "").replace("\n", "<br/>")


def _html_wrap(title: str, body_html: str) -> str:
    """Minimal HTML wrapper — readable in any email client."""
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;font-size:14px;color:#1e293b;max-width:600px;margin:0 auto;padding:24px;">
  <h2 style="color:#4f46e5;margin-bottom:4px;">{title}</h2>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:12px 0 20px;">
  {body_html}
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0 12px;">
  <p style="font-size:12px;color:#94a3b8;">{PLATFORM_NAME} Awards Committee · This is an automated notification.</p>
</body>
</html>"""


# ── TenX emails ───────────────────────────────────────────────────────────────

def send_tenx_award_email(sub, reason: str) -> None:
    """Send award congratulations email to all TenX submission members."""
    try:
        title = getattr(sub, "title", "") or "your submission"
        recipients = _recipients(sub)
        subject = f"🏆 Congratulations — Your 10x Award submission has been selected: {title}"
        body = _html_wrap(
            "10x Award — Submission Selected 🏆",
            f"""
            <p>Dear Team,</p>
            <p>We are delighted to inform you that your submission
               <strong>"{_html.escape(title)}"</strong> has been selected for the <strong>10x Award</strong>.</p>
            <p><strong>Committee citation:</strong></p>
            <blockquote style="border-left:4px solid #4f46e5;margin:8px 0;padding:8px 16px;
                               background:#f5f3ff;color:#3730a3;border-radius:4px;">
              {_safe(reason)}
            </blockquote>
            <p>Congratulations to the entire team! 🎉</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("tenx award email sent to %s for submission %s", [mask_email(r) for r in recipients], sub.id)
    except Exception as e:
        logger.error("send_tenx_award_email failed: %s", e)


def send_tenx_reject_email(sub, reason: str) -> None:
    """Send rejection notification email to all TenX submission members."""
    try:
        title = getattr(sub, "title", "") or "your submission"
        recipients = _recipients(sub)
        subject = f"Your 10x Award submission — Committee Decision: {title}"
        body = _html_wrap(
            "10x Award — Committee Decision",
            f"""
            <p>Dear Team,</p>
            <p>Your submission <strong>"{_html.escape(title)}"</strong> has been reviewed by the committee.</p>
            <p><strong>Decision:</strong> Not selected for this cycle.</p>
            <p><strong>Committee feedback:</strong></p>
            <blockquote style="border-left:4px solid #ef4444;margin:8px 0;padding:8px 16px;
                               background:#fef2f2;color:#991b1b;border-radius:4px;">
              {_safe(reason)}
            </blockquote>
            <p>If the submission window is open (1st–14th of each month), you may submit
               a new updated entry directly from the Submit tab.</p>
            <p>Thank you for your contribution.</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("tenx reject email sent to %s for submission %s", [mask_email(r) for r in recipients], sub.id)
    except Exception as e:
        logger.error("send_tenx_reject_email failed: %s", e)


# ── OSS emails ────────────────────────────────────────────────────────────────

def send_oss_award_email(sub, note: str) -> None:
    """Send award congratulations email to all OSS submission members."""
    try:
        title = getattr(sub, "title", "") or getattr(sub, "summary", "") or "your submission"
        recipients = _recipients(sub)
        subject = f"🏆 Congratulations — Your OSS submission has been selected: {title}"
        body = _html_wrap(
            "OSS Contributor Awards — Submission Selected 🏆",
            f"""
            <p>Dear Team,</p>
            <p>We are delighted to inform you that your OSS submission
               <strong>"{_html.escape(title)}"</strong> has been selected for the award.</p>
            <p><strong>Committee citation:</strong></p>
            <blockquote style="border-left:4px solid #4f46e5;margin:8px 0;padding:8px 16px;
                               background:#f5f3ff;color:#3730a3;border-radius:4px;">
              {_safe(note)}
            </blockquote>
            <p>Congratulations to the entire team! 🎉</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("oss award email sent to %s for submission %s", [mask_email(r) for r in recipients], sub.id)
    except Exception as e:
        logger.error("send_oss_award_email failed: %s", e)


def send_oss_reject_email(sub, reason: str) -> None:
    """Send rejection notification email to all OSS submission members."""
    try:
        title = getattr(sub, "title", "") or getattr(sub, "summary", "") or "your submission"
        recipients = _recipients(sub)
        subject = f"Your OSS submission — Committee Decision: {title}"
        body = _html_wrap(
            "OSS Contributor Awards — Committee Decision",
            f"""
            <p>Dear Team,</p>
            <p>Your OSS submission <strong>"{_html.escape(title)}"</strong> has been reviewed by the committee.</p>
            <p><strong>Decision:</strong> Not selected for this cycle.</p>
            <p><strong>Committee feedback:</strong></p>
            <blockquote style="border-left:4px solid #ef4444;margin:8px 0;padding:8px 16px;
                               background:#fef2f2;color:#991b1b;border-radius:4px;">
              {_safe(reason)}
            </blockquote>
            <p>If the submission window is open (1st–14th of each month), you may submit
               a new updated entry directly from the Submit tab.</p>
            <p>Thank you for your contribution.</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("oss reject email sent to %s for submission %s", [mask_email(r) for r in recipients], sub.id)
    except Exception as e:
        logger.error("send_oss_reject_email failed: %s", e)


# ── Data-based variants (no ORM object — safe to call from background threads) ─
# These accept plain strings instead of SQLAlchemy ORM objects, avoiding
# DetachedInstanceError when the DB session is closed before the thread runs.

def send_tenx_award_email_data(title: str, recipients: List[str], reason: str) -> None:
    """Send TenX award email using plain data (no ORM object)."""
    try:
        subject = f"🏆 Congratulations — Your 10x Award submission has been selected: {title}"
        body = _html_wrap(
            "10x Award — Submission Selected 🏆",
            f"""
            <p>Dear Team,</p>
            <p>We are delighted to inform you that your submission
               <strong>"{_html.escape(title)}"</strong> has been selected for the <strong>10x Award</strong>.</p>
            <p><strong>Committee citation:</strong></p>
            <blockquote style="border-left:4px solid #4f46e5;margin:8px 0;padding:8px 16px;
                               background:#f5f3ff;color:#3730a3;border-radius:4px;">
              {_safe(reason)}
            </blockquote>
            <p>Congratulations to the entire team! 🎉</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("tenx award email sent to %s", [mask_email(r) for r in recipients])
    except Exception as e:
        logger.error("send_tenx_award_email_data failed: %s", e)


def send_tenx_reject_email_data(title: str, recipients: List[str], reason: str) -> None:
    """Send TenX reject email using plain data (no ORM object)."""
    try:
        subject = f"Your 10x Award submission — Committee Decision: {title}"
        body = _html_wrap(
            "10x Award — Committee Decision",
            f"""
            <p>Dear Team,</p>
            <p>Your submission <strong>"{_html.escape(title)}"</strong> has been reviewed by the committee.</p>
            <p><strong>Decision:</strong> Not selected for this cycle.</p>
            <p><strong>Committee feedback:</strong></p>
            <blockquote style="border-left:4px solid #ef4444;margin:8px 0;padding:8px 16px;
                               background:#fef2f2;color:#991b1b;border-radius:4px;">
              {_safe(reason)}
            </blockquote>
            <p>If the submission window is open (1st–14th of each month), you may submit
               a new updated entry directly from the Submit tab.</p>
            <p>Thank you for your contribution.</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("tenx reject email sent to %s", [mask_email(r) for r in recipients])
    except Exception as e:
        logger.error("send_tenx_reject_email_data failed: %s", e)


def send_oss_award_email_data(title: str, recipients: List[str], note: str) -> None:
    """Send OSS award email using plain data (no ORM object)."""
    try:
        subject = f"🏆 Congratulations — Your OSS submission has been selected: {title}"
        body = _html_wrap(
            "OSS Contributor Awards — Submission Selected 🏆",
            f"""
            <p>Dear Team,</p>
            <p>We are delighted to inform you that your OSS submission
               <strong>"{_html.escape(title)}"</strong> has been selected for the award.</p>
            <p><strong>Committee citation:</strong></p>
            <blockquote style="border-left:4px solid #4f46e5;margin:8px 0;padding:8px 16px;
                               background:#f5f3ff;color:#3730a3;border-radius:4px;">
              {_safe(note)}
            </blockquote>
            <p>Congratulations to the entire team! 🎉</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("oss award email sent to %s", [mask_email(r) for r in recipients])
    except Exception as e:
        logger.error("send_oss_award_email_data failed: %s", e)


def send_oss_reject_email_data(title: str, recipients: List[str], reason: str) -> None:
    """Send OSS reject email using plain data (no ORM object)."""
    try:
        subject = f"Your OSS submission — Committee Decision: {title}"
        body = _html_wrap(
            "OSS Contributor Awards — Committee Decision",
            f"""
            <p>Dear Team,</p>
            <p>Your OSS submission <strong>"{_html.escape(title)}"</strong> has been reviewed by the committee.</p>
            <p><strong>Decision:</strong> Not selected for this cycle.</p>
            <p><strong>Committee feedback:</strong></p>
            <blockquote style="border-left:4px solid #ef4444;margin:8px 0;padding:8px 16px;
                               background:#fef2f2;color:#991b1b;border-radius:4px;">
              {_safe(reason)}
            </blockquote>
            <p>If the submission window is open (1st–14th of each month), you may submit
               a new updated entry directly from the Submit tab.</p>
            <p>Thank you for your contribution.</p>
            """,
        )
        _send(recipients, subject, body)
        logger.info("oss reject email sent to %s", [mask_email(r) for r in recipients])
    except Exception as e:
        logger.error("send_oss_reject_email_data failed: %s", e)
