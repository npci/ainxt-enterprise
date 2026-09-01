# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BUDGET REQUEST EMAIL — Outlook-safe notifications for the HOD
# budget-increase approval flow and the 10x-winner extra-budget grant.
#
# Mirrors the style used in services/budget_audit_service.py: simple
# table-based HTML with inline styles (no external CSS/JS — Outlook-safe),
# plus a plain-text fallback for every email.
# ============================================================

from __future__ import annotations

import html as _html
from typing import Optional

from services.smtp_service import send_html_email
from core.logger import logger, mask_email


def _fmt_usd(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "$0.00"


def send_hod_request_email(
    hod_email: str,
    requester_name: str,
    requester_email: str,
    requester_department: str,
    requested_extra_usd: float,
    current_base_usd: float,
    current_extra_usd: float,
    justification: str,
    other_hod_count: int = 0,
) -> bool:
    """
    Notify one HOD (of possibly several fanned-out for the same request) that
    a budget-increase request is awaiting their approval.

    other_hod_count: how many OTHER HODs this same request was also sent to
    (0 if this HOD is the only one mapped to the department).
    """
    resulting_total = current_base_usd + current_extra_usd + requested_extra_usd
    safe_name  = _html.escape(requester_name or requester_email or "A user")
    safe_email = _html.escape(requester_email or "")
    safe_dept  = _html.escape(requester_department or "—")
    safe_just  = _html.escape(justification or "")

    fanout_note_html = ""
    fanout_note_text = ""
    if other_hod_count > 0:
        fanout_note_html = (
            f"<p style='color:#666;font-size:12px;'>This request was also sent to "
            f"{other_hod_count} other HOD{'s' if other_hod_count != 1 else ''} mapped to this "
            f"department. Whoever acts first (approve or reject) resolves it for everyone — "
            f"if another HOD already acted, no further action is needed from you.</p>"
        )
        fanout_note_text = (
            f"\nNote: this request was also sent to {other_hod_count} other HOD(s) for this "
            f"department. Whoever acts first resolves it for everyone.\n"
        )

    subject = "AiNxt - Budget increase request awaiting your approval"
    html_body = f"""
    <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;">
      <h2 style="color:#1a5cb0;">Budget increase request awaiting your approval</h2>
      <p>Hi,</p>
      <p><b>{safe_name}</b> ({safe_email}) has requested additional AiNxt budget.</p>
      <table cellpadding="6" style="border-collapse:collapse;">
        <tr><td><b>Department</b></td><td>{safe_dept}</td></tr>
        <tr><td><b>Current base budget</b></td><td>{_fmt_usd(current_base_usd)}</td></tr>
        <tr><td><b>Current extra granted</b></td><td>{_fmt_usd(current_extra_usd)}</td></tr>
        <tr><td><b>Requested extra</b></td><td>{_fmt_usd(requested_extra_usd)}</td></tr>
        <tr><td><b>Resulting total if approved</b></td><td><b>{_fmt_usd(resulting_total)}</b></td></tr>
      </table>
      <p style="margin-top:12px;"><b>Justification:</b><br>{safe_just}</p>
      <p style="margin-top:16px;"><b>Approval steps:</b></p>
      <ol>
        <li>Open AiNxt and go to your Inbox, or Budget Manager &rarr; Team &rarr; Pending Requests.</li>
        <li>Review the request details and justification above.</li>
        <li>Click Approve or Reject.</li>
      </ol>
      <p style="color:#888;font-size:12px;">
        Approving adds the requested amount on top of the user's base budget — it does not
        replace it. The requester's base budget is left unchanged.
      </p>
      {fanout_note_html}
      <p style="color:#888;font-size:12px;">— AiNxt Platform</p>
    </body></html>
    """
    text_body = (
        f"Budget increase request awaiting your approval\n\n"
        f"{requester_name or requester_email} ({requester_email}) has requested additional AiNxt budget.\n\n"
        f"  Department               : {requester_department or '-'}\n"
        f"  Current base budget      : {_fmt_usd(current_base_usd)}\n"
        f"  Current extra granted    : {_fmt_usd(current_extra_usd)}\n"
        f"  Requested extra          : {_fmt_usd(requested_extra_usd)}\n"
        f"  Resulting total if approved: {_fmt_usd(resulting_total)}\n\n"
        f"Justification:\n{justification or ''}\n\n"
        f"Approval steps:\n"
        f"  1. Open AiNxt and go to your Inbox, or Budget Manager -> Team -> Pending Requests.\n"
        f"  2. Review the request details and justification above.\n"
        f"  3. Click Approve or Reject.\n\n"
        f"Approving adds the requested amount on top of the user's base budget — it does not "
        f"replace it.\n"
        f"{fanout_note_text}\n"
        f"— AiNxt Platform\n"
    )

    try:
        return send_html_email(to=[hod_email], subject=subject, html_body=html_body, text_body=text_body)
    except Exception as e:
        logger.warning(f"budget_request_email: failed to send HOD request email to {mask_email(hod_email)}: {e}")
        return False
def send_winner_base_increase_email(
    user_email: str,
    user_name: str,
    granted_extra_usd: float,
    new_extra_usd: float,
    new_total_usd: float,
    base_usd: float = 50.0,
) -> bool:
    """Notify a 10x winner that extra budget was granted to them.

    This is the SINGLE canonical winner-award template — the router must not
    inline its own copy (an earlier duplicate had drifted out of sync with
    this one and outlived the allocation model it described).

    granted_extra_usd: the amount awarded by this action.
    new_extra_usd:     resulting total extra balance (award + any carryover).
    new_total_usd:     resulting spendable total (base + new_extra_usd).
    """
    safe_name = _html.escape(user_name or user_email or "there")
    carried = float(new_extra_usd or 0.0) - float(granted_extra_usd or 0.0)

    # Only mention carryover when there actually was a prior balance —
    # otherwise the row reads as a confusing "$0.00 carried forward".
    carried_row_html = ""
    carried_row_text = ""
    if carried > 0:
        carried_row_html = (
            f"<tr><td><b>Balance carried from earlier</b></td>"
            f"<td>{_fmt_usd(carried)}</td></tr>"
        )
        carried_row_text = f"  Balance carried from earlier : {_fmt_usd(carried)}\n"

    subject = "AiNxt - You've been granted 10x Winner extra budget"
    html_body = f"""
    <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;">
      <h2 style="color:#1b6e1b;">You've been granted 10x Winner extra budget</h2>
      <p>Hi {safe_name},</p>
      <p>Congratulations — as a 10x winner you've been granted
         <b>{_fmt_usd(granted_extra_usd)}</b> of extra AiNxt budget.</p>
      <table cellpadding="6" style="border-collapse:collapse;">
        <tr><td><b>Base budget (unchanged)</b></td><td>{_fmt_usd(base_usd)}</td></tr>
        <tr><td><b>Extra budget granted</b></td><td>{_fmt_usd(granted_extra_usd)}</td></tr>
        {carried_row_html}
        <tr><td><b>Total extra balance</b></td><td><b>{_fmt_usd(new_extra_usd)}</b></td></tr>
        <tr><td><b>Total spendable now</b></td><td><b>{_fmt_usd(new_total_usd)}</b></td></tr>
      </table>
      <p style="margin-top:16px;">How this works:</p>
      <ul style="font-size:13px;color:#444;">
        <li>Your <b>base budget stays at {_fmt_usd(base_usd)}</b> — it is not replaced.</li>
        <li>The extra budget is <b>usable until exhausted</b>. It is consumed only after
            you've used your {_fmt_usd(base_usd)} base for the month.</li>
        <li>Any <b>unspent balance carries over</b> month to month — it does not expire at
            the monthly reset, so nothing has to be re-applied.</li>
      </ul>
      <p style="color:#888;font-size:12px;">— AiNxt Platform</p>
    </body></html>
    """
    text_body = (
        f"Hi {user_name or user_email},\n\n"
        f"Congratulations — as a 10x winner you've been granted "
        f"{_fmt_usd(granted_extra_usd)} of extra AiNxt budget.\n\n"
        f"  Base budget (unchanged)      : {_fmt_usd(base_usd)}\n"
        f"  Extra budget granted         : {_fmt_usd(granted_extra_usd)}\n"
        f"{carried_row_text}"
        f"  Total extra balance          : {_fmt_usd(new_extra_usd)}\n"
        f"  Total spendable now          : {_fmt_usd(new_total_usd)}\n\n"
        f"How this works:\n"
        f"  - Your base budget stays at {_fmt_usd(base_usd)} — it is not replaced.\n"
        f"  - The extra budget is usable until exhausted, and is consumed only after\n"
        f"    you've used your {_fmt_usd(base_usd)} base for the month.\n"
        f"  - Any unspent balance carries over month to month — it does not expire at\n"
        f"    the monthly reset, so nothing has to be re-applied.\n\n"
        f"— AiNxt Platform\n"
    )

    try:
        return send_html_email(to=[user_email], subject=subject, html_body=html_body, text_body=text_body)
    except Exception as e:
        logger.warning(f"budget_request_email: failed to send winner email to {mask_email(user_email)}: {e}")
        return False
