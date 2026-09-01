# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SDLC RUN STORE
# PostgreSQL-backed persistence for SDLC pipeline runs.
# Gracefully falls back to in-process dict if DB unavailable.
# ============================================================

import json
import time
import uuid
from datetime import datetime
from typing import Optional

from core.logger import logger


class SDLCCancelled(Exception):
    """Raised by a state transition when the run was cancelled out-of-band.

    Pipeline functions catch this to stop cleanly WITHOUT marking the run
    FAILED — the run is already in the terminal CANCELLED state and must stay
    there. Defined here (a leaf module imported by both the pipeline and the
    coding state machine) to avoid a circular import between them.
    """
    def __init__(self, run_id: str = ""):
        self.run_id = run_id
        super().__init__(f"SDLC run {run_id} cancelled")


class SDLCUserTokenMissing(SDLCCancelled):
    """Raised when a user-triggered run needs the triggering user's own GitLab
    PAT (e.g. to clone an indexed repo) but that user has no token stored.

    Subclasses SDLCCancelled deliberately: the caller has ALREADY set the run
    to SUSPENDED with a clear, actionable message before raising, so every
    existing ``except SDLCCancelled`` handler stops the pipeline cleanly
    WITHOUT flipping the run to FAILED and clobbering that message. This is the
    fail-fast alternative to silently returning an empty workspace and letting a
    later CLI spawn die on ``cwd=''`` (``[Errno 2] No such file or directory: ''``).
    """
    def __init__(self, run_id: str = "", reason: str = ""):
        self.reason = reason
        super().__init__(run_id)
        if reason:
            self.args = (reason,)


# In-process fallback when Postgres is unavailable
_runs:   dict = {}
_events: dict = {}   # run_id → list of events


# ── Helper: DB session ────────────────────────────────────────

def _get_session():
    try:
        from db.database import SessionLocal
        return SessionLocal()
    except Exception:
        return None


def _is_valid_uuid(run_id) -> bool:
    """True iff `run_id` is a syntactically valid UUID string safe to use in a
    `WHERE id = %s::UUID` query. Rejects None, non-strings, "", and junk like the
    literal "null" so those never reach Postgres (which would raise an
    InvalidTextRepresentation cast error)."""
    if not isinstance(run_id, str) or not run_id.strip():
        return False
    try:
        uuid.UUID(run_id)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ── Repo-string canonicalization for run → department matching ────────
#
# sdlc_runs.repo (from the trigger request) and product_repos.repo_name (as the
# product owner typed it) are both the "group/project" path, but drift in case,
# surrounding whitespace, a trailing slash, or a ".git" suffix. Both sides are
# normalized identically so the department mapping doesn't silently miss and
# drop a user to owner/admin-only visibility.
#
# NOTE: this intentionally does NOT touch dots or internal slashes (unlike the
# vector index-key slugifiers in models/hybrid_search.py etc.) — "group/project"
# must stay distinct from "group.project" for repo identity.

def _norm_repo(repo: str) -> str:
    """Canonical repo key (Python side): lowercased, trimmed, no trailing slash or ``.git``."""
    r = (repo or "").strip().lower().rstrip("/")
    if r.endswith(".git"):
        r = r[:-4].rstrip("/")
    return r


def _norm_repo_sql(col):
    """SQL expression equivalent of :func:`_norm_repo` for a repo column."""
    from sqlalchemy import func
    expr = func.lower(func.trim(col))                 # case + surrounding whitespace
    expr = func.regexp_replace(expr, r'/+$', '')      # trailing slash(es)
    expr = func.regexp_replace(expr, r'\.git$', '')   # trailing ".git" suffix
    expr = func.regexp_replace(expr, r'/+$', '')      # any slash left before ".git"
    return expr


# ── CREATE RUN ────────────────────────────────────────────────

def create_run(
    run_type:     str,
    jira_key:     str    = "",
    jira_summary: str    = "",
    repo:         str    = "",
    triggered_by: str    = "webhook",
    created_by:   str    = "",
) -> dict:
    """Create and persist a new SDLC run. Returns the run dict."""
    run_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    run    = {
        "id":            run_id,
        "type":          run_type,
        "jira_key":      jira_key,
        "jira_summary":  jira_summary,
        "repo":          repo,
        "branch":        "",
        "pr_number":     None,
        "pr_url":        "",
        "confluence_url": "",
        "state":         "CREATED",
        "current_stage": None,
        "context":       {},
        "error":         None,
        "triggered_by":  triggered_by,
        "created_by":    created_by or triggered_by or "",
        "created_at":    now,
        "updated_at":    now,
    }

    # Synchronous Postgres write FIRST — commit the initial row BEFORE the
    # run_created Kafka event is published (W-race, 2026-08-06).
    #
    # Previously the Kafka event was produced first and the sync insert second.
    # That opened a create-time race: the consumer on App03 could INSERT the row
    # before this synchronous insert committed, so the sync commit then aborted
    # with UniqueViolation on sdlc_runs_pkey. An aborted transaction left the
    # sync writer unable to persist anything, and the UI-polled row could get
    # stuck in the initial "scanning"/CREATED state.
    #
    # Fix: the sync path is the AUTHORITATIVE writer for the initial row and
    # commits before anyone else can act on the event. The insert is also made
    # idempotent (INSERT ... ON CONFLICT (id) DO NOTHING) so that even if a stray
    # duplicate exists, the statement is a no-op instead of raising — the
    # transaction never aborts and later state transitions always persist.
    session = _get_session()
    if session:
        try:
            from sqlalchemy import text as _sqlt
            from datetime import datetime as _dt
            created_dt = _dt.utcnow()
            session.execute(
                _sqlt(
                    "INSERT INTO sdlc_runs "
                    "(id, type, jira_key, jira_summary, repo, branch, state, "
                    " context, triggered_by, created_by, created_at, updated_at) "
                    "VALUES (:id, :type, :jira_key, :jira_summary, :repo, :branch, "
                    " :state, CAST(:context AS jsonb), :triggered_by, :created_by, "
                    " :created_at, :updated_at) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id":           run_id,
                    "type":         run_type,
                    "jira_key":     jira_key,
                    "jira_summary": jira_summary,
                    "repo":         repo,
                    "branch":       "",
                    "state":        "CREATED",
                    "context":      json.dumps({}),
                    "triggered_by": triggered_by,
                    "created_by":   created_by or triggered_by or None,
                    "created_at":   created_dt,
                    "updated_at":   created_dt,
                },
            )
            session.commit()
            logger.info(f"sdlc_store: run {run_id} written to DB (sync)")
        except Exception as e:
            logger.warning(f"sdlc_store: DB create_run sync write failed → {e}")
            try:
                session.rollback()
            except Exception:
                pass
        finally:
            session.close()

    # Fire-and-forget: Kafka event for the App03 secondary write / audit trail.
    # Published AFTER the sync commit above so the consumer's idempotent upsert
    # is a no-op for the row this process already owns.
    try:
        from core.kafka_producer import produce, TOPIC_SDLC_EVENTS
        produce(TOPIC_SDLC_EVENTS, {
            "event":        "run_created",
            "run_id":       run_id,
            "run_type":     run_type,
            "jira_key":     jira_key,
            "jira_summary": jira_summary,
            "repo":         repo,
            "triggered_by": triggered_by,
            "ts":           now,
        }, key=run_id)
        logger.info(f"sdlc_store: created run {run_id} type={run_type} (async)")
    except Exception as e:
        logger.warning(f"sdlc_store: Kafka produce for run_created failed → {e}")

    # Always update in-process cache
    _runs[run_id] = run
    return run


# ── GET RUN ───────────────────────────────────────────────────

def get_run(run_id: str) -> Optional[dict]:
    """Fetch a run by ID. Tries DB first, falls back to in-process cache."""
    # Guard against non-UUID ids (e.g. a caller passing the literal string
    # "null" — a serialized JS/JSON null — or "", None, junk). The id column is
    # a Postgres UUID, so such a value makes the DB reject the query with an
    # InvalidTextRepresentation cast error and logs noise. Skip the DB entirely
    # and go straight to the cache, which returns None for an unknown id — the
    # same "no such run" result every caller already handles.
    if not _is_valid_uuid(run_id):
        return _runs.get(run_id)
    session = _get_session()
    if session:
        try:
            from db.models import SDLCRun
            db = session.query(SDLCRun).filter(SDLCRun.id == run_id).first()
            if db:
                run = _db_to_dict(db)
                _runs[run_id] = run
                return run
        except Exception as e:
            logger.warning(f"sdlc_store: DB get failed → {e}")
        finally:
            session.close()
    return _runs.get(run_id)


def get_run_for_hitl(run_id: str, required_keys: list = None) -> Optional[dict]:
    """
    Fetch a run for HITL resumption with retries.

    In cross-process scenarios (RQ worker wrote the run, gateway reads it on approval),
    the Kafka consumer may not have flushed to DB yet. Retry up to 4 times with
    exponential backoff before falling back to the in-process cache.
    required_keys: if provided, retry until all keys appear in run["context"].
    """
    import time as _time
    required_keys = required_keys or []
    delays = [0.3, 0.8, 1.5, 3.0]   # total wait ~5.6s before giving up

    for attempt, delay in enumerate(delays, start=1):
        run = get_run(run_id)
        if run:
            ctx = run.get("context") or {}
            if not required_keys or all(k in ctx for k in required_keys):
                if attempt > 1:
                    logger.info(f"sdlc_store: get_run_for_hitl {run_id} resolved on attempt {attempt}")
                return run
            logger.warning(
                f"sdlc_store: HITL context incomplete for {run_id} "
                f"(attempt {attempt}) — missing: {[k for k in required_keys if k not in ctx]}"
            )
        if attempt < len(delays):
            _time.sleep(delay)

    logger.error(f"sdlc_store: get_run_for_hitl {run_id} — context still incomplete after retries")
    return get_run(run_id)   # final attempt


# ── UPDATE STATE ──────────────────────────────────────────────

def update_run_state(
    run_id:              str,
    to_state:            str,
    current_stage:       Optional[str] = None,
    context_patch:       Optional[dict] = None,
    branch:              Optional[str] = None,
    pr_number:           Optional[int] = None,
    pr_url:              Optional[str] = None,
    confluence_url:      Optional[str] = None,
    error:               Optional[str] = None,
    suspended_at_stage:  Optional[str] = None,
) -> Optional[dict]:
    """Transition a run to a new state, optionally patching context."""
    run = get_run(run_id)
    if not run:
        logger.error(f"sdlc_store: run {run_id} not found")
        return None

    from_state = run["state"]
    run["state"] = to_state
    if current_stage is not None:
        run["current_stage"] = current_stage
    if context_patch:
        run["context"].update(context_patch)
    if branch is not None:
        run["branch"] = branch
    if pr_number is not None:
        run["pr_number"] = pr_number
    if pr_url is not None:
        run["pr_url"] = pr_url
    if confluence_url is not None:
        run["confluence_url"] = confluence_url
    if error is not None:
        run["error"] = error
    if suspended_at_stage is not None:
        run["suspended_at_stage"] = suspended_at_stage
    run["updated_at"] = datetime.utcnow().isoformat()

    _runs[run_id] = run

    # Synchronous Postgres write with row-level lock — ensures:
    # 1. context_patch is persisted before cross-process HITL resume reads it back
    # 2. Concurrent state transitions from two RQ workers / gateway instances
    #    are serialised at DB level (SELECT FOR UPDATE grabs the row lock before
    #    the modify-write, so context patches from one process cannot overwrite
    #    patches from another).
    session = _get_session()
    if session:
        try:
            from db.models import SDLCRun
            from datetime import datetime as _dt
            from sqlalchemy import text as _sqlt
            # Create-if-missing (W-race, 2026-08-06): guarantee a row exists so
            # this transition is never silently dropped when the create insert
            # was aborted/unavailable. Without this the read-modify-write below
            # would find nothing and no-op, leaving the UI-polled row stuck in
            # its initial state — never reaching the suspend to the approval
            # gate. Idempotent ON CONFLICT (id) DO NOTHING never aborts the txn.
            session.execute(
                _sqlt(
                    "INSERT INTO sdlc_runs "
                    "(id, type, jira_key, jira_summary, repo, state, context, "
                    " triggered_by, created_by, created_at, updated_at) "
                    "VALUES (:id, :type, :jira_key, :jira_summary, :repo, :state, "
                    " CAST(:context AS jsonb), :triggered_by, :created_by, "
                    " :created_at, :updated_at) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id":           run_id,
                    "type":         run.get("type", ""),
                    "jira_key":     run.get("jira_key", ""),
                    "jira_summary": run.get("jira_summary", ""),
                    "repo":         run.get("repo", ""),
                    "state":        "CREATED",
                    "context":      json.dumps({}),
                    "triggered_by": run.get("triggered_by", ""),
                    "created_by":   run.get("created_by") or None,
                    "created_at":   _dt.utcnow(),
                    "updated_at":   _dt.utcnow(),
                },
            )
            # Acquire row-level lock before read-modify-write.
            # withfor_update() is not used here to stay SQLAlchemy-version-neutral.
            session.execute(
                _sqlt("SELECT id FROM sdlc_runs WHERE id = :id FOR UPDATE"),
                {"id": run_id},
            )
            db_run = session.query(SDLCRun).filter(SDLCRun.id == run_id).first()
            if db_run:
                db_run.state = to_state
                if current_stage is not None:
                    db_run.current_stage = current_stage
                if context_patch:
                    db_run.context = {**(db_run.context or {}), **context_patch}
                if branch is not None:
                    db_run.branch = branch
                if pr_number is not None:
                    db_run.pr_number = pr_number
                if pr_url is not None:
                    db_run.pr_url = pr_url
                if confluence_url is not None:
                    db_run.confluence_url = confluence_url
                if error is not None:
                    db_run.error = error
                if suspended_at_stage is not None:
                    db_run.suspended_at_stage = suspended_at_stage
                db_run.updated_at = _dt.utcnow()
                session.commit()
        except Exception as _pg_err:
            logger.warning(f"sdlc_store: sync Postgres write failed → {_pg_err}")
            try:
                session.rollback()
            except Exception:
                pass
        finally:
            session.close()

    # Fire-and-forget: Kafka message for audit trail / App03 secondary write.
    try:
        from core.kafka_producer import produce, TOPIC_SDLC_EVENTS
        produce(TOPIC_SDLC_EVENTS, {
            "event":         "run_state_changed",
            "run_id":        run_id,
            "from_state":    from_state,
            "to_state":      to_state,
            "current_stage": current_stage,
            "context_patch": context_patch or {},
            "branch":        run.get("branch"),
            "pr_number":     run.get("pr_number"),
            "pr_url":        run.get("pr_url"),
            "confluence_url": run.get("confluence_url"),
            "error":         error,
            "ts":            run["updated_at"],
        }, key=run_id)
    except Exception as e:
        logger.warning(f"sdlc_store: Kafka produce for run_state_changed failed → {e}")

    # HOD budget deduction — fires once when the run reaches a terminal state.
    # Idempotent via hod_ledger_id guard; exceptions are swallowed so billing
    # failure never rolls back the state transition.
    if to_state in {"COMPLETE", "FAILED", "CANCELLED"}:
        try:
            from services.sdlc_budget_tracker import finalize_run_budget as _fin_budget
            _fin_budget(run_id)
        except Exception as _budget_err:
            logger.warning(
                f"sdlc_store: finalize_run_budget failed for run={run_id}: {_budget_err}"
            )


def update_run_work_item(run_id: str, work_item_dict: dict) -> bool:
    """Persist the locked WorkItem to sdlc_runs.work_item (JSONB) and set
    normalization_confirmed_at to the current timestamp.

    Idempotent: safe to call multiple times — later calls overwrite earlier ones.
    Returns True on success, False when the DB is unavailable.
    """
    session = _get_session()
    if not session:
        logger.warning(f"sdlc_store: update_run_work_item — DB unavailable for run={run_id}")
        return False
    try:
        from sqlalchemy import text as _sqlt
        from datetime import datetime as _dt
        confirmed_at = _dt.utcnow()
        session.execute(
            _sqlt(
                "UPDATE sdlc_runs "
                "SET work_item = CAST(:wi AS jsonb), "
                "    normalization_confirmed_at = :confirmed_at, "
                "    updated_at = :updated_at "
                "WHERE id = :run_id"
            ),
            {
                "wi":           json.dumps(work_item_dict),
                "confirmed_at": confirmed_at,
                "updated_at":   confirmed_at,
                "run_id":       run_id,
            },
        )
        session.commit()
        logger.info(
            "sdlc_store: work_item persisted",
            run_id=run_id,
            locked=work_item_dict.get("locked", False),
            confirmed_at=confirmed_at.isoformat(),
        )
        return True
    except Exception as exc:
        logger.warning(f"sdlc_store: update_run_work_item failed for run={run_id}: {exc}")
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def patch_run_context(run_id: str, context_patch: dict) -> Optional[dict]:
    """Patch run context fields without changing state.

    Use this instead of update_run_state(run_id, run["state"], context_patch=...)
    to avoid accidentally rolling back the state when the caller holds a stale
    run snapshot from before a _transition() call.
    """
    current = get_run(run_id)
    if not current:
        logger.error(f"sdlc_store: patch_run_context — run {run_id} not found")
        return None
    return update_run_state(run_id, current["state"], context_patch=context_patch)

    logger.info(f"sdlc_store: run {run_id} → {from_state} → {to_state}")

    # Phase 16: send Slack HITL notification when entering approval-waiting states
    _AWAITING_STATES = {
        "AWAITING_CODE_APPROVAL",      # renamed successor
        "AWAITING_DESIGN_APPROVAL",    # legacy alias — dual-read for in-flight rows
        "AWAITING_SOLUTION_APPROVAL",
        "AWAITING_PR_APPROVAL",
    }
    if to_state in _AWAITING_STATES:
        try:
            import os
            if os.getenv("SLACK_BOT_TOKEN"):
                from core.slack_bot import send_hitl_approval_message, SLACK_DEFAULT_CHANNEL
                summary = run.get("jira_summary") or run.get("jira_key") or run_id
                send_hitl_approval_message(
                    channel=SLACK_DEFAULT_CHANNEL,
                    run_id=run_id,
                    run_type=run.get("type", "unknown"),
                    summary=summary,
                )
        except Exception as _slack_err:
            logger.warning(f"sdlc_store: Slack HITL notification failed → {_slack_err}")

    return run


# ── ADD EVENT ─────────────────────────────────────────────────

def add_run_event(
    run_id:     str,
    from_state: str,
    to_state:   str,
    stage:      str  = "",
    actor:      str  = "",
    output:     str  = "",
    data:       Optional[dict] = None,
) -> dict:
    """Append a signed state transition event to the run's audit trail."""
    # DDL column is TEXT — guard against callers passing a dict or non-string
    if not isinstance(actor, str):
        actor = str(actor)
    event = {
        "id":         str(uuid.uuid4()),
        "run_id":     run_id,
        "from_state": from_state,
        "to_state":   to_state,
        "stage":      stage,
        "actor":      actor,
        "output":     output[:2000],  # cap storage
        "data":       data or {},
        "created_at": datetime.utcnow().isoformat(),
    }

    # Phase 18: cryptographically sign the event
    try:
        from core.audit_signer import sign_event
        event["signature"] = sign_event(event)
    except Exception as _sig_err:
        logger.warning(f"sdlc_store: audit signing failed → {_sig_err}")
        event["signature"] = ""

    _events.setdefault(run_id, []).append(event)

    # ── Authoritative synchronous Postgres write ──────────────────────────────
    # The timeline (GET /sdlc/runs/{id}/events) reads sdlc_run_events from Postgres.
    # We write the row DIRECTLY here — mirroring update_run_state()'s synchronous
    # sdlc_runs write — instead of relying on the Kafka→consumer path. Routing
    # ONLY through Kafka meant that whenever produce() returned a Redis-fallback
    # (or an async send was dropped without raising), the row silently never
    # landed → empty timeline. INSERT ... ON CONFLICT (dedupe_key) DO NOTHING keeps
    # this idempotent with the kafka_consumer's own insert (dedupe_key = event UUID),
    # so concurrent gateway/worker writers cannot create duplicate rows.
    session = _get_session()
    if session:
        try:
            from sqlalchemy import text as _sqlt
            from datetime import datetime as _dt
            session.execute(
                _sqlt(
                    "INSERT INTO sdlc_run_events "
                    "(id, run_id, from_state, to_state, stage, actor, output, data, signature, dedupe_key, created_at) "
                    "VALUES (:id, :run_id, :from_state, :to_state, :stage, :actor, :output, CAST(:data AS jsonb), :signature, :dedupe_key, :created_at) "
                    "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"
                ),
                {
                    "id":         event["id"],
                    "run_id":     run_id,
                    "from_state": from_state,
                    "to_state":   to_state,
                    "stage":      stage,
                    "actor":      actor,
                    "output":     event["output"],
                    "data":       __import__("json").dumps(data or {}),
                    "signature":  event.get("signature", ""),
                    "dedupe_key": event["id"],
                    "created_at": _dt.fromisoformat(event["created_at"]),
                },
            )
            session.commit()
        except Exception as e:
            logger.warning(f"sdlc_store: event direct insert failed → {e}")
            try:
                session.rollback()
            except Exception:
                pass
        finally:
            session.close()

    # ── Best-effort Kafka publish (audit trail / App03 secondary write) ───────
    # Secondary, fire-and-forget: the consumer's insert is deduped by dedupe_key,
    # so even if it also runs it cannot create a duplicate. A Kafka failure here
    # no longer loses the event — the direct insert above already persisted it.
    try:
        from core.kafka_producer import produce, TOPIC_SDLC_EVENTS
        produce(TOPIC_SDLC_EVENTS, {
            "event":      "run_event_appended",
            "run_id":     run_id,
            "event_id":   event["id"],
            "dedupe_key": event["id"],   # idempotency key = event UUID
            "from_state": from_state,
            "to_state":   to_state,
            "stage":      stage,
            "actor":      actor,
            "output":     event["output"],
            "data":       data or {},
            "signature":  event.get("signature", ""),
            "ts":         event["created_at"],
        }, key=run_id)
    except Exception as _ke:
        logger.debug(f"sdlc_store: Kafka produce for run_event_appended failed (non-fatal, row already persisted) → {_ke}")

    return event


# ── MULTI-REPO: sdlc_run_repos helpers ────────────────────────
#
# Added 2026-05-19 as part of multi-repo SDLC support (Phase 2).
# `sdlc_runs.(repo, pr_url, pr_number)` continue to denote the PRIMARY repo;
# these helpers manage the sibling table for every repo touched in a run
# (primary + editable + compile-only).
#
# Both helpers degrade to no-ops when Postgres is unavailable. The single-repo
# pipeline path never calls into here, so the in-process fallback dict that
# `create_run`/`update_run_state` use is intentionally NOT mirrored — multi-
# repo requires a real DB.

def upsert_run_repo(
    run_id:         str,
    repo:           str,
    ref:            str,
    kind:           str,
    *,
    source:         str = "",
    ref_sha:        Optional[str] = None,
    build_order:    Optional[int] = None,
    repo_ctx:       Optional[dict] = None,
    workspace_path: Optional[str] = None,
    state:          Optional[str] = None,
    error:          Optional[str] = None,
    working_branch: Optional[str] = None,
    pr_url:         Optional[str] = None,
    pr_number:      Optional[int] = None,
) -> Optional[dict]:
    """
    Insert or update a row in `sdlc_run_repos` for `(run_id, repo)`.

    Idempotent: callers can re-invoke during retries without producing
    duplicates (UNIQUE constraint on `(run_id, repo)`). Only fields whose
    arguments are not None are overwritten — existing values for omitted
    fields are preserved.
    """
    session = _get_session()
    if not session:
        logger.warning(
            f"sdlc_store.upsert_run_repo: DB unavailable, skipping write "
            f"for run={run_id} repo={repo!r}"
        )
        return None
    try:
        from db.models import SDLCRunRepo
        existing = (
            session.query(SDLCRunRepo)
            .filter(SDLCRunRepo.run_id == run_id, SDLCRunRepo.repo == repo)
            .first()
        )
        if existing is None:
            existing = SDLCRunRepo(
                run_id=run_id,
                repo=repo,
                ref=ref or "main",
                kind=kind,
                source=source or None,
                build_order=build_order,
                ref_sha=ref_sha,
                repo_ctx=repo_ctx or {},
                workspace_path=workspace_path,
                state=state,
                error=error,
                working_branch=working_branch,
                pr_url=pr_url,
                pr_number=pr_number,
            )
            session.add(existing)
        else:
            existing.ref = ref or existing.ref
            existing.kind = kind or existing.kind
            if source:
                existing.source = source
            if build_order is not None:
                existing.build_order = build_order
            if ref_sha is not None:
                existing.ref_sha = ref_sha
            if repo_ctx is not None:
                existing.repo_ctx = repo_ctx
            if workspace_path is not None:
                existing.workspace_path = workspace_path
            if state is not None:
                existing.state = state
            if error is not None:
                existing.error = error
            if working_branch is not None:
                existing.working_branch = working_branch
            if pr_url is not None:
                existing.pr_url = pr_url
            if pr_number is not None:
                existing.pr_number = pr_number
        session.commit()
        session.refresh(existing)
        return _run_repo_to_dict(existing)
    except Exception as exc:
        logger.error(
            f"sdlc_store.upsert_run_repo failed for run={run_id} repo={repo!r}: {exc}"
        )
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        session.close()


def list_run_repos(run_id: str) -> list:
    """Return all `sdlc_run_repos` rows for a run, ordered by build_order then repo."""
    session = _get_session()
    if not session:
        return []
    try:
        from db.models import SDLCRunRepo
        from sqlalchemy import asc, nullslast
        rows = (
            session.query(SDLCRunRepo)
            .filter(SDLCRunRepo.run_id == run_id)
            .order_by(nullslast(asc(SDLCRunRepo.build_order)), asc(SDLCRunRepo.repo))
            .all()
        )
        return [_run_repo_to_dict(r) for r in rows]
    except Exception as exc:
        logger.warning(f"sdlc_store.list_run_repos failed for run={run_id}: {exc}")
        return []
    finally:
        session.close()


def _run_repo_to_dict(r) -> dict:
    """Serialize an SDLCRunRepo ORM row to a plain dict."""
    return {
        "id":             r.id,
        "run_id":         r.run_id,
        "repo":           r.repo,
        "ref":            r.ref,
        "ref_sha":        r.ref_sha,
        "kind":           r.kind,
        "build_order":    r.build_order,
        "source":         r.source,
        "pr_url":         r.pr_url,
        "pr_number":      r.pr_number,
        "working_branch": r.working_branch,
        "repo_ctx":       r.repo_ctx or {},
        "workspace_path": r.workspace_path,
        "state":          r.state,
        "error":          r.error,
        "created_at":     r.created_at.isoformat() if r.created_at else None,
        "updated_at":     r.updated_at.isoformat() if r.updated_at else None,
    }


# ── LIST RUNS ─────────────────────────────────────────────────

def list_runs(
    limit: int = 50,
    run_type: Optional[str] = None,
    *,
    is_admin: bool = True,
    owner_ids: Optional[list] = None,
    department: Optional[str] = None,
) -> list:
    """
    Return recent SDLC runs, newest first.

    Uses the *light* projection — `context` is collapsed to a small set of
    UI-required fields (URLs, revision_count, MERGE_CONFLICT proposal). The
    heavy fields (`repo_ctx`, `classification`, `triage`, `analysis`,
    `design`, `fix`, `rca`, `code_output`, `test_result`, `pr_review`) are
    only available via `get_run(run_id)`. This keeps `/sdlc/runs?limit=100`
    payloads ~KB instead of tens of MB.

    Department scoping (when ``is_admin`` is False): a run is returned only if
    the caller created it (``created_by`` ∈ ``owner_ids``) OR its repo is mapped
    to the caller's ``department`` via ``product_repos ⋈ dept_product_mappings``.
    Runs on unmapped/empty repos are therefore visible only to their creator and
    to admins. The predicate is pushed into SQL *before* ``limit`` so the caller
    always gets up to ``limit`` runs they can actually see (never a page silently
    thinned by post-filtering). ``is_admin`` defaults to True so internal callers
    (workers, compliance sweep) keep their unscoped view unless a request passes
    the caller's JWT scope explicitly.
    """
    owner_ids = [o for o in (owner_ids or []) if o]
    session = _get_session()
    if session:
        try:
            from db.models import SDLCRun, ProductRepo, DeptProductMapping
            from sqlalchemy import desc, or_, false
            q = session.query(SDLCRun).order_by(desc(SDLCRun.created_at))
            if run_type:
                q = q.filter(SDLCRun.type == run_type)
            if not is_admin:
                conds = []
                if owner_ids:
                    conds.append(SDLCRun.created_by.in_(owner_ids))
                if department:
                    # Repos mapped to the caller's department, normalized (case /
                    # whitespace / trailing slash / ".git") via _norm_repo_sql so
                    # casing drift doesn't miss the mapping. Materialized to a list
                    # (a department maps to at most tens of repos) — the codebase
                    # idiom is `.in_(list)`, not `.in_(subquery)`. A run whose repo
                    # isn't in the set (empty repo, unregistered repo, product with
                    # no dept mapping) falls through to owner-only visibility.
                    dept_repos = [
                        row[0] for row in (
                            session.query(_norm_repo_sql(ProductRepo.repo_name))
                            .join(DeptProductMapping,
                                  DeptProductMapping.product_id == ProductRepo.product_id)
                            .filter(DeptProductMapping.department == department)
                            .all()
                        )
                    ]
                    if dept_repos:
                        conds.append(_norm_repo_sql(SDLCRun.repo).in_(dept_repos))
                # No identity at all (no owner_ids, no department) → see nothing.
                q = q.filter(or_(*conds) if conds else false())
            q = q.limit(limit)
            return [_db_to_dict_light(r) for r in q.all()]
        except Exception as e:
            logger.warning(f"sdlc_store: list failed → {e}")
        finally:
            session.close()

    # fallback (DB unavailable) — light-project the in-process cache. The
    # repo→department mapping lives in Postgres, so when the DB is down only the
    # owner scope can be honoured; non-admins see just their own runs.
    runs = sorted(_runs.values(), key=lambda r: r["created_at"], reverse=True)
    if run_type:
        runs = [r for r in runs if r["type"] == run_type]
    if not is_admin:
        runs = [r for r in runs if r.get("created_by") in owner_ids]
    return [_dict_to_light(r) for r in runs[:limit]]


def run_visible_to_user(
    run: dict,
    *,
    is_admin: bool = False,
    owner_ids: Optional[list] = None,
    department: Optional[str] = None,
) -> bool:
    """
    Authorization predicate for a single run — the IDOR guard behind every
    ``/sdlc/runs/{id}`` read and mutating endpoint. Mirrors the ``list_runs``
    scoping exactly: True if the caller is an admin, created the run
    (``created_by`` ∈ ``owner_ids``), or the run's repo is mapped to the
    caller's ``department`` via ``product_repos ⋈ dept_product_mappings``.

    Fails closed: on any DB error (or DB unavailable) the repo→department check
    returns False, so cross-user access is denied — the owner check has already
    run in-memory before we reach the DB.
    """
    if is_admin:
        return True
    owner_ids = [o for o in (owner_ids or []) if o]
    if run.get("created_by") and run.get("created_by") in owner_ids:
        return True
    repo = (run.get("repo") or "").strip()
    if not (repo and department):
        return False
    session = _get_session()
    if not session:
        return False
    try:
        from db.models import ProductRepo, DeptProductMapping
        row = (
            session.query(ProductRepo.id)
            .join(DeptProductMapping,
                  DeptProductMapping.product_id == ProductRepo.product_id)
            .filter(DeptProductMapping.department == department,
                    _norm_repo_sql(ProductRepo.repo_name) == _norm_repo(repo))
            .first()
        )
        return row is not None
    except Exception as e:
        logger.warning(f"sdlc_store: run_visible_to_user check failed → {e}")
        return False
    finally:
        session.close()


# ── GET EVENTS ────────────────────────────────────────────────

def get_run_events(run_id: str) -> list:
    """Return all state transition events for a run."""
    session = _get_session()
    if session:
        try:
            from db.models import SDLCRunEvent
            from sqlalchemy import asc
            events = (
                session.query(SDLCRunEvent)
                .filter(SDLCRunEvent.run_id == run_id)
                .order_by(asc(SDLCRunEvent.created_at))
                .all()
            )
            return [_event_to_dict(e) for e in events]
        except Exception as e:
            logger.warning(f"sdlc_store: get_events failed → {e}")
        finally:
            session.close()

    return _events.get(run_id, [])


# ── HELPERS ───────────────────────────────────────────────────

# ── CANCEL STALE RUNS ─────────────────────────────────────────

def cancel_stale_runs(max_age_hours: int = 4) -> int:
    """
    Auto-cancel runs stuck in a non-terminal state past a STATE-AWARE inactivity
    window. Returns the count of runs cancelled.

    A run parked at a human-approval / suspended gate must survive a multi-day
    review, so gate/suspended states get the long HITL window (governance = 7d);
    every other non-terminal (ACTIVE working) state keeps the short window so a
    genuinely-wedged worker segment is still reaped promptly. The per-state
    window comes from `core.config.sdlc_reaper_window_hours(state)`.

    `max_age_hours` is retained for backward compatibility but no longer applies
    a single blanket cutoff — the effective window is chosen per run by state.
    """
    from core.config import sdlc_reaper_window_hours
    # EXPIRED is terminal too — a HITL gate that already timed out must not be
    # re-cancelled by the reaper.
    _TERMINAL = {"COMPLETE", "MERGED", "FAILED", "CANCELLED", "EXPIRED"}
    cancelled = 0

    def _release_slot_safe(jira_key: str, run_id: str) -> None:
        """Free the Redis dedup slot for an auto-cancelled run (compare-and-delete
        by run_id so we never clear a re-triggered run's slot). Best-effort."""
        try:
            from core.job_queue import release_sdlc_slot
            release_sdlc_slot(jira_key or "", reporter=None, owner=run_id)
        except Exception:
            pass

    session = _get_session()
    if session:
        try:
            from db.models import SDLCRun
            # The reap window is state-dependent, so a single SQL cutoff can't
            # express it — fetch all non-terminal runs and decide per run.
            now = datetime.utcnow()
            candidates = (
                session.query(SDLCRun)
                .filter(SDLCRun.state.notin_(list(_TERMINAL)))
                .all()
            )
            _released = []
            _did_cancel = False
            for db_run in candidates:
                state = db_run.state or ""
                window_hours = sdlc_reaper_window_hours(state)
                updated = db_run.updated_at
                if updated is None:
                    continue
                age_hours = (now - updated).total_seconds() / 3600.0
                rid = str(db_run.id)
                if age_hours < window_hours:
                    continue
                reason = f"Auto-cancelled: stale for >{window_hours}h in state {state}"
                db_run.state = "CANCELLED"
                db_run.error = reason
                # update cache
                if rid in _runs:
                    _runs[rid]["state"] = "CANCELLED"
                    _runs[rid]["error"] = reason
                _released.append((db_run.jira_key or "", rid))
                cancelled += 1
                _did_cancel = True
                logger.info(
                    "sdlc_store: cancel_stale_runs cancelled run",
                    run_id=rid, state=state,
                    age_hours=round(age_hours, 2),
                    window_hours=window_hours, action="cancelled",
                )
            if _did_cancel:
                session.commit()
                for _jk, _rid in _released:
                    _release_slot_safe(_jk, _rid)
                    # Record cost accumulated up to cancellation point
                    try:
                        from services.sdlc_budget_tracker import finalize_run_budget as _fin
                        _fin(_rid)
                    except Exception as _fe:
                        logger.warning(
                            f"sdlc_store: finalize_run_budget failed for auto-cancelled run={_rid}: {_fe}"
                        )
        except Exception as e:
            logger.warning(f"sdlc_store: cancel_stale_runs failed → {e}")
            session.rollback()
        finally:
            session.close()
    else:
        # In-process fallback — identical state-aware window logic.
        now = datetime.utcnow()
        for rid, run in list(_runs.items()):
            state = run.get("state") or ""
            if state in _TERMINAL:
                continue
            try:
                updated = datetime.fromisoformat(run["updated_at"])
                window_hours = sdlc_reaper_window_hours(state)
                age_hours = (now - updated).total_seconds() / 3600.0
                if age_hours < window_hours:
                    logger.info(
                        "sdlc_store: cancel_stale_runs kept run (in-process)",
                        run_id=rid, state=state,
                        age_hours=round(age_hours, 2),
                        window_hours=window_hours, action="kept",
                    )
                    continue
                reason = f"Auto-cancelled: stale for >{window_hours}h in state {state}"
                run["state"] = "CANCELLED"
                run["error"] = reason
                cancelled += 1
                _release_slot_safe(run.get("jira_key", ""), rid)
                logger.info(
                    "sdlc_store: cancel_stale_runs cancelled run (in-process)",
                    run_id=rid, state=state,
                    age_hours=round(age_hours, 2),
                    window_hours=window_hours, action="cancelled",
                )
                try:
                    from services.sdlc_budget_tracker import finalize_run_budget as _fin
                    _fin(rid)
                except Exception as _fe:
                    logger.warning(
                        f"sdlc_store: finalize_run_budget failed for auto-cancelled run={rid}: {_fe}"
                    )
            except Exception:
                pass

    return cancelled


def _db_to_dict(db) -> dict:
    # JSONB should auto-deserialize, but guard against string-encoded JSON
    ctx = db.context or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}
    return {
        "id":            str(db.id),
        "type":          db.type,
        "jira_key":      db.jira_key or "",
        "jira_summary":  db.jira_summary or "",
        "repo":          db.repo or "",
        "branch":        db.branch or "",
        "pr_number":     db.pr_number,
        "pr_url":        db.pr_url or "",
        "confluence_url": db.confluence_url or "",
        "state":         db.state,
        "current_stage": db.current_stage,
        "context":       ctx,
        "error":         db.error,
        "triggered_by":       db.triggered_by or "",
        "suspended_at_stage": db.suspended_at_stage if db.suspended_at_stage else None,
        "created_by":         db.created_by if db.created_by else None,
        "created_at":         db.created_at.isoformat() if db.created_at else "",
        "updated_at":         db.updated_at.isoformat() if db.updated_at else "",
        # Governance evidence anchors — required by ensure_change_ticket()'s
        # primary idempotency guard so the linked Jira Change ticket is reused
        # across domain-decision and final-attestation calls (see RCA).
        "governance_evidence_jira_key": db.governance_evidence_jira_key or "",
        "governance_evidence_sha":      db.governance_evidence_sha or "",
        "governance_evidence_posted_at": (
            db.governance_evidence_posted_at.isoformat()
            if db.governance_evidence_posted_at else None
        ),
    }


# Fields the SDLC list view actually reads off `context`. Anything else
# (`repo_ctx`, `classification`, `triage`, `analysis`, `design`, `fix`,
# `rca`, `code_output`, `test_result`, `pr_review`) is only rendered in the
# expanded detail panel, which fetches via GET /sdlc/runs/{id}.
def _light_context(ctx: dict, state: str) -> dict:
    if not isinstance(ctx, dict):
        return {}
    out = {
        "jira_url":         ctx.get("jira_url", ""),
        "gitlab_issue_url": ctx.get("gitlab_issue_url", ""),
        "revision_count":   ctx.get("revision_count", 0),
    }
    if state == "MERGE_CONFLICT":
        out["resolution_proposal"] = ctx.get("resolution_proposal", "")
    return out


def _db_to_dict_light(db) -> dict:
    """List-view projection: identical to `_db_to_dict` but with a compact `context`."""
    ctx = db.context or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = {}
    return {
        "id":             str(db.id),
        "type":           db.type,
        "jira_key":       db.jira_key or "",
        "jira_summary":   db.jira_summary or "",
        "repo":           db.repo or "",
        "branch":         db.branch or "",
        "pr_number":      db.pr_number,
        "pr_url":         db.pr_url or "",
        "confluence_url": db.confluence_url or "",
        "state":          db.state,
        "current_stage":  db.current_stage,
        "context":        _light_context(ctx, db.state),
        "error":          db.error,
        "triggered_by":       db.triggered_by or "",
        "suspended_at_stage": db.suspended_at_stage if db.suspended_at_stage else None,
        "created_by":         db.created_by if db.created_by else None,
        "created_at":         db.created_at.isoformat() if db.created_at else "",
        "updated_at":         db.updated_at.isoformat() if db.updated_at else "",
    }


def _dict_to_light(run: dict) -> dict:
    """In-memory fallback equivalent of `_db_to_dict_light` for cached run dicts."""
    out = dict(run)
    out["context"] = _light_context(run.get("context") or {}, run.get("state", ""))
    return out


def _event_to_dict(db) -> dict:
    return {
        "id":         str(db.id),
        "run_id":     str(db.run_id),
        "from_state": db.from_state or "",
        "to_state":   db.to_state,
        "stage":      db.stage or "",
        "actor":      db.actor or "",
        "output":     db.output or "",
        "data":       db.data or {},
        "signature":  getattr(db, "signature", "") or "",
        "created_at": db.created_at.isoformat() if db.created_at else "",
    }
