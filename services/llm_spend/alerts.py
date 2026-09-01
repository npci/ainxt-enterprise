# SPDX-License-Identifier: Apache-2.0
# ============================================================
# services.llm_spend.alerts
#
# High-priority email to the on-call list when a digest is about
# to fire but the underlying fetch hasn't covered the period.
#
# Delivery: services.smtp_service.send_html_email — same AiNxt relay
# as the digests. Subject prefixed [URGENT].
# ============================================================

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable, List, Sequence, Tuple

from sqlalchemy import text

from core.logger import logger
from db.database import SessionLocal
from services.llm_spend.recipients import resolve_alert_recipients
from services.smtp_service import send_html_email


_RECENT_RUNS_SQL = text(
    """
    -- One row per (provider, window) showing the LATEST attempt only.
    -- Multi-worker fanout produces N identical 'failed' rows at the
    -- same minute; rendering all of them turns the URGENT mail into
    -- a wall of duplicate retry noise. id DESC after run_started DESC
    -- keeps the tie-break deterministic when two retries share a
    -- truncated timestamp.
    SELECT DISTINCT ON (provider, window_start, window_end)
           provider, window_start, window_end, status,
           rows_upserted, error_text, run_started, run_finished
    FROM ainxt.llm_spend_fetch_runs
    WHERE window_end >= :since
    ORDER BY provider, window_start, window_end, run_started DESC, id DESC
    LIMIT 50
    """
)


# ── dedup: only one mail per (kind, cadence, window, dedup_key) ──────────
#
# Why: APScheduler in services/llm_spend/gateway_bootstrap.py is
# configured per-process (max_instances=1, coalesce=True). When the
# gateway runs with multiple uvicorn workers or replicas every process
# fires its own copy of the cron job at the same minute, and the admin
# router can also re-trigger the same path. Previously each call sent a
# fresh URGENT mail, producing 10+ duplicates in the admin inbox.
#
# The fix: INSERT ... ON CONFLICT DO NOTHING RETURNING id is atomic in
# Postgres. Exactly one of the racing callers gets a non-empty RETURNING
# (the "winner") and proceeds to send; all others get an empty result and
# silently no-op. Schema lives in
# db/sql/prod_catchup_2026_06_19_llm_spend_alerts_sent.sql.

_CLAIM_SQL = text(
    """
    INSERT INTO ainxt.llm_spend_alerts_sent
        (kind, cadence, window_start, window_end, dedup_key,
         recipients, smtp_ok, sent_at)
    VALUES
        (:kind, :cadence, :ws, :we, :dedup_key,
         0, FALSE, NOW())
    ON CONFLICT (kind, cadence, window_start, window_end, dedup_key)
    DO NOTHING
    RETURNING id
    """
)

_MARK_SENT_SQL = text(
    """
    UPDATE ainxt.llm_spend_alerts_sent
       SET recipients = :recipients,
           smtp_ok    = :smtp_ok
     WHERE id = :id
    """
)


def _claim_alert(
    kind:      str,
    cadence:   str,
    ws:        date,
    we:        date,
    dedup_key: str,
) -> int | None:
    """Atomically claim the right to send one mail for this outage.

    Returns the new row's id if THIS process won the race; None if another
    worker / replica / earlier call already claimed it (in which case the
    caller MUST NOT send).
    """
    # dedup_key is bounded to VARCHAR(255) in the DDL; truncate defensively
    # so a freak long missing-dates list never blows up the INSERT.
    key = (dedup_key or "")[:255]
    try:
        with SessionLocal() as session:
            row = session.execute(
                _CLAIM_SQL,
                {"kind": kind, "cadence": cadence,
                 "ws":   ws,   "we":      we,
                 "dedup_key": key},
            ).first()
            session.commit()
        return row[0] if row else None
    except Exception as e:
        # Fail-open: if the dedup table is unreachable we'd rather send a
        # duplicate than swallow a real incident. Log loudly so ops notice.
        logger.error(
            f"[llm_spend.alerts] dedup claim failed ({kind}/{cadence} "
            f"{ws}->{we}): {e}; sending without dedup"
        )
        return -1   # sentinel: "winner", but no row to back-fill


def _record_send(claim_id: int, recipients: int, smtp_ok: bool) -> None:
    """Back-fill audit fields on the claim row after SMTP returns.

    Best-effort: a failed UPDATE must never cause a re-send, so we swallow
    exceptions. The unique row already exists from _claim_alert(), which
    is what actually enforces dedup.
    """
    if claim_id is None or claim_id < 0:
        return
    try:
        with SessionLocal() as session:
            session.execute(
                _MARK_SENT_SQL,
                {"id": claim_id,
                 "recipients": recipients,
                 "smtp_ok":    smtp_ok},
            )
            session.commit()
    except Exception as e:
        logger.warning(f"[llm_spend.alerts] _record_send failed for id={claim_id}: {e}")


# ── digest send dedup (multi-worker / multi-replica safety) ──────────────
#
# Why: gateway_bootstrap.start() registers the APScheduler jobs IN-PROCESS.
# With N uvicorn workers (e.g. N=8 in a multi-worker deployment) every worker
# fires its own copy of the digest cron at the same minute. The alert mails
# are already deduped via _claim_alert; the DIGEST send was not — so without
# this guard all N workers would each build + SMTP the same exec digest,
# producing N identical mails in execs' inboxes.
#
# The fix reuses the exact same atomic INSERT ... ON CONFLICT DO NOTHING
# RETURNING id pattern and the same ainxt.llm_spend_alerts_sent table, with
# kind='digest_sent'. Exactly one racing worker gets a non-empty RETURNING
# (the "winner") and proceeds to send; all others see the conflict and
# no-op. dedup_key is the period_label so a re-trigger for a *different*
# window (different label) is still allowed through.

def claim_digest_send(
    cadence:      str,
    window_start: date,
    window_end:   date,
    dedup_key:    str,
) -> int | None:
    """Atomically claim the right to SEND one digest for this (cadence, window).

    Returns the claim row id if THIS worker won the race (caller MUST send);
    None if another worker already claimed it (caller MUST NOT send). On a
    dedup-table error it fails OPEN (returns sentinel -1) so a transient DB
    blip never silently suppresses the exec digest entirely.
    """
    return _claim_alert(
        kind="digest_sent",
        cadence=cadence,
        ws=window_start, we=window_end,
        dedup_key=dedup_key,
    )


def record_digest_send(claim_id: int | None, recipients: int, smtp_ok: bool) -> None:
    """Back-fill audit fields on the digest claim row after SMTP returns."""
    _record_send(claim_id, recipients=recipients, smtp_ok=smtp_ok)


# ── nightly fetch dedup (multi-worker / multi-replica safety) ────────────
#
# Same root cause as the digest dedup above: every uvicorn worker registers
# its own APScheduler nightly-fetch job, so the 01:30 fetch fires N times
# and all N workers hammer the provider cost APIs (via llm_proxy) for the
# identical window. The UPSERTs are idempotent so the DATA is fine, but it
# is N× the upstream API calls and N× fetch_runs rows every night.
#
# This claim lets exactly ONE worker run the nightly fetch per window. We
# reuse ainxt.llm_spend_alerts_sent with kind='fetch_run'; dedup_key is the
# window string so a *different* window (e.g. an admin backfill of another
# range) is never blocked by tonight's claim. Fails OPEN (sentinel -1) on a
# dedup-table error so a DB blip never silently skips the whole night's fetch.

def claim_fetch_run(
    cadence:      str,
    window_start: date,
    window_end:   date,
    dedup_key:    str,
) -> int | None:
    """Atomically claim the right to RUN the fetch for this (cadence, window).

    Returns the claim row id if THIS worker won the race (caller MUST fetch);
    None if another worker already claimed it (caller MUST NOT fetch).
    """
    return _claim_alert(
        kind="fetch_run",
        cadence=cadence,
        ws=window_start, we=window_end,
        dedup_key=dedup_key,
    )


def alert_missing_fetch(
    cadence:      str,
    period_label: str,
    window_start: date,
    window_end:   date,
    missing_dates: Sequence[date],
) -> bool:
    """Email the on-call list. Returns True on send success.

    Idempotent across workers / replicas via ainxt.llm_spend_alerts_sent:
    repeat invocations for the same (cadence, window, missing_dates) are
    suppressed after the first successful claim. "Already sent" is
    reported back as True so the orchestrator doesn't retry.
    """
    to = resolve_alert_recipients()
    if not to:
        logger.error("[llm_spend.alerts] no alert recipients configured")
        return False

    missing_csv = ", ".join(d.isoformat() for d in missing_dates) or "(none)"

    # Claim dedup BEFORE building HTML / calling SMTP. If another worker
    # already sent this exact alert, _claim_alert returns None and we
    # silently no-op.
    claim_id = _claim_alert(
        kind="missing_fetch",
        cadence=cadence,
        ws=window_start, we=window_end,
        dedup_key=missing_csv,
    )
    if claim_id is None:
        logger.info(
            f"[llm_spend.alerts] missing_fetch alert already sent for "
            f"cadence={cadence} {window_start}->{window_end} ({missing_csv}); "
            f"suppressing duplicate"
        )
        return True

    # Recent fetch_runs for the table
    with SessionLocal() as session:
        rows = session.execute(_RECENT_RUNS_SQL, {"since": window_start}).fetchall()

    runs_html = "".join(
        f"<tr><td>{r.provider}</td>"
        f"<td>{r.window_start} → {r.window_end}</td>"
        f"<td>{r.status}</td>"
        f"<td>{r.rows_upserted}</td>"
        f"<td>{(r.error_text or '')[:200]}</td>"
        f"<td>{r.run_started:%Y-%m-%d %H:%M} → {(r.run_finished and r.run_finished.strftime('%H:%M')) or '—'}</td></tr>"
        for r in rows
    ) or '<tr><td colspan="6">no recent fetch runs</td></tr>'

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#222">
  <h2 style="color:#b00020;margin:0 0 8px">[URGENT] AiNxt LLM spend — {cadence} digest skipped</h2>
  <p>The <b>{cadence}</b> digest for <b>{period_label}</b>
     (window {window_start} → {window_end}) was about to send, but no successful
     fetch run covers the following date(s):</p>
  <p style="background:#fff3cd;padding:8px;border:1px solid #ffeeba"><code>{missing_csv}</code></p>
  <p>The exec email has been suppressed. To recover:</p>
  <pre style="background:#f6f8fa;padding:8px;border-radius:4px">curl -X POST \\
  -H "Authorization: Bearer &lt;admin-jwt&gt;" \\
  "https://<YOUR_BASE_URL>/ainxt/v1/api/admin/llm-spend/fetch?for_date=YYYY-MM-DD"</pre>
  <p>After a successful fetch, re-trigger the digest:</p>
  <pre style="background:#f6f8fa;padding:8px;border-radius:4px">curl -X POST \\
  -H "Authorization: Bearer &lt;admin-jwt&gt;" \\
  "https://<YOUR_BASE_URL>/ainxt/v1/api/admin/llm-spend/email/{cadence}"</pre>
  <h3 style="margin-top:24px">Recent fetch_runs (window_end &gt;= {window_start})</h3>
  <table cellspacing="0" cellpadding="6" border="1" style="border-collapse:collapse;font-size:13px">
    <thead style="background:#f1f3f5"><tr>
      <th>provider</th><th>window</th><th>status</th><th>rows</th><th>error</th><th>started → finished</th>
    </tr></thead>
    <tbody>{runs_html}</tbody>
  </table>
  <p style="color:#666;font-size:12px;margin-top:24px">
    This alert was generated automatically by services/llm_spend/alerts.py.
    Recipients are configured via the LLM_SPEND_ALERT_EMAILS environment variable.
  </p>
</body></html>"""

    text_body = (
        f"[URGENT] AiNxt LLM spend — {cadence} digest skipped\n"
        f"Period: {period_label} ({window_start} → {window_end})\n"
        f"Missing dates: {missing_csv}\n\n"
        f"Recover with POST /ainxt/v1/api/admin/llm-spend/fetch?for_date=...\n"
        f"Then POST /ainxt/v1/api/admin/llm-spend/email/{cadence}\n"
    )

    try:
        ok = send_html_email(
            to        = to,
            subject   = f"[URGENT][AiNxt LLM Spend] {cadence} digest skipped — fetch missing for {missing_csv}",
            html_body = html,
            text_body = text_body,
        )
        if ok:
            logger.warning(
                f"[llm_spend.alerts] alert sent to {len(to)} recipients "
                f"for cadence={cadence} missing={missing_csv}"
            )
        else:
            logger.error(f"[llm_spend.alerts] SMTP returned False for cadence={cadence}")
        _record_send(claim_id, recipients=len(to), smtp_ok=bool(ok))
        return bool(ok)
    except Exception as e:
        logger.error(f"[llm_spend.alerts] send failed: {e}")
        _record_send(claim_id, recipients=len(to), smtp_ok=False)
        return False


# ── PARTIAL outage: digest shipped without some providers ────────────────
#
# Fired (for ANY cadence) when at least one — but NOT all — required
# providers were down for the digest window. The exec digest is still sent
# carrying the surviving providers; this alert tells the on-call list which
# provider(s) were dropped so the gap can be backfilled. Distinct from
# alert_missing_fetch / alert_failed_fetch, which are TOTAL-outage alerts
# fired only when the digest is fully cancelled.

def alert_partial_fetch(
    cadence:       str,
    period_label:  str,
    window_start:  date,
    window_end:    date,
    down:          Sequence[str],
) -> bool:
    """Email LLM_SPEND_ALERT_EMAILS that the digest shipped WITHOUT `down`.

    The exec digest was still sent (some providers had usable data), so this
    is informational/recovery rather than a hard outage. Returns True on send
    success. Idempotent across workers / replicas via
    ainxt.llm_spend_alerts_sent keyed on (kind=partial_fetch, cadence,
    window, providers_csv).
    """
    to = resolve_alert_recipients()
    if not to:
        logger.error("[llm_spend.alerts] no alert recipients configured")
        return False

    providers = sorted(set(down)) or ["(unknown)"]
    providers_csv = ", ".join(providers)

    claim_id = _claim_alert(
        kind="partial_fetch",
        cadence=cadence,
        ws=window_start, we=window_end,
        dedup_key=providers_csv,
    )
    if claim_id is None:
        logger.info(
            f"[llm_spend.alerts] partial_fetch alert already sent for "
            f"{cadence} {window_start}->{window_end} (providers={providers_csv}); "
            f"suppressing duplicate"
        )
        return True

    # Recent fetch_runs for context.
    with SessionLocal() as session:
        rows = session.execute(_RECENT_RUNS_SQL, {"since": window_start}).fetchall()

    runs_html = "".join(
        f"<tr><td>{r.provider}</td>"
        f"<td>{r.window_start} → {r.window_end}</td>"
        f"<td>{r.status}</td>"
        f"<td>{r.rows_upserted}</td>"
        f"<td>{(r.error_text or '')[:200]}</td>"
        f"<td>{r.run_started:%Y-%m-%d %H:%M} → {(r.run_finished and r.run_finished.strftime('%H:%M')) or '—'}</td></tr>"
        for r in rows
    ) or '<tr><td colspan="6">no recent fetch runs</td></tr>'

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#222">
  <h2 style="color:#b36b00;margin:0 0 8px">[WARN] AiNxt LLM spend — {cadence} digest shipped without some providers</h2>
  <p>The <b>{cadence}</b> digest for <b>{period_label}</b>
     (window {window_start} → {window_end}) was <b>still sent</b> to execs, but
     the following provider(s) had no usable data for the window and were
     <b>omitted</b> from the report:</p>
  <p style="background:#fff3cd;padding:8px;border:1px solid #ffeeba"><code>{providers_csv}</code></p>
  <p>The exec numbers therefore exclude the provider(s) above. To backfill
     and re-send a complete digest:</p>
  <pre style="background:#f6f8fa;padding:8px;border-radius:4px">curl -X POST \\
  -H "Authorization: Bearer &lt;admin-jwt&gt;" \\
  "https://<YOUR_BASE_URL>/ainxt/v1/api/admin/llm-spend/fetch?for_date=YYYY-MM-DD"

curl -X POST \\
  -H "Authorization: Bearer &lt;admin-jwt&gt;" \\
  "https://<YOUR_BASE_URL>/ainxt/v1/api/admin/llm-spend/email/{cadence}"</pre>
  <h3 style="margin-top:24px">Recent fetch_runs (window_end &gt;= {window_start})</h3>
  <table cellspacing="0" cellpadding="6" border="1" style="border-collapse:collapse;font-size:13px">
    <thead style="background:#f1f3f5"><tr>
      <th>provider</th><th>window</th><th>status</th><th>rows</th><th>error</th><th>started → finished</th>
    </tr></thead>
    <tbody>{runs_html}</tbody>
  </table>
  <p style="color:#666;font-size:12px;margin-top:24px">
    This alert was generated automatically by services/llm_spend/alerts.py
    (partial-outage path). Recipients are configured via the
    LLM_SPEND_ALERT_EMAILS environment variable.
  </p>
</body></html>"""

    text_body = (
        f"[WARN] AiNxt LLM spend — {cadence} digest shipped without some providers\n"
        f"Period: {period_label} ({window_start} → {window_end})\n"
        f"Omitted providers: {providers_csv}\n\n"
        f"The exec digest was sent with the remaining providers.\n"
        f"Backfill with POST /ainxt/v1/api/admin/llm-spend/fetch?for_date=...\n"
        f"Then POST /ainxt/v1/api/admin/llm-spend/email/{cadence}\n"
    )

    try:
        ok = send_html_email(
            to        = to,
            subject   = f"[WARN][AiNxt LLM Spend] {cadence} digest shipped without {providers_csv}",
            html_body = html,
            text_body = text_body,
        )
        if ok:
            logger.warning(
                f"[llm_spend.alerts] partial-fetch alert sent to {len(to)} "
                f"recipients ({cadence}, omitted={providers_csv})"
            )
        else:
            logger.error(f"[llm_spend.alerts] SMTP returned False for partial-fetch ({cadence})")
        _record_send(claim_id, recipients=len(to), smtp_ok=bool(ok))
        return bool(ok)
    except Exception as e:
        logger.error(f"[llm_spend.alerts] partial-fetch alert send failed: {e}")
        _record_send(claim_id, recipients=len(to), smtp_ok=False)
        return False


# ── misconfiguration: cadence has no To: recipients configured ───────────
#
# Fired when a digest is due but its per-cadence To: env var
# (LLM_SPEND_{DAILY,WEEKLY,MONTHLY,QUARTERLY}_TO) is empty/unset. The exec
# digest cannot be sent (no audience), so we alert the on-call list to fix
# the env rather than silently dropping the report.

def alert_missing_recipients(
    cadence:      str,
    period_label: str,
    window_start: date,
    window_end:   date,
    env_var:      str,
) -> bool:
    """Email LLM_SPEND_ALERT_EMAILS that `cadence` has no To: configured.

    Idempotent across workers via ainxt.llm_spend_alerts_sent keyed on
    (kind=no_recipients, cadence, window, env_var).
    """
    to = resolve_alert_recipients()
    if not to:
        logger.error("[llm_spend.alerts] no alert recipients configured")
        return False

    claim_id = _claim_alert(
        kind="no_recipients",
        cadence=cadence,
        ws=window_start, we=window_end,
        dedup_key=env_var or "(unset)",
    )
    if claim_id is None:
        logger.info(
            f"[llm_spend.alerts] no_recipients alert already sent for "
            f"{cadence} {window_start}->{window_end} ({env_var}); suppressing duplicate"
        )
        return True

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#222">
  <h2 style="color:#b00020;margin:0 0 8px">[URGENT] AiNxt LLM spend — {cadence} digest not sent (no recipients)</h2>
  <p>The <b>{cadence}</b> digest for <b>{period_label}</b>
     (window {window_start} → {window_end}) could not be sent because its
     recipient environment variable is empty or unset:</p>
  <p style="background:#fff3cd;padding:8px;border:1px solid #ffeeba"><code>{env_var}</code></p>
  <p>Set <code>{env_var}</code> to a comma-separated list of recipient
     addresses and re-trigger the digest:</p>
  <pre style="background:#f6f8fa;padding:8px;border-radius:4px">curl -X POST \\
  -H "Authorization: Bearer &lt;admin-jwt&gt;" \\
  "https://<YOUR_BASE_URL>/ainxt/v1/api/admin/llm-spend/email/{cadence}"</pre>
  <p style="color:#666;font-size:12px;margin-top:24px">
    This alert was generated automatically by services/llm_spend/alerts.py
    (no-recipients path). Recipients are configured via the
    LLM_SPEND_ALERT_EMAILS environment variable.
  </p>
</body></html>"""

    text_body = (
        f"[URGENT] AiNxt LLM spend — {cadence} digest not sent (no recipients)\n"
        f"Period: {period_label} ({window_start} → {window_end})\n"
        f"Unset/empty env var: {env_var}\n\n"
        f"Set {env_var} to a CSV of addresses, then POST "
        f"/ainxt/v1/api/admin/llm-spend/email/{cadence}\n"
    )

    try:
        ok = send_html_email(
            to        = to,
            subject   = f"[URGENT][AiNxt LLM Spend] {cadence} digest not sent — {env_var} is empty",
            html_body = html,
            text_body = text_body,
        )
        if ok:
            logger.warning(
                f"[llm_spend.alerts] no-recipients alert sent to {len(to)} "
                f"recipients ({cadence}, env={env_var})"
            )
        else:
            logger.error(f"[llm_spend.alerts] SMTP returned False for no-recipients ({cadence})")
        _record_send(claim_id, recipients=len(to), smtp_ok=bool(ok))
        return bool(ok)
    except Exception as e:
        logger.error(f"[llm_spend.alerts] no-recipients alert send failed: {e}")
        _record_send(claim_id, recipients=len(to), smtp_ok=False)
        return False


# ── DAILY-ONLY: provider fetch failure alert ─────────────────────────────
#
# Fired by the daily digest path when at least one provider's fetch run
# returned status='failed' for the digest window. The daily digest is
# cancelled and this alert goes to LLM_SPEND_ALERT_EMAILS instead.
# Weekly / monthly / quarterly digests do NOT call this — see the comment
# in report_builder.failed_fetch_runs for the rationale.

def alert_failed_fetch(
    period_label: str,
    window_start: date,
    window_end:   date,
    failed_runs:  Iterable,           # Iterable[report_builder.FailedFetchRun]
) -> bool:
    """Email LLM_SPEND_ALERT_EMAILS that today's digest was cancelled.

    Returns True on send success. `failed_runs` is whatever
    `report_builder.failed_fetch_runs(...)` returned for the window.

    Idempotent across workers / replicas via ainxt.llm_spend_alerts_sent
    keyed on (cadence=daily, window, providers_csv): repeat invocations
    for the same outage are suppressed after the first successful claim.
    """
    to = resolve_alert_recipients()
    if not to:
        logger.error("[llm_spend.alerts] no alert recipients configured")
        return False

    runs = list(failed_runs)
    providers = sorted({r.provider for r in runs}) or ["(unknown)"]
    providers_csv = ", ".join(providers)

    # Claim dedup BEFORE building HTML / calling SMTP. If another worker
    # already sent this exact alert, _claim_alert returns None and we
    # silently no-op.
    claim_id = _claim_alert(
        kind="failed_fetch",
        cadence="daily",
        ws=window_start, we=window_end,
        dedup_key=providers_csv,
    )
    if claim_id is None:
        logger.info(
            f"[llm_spend.alerts] failed_fetch alert already sent for "
            f"daily {window_start}->{window_end} (providers={providers_csv}); "
            f"suppressing duplicate"
        )
        return True

    rows_html = "".join(
        f"<tr><td>{r.provider}</td>"
        f"<td>{r.window_start} → {r.window_end}</td>"
        f"<td>{r.run_started:%Y-%m-%d %H:%M}</td>"
        f"<td><code>{(r.error_text or '')[:300]}</code></td></tr>"
        for r in runs
    ) or '<tr><td colspan="4">no failed runs (logic error?)</td></tr>'

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#222">
  <h2 style="color:#b00020;margin:0 0 8px">[URGENT] AiNxt LLM spend — daily digest cancelled</h2>
  <p>The <b>daily</b> digest for <b>{period_label}</b>
     (window {window_start} → {window_end}) was cancelled because the nightly
     fetch from the following provider(s) <b>failed</b>:</p>
  <p style="background:#fff3cd;padding:8px;border:1px solid #ffeeba"><code>{providers_csv}</code></p>
  <p>The exec email has been suppressed so execs do not see an incomplete
     number for the affected provider(s). To recover, re-run the failed
     fetch(es) and then re-trigger today's digest:</p>
  <pre style="background:#f6f8fa;padding:8px;border-radius:4px">curl -X POST \\
  -H "Authorization: Bearer &lt;admin-jwt&gt;" \\
  "https://<YOUR_BASE_URL>/ainxt/v1/api/admin/llm-spend/fetch?for_date={window_start.isoformat()}"

curl -X POST \\
  -H "Authorization: Bearer &lt;admin-jwt&gt;" \\
  "https://<YOUR_BASE_URL>/ainxt/v1/api/admin/llm-spend/email/daily?for_date={window_start.isoformat()}"</pre>
  <h3 style="margin-top:24px">Failed fetch_runs overlapping {window_start} → {window_end}</h3>
  <table cellspacing="0" cellpadding="6" border="1" style="border-collapse:collapse;font-size:13px">
    <thead style="background:#f1f3f5"><tr>
      <th>provider</th><th>window</th><th>started</th><th>error</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="color:#666;font-size:12px;margin-top:24px">
    This alert was generated automatically by services/llm_spend/alerts.py
    (daily-only path). Recipients are configured via the
    LLM_SPEND_ALERT_EMAILS environment variable.
  </p>
</body></html>"""

    text_body = (
        f"[URGENT] AiNxt LLM spend — daily digest cancelled\n"
        f"Period: {period_label} ({window_start} → {window_end})\n"
        f"Failed providers: {providers_csv}\n\n"
        + "\n".join(
            f"  - {r.provider} ({r.window_start}→{r.window_end}) at "
            f"{r.run_started:%Y-%m-%d %H:%M}: {(r.error_text or '')[:200]}"
            for r in runs
        )
        + f"\n\nRecover with POST /ainxt/v1/api/admin/llm-spend/fetch?for_date={window_start.isoformat()}\n"
        f"Then POST /ainxt/v1/api/admin/llm-spend/email/daily?for_date={window_start.isoformat()}\n"
    )

    try:
        ok = send_html_email(
            to        = to,
            subject   = f"[URGENT][AiNxt LLM Spend] daily digest cancelled — fetch failed for {providers_csv}",
            html_body = html,
            text_body = text_body,
        )
        if ok:
            logger.warning(
                f"[llm_spend.alerts] daily failed-fetch alert sent to {len(to)} "
                f"recipients (providers={providers_csv})"
            )
        else:
            logger.error("[llm_spend.alerts] SMTP returned False for daily failed-fetch alert")
        _record_send(claim_id, recipients=len(to), smtp_ok=bool(ok))
        return bool(ok)
    except Exception as e:
        logger.error(f"[llm_spend.alerts] failed-fetch alert send failed: {e}")
        _record_send(claim_id, recipients=len(to), smtp_ok=False)
        return False
