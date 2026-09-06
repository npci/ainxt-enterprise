#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt COACH — weekly digest worker
# ============================================================
#
# Builds a per-user 7-day practice digest and delivers it via:
#   1. the Inbox (publish_inbox_item type="coach_digest"), and
#   2. an HTML email (when SMTP is configured), skipping opt-outs.
#
# Gated by COACH_WEEKLY_MAIL_ENABLED. Intended to be run weekly by the
# scheduler thread in workers/start_workers.py (or via cron):
#   python -m workers.coach_weekly_mail_worker
#
# Idempotent and defensive: a failure delivering to one user never aborts the
# batch. Raw prompts are never included — the digest is built from scores +
# rule-hit recommendations only.
# ============================================================

import html
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

# CKMS first so SMTP/DB creds are decrypted.
try:
    from core.ckms import load_at_boot as _ckms_load_at_boot
    _ckms_load_at_boot()
except Exception:
    pass

from core.logger import logger, mask_email
from core.config import ENABLE_COACH, COACH_WEEKLY_MAIL_ENABLED


def _active_user_ids(db, days: int = 7) -> List[str]:
    """User ids with at least one coach event in the window."""
    from db.models import CoachEvent
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(CoachEvent.user_id)
            .filter(CoachEvent.ts >= since)
            .distinct().all())
    return [r[0] for r in rows if r[0]]


def _opted_out(db) -> set:
    from db.models import CoachWeeklyMailOptOut
    return {r.user_id for r in db.query(CoachWeeklyMailOptOut).all()}


def _resolve_email(db, user_id: str) -> Optional[str]:
    """Best-effort resolve a user_id (may be a sub/uuid or an email) to email."""
    if user_id and "@" in user_id:
        return user_id
    try:
        from db.models import User
        u = (db.query(User).filter(User.id == user_id).first()
             or db.query(User).filter(User.email == user_id).first())
        return u.email if u else None
    except Exception:
        return None


def _coach_usage(db, user_id: str, days: int = 7) -> dict:
    from sqlalchemy import func
    from db.models import CoachEvent

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(CoachEvent.channel,
                     func.count(CoachEvent.event_id),
                     func.coalesce(func.sum(CoachEvent.cost_usd), 0.0))
            .filter(CoachEvent.user_id == user_id, CoachEvent.ts >= since)
            .group_by(CoachEvent.channel)
            .order_by(func.count(CoachEvent.event_id).desc())
            .all())
    channels = [
        {"channel": c or "unknown", "events": int(n or 0), "cost_usd": round(float(cost or 0.0), 6)}
        for c, n, cost in rows
    ]
    return {
        "events": sum(row["events"] for row in channels),
        "cost_usd": round(sum(row["cost_usd"] for row in channels), 6),
        "channels": channels,
    }


def _build_html(user_id: str, scores: dict, recs: list,
                usage: Optional[dict] = None,
                custom_note: str = "",
                task_analysis: Optional[dict] = None) -> str:
    esc = html.escape
    overall = scores.get("overall")
    overall_str = f"{overall:.0f}/100" if isinstance(overall, (int, float)) else "n/a"
    event_count = int(scores.get("event_count") or 0)
    usage = usage or {}

    cat_rows = "".join(
        f"<tr><td>{esc(str(cat))}</td><td><b>{val:.0f}</b></td></tr>"
        for cat, val in (scores.get("categories") or {}).items()
        if isinstance(val, (int, float))
    ) or "<tr><td>No category scores yet</td><td><b>—</b></td></tr>"

    rec_items = "".join(
        f"<li><b>{esc(str(r.get('title', '')))}</b><span>{esc(str(r.get('advice', '')))}</span></li>"
        for r in recs[:5]
    ) or "<li><b>Great work</b><span>No recurring issues to flag this week.</span></li>"

    channel_rows = "".join(
        f"<tr><td>{esc(str(row.get('channel') or 'unknown'))}</td><td>{int(row.get('events') or 0)}</td><td>${float(row.get('cost_usd') or 0.0):.4f}</td></tr>"
        for row in usage.get("channels", [])[:6]
    ) or "<tr><td>No usage yet</td><td>0</td><td>$0.0000</td></tr>"

    # Optional task-analysis block — rendered when task_analysis data is provided.
    task_block = ""
    if task_analysis and task_analysis.get("total_events", 0) > 0:
        domain_rows_html = ""
        for d in task_analysis.get("domains", []):
            issues_html = ""
            if d.get("top_issues"):
                issues_html = "<ul style='margin:8px 0 0;padding-left:18px;'>"
                for issue in d["top_issues"]:
                    issues_html += (
                        f"<li style='margin-bottom:6px;font-size:13px;color:#334155'>"
                        f"<span style='font-weight:600;color:#4f46e5'>[{esc(issue['category'])}]</span> "
                        f"{esc(issue['tip'])}</li>"
                    )
                issues_html += "</ul>"
            else:
                issues_html = "<p style='margin:6px 0 0;font-size:13px;color:#10b981'>✓ No issues detected — great work!</p>"
            domain_rows_html += (
                f"<div style='margin-bottom:14px;padding:12px;background:#f8fafc;"
                f"border:1px solid #e2e8f0;border-radius:12px'>"
                f"<div style='font-size:13px;font-weight:700;color:#172033'>"
                f"{esc(d['label'])} "
                f"<span style='font-weight:400;color:#667085'>({d['pct']}% · {d['count']} interaction(s))</span>"
                f"</div>"
                f"{issues_html}"
                f"</div>"
            )
        task_block = (
            f'<div class="card" style="margin-top:14px;border-left:4px solid #7c3aed">'
            f'<div class="label">Task-type analysis</div>'
            f'<p style="margin:8px 0 12px;font-size:13px;color:#334155;line-height:1.6">'
            f'{esc(task_analysis.get("summary", ""))}</p>'
            f'{domain_rows_html}'
            f'</div>'
        )

    # Optional admin note block — only rendered when a note was provided.
    note_block = ""
    if custom_note and custom_note.strip():
        note_block = (
            f'<div class="card" style="margin-top:14px;border-left:4px solid #4f46e5">'
            f'<div class="label">Note from your coach</div>'
            f'<p style="margin:10px 0 0;font-size:14px;line-height:1.6;color:#172033">'
            f'{esc(custom_note.strip()).replace(chr(10), "<br>")}'
            f'</p></div>'
        )

    return f"""\
<html><head><meta charset="utf-8" />
<style>
  body{{margin:0;background:#f5f7fb;font-family:Segoe UI,Arial,sans-serif;color:#172033}}
  .wrap{{max-width:760px;margin:0 auto;padding:28px}}
  .hero{{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:white;border-radius:24px;padding:28px;box-shadow:0 18px 45px rgba(79,70,229,.22)}}
  .hero h1{{margin:0 0 8px;font-size:26px}} .hero p{{margin:0;opacity:.9}}
  .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:18px 0}}
  .card{{background:white;border:1px solid #e7eaf3;border-radius:18px;padding:18px;box-shadow:0 8px 24px rgba(15,23,42,.06)}}
  .label{{font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:.04em}} .value{{font-size:26px;font-weight:800;margin-top:6px}}
  table{{width:100%;border-collapse:collapse}} td{{padding:10px 0;border-bottom:1px solid #edf0f7;font-size:14px}} td:last-child{{text-align:right}}
  ul{{list-style:none;margin:0;padding:0}} li{{padding:12px 0;border-bottom:1px solid #edf0f7}} li span{{display:block;color:#667085;margin-top:4px}}
  .foot{{color:#7b8498;font-size:12px;margin-top:18px;text-align:center}}
</style></head><body><div class="wrap">
  <div class="hero"><h1>Your AiNxt Coach summary</h1><p>No prompt content is stored or shown — only practice signals and scores.</p></div>
  <div class="grid">
    <div class="card"><div class="label">Practice score</div><div class="value">{esc(overall_str)}</div></div>
    <div class="card"><div class="label">Events analysed</div><div class="value">{event_count}</div></div>
    <div class="card"><div class="label">Spend observed</div><div class="value">${float(usage.get('cost_usd') or 0.0):.4f}</div></div>
  </div>
  <div class="card"><div class="label">Category scores</div><table>{cat_rows}</table></div>
  <div class="card" style="margin-top:14px"><div class="label">Usage by channel</div><table>{channel_rows}</table></div>
  <div class="card" style="margin-top:14px"><div class="label">Top opportunities</div><ul>{rec_items}</ul></div>
  {task_block}
  {note_block}
  <div class="foot">This is an automated coaching summary from AiNxt Coach.</div>
</div></body></html>"""


def run_weekly_digest(force: bool = False) -> dict:
    """Generate and deliver weekly digests. Returns a summary dict.

    `force` bypasses the COACH_WEEKLY_MAIL_ENABLED gate (for manual runs)."""
    if not ENABLE_COACH:
        logger.info("coach_weekly_mail: ENABLE_COACH is off — skipping")
        return {"skipped": "coach_disabled"}
    if not COACH_WEEKLY_MAIL_ENABLED and not force:
        logger.info("coach_weekly_mail: COACH_WEEKLY_MAIL_ENABLED is off — skipping")
        return {"skipped": "weekly_mail_disabled"}

    from db.database import SessionLocal
    from agents.coach_evaluator import compute_scores, publish_coach_inbox
    from agents.coach_recommender import recommend_for_user

    db = SessionLocal()
    delivered = 0
    mailed = 0
    skipped = 0
    try:
        users = _active_user_ids(db, days=7)
        opt_outs = _opted_out(db)
        logger.info(f"coach_weekly_mail: {len(users)} active user(s), {len(opt_outs)} opt-out(s)")

        for uid in users:
            try:
                scores = compute_scores(uid, days=7, db=db)
                recs = recommend_for_user(uid, days=7, limit=5, db=db)

                # Inbox digest — always (cheap, in-app).
                overall = scores.get("overall")
                usage = _coach_usage(db, uid, days=7)
                html_body = _build_html(uid, scores, recs, usage)
                body_lines = [f"Events this week: {scores.get('event_count', 0)}."]
                if isinstance(overall, (int, float)):
                    body_lines.append(f"Overall practice score: {overall:.0f}/100.")
                for r in recs[:3]:
                    body_lines.append(f"• {r.get('title','')}: {r.get('advice','')}")
                publish_coach_inbox(
                    uid,
                    title="Your weekly AiNxt Coach summary",
                    body="\n".join(body_lines),
                    source_id=f"weekly:{datetime.now(timezone.utc):%Y-%m-%d}",
                    metadata={
                        "kind": "weekly_digest",
                        "overall": overall,
                        "overall_score": overall,
                        "usage": usage,
                        "html_body": html_body,
                        "scores": scores,
                        "recs": [{"title": r.get("title",""), "advice": r.get("advice",""), "category": r.get("category",""), "severity": r.get("severity","low"), "count": r.get("count",0)} for r in recs[:5]],
                    },
                )
                delivered += 1

                # Email — only when not opted out and SMTP available.
                if uid in opt_outs:
                    skipped += 1
                    continue
                email = _resolve_email(db, uid)
                if not email:
                    continue
                try:
                    from services.smtp_service import send_html_email
                    if send_html_email([email], "Your AiNxt Coach — weekly summary", html_body):
                        mailed += 1
                except Exception as e:
                    logger.warning(f"coach_weekly_mail: email to {mask_email(email)} failed ({e.__class__.__name__})")
            except Exception as e:
                logger.error(f"coach_weekly_mail: digest for {uid} failed ({e.__class__.__name__}: {e})")

        summary = {"users": len(users), "delivered": delivered, "mailed": mailed, "skipped": skipped}
        logger.info(f"coach_weekly_mail: done — {summary}")
        return summary
    finally:
        db.close()


if __name__ == "__main__":
    force = "--force" in sys.argv
    run_weekly_digest(force=force)
