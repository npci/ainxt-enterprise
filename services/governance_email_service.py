# SPDX-License-Identifier: Apache-2.0
# ============================================================
# GOVERNANCE EMAIL SERVICE — SDLC governance pipeline notifications
#
# Sends two kinds of best-effort HTML emails through the internal SMTP
# relay (services.smtp_service.send_html_email):
#
#   1. notify_governance_teams_submitted(run_id, ...)
#        Fired when the author clicks "send to governance teams"
#        (routers.sdlc_router.author_submit_to_teams). Emails every domain
#        approver for the domains that still have open findings, telling them a
#        run is awaiting their review.
#
#   2. notify_author_of_decision(run_id, decision=..., ...)
#        Fired when a governance team approves the whole run (all domains
#        approved) or sends a domain back to the author. Emails the run author
#        (from run.context) with the outcome and any reviewer feedback.
#
# Templating mirrors the HOD digest / monthly-statement style: Jinja2 templates
# in assets/email_templates/ rendered through the shared `_jinja` environment
# defined in services.monthly_statement_service.
#
# Everything here is BEST-EFFORT: any failure is logged and swallowed so an
# email problem can never break the governance state transition or 500 an API
# response. Public functions return True on a successful send, False otherwise.
#
# Env overrides (all optional):
#   AINXT_GOVERNANCE_UI_BASE   base URL used to build the "open run" deep link.
#                              Falls back to core.config.PLATFORM_BASE_URL.
#                              When unset/blank the CTA link is simply omitted.
# ============================================================

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence

from core.logger import logger, mask_email


# ── IST label (mirrors services.digest_service._now_ist_label) ─────────────
_IST_TZ = timezone(timedelta(hours=5, minutes=30))


def _now_ist_label() -> str:
    return datetime.now(timezone.utc).astimezone(_IST_TZ).strftime("%d %b %Y, %H:%M IST")


# ── Template rendering (shared Jinja env) ──────────────────────────────────
def _render(template_name: str, payload: Dict[str, Any]) -> str:
    from services.monthly_statement_service import _jinja
    return _jinja.get_template(template_name).render(**payload)


# ── Deep-link helper ───────────────────────────────────────────────────────
def _run_url(run_id: str) -> str:
    """Best-effort deep link to the governance board for this run.

    Returns "" when no base URL is configured — the templates treat an empty
    string as "no CTA link" and simply omit the button.
    """
    base = (os.getenv("AINXT_GOVERNANCE_UI_BASE") or "").strip()
    if not base:
        try:
            from core.config import PLATFORM_BASE_URL
            base = (PLATFORM_BASE_URL or "").strip()
        except Exception:
            base = ""
    if not base:
        return ""
    return f"{base.rstrip('/')}/sdlc/runs/{run_id}/governance"


# ── Author resolution ──────────────────────────────────────────────────────
def _resolve_author(run: Dict[str, Any]) -> Dict[str, str]:
    """Resolve the run author's {email, name}.

    Prefers run.context.triggered_by_email, then falls back to run.created_by.
    Looks the value up in the users table (by email or id) to enrich the display
    name; degrades gracefully to just the email when the DB is unavailable.
    """
    ctx = run.get("context") or {}
    email = (ctx.get("triggered_by_email") or ctx.get("user_email") or "").strip()
    created_by = str(run.get("created_by") or "").strip()
    trig_uid = str(ctx.get("triggered_by_user_id") or ctx.get("user_id") or "").strip()

    name = ""
    try:
        from db.database import SessionLocal
        from db.models import User
        db = SessionLocal()
        try:
            row = None
            if email:
                row = db.query(User).filter(User.email.ilike(email)).first()
            if row is None and (trig_uid or created_by):
                for cand in (trig_uid, created_by):
                    if not cand:
                        continue
                    row = db.query(User).filter(User.id == cand).first()
                    if row is None:
                        row = db.query(User).filter(User.email.ilike(cand)).first()
                    if row is not None:
                        break
            if row is not None:
                email = email or (row.email or "")
                name = row.name or ""
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[SDLC-GOV-MAIL] author lookup failed (non-fatal)", error=str(exc))

    # Last resort: if we still have no email but created_by looks like one.
    if not email and "@" in created_by:
        email = created_by

    return {"email": email, "name": name}


def _run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    """Compact view of a run for the templates."""
    return {
        "id":           run.get("id") or "",
        "jira_key":     run.get("jira_key") or "",
        "jira_summary": run.get("jira_summary") or "",
        "repo":         run.get("repo") or (run.get("context") or {}).get("repo") or "",
        "branch":       run.get("branch") or (run.get("context") or {}).get("working_branch")
                        or (run.get("context") or {}).get("head_branch") or "",
    }


# ── PUBLIC: notify governance teams on submit ──────────────────────────────
def notify_governance_teams_submitted(run_id: str) -> bool:
    """Email every domain approver for domains still awaiting review.

    Called from author_submit_to_teams AFTER the pending domains have been
    seeded. Sends one email per approver, addressed to their domain, listing the
    run details and the domains awaiting their review. Best-effort throughout.
    """
    try:
        from store.sdlc_store import get_run
        run = get_run(run_id)
        if not run:
            logger.warning("[SDLC-GOV-MAIL] submit-notify: run not found", run_id=run_id)
            return False

        # Domains still awaiting review = pending (or changes_requested that was
        # just reset). We notify approvers for any non-approved domain.
        from store.sdlc_governance_approvers import list_domain_approvals, list_approvers
        approvals = list_domain_approvals(run_id) or []
        pending_domains = [
            (a.get("domain") or "").upper()
            for a in approvals
            if (a.get("status") or "pending") != "approved"
        ]
        pending_domains = [d for d in pending_domains if d]
        if not pending_domains:
            logger.info("[SDLC-GOV-MAIL] submit-notify: no pending domains", run_id=run_id)
            return False

        open_by_domain = {
            (a.get("domain") or "").upper(): a.get("open_count")
            for a in approvals
        }

        run_view = _run_summary(run)
        author = _resolve_author(run)
        run_url = _run_url(run_id)
        submitted_at = _now_ist_label()

        sent_any = False
        for domain in pending_domains:
            approvers = list_approvers(domain) or []
            recipients = sorted({
                (a.get("approver_email") or "").strip()
                for a in approvers
                if (a.get("approver_email") or "").strip()
            })
            if not recipients:
                logger.warning(
                    "[SDLC-GOV-MAIL] submit-notify: no approvers configured for domain",
                    domain=domain, run_id=run_id,
                )
                continue

            payload = {
                "run":              run_view,
                "author":           author,
                "domain_label":     domain,
                "domains":          [{"domain": domain, "open_count": open_by_domain.get(domain, 0)}],
                "run_url":          run_url,
                "submitted_at_ist": submitted_at,
            }
            try:
                html_body = _render("governance_submit_to_teams.html", payload)
                text_body = _render("governance_submit_to_teams.txt", payload)
            except Exception as exc:
                logger.error("[SDLC-GOV-MAIL] submit-notify render failed",
                             domain=domain, error=str(exc))
                continue

            subject = (
                f"AiNxt Governance — review requested for {run_view['jira_key'] or run_id} "
                f"[{domain}]"
            )
            if _send(recipients, subject, html_body, text_body):
                sent_any = True

        return sent_any
    except Exception as exc:
        logger.error("[SDLC-GOV-MAIL] notify_governance_teams_submitted crashed (non-fatal)",
                     run_id=run_id, error=str(exc))
        return False


# ── PUBLIC: notify author on approve / send-back ───────────────────────────
def notify_author_of_decision(
    run_id: str,
    decision: str,
    domain: Optional[str] = None,
    comment: Optional[str] = None,
    decided_by: Optional[str] = None,
) -> bool:
    """Email the run author about a governance decision.

    `decision` is one of:
      • "approved"           — the whole run is approved (all domains signed off).
      • "changes_requested"  — a domain was sent back to the author.

    For a send-back, pass the `domain`, the reviewer `comment`, and `decided_by`.
    Best-effort throughout.
    """
    try:
        decision = (decision or "").strip().lower()
        if decision not in ("approved", "changes_requested"):
            logger.warning("[SDLC-GOV-MAIL] decision-notify: unknown decision", decision=decision)
            return False

        from store.sdlc_store import get_run
        run = get_run(run_id)
        if not run:
            logger.warning("[SDLC-GOV-MAIL] decision-notify: run not found", run_id=run_id)
            return False

        author = _resolve_author(run)
        recipient = (author.get("email") or "").strip()
        if not recipient:
            logger.warning(
                "[SDLC-GOV-MAIL] decision-notify: could not resolve author email", run_id=run_id,
            )
            return False

        run_view = _run_summary(run)
        payload = {
            "run":            run_view,
            "author":         author,
            "decision":       decision,
            "domain":         (domain or "").upper(),
            "comment":        comment or "",
            "decided_by":     decided_by or "",
            "run_url":        _run_url(run_id),
            "decided_at_ist": _now_ist_label(),
        }
        try:
            html_body = _render("governance_author_decision.html", payload)
            text_body = _render("governance_author_decision.txt", payload)
        except Exception as exc:
            logger.error("[SDLC-GOV-MAIL] decision-notify render failed",
                         run_id=run_id, error=str(exc))
            return False

        if decision == "approved":
            subject = (
                f"AiNxt Governance — approved: {run_view['jira_key'] or run_id}"
            )
        else:
            subject = (
                f"AiNxt Governance — changes requested [{payload['domain']}]: "
                f"{run_view['jira_key'] or run_id}"
            )
        return _send([recipient], subject, html_body, text_body)
    except Exception as exc:
        logger.error("[SDLC-GOV-MAIL] notify_author_of_decision crashed (non-fatal)",
                     run_id=run_id, error=str(exc))
        return False


# ── Internal send wrapper ──────────────────────────────────────────────────
def _send(to: Sequence[str], subject: str, html_body: str, text_body: str) -> bool:
    try:
        from services.smtp_service import send_html_email
        ok = send_html_email(
            to=list(to),
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
        if ok:
            logger.info("[SDLC-GOV-MAIL] sent", subject=subject, to=[mask_email(e) for e in to])
        else:
            logger.warning("[SDLC-GOV-MAIL] send returned False", subject=subject, to=[mask_email(e) for e in to])
        return bool(ok)
    except Exception as exc:
        logger.error("[SDLC-GOV-MAIL] send failed",
                     subject=subject, to=list(to), error=str(exc))
        return False
