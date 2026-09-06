# SPDX-License-Identifier: MIT
# ============================================================
# COWORK TASK WORKER — RQ job that runs ONE scheduled Cowork
# task headless (server-side, no desktop attached).
#
# Flow:
#   1. Load the task row from cowork_scheduled_tasks
#   2. Skip if disabled / not yet due (defensive re-check)
#   3. Run the SERVER office mode: agents.orchestrator.agent.run(
#        ..., mode="office") with the task's role + prompt — exactly
#      the path gateway uses for the Cowork tab. Connector READS are
#      planned by orchestrator._plan_office; WRITES are never auto-planned.
#   4. Output compliance: REDACT (output is never blocked — read path)
#   5. Deliver (v1): store the redacted result to cowork_task_runs
#      (the "outbox"). If the task has a pre-approved connector
#      action AND that exact action is on the task's allowlist, route
#      it through the CONFIRMED, compliance-gated connector path
#      (connectors_router.connector_action equivalent: HARD-BLOCK on
#      sensitive content). Arbitrary sends are NEVER auto-confirmed.
#   6. Update last_run / next_run on the task row.
#
# AiNxt guardrails honoured:
#   - Reads: compliance REDACTS, never blocks (Cowork UX parity).
#   - Writes/sends: HARD-BLOCK on sensitive content; only execute when
#     the action is explicitly pre-approved AND allowlisted; otherwise
#     the draft is stored to the outbox for a human to confirm later.
#   - Never log secrets/tokens; connector tokens resolved per-user
#     inside connector_registry.execute (user_id only is passed here).
# ============================================================
from __future__ import annotations

import json
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from core.logger import logger, mask_email

# Output is collected (not streamed) — cap the headless run so a runaway
# plan cannot tie up an agent worker indefinitely.
_MAX_OUTPUT_CHARS = 60000


def run_scheduled_task(payload) -> dict:
    """
    RQ job entry point. Executes a single scheduled Cowork task headless.

    Args:
      payload — the scheduler enqueues the FULL task payload dict
                ({task_id, user_id, role, prompt, connectors, ...}); we also
                accept a bare task_id string for compatibility.

    Returns a small dict for the RQ result store / observability:
      {status, task_id, run_id?, error?}

    Never raises — failures are recorded on the run row and returned.
    """
    # Accept either the full payload dict (current scheduler) or a bare id.
    task_id = payload.get("task_id") if isinstance(payload, dict) else payload
    task_id = str(task_id or "")
    run_id = str(_uuid_mod.uuid4())
    logger.info(f"cowork_task_worker: starting task={task_id} run={run_id}")

    task = _load_task(task_id)
    if not task:
        logger.warning(f"cowork_task_worker: task {task_id} not found — skipping")
        return {"status": "not_found", "task_id": task_id}

    # Defensive re-check: the scheduler may have enqueued a task that was
    # paused between enqueue and execution.
    if (task.get("status") or "active") != "active":
        logger.info(f"cowork_task_worker: task {task_id} not active — skipping")
        return {"status": "skipped_disabled", "task_id": task_id}

    user_id = task.get("user_id") or ""
    role = (task.get("role") or "").strip()
    prompt = (task.get("prompt") or "").strip()

    if not user_id or not prompt:
        msg = "task missing user_id or prompt"
        logger.warning(f"cowork_task_worker: {msg} (task={task_id})")
        _record_run(run_id, task_id, user_id, status="error", error=msg, output="")
        _update_schedule(task_id, task, last_run_status="error")
        return {"status": "error", "task_id": task_id, "run_id": run_id, "error": msg}

    # ── 0. PREFLIGHT: warm the connectors this task declares ───────────────────
    # A scheduled run has no human present, so a stale access token used to
    # surface as a confusing agent answer ("I can't access your mail…") or a bare
    # "connect the connector" with no indication of WHY. Touch each declared
    # connector first: this triggers the engine's refresh path while we can still
    # report a precise reason, and distinguishes "the server couldn't refresh"
    # (retry, connection intact) from "you genuinely must reconnect".
    _preflight = _preflight_connectors(user_id, task.get("connectors") or [])
    if _preflight.get("reauth_needed"):
        names = ", ".join(_preflight["reauth_needed"])
        msg = (
            f"Connector(s) need to be reconnected before this task can run: {names}. "
            f"Go to Settings → Connectors and connect them again."
        )
        logger.warning(f"cowork_task_worker: task={task_id} blocked — {msg}")
        _record_run(run_id, task_id, user_id, status="error", error=msg, output="")
        _update_schedule(task_id, task, last_run_status="error")
        _notify_run_blocked(user_id, task_id, task, msg)
        return {"status": "error", "task_id": task_id, "run_id": run_id, "error": msg}
    if _preflight.get("transient"):
        names = ", ".join(_preflight["transient"])
        msg = (
            f"Could not reach {names} from the server right now (temporary issue). "
            f"Your connection is still valid — this run was skipped and the next "
            f"scheduled run will retry."
        )
        logger.error(f"cowork_task_worker: task={task_id} deferred — {msg}")
        _record_run(run_id, task_id, user_id, status="error", error=msg, output="")
        _update_schedule(task_id, task, last_run_status="error")
        return {"status": "error", "task_id": task_id, "run_id": run_id, "error": msg}

    # ── 1. INPUT compliance: redact the stored prompt, never block ─────────────
    # The prompt is author-supplied config; redaction keeps any accidental PII
    # out of the model call without interrupting the scheduled run.
    safe_prompt = prompt
    try:
        from agents.compliance_engine import compliance_engine as _ce
        # keep_types: preserve contact identifiers (EMAIL/MOBILE/UPI) so the
        # recipient in a "send an email to x@y.com" task survives redaction and
        # the send path resolves the correct recipient (not the self-email
        # fallback). Mirrors the live Cowork /ask path (gateway.py) and
        # connectors/mcp_bridge. Secrets/keys/cards are still redacted + blocked.
        _in = _ce.validate_input(prompt, keep_types={"EMAIL", "MOBILE", "UPI"})
        if _in.get("redacted_text"):
            safe_prompt = _in["redacted_text"]
        elif _in.get("blocked"):
            # Read path: do not block — fall back to redact_text.
            safe_prompt, _types = _ce.redact_text(prompt)
            if _types:
                logger.info(f"cowork_task_worker: prompt redacted types={_types}")
    except Exception as _ce_err:
        logger.warning(f"cowork_task_worker: input compliance failed (fail-open redact): {_ce_err}")

    # ── 1b. Detect delivery intent from the prompt ─────────────────────────────
    # The intent selects which envelope contract the LLM is asked to fill AND
    # which connector tool the delivery step ultimately dispatches. For any
    # prompt that today would go down the email path (or is disabled via the
    # BUDDY_TEAMS_DELIVERY_ENABLED kill switch) this returns {kind: "email"}
    # or {kind: "none"}, and the rest of the function behaves identically to
    # its pre-Teams implementation.
    intent = _detect_delivery_intent(prompt)
    logger.info(
        f"cowork_task_worker: task={task_id} delivery intent={intent.get('kind')}"
    )

    # Frame the office persona around the task's role (mirrors gateway's
    # Cowork office persona block — kept terse here; orchestrator mode="office"
    # drives the connector/KB-aware planner).
    #
    # For email intent this returns the exact framing string this file has
    # always built (proven by test_cowork_task_worker_email_regression). Teams
    # intents get a {"message": ..} envelope contract instead.
    framed_question = _frame_task(safe_prompt, role, intent)

    # ── 2. Run SERVER office mode (same entry point as the gateway) ────────────
    try:
        from agents.orchestrator import agent
        _user_ctx = {"user_id": user_id}
        iterator = agent.run(
            framed_question,
            None,                         # repo_filter — office mode is connector/KB scoped
            # Cowork is cloud-only, Claude-primary (per the Cowork/SDLC model policy:
            # Claude Sonnet 4.6 primary, never the local in-house model). 'complex'
            # → Claude Sonnet, the same model the desktop agent uses successfully.
            model_hint="complex",
            request_id=run_id,
            # MUST be the FRAMED question, not the bare prompt. In office mode the
            # final generation prompt is built from `raw_question`
            # (agents/tools.py → OFFICE_PROMPT.format(question=_raw_q)), so passing
            # safe_prompt here DISCARDED the {"subject","body"} envelope contract
            # built above. The model then returned prose, _parse_email_envelope()
            # failed, and every run degraded to `[delivery: outbox]` — i.e. the mail
            # was never sent. Passing the framed text keeps the contract intact.
            raw_question=framed_question,
            user_ctx=_user_ctx,
            messages=[],
            compliance_passed=True,       # input already redacted above
            mode="office",                # Cowork office assistant planner persona
        )
        parts: list[str] = []
        total = 0
        for token in iterator:
            if token is None:
                continue
            if not isinstance(token, str):
                token = getattr(token, "token", None) or getattr(token, "text", "") or str(token)
            parts.append(token)
            total += len(token)
            if total >= _MAX_OUTPUT_CHARS:
                logger.warning(f"cowork_task_worker: output cap reached for task={task_id}")
                break
        raw_output = "".join(parts).strip()
    except Exception as exc:
        logger.error(f"cowork_task_worker: agent run failed task={task_id}: {exc}")
        _record_run(run_id, task_id, user_id, status="error", error=str(exc), output="")
        _update_schedule(task_id, task, last_run_status="error")
        return {"status": "error", "task_id": task_id, "run_id": run_id, "error": str(exc)}

    if not raw_output:
        raw_output = "(no output produced)"

    # ── 3. Parse the structured envelope the LLM was asked to fill ─────────────
    # Email path: {"subject": "...", "body": "..."} — unchanged behaviour.
    # Teams path: {"message": "..."} — no subject line in Teams.
    # If the branch below doesn't apply to a task the file used to handle, that
    # is a regression: the email branch here is the same code that has always
    # run for email tasks.
    llm_subject: str = ""
    llm_body: str = ""
    if intent.get("kind") in ("teams_chat", "teams_channel"):
        _parsed = _parse_teams_envelope(raw_output)
        if _parsed is not None:
            llm_body = str(_parsed.get("message") or "").strip()
            logger.info(
                f"cowork_task_worker: LLM produced structured Teams message — "
                f"body_len={len(llm_body)}"
            )
        else:
            logger.warning(
                f"cowork_task_worker: no {{message}} envelope in output for "
                f"task={task_id} (len={len(raw_output)}) — body will be composed "
                f"defensively and withheld if it looks like narration"
            )
            # One bounded repair pass, only when we know the audience.
            if intent.get("recipient_email") or intent.get("channel"):
                _repaired = _reask_teams_envelope(raw_output, prompt, task_id)
                if _repaired is not None:
                    llm_body = str(_repaired.get("message") or "").strip()
    else:
        # ── Email/none — original behaviour, preserved line for line. ─────────
        _parsed = _parse_email_envelope(raw_output)
        if _parsed is not None:
            llm_subject = str(_parsed.get("subject") or "").strip()
            llm_body    = str(_parsed.get("body")    or "").strip()
            logger.info(
                f"cowork_task_worker: LLM produced structured email — "
                f"subject={llm_subject!r} body_len={len(llm_body)}"
            )
        else:
            # Log it: a silent parse failure here is what let a narrative response
            # become the email body (recipient/subject leaking into the message).
            logger.warning(
                f"cowork_task_worker: no {{subject,body}} envelope in output for "
                f"task={task_id} (len={len(raw_output)}) — body will be composed "
                f"defensively and withheld if it looks like narration"
            )
            # One bounded repair attempt: ask the model to reshape its own prose into
            # the envelope. Only for tasks that are actually a send (an explicit
            # recipient in the prompt) — a digest/report task has nothing to repair.
            # Costs at most ONE extra call, and only after a parse already failed.
            if _extract_email_recipient(prompt):
                _repaired = _reask_email_envelope(raw_output, prompt, task_id)
                if _repaired is not None:
                    llm_subject = str(_repaired.get("subject") or "").strip()
                    llm_body    = str(_repaired.get("body")    or "").strip()

    # ── 4. Pass LLM output through directly ────────────────────────────────────
    final_output = raw_output

    # ── 5. Deliver — pass a clean, CoWork-identical body to the send ───────────
    # Body selection + trust live in _compose_message_body, which is a pure
    # passthrough to _compose_email_body for email/none intents — so the two
    # delivery paths can never diverge from what they have always been.
    # `confident=False` means the body is a sanitized best-effort from an
    # unparsed narrative: it is stored to the outbox rather than sent, because
    # a wrong message is worse than a held draft.
    email_body, _body_source, _body_confident = _compose_message_body(
        prompt, llm_body, final_output, intent
    )
    logger.info(
        f"cowork_task_worker: body composed task={task_id} source={_body_source} "
        f"confident={_body_confident} len={len(email_body)} kind={intent.get('kind')}"
    )
    # llm_subject is forwarded to _maybe_deliver_preapproved so it overrides any
    # heuristic subject derivation — the email arrives with the subject the LLM wrote.
    delivery = _maybe_deliver_preapproved(
        task, user_id, email_body,
        llm_subject=llm_subject,
        body_confident=_body_confident,
        body_source=_body_source,
        intent=intent,
    )

    # ── 6. Persist the run (outbox / history) ──────────────────────────────────
    _record_run(
        run_id, task_id, user_id,
        status="done",
        error=None,
        output=email_body,
        delivery=delivery,
    )

    # ── 5b. Deliver the result to the user's Inbox so a headless run is VISIBLE ──
    # (the run is otherwise only in cowork_task_runs with no UI). Read-style result,
    # already compliance-redacted above; nothing is sent to any connector here.
    # G-C: make delivery outcome VISIBLE. If the email send failed (e.g. connector /
    # vault-key / relay issue), say so with the REAL reason instead of implying success.
    try:
        from store.inbox_store import publish_inbox_item
        _title = (task.get("prompt") or "Scheduled task").strip()[:60]
        _mode = (delivery or {}).get("mode", "outbox")
        # `_kind` classifies the delivery target for the inbox subject: only
        # Teams deliveries get a decorated subject. Email keeps the historical
        # "Scheduled task ran: <title>" / "Scheduled task ran (not emailed): ..."
        # strings so downstream tooling that greps for them keeps working.
        _kind = (delivery or {}).get("kind") or (intent or {}).get("kind") or "email"
        _is_teams = _kind in ("teams_chat", "teams_channel")
        if _mode == "sent":
            if _kind == "teams_chat":
                _who = (delivery or {}).get("recipient") or (intent or {}).get("recipient_email") or ""
                _subj = f"Scheduled task ran (Teams chat{' → ' + _who if _who else ''}): {_title}"
            elif _kind == "teams_channel":
                _chan = (delivery or {}).get("channel") or (intent or {}).get("channel") or ""
                _subj = f"Scheduled task ran (Teams channel {_chan}): {_title}" if _chan \
                    else f"Scheduled task ran (Teams channel): {_title}"
            else:
                _subj = f"Scheduled task ran: {_title}"
            _body = (final_output or "(no output)")[:4000]
        else:
            _why = (delivery or {}).get("error") or (delivery or {}).get("reason") or "not delivered"
            if _is_teams:
                _subj = f"Scheduled task ran (not posted to Teams): {_title}"
                _body = (
                    f"Your scheduled task ran, but the result could NOT be posted to Teams.\n"
                    f"Reason: {_why}\n\n"
                    f"--- Result ---\n{(final_output or '(no output)')[:3800]}"
                )
            else:
                _subj = f"Scheduled task ran (not emailed): {_title}"
                _body = (
                    f"Your scheduled task ran, but the result could NOT be emailed to you.\n"
                    f"Reason: {_why}\n\n"
                    f"--- Result ---\n{(final_output or '(no output)')[:3800]}"
                )
        publish_inbox_item(
            str(user_id), "scheduled_result", _subj, _body,
            source_id=str(task_id),
            metadata={"kind": "cowork_scheduled", "task_id": str(task_id),
                      "run_id": run_id, "delivery": _mode,
                      "delivery_kind": _kind},
        )
    except Exception as _inbox_err:
        logger.warning(f"cowork_task_worker: inbox delivery failed (non-fatal): {_inbox_err}")

    # ── 5c. Self-improving skill loop: record this successful run signature ─────
    # Cowork scheduled tasks are already-recurring → the highest-precision source.
    # safe_prompt is ALREADY compliance-redacted above. O(1) Redis write, guarded.
    try:
        from core.config import ENABLE_SKILL_LOOP, SKILL_LOOP_SOURCES
        if ENABLE_SKILL_LOOP and "cowork_task" in SKILL_LOOP_SOURCES:
            from store.skill_loop_store import record_run_signature
            record_run_signature(
                "cowork_task", task_id, safe_prompt, [],
                department=(task.get("department") or ""),
            )
    except Exception as _sl_err:
        logger.debug(f"cowork_task_worker: skill-loop capture skipped: {_sl_err}")

    # ── 6. Update last_run / next_run on the task ──────────────────────────────
    _update_schedule(task_id, task, last_run_status="done")

    logger.info(
        f"cowork_task_worker: task={task_id} run={run_id} done "
        f"(output={len(final_output)} chars, delivery={delivery.get('mode')})"
    )
    return {"status": "done", "task_id": task_id, "run_id": run_id}


# ── Preflight: verify + warm the task's connectors before running the agent ────

def _preflight_connectors(user_id: str, connectors: list) -> dict:
    """Check every connector this task declares BEFORE running the agent.

    Returns {"reauth_needed": [...], "transient": [...]}.

    Why this exists: the connector token is resolved deep inside the agent run, so
    a stale or unrefreshable token used to surface either as a vague agent answer
    or as a bare "connect the connector" — with no way to tell a genuine
    disconnection from a server-side refresh failure. Touching the token here
    forces the engine's refresh path to run while we can still report precisely.

    Deliberately conservative: anything we cannot classify is IGNORED and the run
    proceeds, so this can never block a task that would otherwise have worked.
    """
    result: dict = {"reauth_needed": [], "transient": []}
    if not connectors:
        return result

    try:
        from connectors.base import (
            ConnectorNotConnectedError,
            ConnectorReauthRequired,
            ConnectorTransientError,
        )
        from connectors.engine import connector_engine
        from connectors.registry import connector_registry
    except Exception as exc:
        logger.warning(f"cowork_task_worker: preflight imports unavailable: {exc}")
        return result

    # Only consider connector NAMES we actually know about. The task's
    # `connectors` column may hold tool names or free text.
    try:
        known = {c.get("name") for c in connector_registry.get_available()}
    except Exception as exc:
        logger.warning(f"cowork_task_worker: preflight could not list connectors: {exc}")
        return result

    for raw in connectors:
        name = str(raw or "").strip()
        if not name or name not in known:
            continue
        try:
            defn = connector_engine._load_definition(name)
            # Resolves the row, decrypts it, and refreshes when near expiry —
            # exactly what the agent would do later, but with clean error handling.
            connector_engine._get_token_row(user_id, name, defn)
            logger.info(f"cowork_task_worker: preflight OK for {name}")
        except ConnectorReauthRequired:
            result["reauth_needed"].append(name)
        except ConnectorNotConnectedError as exc:
            # This covers both "never connected" and "deactivated", plus the
            # FERNET_KEY-mismatch case. The message already explains which.
            logger.warning(f"cowork_task_worker: preflight {name} not usable: {exc}")
            result["reauth_needed"].append(name)
        except ConnectorTransientError as exc:
            logger.warning(f"cowork_task_worker: preflight {name} transient: {exc}")
            result["transient"].append(name)
        except Exception as exc:
            # Unknown failure — do NOT block the run on it.
            logger.warning(
                f"cowork_task_worker: preflight {name} inconclusive "
                f"({type(exc).__name__}) — continuing"
            )

    return result


def _notify_run_blocked(user_id: str, task_id: str, task: dict, reason: str) -> None:
    """Tell the user a scheduled run was blocked, and why. Best-effort."""
    try:
        from store.inbox_store import publish_inbox_item
        title = (task.get("prompt") or "Scheduled task").strip()[:60]
        publish_inbox_item(
            str(user_id),
            "scheduled_result",
            f"Scheduled task could not run: {title}",
            f"{reason}\n\nThis task will run normally once the connection is restored.",
            source_id=str(task_id),
            metadata={"kind": "cowork_scheduled", "task_id": str(task_id),
                      "delivery": "blocked"},
        )
    except Exception as exc:
        logger.warning(f"cowork_task_worker: blocked-run notify failed: {exc}")


# ── Pre-approved connector write (CONFIRMED path only) ─────────────────────────

def _resolve_user_email(user_id: str) -> str:
    """The user's own M365 mailbox address — for delivering a scheduled result to
    themselves. Returns "" if Microsoft 365 isn't connected for this user."""
    try:
        from connectors.registry import connector_registry
        for c in connector_registry.get_user_status(user_id):
            if c.get("name") == "microsoft_365" and c.get("connected"):
                return (c.get("connected_as") or "").strip()
    except Exception as exc:
        logger.warning(f"cowork_task_worker: resolve user email failed: {exc}")
    return ""


def _send_via_cowork_pipeline(user_id: str, connector: str, tool: str, params: dict) -> dict:
    """Send/execute a connector action through the SAME pipeline live (normal)
    CoWork uses: connectors.mcp_bridge.call_tool.

    This makes a scheduled send behave EXACTLY like a normal CoWork send — the
    structured {to, subject, body} arguments, outbound compliance HARD-BLOCK on
    sensitive content, recipient validation, and attachment handling are all
    applied identically (see connectors/mcp_bridge.py::_connector_call). This is
    the same code path the interactive CoWork tool call hits, so the delivered
    email has the same clean format.

    Returns {"ok": bool, "text": str}. `text` carries the pipeline's human-
    readable result (a success confirmation, or the reason it was blocked/failed).
    """
    from connectors import mcp_bridge
    # call_tool expects the canonical "<connector>__<tool>" name; call_tool itself
    # also restores single→double underscore, but pass the canonical form directly.
    name = f"{connector}__{tool}"
    result = mcp_bridge.call_tool(user_id, name, dict(params or {}), allowed=None)
    is_err = bool(result.get("isError")) if isinstance(result, dict) else True
    text = ""
    try:
        text = (result.get("content") or [{}])[0].get("text", "") or ""
    except Exception:
        pass
    return {"ok": (not is_err), "text": text}


def _extract_email_recipient(prompt: str) -> str:
    """Extract an explicit email recipient from the task prompt.

    Looks for patterns like:
      - "send an email to foo@bar.com"
      - "email to foo@bar.com"
      - "to: foo@bar.com"
    Returns the first match, or "" if none found.
    """
    import re
    # Match a bare email address that follows common "to" keywords
    _RE = re.compile(
        r"(?:send\s+(?:an?\s+)?email\s+to|email\s+to|to\s*:)\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
        re.IGNORECASE,
    )
    m = _RE.search(prompt)
    if m:
        return m.group(1).strip()
    # Fallback: any bare email address in the prompt
    _RE2 = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    m2 = _RE2.search(prompt)
    return m2.group(0).strip() if m2 else ""


def _extract_email_subject(prompt: str) -> str:
    """Extract an explicit email subject from the task prompt.

    Looks for patterns like:
      - 'with subject "REDG Task Status Update"'
      - "subject: REDG Task Status Update"
      - 'subject "..."'
    Returns the subject string (without surrounding quotes), or "" if not found.
    """
    import re
    # Quoted subject after "subject" keyword
    _RE = re.compile(
        r'(?:with\s+)?subject\s*[:\-]?\s*["\u201c\u2018]([^"\u201d\u2019\n]{1,200})["\u201d\u2019]',
        re.IGNORECASE,
    )
    m = _RE.search(prompt)
    if m:
        return m.group(1).strip()
    # Unquoted subject after "subject:" keyword
    _RE2 = re.compile(r'(?:with\s+)?subject\s*:\s*(.+?)(?:\s+and\s+body|\s+body\s*:|\n|$)', re.IGNORECASE)
    m2 = _RE2.search(prompt)
    return m2.group(1).strip() if m2 else ""


def _extract_email_body(prompt: str) -> str:
    """Extract an explicit email body dictated in the task prompt.

    Mirrors _extract_email_subject: when the user already wrote the body verbatim
    (e.g. '... and body: "Hi Adarsh, what is the daily status update?"'), that text
    IS the email body and must be used as-is — never the LLM's rephrasing and never
    the raw instruction blob. Returns "" when no explicit body is present (the
    caller then falls back to the LLM-composed body).
    """
    import re
    # Quoted body after a "body" keyword — greedy to the LAST closing quote so a
    # multi-sentence body with internal punctuation is captured whole.
    _RE = re.compile(
        r'body\s*[:\-]?\s*["\u201c\u2018](.+)["\u201d\u2019]',
        re.IGNORECASE | re.DOTALL,
    )
    m = _RE.search(prompt)
    if m:
        return m.group(1).strip()
    # Unquoted body after a "body:" keyword — take everything after it.
    _RE2 = re.compile(r'body\s*[:\-]\s*(.+)$', re.IGNORECASE | re.DOTALL)
    m2 = _RE2.search(prompt)
    return m2.group(1).strip() if m2 else ""


# ── Delivery intent detection (2026-08-12) ─────────────────────────────────────
#
# Scheduled tasks used to have ONE delivery mode: send the result as an Outlook
# email. Users have started writing prompts like "post the digest to the REDG
# channel in the engineering team" or "send a teams chat to user@example.com"
# and the scheduler must honour those instead of silently defaulting to email.
#
# _detect_delivery_intent inspects the prompt and returns a small descriptor the
# rest of the worker branches on. It is deliberately CONSERVATIVE — a prompt
# that only mentions "team" (no Teams-specific verb) falls through to the email
# path. Silently posting to a Teams channel the user didn't ask for is worse
# than falling back to email.
#
# The BUDDY_TEAMS_DELIVERY_ENABLED env var is a kill switch: setting it to "0"
# forces this function to only ever return "email" or "none", so an operator
# can revert to the exact pre-Teams behaviour without a code change.

def _teams_delivery_enabled() -> bool:
    """False when the operator has disabled Teams delivery via env."""
    import os as _os
    return _os.getenv("BUDDY_TEAMS_DELIVERY_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _detect_delivery_intent(prompt: str) -> dict:
    """Classify what the task prompt is asking the scheduler to deliver.

    Returns one of:
      {"kind": "teams_channel", "team": "<name>", "channel": "<name>"}
      {"kind": "teams_chat",    "recipient_email": "<addr>"}
      {"kind": "email",         "recipient": "<addr>"}
      {"kind": "none"}                # → self-email / outbox fallback

    Evaluation order matters: Teams intent must be probed BEFORE the email
    fallback, because prompts like "send a teams chat to a@b.com" contain a
    valid email address that would otherwise short-circuit into the email path.

    When the kill switch is off, this function can never return a teams_*
    kind — every prompt collapses back into the existing email/none branches.
    """
    import re as _re

    p = (prompt or "").strip()
    if not p:
        return {"kind": "none"}

    if _teams_delivery_enabled():
        # 1) Teams channel: explicit "post/send … to … channel" phrasing.
        #    Also match "teams channel: NAME" and "in the NAME channel".
        _CHAN_PATTERNS = [
            # "post to the REDG channel in the Acme-PayCore team"
            _re.compile(
                r'\b(?:post|send|share|publish|drop)\b[^\n]{0,40}?'
                r'(?:to|in|on)\s+(?:the\s+)?#?([A-Za-z0-9][\w \-\.]{0,60}?)\s+channel'
                r'(?:[^\n]{0,80}?(?:in|of|on|under)\s+(?:the\s+)?'
                r'([A-Za-z0-9][\w \-\.]{0,60}?)\s+team\b)?',
                _re.IGNORECASE,
            ),
            # "teams channel: NAME" or "channel NAME on teams"
            _re.compile(
                r'\bteams?\s+channel\s*[:\-]?\s*#?([A-Za-z0-9][\w \-\.]{0,60})',
                _re.IGNORECASE,
            ),
        ]
        for pat in _CHAN_PATTERNS:
            m = pat.search(p)
            if m:
                channel = (m.group(1) or "").strip().rstrip(".,;:")
                team = ""
                if m.lastindex and m.lastindex >= 2:
                    team = (m.group(2) or "").strip().rstrip(".,;:")
                if channel:
                    return {"kind": "teams_channel", "team": team, "channel": channel}

        # 2) Teams chat: 1:1 or group DM. Requires a Teams verb AND an email
        #    address in the surrounding phrasing so we don't misfire on random
        #    "team" mentions.
        _EMAIL_RE = r'([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'
        _CHAT_PATTERNS = [
            _re.compile(
                r'(?:send\s+(?:a\s+)?)?teams?\s+(?:chat|message|dm|ping)\s+to\s+' + _EMAIL_RE,
                _re.IGNORECASE,
            ),
            _re.compile(
                r'(?:chat|message|ping|dm)\s+' + _EMAIL_RE + r'\s+(?:on|via|through)\s+teams?',
                _re.IGNORECASE,
            ),
            _re.compile(
                r'(?:via|through|over)\s+teams?\s+to\s+' + _EMAIL_RE,
                _re.IGNORECASE,
            ),
        ]
        for pat in _CHAT_PATTERNS:
            m = pat.search(p)
            if m:
                addr = (m.group(1) or "").strip()
                if addr:
                    return {"kind": "teams_chat", "recipient_email": addr}

        # 3) "on teams" + first email address (weaker cue, kept last so an
        #    explicit "email X" earlier in the prompt still wins).
        if _re.search(r'\bon\s+teams?\b', p, _re.IGNORECASE):
            m = _re.search(_EMAIL_RE, p)
            if m:
                return {"kind": "teams_chat", "recipient_email": m.group(1).strip()}

    # 4) Email fallback — unchanged behaviour.
    recipient = _extract_email_recipient(p)
    if recipient:
        return {"kind": "email", "recipient": recipient}

    return {"kind": "none"}


# ── Framing prompts ────────────────────────────────────────────────────────────
#
# _frame_email_task returns the BYTE-FOR-BYTE identical string the file used to
# build inline in run_scheduled_task. Kept as a separate function so the email
# regression test can lock it down with a string-equality check. Do NOT edit
# this string — it is the framing that today's production tasks depend on.

def _frame_email_task(prompt: str, role: str) -> str:
    """The historical email-envelope framing — moved verbatim into a helper."""
    return (
        f"[SCHEDULED COWORK TASK{f' — role: {role}' if role else ''}]\n"
        f"You are AiNxt Buddy{f' acting as the user' + chr(39) + 's ' + role if role else ''}. "
        f"Complete the task below using the user's connected apps and the knowledge base. "
        f"Write for a non-technical audience.\n\n"
        f"SIGNATURE: when the email or message needs a sign-off, sign as \"AiNxt Buddy\". "
        f"If the user's own saved email signature is available, "
        f"use that verbatim instead.\n\n"
        f"IMPORTANT: If the task involves sending an email or message, respond with ONLY a "
        f"valid JSON object in this exact format — no markdown fences, no extra text:\n"
        f'{{"subject": "<a concise, relevant subject line>", "body": "<the full email body>"}}\n\n'
        f"The subject must be meaningful and specific to the task (never blank or generic). "
        f"The body must be complete, professional, and ready to send as-is.\n"
        f"CRITICAL — the \"body\" field must contain ONLY the message the recipient should "
        f"read. Never repeat the instruction, the recipient's email address, or any "
        f'\"to:\"/\"subject:\"/\"send an email to ...\" text inside the body. '
        f"If the task already provides the exact body text to send, return that body "
        f"VERBATIM in the \"body\" field (do not rephrase or add to it) and still provide a "
        f"suitable \"subject\".\n"
        f"Do NOT refuse or say you cannot send emails.\n\n"
        f"If the task is NOT about sending an email, respond normally in plain text.\n\n"
        f"[TASK]\n{prompt}"
    )


def _frame_teams_task(prompt: str, role: str, intent: dict) -> str:
    """Framing for a Teams chat / channel post.

    Teams has no subject line — the LLM returns `{"message": ...}` only. The
    audience hint (`recipient` for a chat, `team`/`channel` for a channel post)
    is included so the model can pitch tone appropriately.
    """
    kind = (intent or {}).get("kind") or "teams_chat"
    if kind == "teams_channel":
        team = (intent or {}).get("team") or ""
        chan = (intent or {}).get("channel") or ""
        audience = (
            f"The message will be POSTED to the Microsoft Teams channel "
            f"\"{chan}\"" + (f" in the \"{team}\" team" if team else "") + "."
        )
    else:
        who = (intent or {}).get("recipient_email") or ""
        audience = (
            f"The message will be sent as a Microsoft Teams chat"
            + (f" to {who}" if who else "") + "."
        )
    return (
        f"[SCHEDULED COWORK TASK{f' — role: {role}' if role else ''}]\n"
        f"You are AiNxt Buddy{f' acting as the user' + chr(39) + 's ' + role if role else ''}. "
        f"Complete the task below using the user's connected apps and the knowledge base.\n"
        f"{audience}\n\n"
        f"SIGNATURE: when the message needs a sign-off, sign as \"AiNxt Buddy\". "
        f"If the user's own saved signature is available, use that verbatim instead.\n\n"
        f"IMPORTANT: Respond with ONLY a valid JSON object in this exact format — "
        f"no markdown fences, no extra text:\n"
        f'{{"message": "<the full message the recipient should read>"}}\n\n'
        f"The message must be complete, professional, ready to post as-is, and written "
        f"in a Teams chat register — concise, direct, minimal markdown (Teams supports "
        f"only a small subset). Never include a \"To:\" line, a channel name, a team "
        f"name, or the send instruction inside the message body itself. If the task "
        f"already provides the exact message text, return it VERBATIM in the \"message\" "
        f"field.\n"
        f"Do NOT refuse or say you cannot post to Teams.\n\n"
        f"[TASK]\n{prompt}"
    )


def _frame_task(prompt: str, role: str, intent: dict) -> str:
    """Return the framing string for the given delivery intent.

    Email/none delegates to _frame_email_task, which returns the exact string
    the file has always built. Teams intents get _frame_teams_task.
    """
    kind = (intent or {}).get("kind") or "email"
    if kind in ("teams_chat", "teams_channel"):
        return _frame_teams_task(prompt, role, intent)
    return _frame_email_task(prompt, role)


def _strip_line_markup(text: str) -> str:
    """Remove markdown emphasis/quote/heading markers from LINE STARTS only.

    Used so a header label survives detection regardless of how the model
    decorated it — `**To:**`, `### To:`, `> To:` all normalise to `To:`.

    Deliberately line-anchored: emphasis INSIDE prose ("this is **important**")
    is real content and must be left untouched.
    """
    import re

    if not text:
        return text
    out = re.sub(r'^[ \t]*(?:[>#]+[ \t]*)*(?:\*\*|__|\*|_)?[ \t]*', "", text, flags=re.MULTILINE)
    # Drop the closing emphasis of a bold label, e.g. "To:** value" → "To: value".
    return re.sub(r'^([A-Za-z][A-Za-z ]{0,14})(?:\*\*|__|\*|_)(\s*[:\-])',
                  r'\1\2', out, flags=re.MULTILINE)


# ── Narration prefix ──────────────────────────────────────────────────────────
# Matches the opening of an agent response that TALKS ABOUT an email instead of
# being one ("I have sent an email to X…", "Here is the email I composed…").
#
# Every alternative requires an email-specific noun (email / mail / message /
# draft). Without that requirement "Here is the summary of your open MRs" — a
# perfectly good weekly-digest body — would be misread as narration and the
# email withheld. Shared by _looks_like_leaky_output and _sanitize_email_body so
# detection and scrubbing can never disagree.
_EMAIL_NOUN = r'(?:e-?mail|mail|message|draft)'
_NARRATION_PREFIX_RE = (
    r'^\s*(?:ok(?:ay)?[,.\s]+|sure[,.\s]+|done[,.\s]+|certainly[,.\s]+)?'
    r'(?:'
    #   "I have sent/drafted/composed … <email>"  (noun within ~40 chars)
    r'i\s*(?:\'ve|\s+have)?\s*(?:just\s+)?'
    r'(?:sent|drafted|composed|prepared|created|written)\b[^\n]{0,40}?' + _EMAIL_NOUN + r'\b'
    #   "Here is/are the <email>"
    r'|here\s*(?:\'s|\s+(?:is|are))\s+(?:the\s+|your\s+|a\s+)?' + _EMAIL_NOUN + r'\b'
    #   "The email has been sent", "Email sent successfully:", "Email sent to X"
    #   The trailing context matters: it separates the status report
    #   "Email sent successfully:" from a legitimate body that happens to open
    #   with "Email sent count for the week: 42" (a metric, not narration).
    r'|(?:the\s+)?' + _EMAIL_NOUN + r'\s+(?:has\s+been\s+)?sent\b'
    r'(?=\s*(?:$|[:.\n]|to\b|successfully\b|via\b|from\b|with\b))'
    #   the instruction echoed back verbatim
    r'|send\s+(?:an?\s+)?' + _EMAIL_NOUN + r'\s+to\b'
    r')'
)

# Header labels that belong in the Graph payload's own fields, never in the body.
_HEADER_LABEL_RE = None  # lazily compiled in _header_label_match


def _header_label_match(line: str):
    """True when `line` is an email header label line (To:/Subject:/Cc:/…)."""
    import re

    global _HEADER_LABEL_RE
    if _HEADER_LABEL_RE is None:
        _HEADER_LABEL_RE = re.compile(
            r'^\s*(?:to|cc|bcc|from|recipient|recipients|subject|subject\s+line|'
            r'importance|priority)\s*[:\-]\s*(.*)$',
            re.IGNORECASE,
        )
    return _HEADER_LABEL_RE.match(line)


def _looks_like_leaky_output(text: str) -> bool:
    """True when `text` looks like agent NARRATION or an email header block
    rather than the message a recipient should read.

    This is the guard that stops the whole agent transcript being used as an
    email body (the root cause of to/subject appearing inside the message).

    Deliberately conservative — it must NOT flag a legitimate body that merely
    mentions an address mid-sentence ("please copy finance@example.com") or uses
    the word "subject" in prose ("the subject of the audit is ...").
    """
    import re

    if not text or not str(text).strip():
        return False
    raw = str(text).strip()

    # 1. Raw JSON envelope / Graph payload residue.
    if re.search(r'"(?:subject|body|toRecipients|saveToSentItems)"\s*:', raw):
        return True

    # 2. Narration ABOUT an email, anchored to the start.
    #    Every branch requires an email-specific noun (email/mail/message/draft).
    #    That distinction matters: "Here is the email I composed" is narration,
    #    but "Here is the summary of your open MRs" is a legitimate digest body
    #    and must NOT be flagged — weekly-digest tasks are a common Cowork use.
    if re.match(_NARRATION_PREFIX_RE, raw, re.IGNORECASE):
        return True

    # 3. A header-label block in the opening lines (post-markdown-normalisation).
    for line in _strip_line_markup(raw).splitlines()[:_HEADER_SCAN_LINES]:
        if not line.strip():
            break                      # blank line ends the header region
        if _header_label_match(line):
            return True
    return False


# How many opening lines may be treated as an email header block. Bounded so a
# mid-body sentence containing "subject" is never in scope.
_HEADER_SCAN_LINES = 6


def _sanitize_email_body(body: str, recipient: str = "", subject: str = "") -> str:
    """Ensure the email body never carries the send-instruction / recipient /
    subject preamble or a raw JSON envelope.

    This is a safety net: regardless of what the LLM returned (clean body, a JSON
    string, or an echo of the whole "send an email to X with subject Y and body: Z"
    instruction), the delivered body is the actual message only — exactly like an
    interactive CoWork send. Never returns empty: if scrubbing would empty the body,
    the original is kept.

    Ordering matters: narration is stripped BEFORE the JSON unwrap, so
    `Here is the email:\\n{"subject":..,"body":..}` still unwraps correctly.
    """
    import json as _json
    import re

    if not body:
        return body
    original = body
    # Normalise line endings so ^/$ anchors behave identically on CRLF input.
    text = str(body).replace("\r\n", "\n").replace("\r", "\n").strip()

    # 1. Strip a leading narration/instruction preamble.
    #    De-anchored from the old `^\s*send` so real output ("I have sent an
    #    email to X with subject Y and body: ...") is matched too. Uses the same
    #    _NARRATION_PREFIX_RE as the detector, so both agree on what narration is.
    _narration = re.compile(
        _NARRATION_PREFIX_RE
        + r'[^\n:]{0,120}?'                                 # "…to X with"
          r'(?:'
          r'  subject\s*[:\-]?\s*["\u201c\u2018][^"\u201d\u2019\n]{0,200}["\u201d\u2019]\s*'
          r'  (?:and\s+)?body\s*[:\-]\s*'                   # …subject "Y" and body:
          r'| body\s*[:\-]\s*'                              # …body:
          r'| \s*[:\-]\s*\n'                                # "Here is the email:"
          r'| \s*\n'                                        # "Here is the email\n"
          r'| \s*[:\-]\s*'                                  # "Email sent: …"
          r')',
        re.IGNORECASE | re.VERBOSE,
    )
    text = _narration.sub("", text, count=1).strip()

    # 2. If what remains is a JSON envelope {"subject":..,"body":..}, unwrap it.
    #    Tolerates a markdown fence left behind by the narration strip.
    _unfenced = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', "", text).strip()
    if _unfenced.startswith("{") and '"body"' in _unfenced:
        try:
            _parsed = _json.loads(_unfenced)
            if isinstance(_parsed, dict) and "body" in _parsed:
                text = str(_parsed.get("body") or "").strip()
        except (ValueError, TypeError):
            pass

    # 3. Strip a leading email HEADER BLOCK (To:/Cc:/Subject:/…).
    #    Bounded to the first _HEADER_SCAN_LINES lines and stops at the first
    #    blank line or first non-label line, so a mid-body "The subject of the
    #    audit is ..." can never be removed.
    lines = text.split("\n")
    probe = _strip_line_markup(text).split("\n")
    cut = 0
    for i in range(min(_HEADER_SCAN_LINES, len(lines))):
        stripped = probe[i].strip()
        if not stripped:
            if cut:                      # blank line closes the header block
                cut = i + 1
            break
        if _header_label_match(probe[i]):
            cut = i + 1
            continue
        break                            # first prose line ends the scan
    if cut:
        text = "\n".join(lines[cut:]).strip()

    # 4. Strip a single pair of wrapping quotes left by the preamble removal.
    text = text.strip()
    if len(text) >= 2 and text[0] in '"\u201c\u2018' and text[-1] in '"\u201d\u2019':
        text = text[1:-1].strip()

    # 5. Never send an empty body — fall back to the original if scrubbing emptied it.
    return text if text else original.strip()


def _parse_email_envelope(raw_output: str) -> dict | None:
    """Extract the {"subject": .., "body": ..} envelope from an LLM response.

    Returns the parsed dict, or None when the response isn't an email envelope.

    Tolerant by design, because every parse failure used to degrade into
    "send the whole narrative as the body":
      1. the response as-is
      2. the first fenced ``` block found ANYWHERE (not just at position 0 —
         the old `split("```")[1]` was defeated by a single word of lead-in prose)
      3. the outermost {...} span in the response
    """
    import re as _re

    if not raw_output or not raw_output.strip():
        return None
    text = raw_output.strip()

    candidates: list[str] = [text]

    # Any fenced block, anywhere in the response.
    for m in _re.finditer(r'```[a-zA-Z]*\s*\n?(.*?)```', text, _re.DOTALL):
        block = m.group(1).strip()
        if block:
            candidates.append(block)

    # Outermost brace span — recovers `Here is the email: {...} Let me know.`
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and "subject" in parsed and "body" in parsed:
            return parsed
    return None


def _reask_email_envelope(raw_output: str, prompt: str, task_id: str = "") -> dict | None:
    """Second chance: ask the model to reshape its own prose into {subject, body}.

    Called ONLY when the first response carried no envelope AND the task is a real
    send (an explicit recipient was found in the prompt). Without this, a single
    stray prose reply silently downgrades the run to the outbox and the recipient
    never hears from us.

    Deliberately bounded and fail-safe: exactly one extra model call, no tools, no
    connector access, and any error returns None so the caller falls through to the
    existing defensive composition. It can only ever turn an outbox into a send —
    never the reverse.
    """
    if not raw_output or not raw_output.strip():
        return None
    try:
        from models.model_router import model_router
        repair_prompt = (
            "Convert the assistant response below into an email envelope.\n\n"
            'Reply with ONLY a valid JSON object — no markdown fences, no commentary:\n'
            '{"subject": "<concise, specific subject line>", "body": "<the full email body>"}\n\n'
            "Rules:\n"
            "- The body must contain ONLY the message the recipient should read.\n"
            "- Never include the recipient address, a \"To:\" line, or a \"Subject:\" "
            "line inside the body.\n"
            "- Do not summarise or shorten the content — carry it over faithfully.\n"
            "- The subject must never be blank or generic.\n\n"
            f"[ORIGINAL TASK]\n{prompt}\n\n"
            f"[ASSISTANT RESPONSE TO CONVERT]\n{raw_output}"
        )
        reply = model_router.generate(repair_prompt, model_hint="complex")
        parsed = _parse_email_envelope(str(reply or ""))
        if parsed is not None:
            logger.info(
                f"cowork_task_worker: envelope repaired on re-ask task={task_id} "
                f"subject={str(parsed.get('subject') or '')[:60]!r}"
            )
            return parsed
        logger.warning(
            f"cowork_task_worker: envelope re-ask still unparsed task={task_id} "
            f"— falling back to defensive composition"
        )
    except Exception as exc:
        logger.warning(f"cowork_task_worker: envelope re-ask failed task={task_id}: {exc}")
    return None


def _compose_email_body(prompt: str, llm_body: str, raw_output: str) -> tuple[str, str, bool]:
    """Choose the email body and say how much we trust it.

    Returns ``(body, source, confident)``.

    Priority — highest trust first:
      1. ``prompt``     — a body the user dictated verbatim  → confident
      2. ``llm_body``   — from a parsed {"subject","body"}   → confident
      3. ``raw_output`` — ONLY when it doesn't look like agent narration or an
                          email header block                → confident
      4. otherwise      — narration/header block: sanitized best-effort, but
                          ``confident=False`` so the caller holds it back

    ``confident=False`` is what stops the historic bug: the agent's whole
    narrative used to be sent as the body, which is how the recipient address
    and subject line ended up inside the message. Callers must route a
    non-confident body to the outbox instead of sending it.
    """
    explicit = _extract_email_body(prompt) if prompt else ""
    if explicit and explicit.strip():
        return _sanitize_email_body(explicit), "prompt", True

    if llm_body and llm_body.strip():
        return _sanitize_email_body(llm_body), "llm_json", True

    if raw_output and raw_output.strip():
        # A clean, direct answer is fine to send; narration/headers are not.
        if not _looks_like_leaky_output(raw_output):
            return _sanitize_email_body(raw_output), "raw_output", True
        return _sanitize_email_body(raw_output), "raw_output_unparsed", False

    return "", "none", False


# ── Teams envelope helpers (2026-08-12) ────────────────────────────────────────
#
# Mirror the {subject,body} email helpers above, but for Teams: the LLM returns
# {"message": "..."} for a chat/channel post — there is no subject line in Teams.
# All email helpers stay untouched; these are strictly additive.

def _parse_teams_envelope(raw_output: str) -> dict | None:
    """Extract a {"message": ..} envelope from an LLM response.

    Uses the same tolerant strategy as _parse_email_envelope so a prose lead-in
    like "Here is your Teams message: {..}" or a fenced ```json block anywhere
    in the response still parses. Returns None when no envelope is present.
    """
    import re as _re

    if not raw_output or not raw_output.strip():
        return None
    text = raw_output.strip()

    candidates: list[str] = [text]

    for m in _re.finditer(r'```[a-zA-Z]*\s*\n?(.*?)```', text, _re.DOTALL):
        block = m.group(1).strip()
        if block:
            candidates.append(block)

    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and "message" in parsed:
            return parsed
    return None


def _reask_teams_envelope(raw_output: str, prompt: str, task_id: str = "") -> dict | None:
    """One-shot repair pass: ask the model to reshape its prose into {"message": ..}.

    Same shape as _reask_email_envelope — exactly one extra model call, no tools,
    any error returns None so the caller falls through to defensive composition.
    """
    if not raw_output or not raw_output.strip():
        return None
    try:
        from models.model_router import model_router
        repair_prompt = (
            "Convert the assistant response below into a Teams message envelope.\n\n"
            'Reply with ONLY a valid JSON object — no markdown fences, no commentary:\n'
            '{"message": "<the full message the recipient should read>"}\n\n'
            "Rules:\n"
            "- The message must contain ONLY the text the recipient should see.\n"
            "- Never include the recipient address, a channel name, or a team name\n"
            "  inside the message unless it is part of the actual conversation.\n"
            "- Do not summarise or shorten the content — carry it over faithfully.\n\n"
            f"[ORIGINAL TASK]\n{prompt}\n\n"
            f"[ASSISTANT RESPONSE TO CONVERT]\n{raw_output}"
        )
        reply = model_router.generate(repair_prompt, model_hint="complex")
        parsed = _parse_teams_envelope(str(reply or ""))
        if parsed is not None:
            logger.info(
                f"cowork_task_worker: teams envelope repaired on re-ask task={task_id}"
            )
            return parsed
        logger.warning(
            f"cowork_task_worker: teams envelope re-ask still unparsed task={task_id}"
        )
    except Exception as exc:
        logger.warning(f"cowork_task_worker: teams envelope re-ask failed task={task_id}: {exc}")
    return None


def _sanitize_teams_body(body: str) -> str:
    """Strip narration prefixes and unwrap a leaked {"message": ..} envelope.

    Deliberately narrower than _sanitize_email_body: Teams chats legitimately
    start with "Hi Adarsh," or "Subject update:" — the email header-block scrub
    would eat those. We only strip narration ("I have sent a message …",
    "Here is the message …") and unwrap a stray JSON envelope. Never returns
    empty: if scrubbing would empty the body, the original is kept.
    """
    import json as _json
    import re as _re

    if not body:
        return body
    original = body
    text = str(body).replace("\r\n", "\n").replace("\r", "\n").strip()

    # 1. Strip narration prefix (shares _NARRATION_PREFIX_RE with the email path
    #    so "I have sent a message …" is scrubbed here too).
    _narration = _re.compile(
        _NARRATION_PREFIX_RE + r'[^\n:]{0,120}?(?:\s*[:\-]\s*\n?|\s*\n)',
        _re.IGNORECASE | _re.VERBOSE,
    )
    text = _narration.sub("", text, count=1).strip()

    # 2. Unwrap {"message": ..} if it leaked through.
    _unfenced = _re.sub(r'^```[a-zA-Z]*\s*|\s*```$', "", text).strip()
    if _unfenced.startswith("{") and '"message"' in _unfenced:
        try:
            _parsed = _json.loads(_unfenced)
            if isinstance(_parsed, dict) and "message" in _parsed:
                text = str(_parsed.get("message") or "").strip()
        except (ValueError, TypeError):
            pass

    # 3. Strip a single pair of wrapping quotes left by the preamble removal.
    text = text.strip()
    if len(text) >= 2 and text[0] in '"\u201c\u2018' and text[-1] in '"\u201d\u2019':
        text = text[1:-1].strip()

    return text if text else original.strip()


def _looks_like_teams_narration(text: str) -> bool:
    """True when `text` is agent NARRATION about a Teams message, not the message.

    Narrower than _looks_like_leaky_output: we do NOT reject header labels
    (Teams messages can legitimately open with "Subject:" or "Update:"). Only
    narration prefixes and raw JSON envelope residue count.
    """
    import re as _re
    if not text or not str(text).strip():
        return False
    raw = str(text).strip()
    if _re.search(r'"(?:message|body)"\s*:', raw) and raw.startswith("{"):
        return True
    if _re.match(_NARRATION_PREFIX_RE, raw, _re.IGNORECASE):
        return True
    return False


def _compose_teams_body(
    prompt: str, llm_message: str, raw_output: str,
) -> tuple[str, str, bool]:
    """Choose the Teams message body and say how much we trust it.

    Returns ``(body, source, confident)`` with the same semantics as
    _compose_email_body: confident=False means the caller MUST hold the message
    in the outbox rather than post it.

    Priority:
      1. explicit body dictated in the prompt (`body: "..."`) → confident
      2. parsed {"message": ..} from the LLM                  → confident
      3. raw output that doesn't look like narration          → confident
      4. otherwise                                             → not confident
    """
    explicit = _extract_email_body(prompt) if prompt else ""
    if explicit and explicit.strip():
        return _sanitize_teams_body(explicit), "prompt", True

    if llm_message and llm_message.strip():
        return _sanitize_teams_body(llm_message), "llm_json", True

    if raw_output and raw_output.strip():
        if not _looks_like_teams_narration(raw_output):
            return _sanitize_teams_body(raw_output), "raw_output", True
        return _sanitize_teams_body(raw_output), "raw_output_unparsed", False

    return "", "none", False


def _compose_message_body(
    prompt: str, llm_body: str, raw_output: str, intent: dict,
) -> tuple[str, str, bool]:
    """Dispatcher: pick the composer that matches the delivery intent.

    Pure passthrough for email/none — delegates to _compose_email_body with the
    same arguments and return, so the Outlook regression suite (Testing §4) can
    prove nothing changes for the historical path.
    """
    kind = (intent or {}).get("kind") or "email"
    if kind in ("teams_chat", "teams_channel"):
        return _compose_teams_body(prompt, llm_body, raw_output)
    # email / none — unchanged path.
    return _compose_email_body(prompt, llm_body, raw_output)


def _maybe_deliver_preapproved(
    task: dict, user_id: str, output: str, llm_subject: str = "",
    body_confident: bool = True, body_source: str = "",
    intent: dict | None = None,
) -> dict:
    """
    Deliver the scheduled task result via the appropriate path:
      1. If the task has a pre-approved connector action AND it is allowlisted,
         execute it (PII is redacted before sending, but sends are never blocked).
      2. If no approved_action, dispatch on the detected `intent`:
         • teams_chat    → teams_start_chat + teams_send_chat_message
         • teams_channel → resolve team_id/channel_id + teams_send_message
         • email         → extract recipient from prompt and outlook_send_mail
      3. Fall back to the user's own mailbox if no recipient is found in the prompt.
      4. Store to outbox only if no connector is available.

    llm_subject:    the subject line generated by the LLM (preferred over heuristic
                    derivation from the prompt). Passed through to the send call.
    body_confident: False when `output` is a best-effort body salvaged from an
                    unparsed narrative. Such a body is STORED, never sent — it is
                    how the recipient address and subject line used to leak into
                    the message. Override with BUDDY_SEND_ON_UNPARSED=1.
    body_source:    provenance of the body, for the outbox record and logs.
    intent:         delivery intent descriptor from _detect_delivery_intent. When
                    None (or kind email/none) the function behaves EXACTLY as it
                    did before Teams support was added.

    Returns a delivery descriptor: {mode, ...}. Default mode is "outbox" — the
    result is simply stored; nothing is sent.
    """
    import os as _os

    if not body_confident and _os.getenv("BUDDY_SEND_ON_UNPARSED", "").strip() not in ("1", "true", "TRUE"):
        # Include the task id: without it this line can't be traced back to the
        # task that silently stopped emailing, which is how an all-outbox
        # regression went unnoticed. NOT_EMAILED is the grep handle.
        logger.warning(
            f"cowork_task_worker: NOT_EMAILED task={task.get('id') or '?'} — body not "
            f"confidently composed (source={body_source or 'unknown'}); storing draft to "
            f"outbox instead of sending. Set BUDDY_SEND_ON_UNPARSED=1 to force "
            f"best-effort delivery."
        )
        return {
            "mode": "outbox",
            "reason": "unparsed_composition",
            "body_source": body_source or "unknown",
        }

    # ── Teams delivery (2026-08-12) ─────────────────────────────────────────
    # Early return so the email path below is reached only for email/none
    # intents — every line of the historical email path is preserved as-is.
    _kind = ((intent or {}).get("kind") or "").lower()
    if _kind in ("teams_chat", "teams_channel"):
        return _deliver_to_teams(task, user_id, output, intent or {})

    action = task.get("approved_action")
    if not action or not isinstance(action, dict):
        # No explicit pre-approved action — extract recipient from the task prompt
        # (e.g. "Send an email to foo@bar.com"). Fall back to the user's own mailbox.
        prompt_text = task.get("prompt") or ""
        _to_recipient = _extract_email_recipient(prompt_text)

        # Subject priority: LLM-generated → explicit in prompt → fallback label
        _subject = (
            llm_subject
            or _extract_email_subject(prompt_text)
            or f"AiNxt scheduled: {(task.get('role') or 'update').strip()[:60] or 'update'}"
        )

        # Body: already selected and sanitized by _compose_email_body — do NOT
        # re-derive it here. The old `_extract_email_body(prompt) or output` chain
        # duplicated that logic and was a second route for the raw narrative to
        # become the body. Sanitize once more only as an idempotent safety net.
        _body = _sanitize_email_body(output, recipient=_to_recipient, subject=_subject)

        # Determine the actual recipient: prompt-specified → self → outbox
        if _to_recipient:
            _send_to = _to_recipient
            _send_mode = "to_recipient"
        else:
            _send_to = _resolve_user_email(user_id)
            _send_mode = "self"

        if _send_to:
            try:
                # Send through the SAME pipeline normal CoWork uses (mcp_bridge.call_tool)
                # so the delivered email has the identical structured format.
                res = _send_via_cowork_pipeline(
                    user_id, "microsoft_365", "outlook_send_mail",
                    {"to": _send_to, "subject": _subject, "body": _body},
                )
                if res["ok"]:
                    logger.info(
                        f"cowork_task_worker: scheduled result emailed "
                        f"({_send_mode}) to {_send_to} subject={_subject!r}"
                    )
                    return {
                        "mode": "sent",
                        "action": "microsoft_365.outlook_send_mail",
                        "to": _send_mode,
                    }
                err = res["text"] or "unknown"
                logger.warning(f"cowork_task_worker: email send failed ({mask_email(_send_mode)}): {err}")
                return {"mode": "outbox", "reason": "send_failed", "error": err}
            except Exception as exc:
                logger.error(f"cowork_task_worker: email send error ({mask_email(_send_mode)}): {exc}")
                return {"mode": "outbox", "reason": "send_error", "error": str(exc)}
        return {"mode": "outbox"}

    connector = (action.get("connector") or "").strip()
    tool = (action.get("tool") or "").strip()
    params = dict(action.get("params") or {})
    allowlist = task.get("action_allowlist") or []

    key = f"{connector}.{tool}"

    # ── Platform-wide permission check (takes precedence over per-task allowlist) ──
    # If the user has stored always_allow=TRUE for this connector+tool in
    # ainxt.user_connector_permissions, execute without requiring the action to be
    # in the per-task action_allowlist. This lets users pre-approve recurring
    # scheduled actions once, platform-wide, rather than per-task.
    if connector and tool:
        try:
            from connectors.engine import connector_engine
            _perm = connector_engine._check_user_permission(user_id, connector, tool)
            if _perm == "always_allow":
                logger.info(
                    f"cowork_task_worker: {key} has platform-wide always_allow "
                    f"for user {user_id} — executing without per-task allowlist check"
                )
                return _execute_approved_connector_action(connector, tool, params, user_id, output, key, task=task, llm_subject=llm_subject)
        except Exception as _pe:
            logger.warning(f"cowork_task_worker: platform permission check failed — {_pe}")

    # ── Per-task allowlist gate ────────────────────────────────────────────────
    # Allowlist gate: the exact "connector.tool" must be explicitly permitted.
    if not connector or not tool or key not in set(allowlist):
        logger.info(
            f"cowork_task_worker: write {key or '(none)'} not allowlisted — "
            f"storing draft to outbox (no auto-send)"
        )
        return {"mode": "outbox", "reason": "not_allowlisted", "requested": key}

    return _execute_approved_connector_action(connector, tool, params, user_id, output, key, task=task, llm_subject=llm_subject)


def _execute_approved_connector_action(
    connector: str, tool: str, params: dict, user_id: str, output: str, key: str,
    task: dict | None = None,
    llm_subject: str = "",
) -> dict:
    """
    Execute a pre-approved connector write action with compliance gating.
    Shared by both the per-task allowlist path and the platform-wide always_allow path.

    llm_subject: LLM-generated subject line (preferred over heuristic derivation).
    """
    # The agent output becomes the body unless the task fixed one explicitly.
    # Sanitize it first so no send-instruction / recipient / subject preamble can
    # leak into the message — the pre-approved send is then as clean as an
    # interactive CoWork send.
    _clean_output = _sanitize_email_body(output)
    for body_field in ("body", "message", "content", "text"):
        if body_field in params and not str(params[body_field]).strip():
            params[body_field] = _clean_output

    # Subject priority: LLM-generated → pre-approved params subject → derived from prompt.
    if "subject" in params and not str(params.get("subject", "")).strip():
        if llm_subject:
            params["subject"] = llm_subject
        else:
            _prompt = (task or {}).get("prompt") or ""
            import re as _re
            _clean = _re.sub(
                r"(?:send\s+(?:an?\s+)?(?:email|mail)\s+to\s+[^\s]+\s*)", "", _prompt,
                flags=_re.IGNORECASE,
            ).strip(" .,;")
            params["subject"] = (_clean[:80] or "AiNxt scheduled update").strip()

    # Execute through the SAME pipeline normal CoWork uses (mcp_bridge.call_tool).
    # That pipeline applies the identical outbound compliance HARD-BLOCK, recipient
    # validation and attachment handling as an interactive CoWork send — so the
    # scheduled send behaves and is formatted exactly like a normal CoWork send.
    # (Compliance is enforced INSIDE the pipeline; we no longer redact separately.)
    try:
        res = _send_via_cowork_pipeline(user_id, connector, tool, params)
        if res["ok"]:
            logger.info(f"cowork_task_worker: pre-approved action sent {key}")
            return {"mode": "sent", "action": key}
        err = res["text"] or "unknown"
        logger.warning(f"cowork_task_worker: pre-approved action failed {key}: {err}")
        return {"mode": "outbox", "reason": "send_failed", "action": key, "error": err}
    except Exception as exc:
        logger.error(f"cowork_task_worker: connector execute error {key}: {exc}")
        return {"mode": "outbox", "reason": "send_error", "action": key, "error": str(exc)}


# ── Teams delivery (2026-08-12) ────────────────────────────────────────────────
#
# The two entrypoints below dispatch a Teams chat or channel post through the
# EXACT same mcp_bridge.call_tool pipeline the interactive CoWork tool uses.
# That means outbound compliance HARD-BLOCK, attachment handling, and token
# refresh apply identically to a scheduled Teams post and an interactive one.
#
# Anything that cannot be resolved (unknown channel, unknown recipient, send
# failure) falls back to the outbox with a specific `reason` so the inbox
# notification can tell the user WHY the message didn't land.

def _mcp_call(user_id: str, connector: str, tool: str, params: dict) -> dict:
    """Thin wrapper around mcp_bridge.call_tool that returns {ok, text, data}.

    Read-style tools return JSON in `text`; we surface the parsed object as
    `data` when it decodes so the caller can pick IDs out of it directly.
    """
    from connectors import mcp_bridge
    name = f"{connector}__{tool}"
    result = mcp_bridge.call_tool(user_id, name, dict(params or {}), allowed=None)
    is_err = bool(result.get("isError")) if isinstance(result, dict) else True
    text = ""
    try:
        text = (result.get("content") or [{}])[0].get("text", "") or ""
    except Exception:
        pass
    data = None
    try:
        data = json.loads(text) if text else None
    except (ValueError, TypeError):
        data = None
    return {"ok": (not is_err), "text": text, "data": data}


def _teams_pick_id(data, *keys: str) -> str:
    """Best-effort extractor for an id field from a list/dict response.

    Graph list responses are usually `{"value": [{...}]}` or a bare list; when
    a single object is returned we take its id. Returns "" when nothing plausible
    is found so the caller can fall back to outbox with a clear reason.
    """
    if data is None:
        return ""
    items = data
    if isinstance(data, dict):
        items = data.get("value") if isinstance(data.get("value"), list) else [data]
    if not isinstance(items, list) or not items:
        return ""
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            v = it.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _teams_find_by_name(data, name_field: str, wanted: str) -> dict | None:
    """Case-insensitive find of the first item whose `name_field` matches `wanted`.

    Exact match wins over prefix; None when nothing matches. Used for both
    team-name → team_id and channel-name → channel_id resolution.
    """
    if data is None or not wanted:
        return None
    items = data
    if isinstance(data, dict):
        items = data.get("value") if isinstance(data.get("value"), list) else [data]
    if not isinstance(items, list):
        return None
    want = wanted.strip().lower()
    exact, prefix = None, None
    for it in items:
        if not isinstance(it, dict):
            continue
        val = str(it.get(name_field) or "").strip().lower()
        if not val:
            continue
        if val == want and exact is None:
            exact = it
        elif val.startswith(want) and prefix is None:
            prefix = it
    return exact or prefix


def _deliver_to_teams(task: dict, user_id: str, message: str, intent: dict) -> dict:
    """Post `message` to Teams according to `intent`.

    Returns a delivery descriptor: {"mode": "sent"|"outbox", ...}. Only the
    outbox modes carry a `reason` so the inbox notification can explain
    exactly why the post did not go through.
    """
    kind = (intent or {}).get("kind") or ""
    if not message or not message.strip():
        return {"mode": "outbox", "reason": "empty_message", "kind": kind}

    try:
        if kind == "teams_chat":
            recipient = (intent.get("recipient_email") or "").strip()
            if not recipient:
                return {"mode": "outbox", "reason": "teams_chat_recipient_missing"}

            # Step 1 — get (or create) the 1:1 chat. Graph is idempotent.
            chat = _mcp_call(user_id, "microsoft_365", "teams_start_chat",
                             {"user_email": recipient})
            if not chat["ok"]:
                logger.warning(
                    f"cowork_task_worker: teams_start_chat failed for {recipient}: "
                    f"{chat['text'][:200]}"
                )
                return {
                    "mode": "outbox",
                    "reason": "teams_chat_recipient_unresolved",
                    "recipient": recipient,
                    "error": chat["text"],
                }
            chat_id = ""
            if isinstance(chat["data"], dict):
                chat_id = (chat["data"].get("id") or chat["data"].get("chat_id") or "").strip()
            if not chat_id:
                chat_id = _teams_pick_id(chat["data"], "id", "chat_id")
            if not chat_id:
                return {
                    "mode": "outbox",
                    "reason": "teams_chat_id_unresolved",
                    "recipient": recipient,
                    "error": chat["text"],
                }

            # Step 2 — send through the same pipeline interactive CoWork uses.
            send = _mcp_call(user_id, "microsoft_365", "teams_send_chat_message",
                             {"chat_id": chat_id, "message": message})
            if send["ok"]:
                logger.info(
                    f"cowork_task_worker: Teams chat delivered to {recipient} "
                    f"(chat_id={chat_id[:12]}…)"
                )
                return {
                    "mode": "sent",
                    "action": "microsoft_365.teams_send_chat_message",
                    "kind": "teams_chat",
                    "recipient": recipient,
                }
            return {
                "mode": "outbox",
                "reason": "teams_send_failed",
                "kind": "teams_chat",
                "recipient": recipient,
                "error": send["text"],
            }

        # ── teams_channel ────────────────────────────────────────────────
        wanted_channel = (intent.get("channel") or "").strip()
        wanted_team = (intent.get("team") or "").strip()
        if not wanted_channel:
            return {"mode": "outbox", "reason": "teams_channel_missing"}

        # Resolve team_id — the read-side helper is teams_list_teams.
        teams = _mcp_call(user_id, "microsoft_365", "teams_list_teams", {})
        if not teams["ok"]:
            return {
                "mode": "outbox",
                "reason": "teams_list_failed",
                "channel": wanted_channel,
                "team": wanted_team,
                "error": teams["text"],
            }
        team_row = None
        if wanted_team:
            team_row = _teams_find_by_name(teams["data"], "displayName", wanted_team)
        else:
            # No team hint — if the user only belongs to ONE team, pick it. Otherwise
            # bail to outbox so we don't guess wrong.
            items = teams["data"].get("value") if isinstance(teams["data"], dict) else teams["data"]
            if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
                team_row = items[0]
        if not team_row or not team_row.get("id"):
            return {
                "mode": "outbox",
                "reason": "teams_channel_team_not_found",
                "channel": wanted_channel,
                "team": wanted_team,
            }
        team_id = str(team_row.get("id"))

        # Resolve channel_id inside that team.
        chans = _mcp_call(user_id, "microsoft_365", "teams_list_channels",
                          {"team_id": team_id})
        if not chans["ok"]:
            return {
                "mode": "outbox",
                "reason": "teams_channels_list_failed",
                "channel": wanted_channel,
                "team": team_row.get("displayName") or wanted_team,
                "error": chans["text"],
            }
        chan_row = _teams_find_by_name(chans["data"], "displayName", wanted_channel)
        if not chan_row or not chan_row.get("id"):
            return {
                "mode": "outbox",
                "reason": "teams_channel_not_found",
                "channel": wanted_channel,
                "team": team_row.get("displayName") or wanted_team,
            }
        channel_id = str(chan_row.get("id"))

        # Post it.
        send = _mcp_call(user_id, "microsoft_365", "teams_send_message", {
            "team_id":    team_id,
            "channel_id": channel_id,
            "message":    message,
        })
        if send["ok"]:
            logger.info(
                f"cowork_task_worker: Teams channel post delivered — "
                f"team={team_row.get('displayName')!r} channel={wanted_channel!r}"
            )
            return {
                "mode": "sent",
                "action": "microsoft_365.teams_send_message",
                "kind": "teams_channel",
                "team": team_row.get("displayName") or wanted_team,
                "channel": wanted_channel,
            }
        return {
            "mode": "outbox",
            "reason": "teams_send_failed",
            "kind": "teams_channel",
            "team": team_row.get("displayName") or wanted_team,
            "channel": wanted_channel,
            "error": send["text"],
        }
    except Exception as exc:
        logger.error(f"cowork_task_worker: Teams delivery error kind={kind}: {exc}")
        return {"mode": "outbox", "reason": "teams_delivery_error",
                "kind": kind, "error": str(exc)}


# ── Persistence helpers ────────────────────────────────────────────────────────

def _load_task(task_id: str) -> dict | None:
    """Load the scheduled-task row as a plain dict (JSONB fields parsed).

    approved_action is now JSONB (nullable) — psycopg2 returns it as a Python
    dict already. We still call _as_json() as a safety net for any edge case
    where the driver returns a raw JSON string, but we explicitly guard against
    the legacy BOOLEAN case (True/False) so un-migrated rows don't crash.
    """
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            row = db.execute(
                sa.text(
                    "SELECT id, user_id, role, prompt, cron, connectors, status, "
                    "approved_action, action_allowlist, last_run, next_run, "
                    "COALESCE(tz, 'UTC') AS tz "
                    "FROM cowork_scheduled_tasks WHERE id = :tid"
                ),
                {"tid": task_id},
            ).mappings().first()
            if not row:
                return None
            task = dict(row)
            # approved_action: JSONB → dict | None.
            # Guard: if the column is still BOOLEAN on an un-migrated DB, treat as None
            # so the worker falls back gracefully instead of crashing.
            raw_aa = task.get("approved_action")
            if isinstance(raw_aa, bool) or raw_aa is False or raw_aa is True:
                task["approved_action"] = None
            else:
                task["approved_action"] = _as_json(raw_aa)  # dict, None, or parsed str
            task["action_allowlist"] = _as_json(task.get("action_allowlist")) or []
            return task
        finally:
            db.close()
    except Exception as exc:
        logger.error(f"cowork_task_worker: load task {task_id} failed: {exc}")
        return None


def _record_run(
    run_id: str,
    task_id: str,
    user_id: str,
    *,
    status: str,
    error: str | None,
    output: str,
    delivery: dict | None = None,
) -> None:
    """Insert a history/outbox row. Output is already compliance-redacted.

    The delivery descriptor is appended to the run output for observability —
    cowork_task_runs has no dedicated delivery column in the canonical schema.
    """
    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            stored_output = output[:_MAX_OUTPUT_CHARS]
            if delivery:
                # Annotate the run output with the (non-sensitive) delivery mode
                # so the outbox/history reflects how the result was handled.
                suffix = f"\n\n[delivery: {delivery.get('mode', 'outbox')}]"
                stored_output = (stored_output + suffix)[:_MAX_OUTPUT_CHARS]
            db.execute(
                sa.text(
                    "INSERT INTO cowork_task_runs "
                    "(id, task_id, user_id, status, output, error, created_at) "
                    "VALUES (:id, :task_id, :user_id, :status, :output, "
                    ":error, NOW())"
                ),
                {
                    "id": run_id,
                    "task_id": task_id,
                    "user_id": user_id,
                    "status": status,
                    "output": stored_output,
                    "error": error,
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"cowork_task_worker: record run {run_id} failed: {exc}")


def _update_schedule(task_id: str, task: dict, last_run_status: str = "done") -> None:
    """Set last_run=now, last_run_status, and compute next_run from the cron expr.

    The scheduler is the authoritative driver of next_run, but we advance it here
    too so a manually-triggered (run-now) execution keeps the schedule coherent.
    """
    now = datetime.now(timezone.utc)
    next_run = None
    cron_expr = task.get("cron")
    try:
        if cron_expr:
            # Interpret the cron in the task's timezone (the scheduler does the
            # same via _next_run_utc). Passing a UTC `now` with no tz here was the
            # bug that pushed '20 14 * * *' to 14:20 UTC (= 19:50 IST) each run.
            from workers.cowork_scheduler import _next_run_utc
            next_run = _next_run_utc(str(cron_expr), now, task.get("tz") or "UTC")
    except Exception:
        next_run = None

    try:
        from db.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(
                sa.text(
                    "UPDATE cowork_scheduled_tasks "
                    "SET last_run = :last_run, last_run_status = :last_run_status, "
                    "next_run = :next_run, updated_at = NOW() "
                    "WHERE id = :tid"
                ),
                {
                    "last_run": now,
                    "last_run_status": last_run_status,
                    "next_run": next_run,
                    "tid": task_id,
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"cowork_task_worker: update schedule {task_id} failed: {exc}")


def _as_json(value):
    """Coerce a JSONB column that may arrive as str or already-parsed object."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
