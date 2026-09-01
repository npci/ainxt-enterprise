# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DISCUSSION NOTIFY — email people who were tagged on a discussion.
#
# When a user posts a discussion (question) the org-wide default recipients
# (configured via DISCUSSIONS_DEFAULT_NOTIFY_EMAILS in .env) are notified
# two ways:
#
#   1. In-app inbox  — handled inline in routers/discussions_router.py
#                      via store.inbox_store.publish_inbox_item (internal
#                      users only).
#   2. Email         — handled HERE, asynchronously, via an RQ job so the
#                      poster's request is never blocked and delivery is
#                      retried on failure (RQ retry -> DLQ).
#
# The HTML + plain-text bodies are rendered from shared Jinja templates in
# assets/email_templates/ (discussion_mention.html / .txt), the same
# convention used by services/monthly_statement_service.py and
# services/digest_service.py. Jinja autoescape handles HTML-escaping of all
# user-supplied values — no manual html.escape needed.
# ============================================================

from __future__ import annotations

import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

from services.smtp_service import send_html_email
from core.logger import logger, mask_email


# ── Template environment (mirrors monthly_statement_service.py) ─────────────
_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "email_templates",
)
_jinja = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


def _excerpt(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def send_discussion_email(payload: dict) -> bool:
    """RQ worker entrypoint.

    payload keys:
      to            : recipient email (str)
      author_name   : name of the person who posted (str)
      title         : discussion title (str)
      content       : discussion body (str)
      question_id   : AiNxt question id (str)

    Returns True on accepted send. Raises on hard failure so RQ can retry
    (2x) and then route the job to the DLQ — no custom retry logic here.
    """
    to = (payload.get("to") or "").strip()
    if not to:
        logger.warning("discussion_notify: no recipient in payload; skipping")
        return False

    author_name = payload.get("author_name") or "Someone"
    title = payload.get("title") or "a discussion"
    content = payload.get("content") or ""
    question_id = payload.get("question_id") or ""

    ctx = {
        "author_name": author_name,
        "title": title,
        "excerpt": _excerpt(content),
    }

    subject = f"{author_name} mentioned you in a discussion: {title}"
    html_body = _jinja.get_template("discussion_mention.html").render(**ctx)
    text_body = _jinja.get_template("discussion_mention.txt").render(**ctx)

    ok = send_html_email(to=[to], subject=subject, html_body=html_body, text_body=text_body)
    if not ok:
        # send_html_email returned falsy — raise so RQ retries / DLQs the job.
        raise RuntimeError(f"discussion_notify: relay did not accept mail to {to}")
    logger.info(f"discussion_notify: sent discussion email to {mask_email(to)} (question={question_id})")
    return True
