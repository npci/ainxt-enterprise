# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BUDGET ROUTER — /budget
#
# Allocation model:
#   - Every user's cost allocation is base_cost_usd + extra_cost_usd.
#   - base_cost_usd: $50 for EVERYONE, always — including 10x winners. It is
#     never raised by any action, and the monthly reset returns it to $50.
#   - extra_cost_usd: the POOLED extra budget. Two things feed it:
#       (a) approved HOD budget-increase requests (My Budget → Request
#           Increase). This portion does NOT survive the monthly reset.
#       (b) the admin-only 10x-winner grant (POST /budget/admin/
#           winner-allocation/batch), which adds $1000 of extra budget and
#           records the winner-origin slice in winner_extra_usd. This portion
#           DOES carry over month to month, depleting only as the user spends
#           above their $50 base, until exhausted.
#     At the monthly reset the HOD portion (extra_cost_usd - winner_extra_usd)
#     is drained first, so a winner's balance survives intact until the HOD
#     money runs out. See services/budget_audit_service._snapshot_one_user.
#   - Admins and HODs have no direct "allocate/edit" action for arbitrary
#     users (POST/DELETE /budget/users are removed) — the ONLY ways a user's
#     budget changes are (a) the HOD approval flow below, and (b) the admin
#     10x-winner grant.
#   - No automatic daily reset; usage accumulates until the monthly reset.
# ============================================================

import json

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel, field_validator, model_validator

from core.logger import logger, mask_email
from core.pii_crypto import encrypt_pii, decrypt_pii
from auth.dependencies import get_current_user
from auth.rbac import (
    is_admin,
    is_hod,
    get_hod_departments,
    get_visible_user_filter,
)
from core.rate_limiter import enforce_rate_limit_with_behaviour, BUDGET_REQUEST, BUDGET_ADMIN
from core.security_validation import validate_budget_request
from services import hod_budget_governor as _hod_governor


# ── HOD audit helper ──────────────────────────────────────────────────────────
# Lightweight structured logging: every write action performed by an HOD is
# recorded so production incidents can be traced cleanly. We deliberately use
# logger.info rather than the heavier RequestAuditLog DB writes; per the spec,
# a single structured log line is acceptable for v1.

def _audit_hod_action(current_user: dict, target_user_id: str, action: str) -> None:
    """Emit a structured audit-log line if the actor is an HOD."""
    if not is_hod(current_user):
        return
    try:
        logger.info(
            "hod_action actor_email=%s target_user_id=%s action=%s hod_departments=%s",
            mask_email((current_user.get("email") or "").lower()),
            target_user_id,
            action,
            get_hod_departments(current_user),
        )
    except Exception:
        # Audit logging must never block the action.
        pass


_HOD_ACTION_RESET_USAGE     = "reset_usage"
_HOD_ACTION_APPROVE_REQUEST = "approve_request"
_HOD_ACTION_REJECT_REQUEST  = "reject_request"


def _get_org_tree_subtree(manager_email: str, max_rows: int = 1000) -> list:
    """
    Return the full recursive subtree for `manager_email` using org_tree.

    Replaces the hierarchy_table-based get_caller_and_subtree() call.
    Each entry dict has the same keys the rest of the router expects:
      user_id, mail, display_name, title, department,
      level (ad_level), relative_depth, manager_node_id (empty string).

    Only rows with a matching ainxt.users row are included (INNER JOIN),
    so null-mail / contractor-only org_tree rows are automatically excluded.
    Returns [] on any error (fail-closed).
    """
    if not manager_email:
        return []
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text
        db = SessionLocal()
        try:
            rows = db.execute(
                _text("""
                    WITH RECURSIVE subtree AS (
                        SELECT o.node_id, o.mail, o.display_name,
                               o.department, o.title, 1 AS depth
                        FROM   org_tree o
                        WHERE  o.manager = (
                                 SELECT node_id FROM org_tree
                                 WHERE  lower(mail) = lower(:email)
                                 LIMIT  1
                               )
                        UNION ALL
                        SELECT o.node_id, o.mail, o.display_name,
                               o.department, o.title, s.depth + 1
                        FROM   org_tree o
                        JOIN   subtree s ON o.manager = s.node_id
                    )
                    SELECT s.depth, s.mail, s.display_name,
                           s.department, s.title,
                           u.id        AS user_id,
                           u.ad_level,
                           u.is_active
                    FROM   subtree s
                    JOIN   users u ON lower(u.email) = lower(s.mail)
                    ORDER  BY s.depth, s.display_name
                    LIMIT  :lim
                """),
                {"email": manager_email.strip().lower(), "lim": max_rows},
            ).fetchall()
        finally:
            db.close()

        result = []
        for r in rows:
            m = dict(r._mapping)
            result.append({
                "user_id":        str(m["user_id"]) if m.get("user_id") else "",
                "mail":           (m.get("mail") or "").lower(),
                "display_name":   m.get("display_name") or "",
                "title":          m.get("title") or "",
                "department":     m.get("department") or "",
                "level":          int(m["ad_level"]) if m.get("ad_level") is not None else 0,
                "relative_depth": int(m.get("depth") or 1),
                "manager_node_id": "",   # not available from org_tree; kept for shape compat
            })
        return result
    except Exception as exc:
        logger.warning("_get_org_tree_subtree failed for %s: %s", mask_email(manager_email), exc)
        return []


def _is_reporting_manager_with_scope(
    current_user: dict,
    target_user_id: str,
    request: Request,
) -> bool:
    """
    Check whether a non-HOD, non-admin user is a reporting manager whose
    hierarchy subtree contains target_user_id.

    Memoises on request.state so the subtree query runs at most once per request.
    """
    if request is not None:
        cached = getattr(request.state, "rm_subtree_ids", None)
        if cached is not None:
            return str(target_user_id) in cached

    caller_email = (current_user.get("email") or "").strip().lower()
    if not caller_email:
        return False

    subtree_ids: set = set()
    for entry in _get_org_tree_subtree(caller_email):
        uid = entry.get("user_id")
        if uid:
            subtree_ids.add(str(uid))

    if request is not None:
        request.state.rm_subtree_ids = subtree_ids
    return str(target_user_id) in subtree_ids


def _scope_or_403(
    current_user: dict,
    target_user_id: str,
    request: Request,
) -> None:
    """
    Enforce scope on a single-target read/write action.

    Admin:              passes unconditionally.
    HOD:                passes if target_user_id is in their department-scoped set; else 403.
    Reporting manager:  passes if target_user_id is in their hierarchy subtree; else 403.
    Other:              403.

    Uses the per-request memoised scope cache, so calling this from multiple
    endpoints (or twice in the same handler) does not re-query the DB.
    """
    if is_admin(current_user):
        return
    allowed = get_visible_user_filter(current_user, request=request)
    if allowed is None:   # safety net — only None for admin, already returned
        return
    if str(target_user_id) in allowed:
        return
    # HOD scope missed — check reporting-manager subtree before rejecting.
    if _is_reporting_manager_with_scope(current_user, target_user_id, request):
        return
    raise HTTPException(status_code=403, detail="Out of scope")


# SQL expression that normalises the raw `source_channel` value into a stable,
# upper-cased segment key. Blank/NULL channels are bucketed as 'UNKNOWN' so the
# empty-channel rows still show up as a single labelled slice instead of being
# dropped or splitting off inconsistent casing (e.g. 'cli' vs 'CLI').
_CHANNEL_KEY_SQL = "UPPER(NULLIF(TRIM(source_channel), ''))"

# SQL expression that canonicalises the raw `model` value so the SAME underlying
# model aggregates into ONE slice regardless of how it was logged. The `model`
# column is written inconsistently across code paths:
#   'claude-sonnet-4-6'                         (raw id)
#   'Claude Sonnet (claude-sonnet-4-6)'         (friendly wrapper -> id in parens)
#   'GPT-5.2 (Coding) (gpt-5.2) [fallback]'     (friendly + trailing [tag])
#   'local:Kimi-k2.5'                           (local: prefix)
# We reduce each of these to the lower-cased canonical id so they group together.
# Precedence: text inside the LAST parenthesised group wins; else strip a
# leading 'local:'; else use the trimmed value as-is.
_MODEL_KEY_SQL = """
    LOWER(
      COALESCE(
        NULLIF(
          -- last (...) group, trailing [..] tags removed first
          (regexp_match(
             regexp_replace(TRIM(model), '\\s*\\[[^\\]]*\\]\\s*$', ''),
             '\\(([^()]*)\\)[^()]*$'
           ))[1],
          ''
        ),
        NULLIF(regexp_replace(TRIM(model), '^local:', ''), ''),
        NULLIF(TRIM(model), '')
      )
    )
"""


def _build_model_alias_map() -> dict[str, str]:
    """
    Build a lower-cased alias → canonical-model-id lookup sourced entirely
    from core.model_registry constants — the same source the gateway uses
    when it logs model names to model_usages.  No strings are hardcoded here;
    env-var overrides (OPENAI_TERA_MODEL, CLAUDE_PRIMARY_MODEL, etc.) are
    automatically reflected.

    The map is used Python-side after SQL normalisation to merge rows that
    still differ (bare shorthand aliases, date-suffix variants, inline-comment
    artefacts) into a single canonical slice.
    """
    try:
        from core.model_registry import (
            OPENAI_SIMPLE_MODEL, OPENAI_CODING_MODEL, OPENAI_LATEST_MODEL,
            OPENAI_TERA_MODEL, OPENAI_LUNA_MODEL,
            CLAUDE_PRIMARY_MODEL, CLAUDE_SONNET_5_MODEL,
            CLAUDE_HAIKU, CLAUDE_OPUS_MODEL,
            CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL,
        )
    except ImportError:
        return {}

    # Also pull the richer alias table from ABStudio if available.
    try:
        from ABStudio.backend.workflow_factory.pipeline import (
            _MODEL_ALIASES as _ws_aliases,
        )
        ws_aliases: dict[str, str] = {k.lower(): v for k, v in _ws_aliases.items()}
    except ImportError:
        ws_aliases = {}

    aliases: dict[str, str] = {
        # ── gateway hint ids → canonical model ids ───────────────────────
        # Source: gateway.py reference-models list 
        "mini":     OPENAI_SIMPLE_MODEL,   # gpt-5-mini
        "deep":     OPENAI_LATEST_MODEL,   # gpt-5.5
        "tera":     OPENAI_TERA_MODEL,     # gpt-5.6-terra
        "luna":     OPENAI_LUNA_MODEL,     # gpt-5.6-luna
        "gpt":      OPENAI_CODING_MODEL,   # gpt-5.4
        "haiku":    CLAUDE_HAIKU,           # claude-haiku-4-5
        "sonnet-5": CLAUDE_SONNET_5_MODEL, # claude-sonnet-5
        "sonnet 4.6": CLAUDE_PRIMARY_MODEL,# claude-sonnet-4-6
        "opus":     CLAUDE_OPUS_MODEL,     # claude-opus-4-7
        "opus-4-8": CLAUDE_OPUS_48_MODEL,  # claude-opus-4-8
        "opus-5":   CLAUDE_OPUS_5_MODEL,   # claude-opus-5
        # ── auto-select placeholders → single bucket ─────────────────────
        "auto_select": "auto",
        # ── date-suffix variants → base model id ─────────────────────────
        CLAUDE_HAIKU: CLAUDE_HAIKU,
        # Legacy date-suffixed ids that may still exist in stored rows, logs or
        # saved workspace configs. The Anthropic API does not accept a date
        # suffix on these models, so they are normalised to the base id rather
        # than left to fail at request time.
        "claude-haiku-4-5-20251001":       "claude-haiku-4-5",
        "claude-sonnet-4-5-20250929":      "claude-sonnet-4-5",
        # ── inline-comment artefact from "deepseek-v4-flash  # fast (~7s)" ─
        "~7s": "deepseek-v4-flash",
    }

    # Merge ABStudio alias table last so its entries can override if needed.
    aliases.update(ws_aliases)
    return aliases


# Built once at import time — cheap dict lookup at query time.
_MODEL_ALIAS_MAP: dict[str, str] = _build_model_alias_map()


def _normalise_model_breakdown(rows: list) -> list:
    """
    Apply _MODEL_ALIAS_MAP to a breakdown list and re-aggregate any rows that
    collapse to the same canonical key.
    Input rows must have: key, cost_usd, requests, tokens, unique_users.
    """
    merged: dict[str, dict] = {}
    for r in rows:
        canonical = _MODEL_ALIAS_MAP.get(r["key"], r["key"])
        if canonical in merged:
            m = merged[canonical]
            m["cost_usd"]     = round(m["cost_usd"]     + r["cost_usd"],     6)
            m["requests"]     += r["requests"]
            m["tokens"]       += r["tokens"]
            m["unique_users"] += r["unique_users"]
        else:
            merged[canonical] = {**r, "key": canonical}
    return sorted(merged.values(), key=lambda x: x["cost_usd"], reverse=True)


def _breakdown_for_users(db, user_ids: list, dimension: str) -> list:
    """
    Aggregate month-to-date cost from model_usages for the given user ids,
    grouped by either source_channel ('channel') or model ('model').

    The raw `source_channel` and `model` columns are logged inconsistently
    across code paths, so we canonicalise them in-SQL before grouping (see
    _CHANNEL_KEY_SQL / _MODEL_KEY_SQL) — otherwise a single channel/model
    splits into several bogus slices (or vanishes) in the pie chart.

    model_usages.user_id is TEXT and the table is range-partitioned by
    created_at, so we filter with user_id::text = ANY(:uids) AND
    created_at >= date_trunc('month', now()) for partition pruning.
    Returns [{key, cost_usd, requests, tokens}] ordered by cost desc.
    """
    from sqlalchemy import text as _text
    if not user_ids:
        return []
    key_expr = _CHANNEL_KEY_SQL if dimension == "channel" else _MODEL_KEY_SQL
    rows = db.execute(_text(f"""
        SELECT COALESCE({key_expr}, 'UNKNOWN') AS key,
               COALESCE(SUM(cost_usd), 0)      AS cost_usd,
               COUNT(*)                        AS requests,
               COALESCE(SUM(total_tokens), 0)  AS tokens
        FROM model_usages
        WHERE user_id::text = ANY(:uids)
          AND created_at >= date_trunc('month', now())
        GROUP BY 1
        ORDER BY cost_usd DESC
    """), {"uids": [str(u) for u in user_ids]}).fetchall()
    out = []
    for r in rows:
        m = dict(r._mapping)
        out.append({
            "key":      m.get("key") or "UNKNOWN",
            "cost_usd": round(float(m.get("cost_usd") or 0.0), 6),
            "requests": int(m.get("requests") or 0),
            "tokens":   int(m.get("tokens") or 0),
        })
    return out


def _resolve_team_user_ids(current_user: dict) -> list:
    """
    Return the list of user_ids that make up the caller's team via org_tree
    recursive subtree (same rule for HODs and reporting managers alike —
    see GET /budget/team). Returns [] when the caller has no team.
    """
    caller_email = (current_user.get("email") or "").strip().lower()
    subtree = _get_org_tree_subtree(caller_email)
    return [e["user_id"] for e in subtree if e.get("user_id")]


def _load_request_group_meta(request_id: str) -> tuple:
    """
    Load (target_user_id, requested_extra_cost_usd, hod_emails, delegatee_emails,
          requester_email, is_resolved) for a request group from
    ainxt.hod_allocation_ledger.

    hod_emails:       from rows where delegated_to IS NULL (the HOD's own
                       slots — usually one entry).
    delegatee_emails: from rows where delegated_to IS NOT NULL (one per
                       nominated delegatee).
    requester_email:  the requester's own email — used to enforce the
                       self-approval guard at the router layer too.

    Returns ("", 0.0, [], [], "", False) on any error / not-found so callers
    fall through to existing not-found / scope checks.
    """
    from store.budget_store import get_request_group
    try:
        rows = get_request_group(request_id)
        if not rows:
            return "", 0.0, [], [], "", False
        target_user_id  = rows[0].get("user_id", "")
        requester_email = (rows[0].get("requester_email") or "").strip().lower()
        requested_extra = float(rows[0].get("requested_extra_cost_usd") or 0.0)
        hod_emails: list = []
        delegatee_emails: list = []
        for r in rows:
            deleg = (r.get("delegated_to") or "").strip().lower()
            if deleg:
                if deleg not in delegatee_emails:
                    delegatee_emails.append(deleg)
            else:
                hod = (r.get("hod_email") or "").strip().lower()
                if hod and hod not in hod_emails:
                    hod_emails.append(hod)
        is_resolved = any(r.get("status") != "pending" for r in rows)
        return (target_user_id, requested_extra, hod_emails, delegatee_emails,
                requester_email, is_resolved)
    except Exception:
        return "", 0.0, [], [], "", False

# ── Business-rule constants ───────────────────────────────────────────────────
# Absolute ceilings enforced at the API layer (regardless of caller).
_MAX_TOKENS_PER_DAY:    int   = 50_000_000   # 50 M tokens/day hard ceiling
_MAX_REQUESTS_PER_DAY:  int   = 100_000      # 100 k requests/day hard ceiling
_MAX_COST_USD_PER_DAY:   float = 10_000.0    # $10,000/day hard ceiling
_MAX_COST_USD_PER_MONTH: float = 100_000.0   # $100,000/month hard ceiling
_MAX_REQUEST_EXTRA_USD:  float = 200.0        # Max extra a user can request in one request

router = APIRouter(tags=["budget"])


# ── Roster response cache ────────────────────────────────────────────────────
_ROSTER_CACHE_TTL_SECONDS: int = 45
_ROSTER_CACHE_KEY_PREFIX:  str = "budget:users:roster:v1"

# Auto-seed defaults (kept here so the roster doesn't have to write budgets
# inline for every user without a Redis entry — that would be 1000+ Redis+PG
# writes on a cold cache and defeat the point of caching).
_ROSTER_DEFAULT_BASE_COST_USD: float = 50.0
_ROSTER_ROLE_LIMITS = {
    "admin":   {"tokens": 100_000_000, "requests": 100_000},
    "default": {"tokens": 100_000_000, "requests":   5_000},
}


def _roster_cache_key(current_user: dict) -> str:
    """Cache key scoped by actor: admin sees everyone, each HOD sees their
    own scope, so their cached rosters must not overlap.
    """
    actor = (
        current_user.get("email")
        or current_user.get("user_id")
        or current_user.get("id")
        or "anon"
    )
    return f"{_ROSTER_CACHE_KEY_PREFIX}:{str(actor).lower()}"


def _synthetic_default_budget(uid: str) -> dict:
    """Return a default budget shape without persisting to Redis/Postgres.

    The real seed happens the first time the user hits ``/budget/me`` (or any
    action that goes through ``check_budget``); doing it here would fire a
    Redis + Postgres write per row on cold cache — exactly what we're trying
    to avoid on the roster path.
    """
    return {
        "user_id":              uid,
        "max_tokens_per_day":   0,
        "max_requests_per_day": 0,
        "max_cost_usd_per_day": 0.0,
        "max_tokens_total":     _ROSTER_ROLE_LIMITS["default"]["tokens"],
        "max_requests_total":   _ROSTER_ROLE_LIMITS["default"]["requests"],
        "max_cost_usd_total":   _ROSTER_DEFAULT_BASE_COST_USD,
        "base_cost_usd":        _ROSTER_DEFAULT_BASE_COST_USD,
        "extra_cost_usd":       0.0,
        "winner_extra_usd":     0.0,
        "winner_origin_period": None,
        "model_limits":         {},
    }


@router.get("/budget/users")
def list_budget_users(
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """List every user visible to the caller with their budget + current usage.

    Scaling notes:
      * Removed the full-keyspace ``SCAN usage:*:total`` — the Redis
        ``budget:users:index`` set plus the DB user list already cover every
        real user, and the scan was O(entire keyspace).
      * ``history`` is no longer included on the list payload; the roster UI
        only needs base/extra/spent for each row. History is fetched lazily
        from ``GET /budget/users/{uid}/usage`` when an admin drills into a
        specific user.
      * Redis reads (budget hash, usage-total hash, usage-today hash) are
        fanned out via a bounded thread pool so ~N users → a handful of
        wall-clock round-trips instead of N × 3 sequential HGETALLs.
      * The final response is cached in Redis for a short TTL, keyed per
        actor (admin vs. each HOD), so repeated opens and concurrent admin
        sessions collapse to a single computation.
      * Auto-seed of default budgets is deferred; ``/budget/me`` still seeds
        on first personal load. Prevents a fan-out of Redis+PG writes on
        cold-cache roster loads for large orgs.
    """
    from store.budget_store import (
        list_budget_users, _get_redis, _today,
        DEFAULT_COST_LIMIT_USD, get_budget, get_usage_total, get_usage_today,
    )

    # ── 0. Serve from cache when hot ──────────────────────────────────────
    rc = _get_redis()
    cache_key = _roster_cache_key(current_user)
    if rc is not None:
        try:
            cached = rc.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.warning("budget.users cache read failed key=%s err=%s",
                           cache_key, e)

    try:
        # ── 1. Assemble the visible user set ─────────────────────────────
        # Sources:
        #   (a) budget:users:index — everyone with a persisted budget row
        #   (b) DB users table    — everyone registered, even without usage
        # We deliberately do NOT SCAN usage:*:total anymore (see docstring).
        try:
            known = set(list_budget_users())
        except Exception as e:
            logger.warning("budget.users list_budget_users failed: %s", e)
            known = set()

        _db_users: dict = {}
        try:
            from db.database import SessionLocal
            from db.models import User as _User
            _db = SessionLocal()
            try:
                for uid, email, role, name in _db.query(
                    _User.id, _User.email, _User.role, _User.name
                ).all():
                    _db_users[str(uid)] = {
                        "email": email or "",
                        "role":  role  or "user",
                        "name":  name  or "",
                    }
                    known.add(str(uid))
            finally:
                _db.close()
        except Exception as e:
            logger.warning("budget.users DB user fetch failed: %s", e)

        # HOD scoping (and "neither admin nor HOD" — empty set) — intersect
        # with `known`. Admin (visible is None) is unrestricted.
        try:
            visible = get_visible_user_filter(current_user, request=request)
        except Exception as e:
            logger.warning("budget.users visibility filter failed: %s", e)
            visible = set()  # fail closed
        if visible is not None:
            known = {uid for uid in known if uid in visible}

        ordered_uids = sorted(known)
        if not ordered_uids:
            payload = {"users": []}
            _write_roster_cache(rc, cache_key, payload)
            return payload

        # ── 2. Batch-fetch budget + usage concurrently ───────────────────
        # Each user needs a budget row + two usage totals. We go through
        # get_budget()/get_usage_total()/get_usage_today() (not raw
        # rc.hgetall()) so that a Redis miss/outage transparently falls back
        # to Postgres — the same safe pattern already used by /budget/me and
        # check_budget(). Reading Redis directly here (the old approach) made
        # a Redis blip look like "nobody has any usage" for the whole roster,
        # instead of correctly falling back to the durable PG source of
        # truth. A small thread pool keeps this parallelised so ~N users
        # still costs a handful of wall-clock round-trips, not N sequential
        # ones.
        import concurrent.futures as _cf

        budgets:      dict = {}
        usage_totals: dict = {}
        usage_todays: dict = {}

        def _fetch_triplet(uid: str):
            try:
                b = get_budget(uid)
            except Exception:
                b = None
            try:
                t = get_usage_total(uid)
            except Exception:
                t = None
            try:
                d = get_usage_today(uid)
            except Exception:
                d = None
            return uid, b, t, d

        max_workers = min(32, max(4, len(ordered_uids)))
        try:
            with _cf.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="budget_roster",
            ) as ex:
                for uid, b, t, d in ex.map(_fetch_triplet, ordered_uids):
                    budgets[uid], usage_totals[uid], usage_todays[uid] = b, t, d
        except Exception as e:
            logger.warning("budget.users batch fetch failed: %s", e)

        # ── 3. Shape the response ────────────────────────────────────────
        result = []
        for uid in ordered_uids:
            db_info = _db_users.get(uid, {})
            role    = db_info.get("role", "default")

            budget = budgets.get(uid) or _synthetic_default_budget(uid)
            budget = {**budget, "user_id": uid, "model_limits": {}}  # not needed on the roster

            usage_total = usage_totals.get(uid) or {
                "tokens_used": 0, "requests_made": 0, "cost_usd_spent": 0.0,
            }
            usage_today = usage_todays.get(uid) or {
                "tokens_used": 0, "requests_made": 0, "cost_usd_spent": 0.0,
            }

            max_cost  = budget.get("max_cost_usd_total", DEFAULT_COST_LIMIT_USD)
            remaining = round(max(0.0, max_cost - usage_total["cost_usd_spent"]), 6)

            result.append({
                **budget,
                "email":         encrypt_pii(db_info.get("email", "")),
                "name":          encrypt_pii(db_info.get("name", "")),
                "role":          role,
                "usage_total":   usage_total,
                "usage_today":   usage_today,
                "remaining_usd": remaining,
                # NOTE: `history` intentionally omitted here — fetched lazily
                # from /budget/users/{uid}/usage when a row is expanded.
            })

        # Sort: most spent first (preserves existing UI ordering).
        result.sort(
            key=lambda x: x["usage_total"].get("cost_usd_spent", 0),
            reverse=True,
        )

        payload = {"users": result}
        _write_roster_cache(rc, cache_key, payload)
        return payload

    except Exception as e:
        # Full try/except so a Redis blip / DB hiccup can't hang the endpoint.
        actor = current_user.get("email") or current_user.get("user_id") or "?"
        logger.exception(
            "budget.users failed actor=%s err=%s", actor, e,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to load user roster. Please retry in a few seconds.",
        )


def _write_roster_cache(rc, cache_key: str, payload: dict) -> None:
    """Best-effort cache write — never raises."""
    if rc is None:
        return
    try:
        rc.setex(cache_key, _ROSTER_CACHE_TTL_SECONDS, json.dumps(payload))
    except Exception as e:
        logger.warning("budget.users cache write failed key=%s err=%s",
                       cache_key, e)


def _invalidate_roster_cache() -> None:
    """Drop every cached roster response so the next admin/HOD load recomputes.

    Called after mutating actions (budget increases, winner allocation, etc.)
    so the UI never shows post-write staleness for more than one refresh.
    Best-effort — cache misses are cheap; a failure here must not break the
    write path.
    """
    try:
        from store.budget_store import _get_redis
        rc = _get_redis()
        if rc is None:
            return
        pattern = f"{_ROSTER_CACHE_KEY_PREFIX}:*"
        try:
            keys = list(rc.keys(pattern))
        except Exception:
            keys = []
        if keys:
            try:
                rc.delete(*keys)
            except Exception as e:
                logger.warning("budget.users cache invalidate failed: %s", e)
    except Exception as e:
        logger.warning("budget.users cache invalidate outer failed: %s", e)


@router.get("/budget/users/{user_id}")
def get_user_budget(
        user_id: str,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    _scope_or_403(current_user, user_id, request)
    from store.budget_store import get_budget, get_usage_total
    budget = get_budget(user_id)
    if not budget:
        raise HTTPException(status_code=404, detail=f"No budget for user '{user_id}'")
    usage = get_usage_total(user_id)
    max_cost  = budget.get("max_cost_usd_total", 50.0)
    remaining = round(max(0.0, max_cost - usage["cost_usd_spent"]), 6)
    return {**budget, "usage_total": usage, "remaining_usd": remaining}


@router.get("/budget/users/{user_id}/usage")
def get_user_usage(
        user_id: str,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    _scope_or_403(current_user, user_id, request)
    from store.budget_store import get_budget, get_usage_total, get_usage_history
    budget      = get_budget(user_id)
    usage_total = get_usage_total(user_id)
    # Month-to-date history, backfilled from Postgres for dates past the
    # Redis 8-day TTL (previously a rolling 30-day window served from Redis
    # only, which showed zeros for anything older than ~7 days).
    history     = get_usage_history(user_id, month_to_date=True)
    max_cost    = (budget or {}).get("max_cost_usd_total", 50.0)
    remaining   = round(max(0.0, max_cost - usage_total["cost_usd_spent"]), 6)
    return {
        "user_id":      user_id,
        "budget":       budget,
        "usage_total":  usage_total,
        "remaining_usd": remaining,
        "history":      history,
    }


@router.get("/budget/users/{user_id}/utilization")
def get_user_utilization(
        user_id: str,
        request: Request,
        dimension: str = "channel",
        current_user: dict = Depends(get_current_user),
):
    """
    Month-to-date cost breakdown for a single user, grouped by source_channel
    (dimension=channel) or model (dimension=model). Sourced from model_usages.
    Admin/HOD/reporting-manager scoped via _scope_or_403.
    """
    _scope_or_403(current_user, user_id, request)
    if dimension not in ("channel", "model"):
        raise HTTPException(status_code=400, detail="dimension must be 'channel' or 'model'")
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        breakdown = _breakdown_for_users(db, [user_id], dimension)
    finally:
        db.close()
    return {
        "user_id":        user_id,
        "dimension":      dimension,
        "breakdown":      breakdown,
        "total_cost_usd": round(sum(b["cost_usd"] for b in breakdown), 6),
    }


@router.post("/budget/users/{user_id}/reset-usage")
def reset_user_usage(
        user_id: str,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """
    Admin or HOD-in-scope action: zero out a user's cumulative usage so they
    start fresh against their existing allocation.  Does NOT change the budget
    limits. Use this when you want to top up without changing the dollar cap,
    or when a new allocation period begins.

    Authorisation: admin OR (HOD AND target ∈ HOD scope). Else 403.
    """
    if not is_admin(current_user):
        if not is_hod(current_user):
            raise HTTPException(status_code=403, detail="Admin access required")
        _scope_or_403(current_user, user_id, request)
    from store.budget_store import reset_usage
    result = reset_usage(user_id)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Reset failed"))
    # Notify the user
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            user_id=user_id,
            type="budget_reset",
            title="Budget allocation renewed",
            body="Your AI usage budget has been reset by your admin. Your allocation is available again.",
            priority="Medium",
        )
    except Exception:
        pass
    logger.info(f"budget_router: reset_usage user={user_id} by actor={current_user.get('sub', '')}")
    _audit_hod_action(current_user, user_id, _HOD_ACTION_RESET_USAGE)
    _invalidate_roster_cache()
    return result


@router.get("/budget/me")
def get_my_budget(
        x_user_id: Optional[str] = Header(default=None),
        current_user: dict = Depends(get_current_user),
):
    # SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    # This endpoint previously trusted the caller-supplied X-User-Id header
    # as-is with no verification, so any caller could read ANY user's budget,
    # spend history, and assigned-HOD/delegate contact info just by sending
    # a different id in the header.
    # Fix: added `current_user: dict = Depends(get_current_user)` as a
    # function parameter (enforces a valid JWT) and, on the next line,
    # overwrite the `x_user_id` variable with the id taken from the
    # verified token (current_user["sub"]) BEFORE it is used anywhere
    # below — the incoming X-User-Id header value is discarded and never
    # reaches the lookup. This makes the endpoint self-scoped in fact, not
    # just in name, while every line after this one is unchanged.
    x_user_id = current_user.get("sub") or current_user.get("user_id") or ""
    if not x_user_id:
        raise HTTPException(status_code=401, detail="unauthenticated")
    from store.budget_store import (
        get_budget, get_usage_total, get_usage_today, get_usage_history, set_budget,
        DEFAULT_COST_LIMIT_USD, DEFAULT_TOKEN_LIMIT, DEFAULT_REQUEST_LIMIT,
    )
    budget = get_budget(x_user_id)

    # Auto-seed budget if none exists. Every user's default base allocation is
    # $50 — no band/role differentiation for the cost dimension. Token/request totals still vary by role
    # since those aren't part of the base/extra cost tracking.
    if budget is None:
        try:
            from db.database import SessionLocal
            from db.models import User as _User
            _db = SessionLocal()
            try:
                u = _db.query(_User).filter(_User.id == x_user_id).first()
                role     = (u.role or "default") if u else "default"
                ad_level = (u.ad_level if u and u.ad_level is not None else 6)
            finally:
                _db.close()
        except Exception:
            role, ad_level = "default", 6

        if role == "admin":
            tokens_total, reqs_total = 10_000_000, 100_000
            cost_total = _get_band_allocation(0)  # band 0 = highest allocation for admins
        else:
            from core.config import APPROVAL_AD_LEVEL as _APPROVAL_LEVEL
            cost_total   = _get_band_allocation(ad_level)
            tokens_total = 1_000_000 if ad_level <= _APPROVAL_LEVEL else 500_000
            reqs_total   = 5_000 if ad_level <= _APPROVAL_LEVEL else 1_000

        # Safe for winners: this branch only runs when get_budget() returned
        # None, i.e. there is no row at all — so the explicit extra=0.0 can
        # never zero an existing winner balance. set_budget additionally
        # preserves winner_extra_usd/winner_origin_period when omitted.
        budget = set_budget(x_user_id,
                            max_tokens_total=tokens_total,
                            max_requests_total=reqs_total,
                            max_cost_usd_total=cost_total,
                            base_cost_usd=cost_total,
                            extra_cost_usd=0.0)
        budget["user_id"] = x_user_id
        budget.setdefault("winner_extra_usd", 0.0)
        budget.setdefault("winner_origin_period", None)

    usage_total = get_usage_total(x_user_id)
    usage_today = get_usage_today(x_user_id)
    # Month-to-date history (1st of current month → today), backfilled from
    # Postgres for dates past the Redis 8-day TTL.
    history = get_usage_history(x_user_id, month_to_date=True)

    max_cost  = budget.get("max_cost_usd_total", DEFAULT_COST_LIMIT_USD)
    remaining = round(max(0.0, max_cost - usage_total["cost_usd_spent"]), 6)
    pct_used  = round(usage_total["cost_usd_spent"] / max_cost * 100, 1) if max_cost > 0 else 0.0

    # Resolve the user's assigned HOD (name + email) automatically from their
    # department, joined against the DBA/seed-managed department_hod_mapping
    # table (same source of truth as store.budget_store.resolve_hod_for_request
    # and routers/governance_router.py) -- so the budget-increase modal can
    # show who will approve the request. users.hod_email is NOT consulted: it
    # has no automatic write path anywhere in the codebase.
    hod_email_out = None
    hod_name_out  = None
    my_email = ""
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text
        _db = SessionLocal()
        try:
            row = _db.execute(
                _text(
                    'SELECT dhm."hod_email", h.name, u.email '
                    "FROM users u "
                    'LEFT JOIN department_hod_mapping dhm '
                    '    ON dhm."department_name" = u.department '
                    "LEFT JOIN users h ON lower(h.email) = lower(dhm.\"hod_email\") "
                    "WHERE u.id = :uid"
                ),
                {"uid": x_user_id},
            ).fetchone()
            if row:
                my_email = (row[2] or "").lower()
                if row[0]:
                    hod_email_out = row[0].lower()
                    hod_name_out  = row[1] or None
        finally:
            _db.close()
    except Exception:
        pass

    # Delegatees the HOD has nominated for approval — shown alongside the
    # HOD in the "Request Increase" modal so the requester knows who else
    # (besides their HOD) may approve/reject their request. Uses the SAME
    # resolve_approvers_for_request() the actual submission endpoint calls,
    # so this preview never shows a set of approvers different from who
    # will really be routed — in particular, it correctly drops the caller's
    # own email from the list if they happen to be one of the HOD's
    # delegatees (a delegatee can never approve their own request).
    delegatee_emails_out: list = []
    try:
        from store.budget_store import resolve_approvers_for_request
        if my_email:
            delegatee_emails_out = resolve_approvers_for_request(my_email).get("delegatees") or []
    except Exception:
        pass

    return {
        "user_id":          x_user_id,
        "budget":           budget,
        "usage_total":      usage_total,
        "usage_today":      usage_today,
        "remaining_usd":    remaining,
        "pct_used":         pct_used,
        "history":          history,
        "hod_email":        encrypt_pii(hod_email_out),
        "hod_name":         encrypt_pii(hod_name_out),
        "delegatee_emails": [encrypt_pii(e) for e in delegatee_emails_out],
    }


@router.get("/budget/me/utilization")
def get_my_utilization(
        x_user_id: Optional[str] = Header(default=None),
        dimension: str = "channel",
        current_user: dict = Depends(get_current_user),
):
    """
    Month-to-date cost breakdown for the calling user, grouped by
    source_channel (dimension=channel) or model (dimension=model).
    Sourced from model_usages. Self-scoped via the verified caller identity.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    previously "self-scoped" only by trusting the X-User-Id header verbatim,
    so any caller could read another user's per-channel/per-model spend by
    supplying a different id.
    Fix: added `current_user: dict = Depends(get_current_user)` as a
    function parameter (enforces a valid JWT) and reassigned `x_user_id`
    below from `current_user["sub"]` before it is used, discarding the
    caller-supplied header value. Rest of the function is unchanged.
    """
    x_user_id = current_user.get("sub") or current_user.get("user_id") or ""
    if not x_user_id:
        raise HTTPException(status_code=401, detail="unauthenticated")
    if dimension not in ("channel", "model"):
        raise HTTPException(status_code=400, detail="dimension must be 'channel' or 'model'")
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        breakdown = _breakdown_for_users(db, [x_user_id], dimension)
    finally:
        db.close()
    return {
        "user_id":        x_user_id,
        "dimension":      dimension,
        "breakdown":      breakdown,
        "total_cost_usd": round(sum(b["cost_usd"] for b in breakdown), 6),
    }


# ── Reporting-manager team view (read-only) ──────────────────────────────────

@router.get("/budget/team")
def get_team_budgets(
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """
    Read-only team budget view.

    Uses hierarchy_table (root_manager_email) to fetch the caller's full
    reporting subtree in one query, then batch-fetches budget + usage data
    via Redis pipeline. Applies identically whether the caller is an HOD or
    a reporting manager — both are resolved as "everyone whose
    root_manager_email is the caller's own email" in hierarchy_table, not
    by raw department-string matching.
    """
    import time as _time
    from store.budget_store import _get_redis, DEFAULT_COST_LIMIT_USD

    # ── Point 7: rate limit ──
    enforce_rate_limit_with_behaviour(request, BUDGET_ADMIN)

    _t0 = _time.monotonic()
    caller_email = (current_user.get("email") or "").strip().lower()

    subtree = _get_org_tree_subtree(caller_email)

    if not subtree:
        return {
            "is_team_viewer": False,
            "reports":        [],
            "total_count":    0,
            "with_budget_count": 0,
        }

    user_ids = [e["user_id"] for e in subtree if e.get("user_id")]

    # ── Batch Redis pipeline: fetch all budgets + usages in 2 round-trips ──
    budget_map: dict = {}
    usage_map: dict  = {}

    rc = _get_redis()
    if rc and user_ids:
        try:
            pipe = rc.pipeline(transaction=False)
            for uid in user_ids:
                pipe.hgetall(f"budget:{uid}")
            budget_results = pipe.execute()

            pipe = rc.pipeline(transaction=False)
            for uid in user_ids:
                pipe.hgetall(f"usage:{uid}:total")
            usage_results = pipe.execute()

            for i, uid in enumerate(user_ids):
                bdata = budget_results[i] if i < len(budget_results) else {}
                if bdata:
                    _cost_total = float(bdata.get("max_cost_usd_total") or bdata.get("max_cost_usd_per_day", 30.0))
                    _base  = bdata.get("base_cost_usd")
                    _extra = bdata.get("extra_cost_usd")
                    budget_map[uid] = {
                        "max_tokens_total":   int(bdata.get("max_tokens_total") or bdata.get("max_tokens_per_day", 10_000_000)),
                        "max_requests_total": int(bdata.get("max_requests_total") or bdata.get("max_requests_per_day", 5_000)),
                        "max_cost_usd_total": _cost_total,
                        "base_cost_usd":      float(_base)  if _base  is not None else _cost_total,
                        "extra_cost_usd":     float(_extra) if _extra is not None else 0.0,
                    }

                udata = usage_results[i] if i < len(usage_results) else {}
                usage_map[uid] = {
                    "tokens_used":    int(udata.get("tokens_used", 0)) if udata else 0,
                    "requests_made":  int(udata.get("requests_made", 0)) if udata else 0,
                    "cost_usd_spent": round(float(udata.get("cost_usd_spent", 0.0)), 6) if udata else 0.0,
                }
        except Exception as exc:
            logger.warning("budget_router /budget/team Redis pipeline error: %s", exc)

    # ── Point 6: batched PG fallback with single connection ──
    missing_budget = [uid for uid in user_ids if uid not in budget_map]
    missing_usage  = [uid for uid in user_ids if uid not in usage_map]
    if missing_budget or missing_usage:
        try:
            import psycopg2
            from core.config import postgres_dsn
            _conn = psycopg2.connect(postgres_dsn(), connect_timeout=5)
            _cur = _conn.cursor()
            try:
                for uid in missing_budget:
                    _cur.execute("""
                        SELECT max_cost_usd_total, max_tokens_total, max_requests_total, monthly_limit_usd,
                               base_cost_usd, extra_cost_usd
                        FROM budget_configs WHERE user_id = %s LIMIT 1
                    """, (uid,))
                    row = _cur.fetchone()
                    if row:
                        _cost_total = float(row[0]) if row[0] is not None else float(row[3] or 50.0)
                        budget_map[uid] = {
                            "max_tokens_total":   int(row[1]) if row[1] is not None else 500_000,
                            "max_requests_total": int(row[2]) if row[2] is not None else 1_000,
                            "max_cost_usd_total": _cost_total,
                            "base_cost_usd":      float(row[4]) if row[4] is not None else _cost_total,
                            "extra_cost_usd":     float(row[5]) if row[5] is not None else 0.0,
                        }
                for uid in missing_usage:
                    _cur.execute("""
                        SELECT tokens_used, requests_made, cost_usd_spent
                        FROM user_usage_totals WHERE user_id = %s
                    """, (uid,))
                    row = _cur.fetchone()
                    usage_map[uid] = {
                        "tokens_used":    int(row[0]) if row else 0,
                        "requests_made":  int(row[1]) if row else 0,
                        "cost_usd_spent": round(float(row[2]), 6) if row else 0.0,
                    }
            finally:
                _cur.close()
                _conn.close()
        except Exception as exc:
            logger.warning("budget_router /budget/team PG fallback error: %s", exc)

    # Build response
    reports = []
    with_budget_count = 0

    for entry in subtree:
        user_id = entry["user_id"]
        if not user_id:
            continue
        budget = budget_map.get(user_id)
        usage  = usage_map.get(user_id, {"tokens_used": 0, "requests_made": 0, "cost_usd_spent": 0.0})

        rec = {
            "email":            encrypt_pii(entry["mail"]),
            "display_name":     encrypt_pii(entry["display_name"]),
            "title":            entry["title"],
            "department":       entry["department"],
            "manager_node_id":  entry["manager_node_id"],
            "level":            entry["level"],
            "relative_depth":   entry["relative_depth"],
            "user_id":          user_id,
            "has_budget":       bool(budget),
            "max_tokens_total":     budget.get("max_tokens_total", 0) if budget else 0,
            "max_requests_total":   budget.get("max_requests_total", 0) if budget else 0,
            # For users with no seeded budget row, fall back to the flat $50 base
            # default that /budget/me and /usage auto-seed, so the roster rows and
            # the team-totals aggregate agree with each user's Details drill-down.
            "max_cost_usd_total":   budget.get("max_cost_usd_total", 0.0) if budget else _ROSTER_DEFAULT_BASE_COST_USD,
            "base_cost_usd":        budget.get("base_cost_usd", 0.0) if budget else _ROSTER_DEFAULT_BASE_COST_USD,
            "extra_cost_usd":       budget.get("extra_cost_usd", 0.0) if budget else 0.0,
            "usage_total":      usage,
            "remaining_usd":    round(max(0.0, (budget.get("max_cost_usd_total", DEFAULT_COST_LIMIT_USD) if budget else _ROSTER_DEFAULT_BASE_COST_USD) - usage["cost_usd_spent"]), 6),
        }
        if budget:
            with_budget_count += 1
        reports.append(rec)

    _elapsed = round((_time.monotonic() - _t0) * 1000, 1)
    logger.info(
        "team_view_read actor_email=%s subtree_size=%d latency_ms=%s",
        caller_email, len(reports), _elapsed,
    )

    return {
        "is_team_viewer":    True,
        "caller": {
            "display_name":  encrypt_pii(current_user.get("name") or caller_email),
            "email":         encrypt_pii(caller_email),
        },
        "reports":           reports,
        "total_count":       len(reports),
        "with_budget_count": with_budget_count,
        # ── Point 9: truncation warning for UI ──
        "truncated":         len(subtree) >= 1000,
    }


@router.get("/budget/team/utilization")
def get_team_utilization(
        request: Request,
        dimension: str = "channel",
        current_user: dict = Depends(get_current_user),
):
    """
    Month-to-date cost breakdown for the caller's entire team, grouped by
    source_channel (dimension=channel) or model (dimension=model). Sourced
    from model_usages. Team membership mirrors GET /budget/team (HOD =
    department scope, reporting manager = hierarchy subtree).
    """
    if dimension not in ("channel", "model"):
        raise HTTPException(status_code=400, detail="dimension must be 'channel' or 'model'")
    user_ids = _resolve_team_user_ids(current_user)
    if not user_ids:
        return {"dimension": dimension, "breakdown": [], "total_cost_usd": 0.0, "team_size": 0}
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        breakdown = _breakdown_for_users(db, user_ids, dimension)
    finally:
        db.close()
    return {
        "dimension":      dimension,
        "breakdown":      breakdown,
        "total_cost_usd": round(sum(b["cost_usd"] for b in breakdown), 6),
        "team_size":      len(user_ids),
    }


@router.get("/budget/hod/cap-status")
def get_hod_cap_status(current_user: dict = Depends(get_current_user)):
    """
    Return the calling HOD's monthly allocation cap status.

    Response shape:
      * is_hod=false  → minimal payload (UI hides the banner)
      * is_hod=true   → cap_usd, consumed_usd, remaining_usd, resets_on, period

    A user who is both admin and HOD gets is_hod=true here so the UI can
    show the HOD cap banner alongside the admin view.
    """
    if not is_hod(current_user):
        return {"is_hod": False}

    hod_email = current_user.get("email", "") or ""
    status = _hod_governor.get_cap_status(hod_email)
    out = status.to_dict()

    # get_cap_status() only covers approved-allocation consumption
    # (consumed_after_usd) — managed-endpoint cloud spend is tracked
    # separately in endpoint_spend_usd (see services/endpoint_budget_governor.py)
    # and must be folded in here too, or a HOD's own banner understates how
    # much of their cap is actually used. Fails soft: an endpoint-spend lookup
    # error must not break this banner.
    try:
        from services.endpoint_budget_governor import get_endpoint_spend
        endpoint_spend = float(get_endpoint_spend(hod_email, out["period_yyyymm"]))
    except Exception:
        endpoint_spend = 0.0
    if endpoint_spend:
        out["consumed_usd"]  = out["consumed_usd"] + endpoint_spend
        out["remaining_usd"] = max(0.0, out["cap_usd"] - out["consumed_usd"])

    return {
        "is_hod":    True,
        "hod_email": encrypt_pii(hod_email.lower()),
        **out,
    }


# ── Budget approval delegation ──────────────────────────────────────────────
# An HOD may nominate one or more of their direct reports (per org_tree) as
# approvers for budget-increase requests routed to them. See
# store.budget_store.set_hod_delegates / resolve_delegates_for_hod for the
# storage model (comma-separated email list on department_hod_mapping.
# delegated_to, written identically to every row for this HOD — delegation
# is per-HOD, not per-department).

class SetDelegatesBody(BaseModel):
    delegatee_emails: List[str]

    @field_validator("delegatee_emails")
    @classmethod
    def _validate_emails(cls, v: List[str]) -> List[str]:
        if v is None:
            return []
        if len(v) > 20:
            raise ValueError("Cannot nominate more than 20 delegatees.")
        return v


@router.get("/budget/hod/direct-reports")
def get_hod_direct_reports(current_user: dict = Depends(get_current_user)):
    """
    HOD-only: list the caller's direct reports (resolved to email + name via
    org_tree) so the Team → Delegation UI can populate its multi-select.
    """
    if not is_hod(current_user):
        raise HTTPException(status_code=403, detail="Only HODs may use this endpoint.")

    from store.budget_store import resolve_direct_report_emails
    hod_email = (current_user.get("email") or "").strip().lower()
    reports = resolve_direct_report_emails(hod_email)
    reports = [
        {**r, "email": encrypt_pii(r.get("email")), "name": encrypt_pii(r.get("name"))}
        for r in reports
    ]
    return {"direct_reports": reports}


@router.get("/budget/hod/delegates")
def get_hod_delegates(current_user: dict = Depends(get_current_user)):
    """HOD-only: return the caller's currently-nominated delegatee emails."""
    if not is_hod(current_user):
        raise HTTPException(status_code=403, detail="Only HODs may use this endpoint.")

    from store.budget_store import resolve_delegates_for_hod
    hod_email = (current_user.get("email") or "").strip().lower()
    return {"delegatee_emails": [encrypt_pii(e) for e in resolve_delegates_for_hod(hod_email)]}


@router.put("/budget/hod/delegates")
def put_hod_delegates(
    body: SetDelegatesBody,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    HOD-only: overwrite the caller's delegatee list. Delegatees must be one
    of the HOD's own direct reports (per org_tree) — this is enforced here
    rather than in the store so the 422 error can name the specific invalid
    email(s), which the store's generic self-reference guard doesn't do.

    Delegation is per-HOD: the same list is written to every
    department_hod_mapping row this HOD owns, regardless of department.
    """
    if not is_hod(current_user):
        raise HTTPException(status_code=403, detail="Only HODs may use this endpoint.")

    enforce_rate_limit_with_behaviour(request, BUDGET_ADMIN)

    from store.budget_store import resolve_direct_report_emails, set_hod_delegates
    hod_email = (current_user.get("email") or "").strip().lower()

    valid_emails = {r["email"] for r in resolve_direct_report_emails(hod_email)}
    # body.delegatee_emails may echo back the encrypted values previously
    # returned by GET /budget/hod/direct-reports or /budget/hod/delegates —
    # decrypt before use (no-op if PII_PAYLOAD_ENCRYPTION_ENABLED is off).
    requested = [(decrypt_pii(e) or "").strip().lower() for e in body.delegatee_emails]
    invalid = [e for e in requested if e and e not in valid_emails]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=(
                "The following are not your direct reports and cannot be "
                f"nominated as delegatees: {', '.join(invalid)}"
            ),
        )

    try:
        result = set_hod_delegates(hod_email, requested)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info(
        "budget_router: HOD %s set delegatees=%s (rows_updated=%d)",
        hod_email, result["delegatees"], result["rows_updated"],
    )
    result["delegatees"] = [encrypt_pii(e) for e in result["delegatees"]]
    return {"success": True, **result}


@router.get("/budget/delegate/cap-status")
def get_delegate_cap_status(current_user: dict = Depends(get_current_user)):
    """
    Delegatee-only surface: return the monthly cap status of every HOD who
    has nominated the caller as a budget-approval delegatee. Does NOT
    require is_hod — a plain user who has been delegated to sees this even
    though they head no department themselves.

    Response: { "delegating_hods": [ { hod_email, cap_usd, consumed_usd,
                                        remaining_usd, period_yyyymm,
                                        resets_on, enforcement }, ... ] }
    Empty list if the caller is not a delegatee for anyone.
    """
    from store.budget_store import resolve_delegating_hods_for
    caller_email = (current_user.get("email") or "").strip().lower()
    delegating_hods = resolve_delegating_hods_for(caller_email)
    if not delegating_hods:
        return {"delegating_hods": []}

    out = []
    for hod_email in delegating_hods:
        status = _hod_governor.get_cap_status(hod_email)
        entry = status.to_dict()
        try:
            from services.endpoint_budget_governor import get_endpoint_spend
            endpoint_spend = float(get_endpoint_spend(hod_email, entry["period_yyyymm"]))
        except Exception:
            endpoint_spend = 0.0
        if endpoint_spend:
            entry["consumed_usd"]  = entry["consumed_usd"] + endpoint_spend
            entry["remaining_usd"] = max(0.0, entry["cap_usd"] - entry["consumed_usd"])
        entry["hod_email"] = encrypt_pii(hod_email)
        out.append(entry)

    return {"delegating_hods": out}


# ── Budget increase requests ───────────────────────────────────
#
# Flow :
#   1. User submits from My Budget → requested EXTRA amount (added on top of
#      base once approved) + mandatory justification.
#   2. Router resolves the requester's department → candidate HODs from
#      ainxt.department_hod_mapping, then cross-checks those candidates
#      against ainxt.hierarchy_table for the requester's own management
#      chain (root_manager_email), picking the closest ancestor (minimum
#      `level`) as the single approving HOD. If no candidate HOD is found
#      in the requester's chain, the request is blocked (no silent
#      admin fallback, no routing to unrelated department HODs).
#   3. store.budget_store.request_budget_increase() inserts one 'pending'
#      row for the resolved HOD into ainxt.hod_allocation_ledger.
#   4. The resolved HOD gets an inbox item + an Outlook-compatible email.
#   5. That HOD (or admin/senior-approver) acts:
#        approve → row 'approved', extra_cost_usd incremented, requester
#                  notified.
#        reject  → row 'rejected', no budget change, requester notified.
#      A concurrent second action on an already-resolved request fails
#      gracefully with "already approved/rejected by X".

class BudgetIncreaseRequest(BaseModel):
    user_id:                    str
    requested_extra_cost_usd:   float
    justification:              str   # MANDATORY — shown to every HOD it's routed to

    @field_validator("requested_extra_cost_usd")
    @classmethod
    def validate_req_extra_cost(cls, v: float) -> float:
        import math as _math
        if not isinstance(v, (int, float)) or not _math.isfinite(float(v)):
            raise ValueError("requested_extra_cost_usd must be a finite number")
        if v <= 0:
            raise ValueError("requested_extra_cost_usd must be > 0")
        if v > _MAX_REQUEST_EXTRA_USD:
            raise ValueError(f"requested_extra_cost_usd cannot exceed ${_MAX_REQUEST_EXTRA_USD:,.2f} per request")
        return v

    @field_validator("justification")
    @classmethod
    def validate_justification(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("justification is required")
        if len(v) > 1000:
            raise ValueError("justification must be ≤ 1000 characters")
        return v


def _lookup_requester(user_id: str) -> dict:
    """Fetch email/name/department for the requester — used for HOD routing,
    ledger snapshot, and email/inbox content."""
    from db.database import SessionLocal
    from db.models import User as _User
    db = SessionLocal()
    try:
        u = db.query(_User).filter(_User.id == user_id).first()
        if not u:
            return {"email": "", "name": "", "department": ""}
        return {"email": u.email or "", "name": u.name or "", "department": u.department or ""}
    finally:
        db.close()


def _hod_approval_email_html(requester_name: str, requester_email: str, department: str,
                              requested_extra: float, justification: str,
                              current_base: float, current_extra: float,
                              delegating_hod_email: str = "") -> tuple:
    """Build (html_body, text_body) for the approver notification email.

    When `delegating_hod_email` is provided, the email is being sent to a
    delegatee (not the HOD themselves) — the copy is adjusted to make it
    explicit that this HOD has delegated budget approval to the recipient.
    """
    import html as _html
    resulting_total = current_base + current_extra + requested_extra
    safe_name  = _html.escape(requester_name or requester_email or "A user")
    safe_email = _html.escape(requester_email or "")
    safe_dept  = _html.escape(department or "—")
    safe_just  = _html.escape(justification or "")
    delegating_hod_email_lc = (delegating_hod_email or "").strip().lower()
    is_delegated = bool(delegating_hod_email_lc)
    safe_hod = _html.escape(delegating_hod_email_lc)

    delegation_html = (
        f'<p style="background:#fff8e1;border:1px solid #f0c36d;padding:8px 12px;'
        f'border-radius:6px;font-size:13px;color:#7a5a00;">'
        f'<b>{safe_hod}</b> has delegated budget approval to you. You may approve '
        f'or reject this request on their behalf; if you approve, the amount is '
        f'charged against <b>{safe_hod}</b>\'s monthly cap, not yours.'
        f'</p>' if is_delegated else ""
    )
    delegation_text = (
        f"{delegating_hod_email_lc} has delegated budget approval to you. If you approve,\n"
        f"the amount is charged against {delegating_hod_email_lc}'s monthly cap, not yours.\n\n"
        if is_delegated else ""
    )

    html_body = f"""
    <html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;">
      <h2 style="color:#1a4fa0;">Budget increase request awaiting your approval</h2>
      {delegation_html}
      <p><b>{safe_name}</b> ({safe_email}) from <b>{safe_dept}</b> has requested an increase to their AiNxt budget.</p>
      <table cellpadding="6" style="border-collapse:collapse;">
        <tr><td><b>Current base allocation</b></td><td>${current_base:.2f}</td></tr>
        <tr><td><b>Current extra granted</b></td><td>${current_extra:.2f}</td></tr>
        <tr><td><b>Requested extra</b></td><td>${requested_extra:.2f}</td></tr>
        <tr><td><b>Resulting total if approved</b></td><td><b>${resulting_total:.2f}</b></td></tr>
        <tr><td><b>Justification</b></td><td>{safe_just}</td></tr>
      </table>
      <p style="margin-top:12px;color:#555;font-size:13px;">
        Note: this increase will be added on top of the user's base budget — approving does NOT
        replace their existing allocation, it only adds the requested extra amount.
      </p>
      <p><b>To approve or reject:</b></p>
      <ol>
        <li>Open AiNxt → Inbox, or Budget Manager → {"My Budget → Pending Requests" if is_delegated else "Team → Pending Requests"}</li>
        <li>Review the request details and justification above</li>
        <li>Click Approve or Reject</li>
      </ol>
      <p style="color:#888;font-size:12px;">— AiNxt Platform</p>
    </body></html>
    """
    text_body = (
        f"Budget increase request from {requester_name or requester_email} ({safe_email}), {department or '—'}.\n\n"
        f"{delegation_text}"
        f"  Current base allocation : ${current_base:.2f}\n"
        f"  Current extra granted   : ${current_extra:.2f}\n"
        f"  Requested extra         : ${requested_extra:.2f}\n"
        f"  Resulting total         : ${resulting_total:.2f}\n"
        f"  Justification           : {justification}\n\n"
        "This increase is added on top of the user's base budget — it does not replace it.\n"
        "Approve or reject from AiNxt Inbox or Budget Manager.\n\n"
        "— AiNxt Platform\n"
    )
    return html_body, text_body


@router.post("/budget/hod/self-increase")
def hod_self_increase(
    body: BudgetIncreaseRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    HOD self-service budget increase — no approval flow.

    When an HOD requests a budget increase for their own account, the
    increase is applied immediately (no pending ledger row, no inbox
    notification to another HOD). The amount is charged directly against
    the HOD's own monthly allocation cap and recorded in
    hod_allocation_ledger so it appears in HOD cap utilisation.

    Constraints:
      * Caller must be an HOD (is_hod=True).
      * Caller may only increase their own budget (body.user_id == caller id).
      * Amount must be > 0 and ≤ $200 per request.
      * Amount must not exceed the HOD's remaining monthly cap.
    """
    if not is_hod(current_user):
        raise HTTPException(status_code=403, detail="Only HODs may use this endpoint.")

    caller_id = str(
        current_user.get("sub")
        or current_user.get("user_id")
        or current_user.get("id")
        or ""
    )
    if not caller_id or str(body.user_id) != caller_id:
        raise HTTPException(
            status_code=403,
            detail="You may only increase your own budget.",
        )

    is_valid, field_errors, sanitized = validate_budget_request(body)
    if not is_valid:
        error_messages = [
            f"{field}: {e}"
            for field, errors in field_errors.items()
            for e in errors
        ]
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    amount = float(sanitized.get("requested_extra_cost_usd") or body.requested_extra_cost_usd)
    if amount > _MAX_REQUEST_EXTRA_USD:
        raise HTTPException(
            status_code=422,
            detail=f"Amount cannot exceed ${_MAX_REQUEST_EXTRA_USD:,.2f} per request.",
        )

    hod_email = (current_user.get("email") or "").strip().lower()
    if not hod_email:
        raise HTTPException(status_code=422, detail="Could not resolve your HOD email.")

    # Check remaining cap before writing anything.
    cap_status = _hod_governor.get_cap_status(hod_email)
    if amount > float(cap_status.remaining_usd):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Amount ${amount:.2f} exceeds your remaining HOD cap "
                f"${float(cap_status.remaining_usd):.2f} for this period."
            ),
        )

    requester = _lookup_requester(caller_id)

    # Apply the increase directly to budget_configs and charge the HOD cap
    # atomically via reserve_and_record (inserts a ledger row with
    # action='approve_request' so it appears in HOD cap utilisation).
    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text

        db = SessionLocal()
        try:
            with db.begin():
                cfg = db.execute(
                    _text(
                        "SELECT base_cost_usd, extra_cost_usd, max_cost_usd_total "
                        "FROM budget_configs WHERE user_id = :uid FOR UPDATE"
                    ),
                    {"uid": caller_id},
                ).fetchone()

                if cfg is not None:
                    old_base  = float(cfg[0]) if cfg[0] is not None else 50.0
                    old_extra = float(cfg[1]) if cfg[1] is not None else 0.0
                else:
                    old_base, old_extra = 50.0, 0.0

                new_extra = old_extra + amount
                new_total = old_base + new_extra

                if cfg is not None:
                    db.execute(
                        _text(
                            "UPDATE budget_configs "
                            "SET extra_cost_usd = :ne, max_cost_usd_total = :nt, updated_at = NOW() "
                            "WHERE user_id = :uid"
                        ),
                        {"ne": new_extra, "nt": new_total, "uid": caller_id},
                    )
                else:
                    db.execute(
                        _text(
                            "INSERT INTO budget_configs "
                            "(id, user_id, base_cost_usd, extra_cost_usd, max_cost_usd_total, "
                            " model_allowlist, created_at, updated_at) "
                            "VALUES (gen_random_uuid(), :uid, :base, :ne, :nt, '[]'::jsonb, NOW(), NOW())"
                        ),
                        {"uid": caller_id, "base": old_base, "ne": new_extra, "nt": new_total},
                    )
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("hod_self_increase: budget_configs update failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to apply budget increase.")

    # Charge the HOD's own cap and write the ledger row.
    # reserve_and_record handles the SELECT FOR UPDATE on the cap row and
    # inserts an 'approve_request' ledger row so it shows in cap utilisation.
    try:
        import uuid as _uuid
        _hod_governor.reserve_and_record(
            hod_email=hod_email,
            target_user_id=caller_id,
            target_user_email=requester["email"],
            action=_hod_governor.ACTION_APPROVE_REQUEST,
            amount_usd=amount,
            previous_limit_usd=old_base + old_extra,
            new_limit_usd=new_total,
            request_id=str(_uuid.uuid4()),
            justification=f"[HOD self-increase] {(sanitized.get('justification') or body.justification or '').strip()}",
        )
    except HTTPException:
        # Cap enforcement blocked it (enforcement mode) — roll back the
        # budget_configs change we already committed. This is a rare race
        # (cap was fine at the check above but consumed by a concurrent
        # allocation before reserve_and_record locked the cap row).
        try:
            from db.database import SessionLocal
            from sqlalchemy import text as _text
            db = SessionLocal()
            try:
                with db.begin():
                    db.execute(
                        _text(
                            "UPDATE budget_configs "
                            "SET extra_cost_usd = :oe, max_cost_usd_total = :ot, updated_at = NOW() "
                            "WHERE user_id = :uid"
                        ),
                        {"oe": old_extra, "ot": old_base + old_extra, "uid": caller_id},
                    )
            finally:
                db.close()
        except Exception:
            pass
        raise
    except Exception as exc:
        logger.error("hod_self_increase: reserve_and_record failed: %s", exc)
        raise HTTPException(status_code=500, detail="Budget updated but cap ledger write failed.")

    logger.info(
        "hod_self_increase actor=%s amount=%.2f old_extra=%.2f new_extra=%.2f",
        hod_email, amount, old_extra, new_extra,
    )

    # Sync updated budget to Redis so the UI reflects the new values immediately
    # without waiting for a cache miss / next /budget/me call.
    try:
        from store.budget_store import _get_redis, _redis_hset_mapping
        rc = _get_redis()
        if rc:
            _redis_hset_mapping(rc, f"budget:{caller_id}", {
                "extra_cost_usd":     new_extra,
                "max_cost_usd_total": new_total,
            })
    except Exception as exc:
        logger.warning("hod_self_increase: Redis sync failed (non-fatal): %s", exc)

    return {
        "success":        True,
        "new_extra_usd":  new_extra,
        "new_total_usd":  new_total,
    }


@router.post("/budget/request-increase")
def request_increase(
    body: BudgetIncreaseRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    User submits a budget increase request. Fans out to every HOD mapped to
    the requester's department (blocked if none) — see module docstring above.

    Authorisation: the authenticated caller may only request an increase for
    their own account. body.user_id must match the caller's id — this stops
    an attacker (or a mis-scoped internal caller) from submitting requests
    on behalf of arbitrary users, which would otherwise spam the routed HODs'
    inboxes and create persistent ledger rows the victim never authorised.
    """
    is_valid, field_errors, sanitized = validate_budget_request(body)
    if not is_valid:
        error_messages = []
        for field, errors in field_errors.items():
            for e in errors:
                error_messages.append(f"{field}: {e}")
        raise HTTPException(status_code=400, detail="; ".join(error_messages))

    # ── Caller-owns-user_id enforcement (defence against IDOR) ───────────────
    caller_id = str(
        current_user.get("sub")
        or current_user.get("user_id")
        or current_user.get("id")
        or ""
    )
    if not caller_id or str(sanitized["user_id"]) != caller_id:
        raise HTTPException(
            status_code=403,
            detail="You may only request a budget increase for your own account.",
        )

    enforce_rate_limit_with_behaviour(request, BUDGET_REQUEST, user_id=caller_id)

    from store.budget_store import request_budget_increase, resolve_approvers_for_request

    requester = _lookup_requester(caller_id)
    approvers = resolve_approvers_for_request(requester["email"])
    resolved_hod = approvers.get("hod_email")
    if not resolved_hod:
        raise HTTPException(
            status_code=422,
            detail=(
                "No HOD is assigned to your account. "
                "Contact your administrator to assign an HOD."
            ),
        )
    hod_emails       = [resolved_hod]
    delegatee_emails = list(approvers.get("delegatees") or [])

    try:
        req = request_budget_increase(
            user_id=caller_id,
            requested_extra_cost_usd=body.requested_extra_cost_usd,
            justification=sanitized.get("justification") or body.justification,
            requester_email=requester["email"],
            requester_name=requester["name"],
            requester_department=requester["department"],
            hod_emails=hod_emails,
            delegatee_emails=delegatee_emails,
        )
    except ValueError as exc:
        logger.warning(f"budget_router request_increase rejected: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))

    # Notify every approver (HOD + delegatees) via inbox + Outlook email.
    from store.budget_store import get_usage_total
    usage = get_usage_total(caller_id)
    spent_usd = float(usage.get("cost_usd_spent") or 0.0)
    current_total = req["current_base_cost_usd"] + req["current_extra_cost_usd"]
    util_pct = min(100, round((spent_usd / current_total) * 100, 1)) if current_total > 0 else 0

    # (approver_email, delegating_hod_email_or_empty) — empty means "this is
    # the HOD themselves". Delegatees get a copy that explicitly names their
    # delegating HOD in both the inbox body and the email.
    notify_targets: list = [(resolved_hod, "")]
    for d in delegatee_emails:
        notify_targets.append((d, resolved_hod))

    try:
        from db.database import SessionLocal
        from db.models import User
        from store.inbox_store import publish_inbox_item
        from services.smtp_service import send_html_email
        db = SessionLocal()
        try:
            for approver_email, delegating_hod in notify_targets:
                html_body, text_body = _hod_approval_email_html(
                    requester["name"], requester["email"], requester["department"],
                    body.requested_extra_cost_usd, req["justification"],
                    req["current_base_cost_usd"], req["current_extra_cost_usd"],
                    delegating_hod_email=delegating_hod,
                )
                inbox_prefix = (
                    f"{delegating_hod} delegated budget approval to you. "
                    if delegating_hod else ""
                )
                approver_user = (
                    db.query(User).filter(User.email.ilike(approver_email)).first()
                )
                if approver_user:
                    try:
                        item_id = publish_inbox_item(
                            user_id=str(approver_user.id),
                            type="budget_request",
                            title=f"Budget increase request from {requester['name'] or requester['email']}",
                            body=(
                                f"{inbox_prefix}"
                                f"Requesting ${body.requested_extra_cost_usd:.2f} extra (added on top of "
                                f"base). Current: base ${req['current_base_cost_usd']:.2f} + "
                                f"extra ${req['current_extra_cost_usd']:.2f} "
                                f"(${spent_usd:.2f} / ${current_total:.2f} used, {util_pct}% utilised). "
                                f"Justification: {req['justification']}"
                            ),
                            metadata={
                                "request_id":     req["id"],
                                "requester":      caller_id,
                                "delegated_by":   delegating_hod or None,
                            },
                        )
                        if not item_id:
                            logger.warning(f"budget request: publish_inbox_item returned empty for approver {mask_email(approver_email)}")
                    except Exception as e:
                        logger.error(f"budget request: failed to publish inbox item for approver {mask_email(approver_email)}: {e}")
                else:
                    logger.warning(f"budget request: no user found for approver email {mask_email(approver_email)}; skipping inbox notification")
                try:
                    send_html_email(
                        to=[approver_email],
                        subject="AiNxt - Budget increase request awaiting your approval",
                        html_body=html_body,
                        text_body=text_body,
                    )
                except Exception as e:
                    logger.warning(f"budget request: failed to email approver {mask_email(approver_email)}: {e}")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"budget request: failed to notify approvers: {e}")
    return {
        "success":          True,
        "request_id":       req["id"],
        "hod_emails":       req["hod_emails"],
        "delegatee_emails": req.get("delegatee_emails", []),
    }


@router.get("/budget/requests")
def list_pending_requests(
        request: Request,
        scope: Optional[str] = None,
        current_user: dict = Depends(get_current_user),
):
    """
    List budget-increase requests from ainxt.hod_allocation_ledger.

    Admin: sees pending + approved + rejected requests (mixed-status view),
           deduplicated by request_id so a multi-HOD fan-out appears once.
    HOD:   sees only their OWN 'pending' rows — their actionable queue. Once
           resolved (by them or another HOD), it drops off this list.
    Other authenticated user: empty list (preserves pre-existing un-gated
           behaviour without leaking data).

    Query param `scope=hod` forces the HOD-scoped pending-only view even for
    admin+HOD users (used by Budget Manager -> Team -> Pending Requests).
    Query param `scope=mine` returns the CALLER's own pending request(s)
    only (used by "My Budget" so a regular requester can see their own
    request is still awaiting approval). Once it's approved or rejected it
    simply stops showing up here — the requester still learns the outcome
    via their inbox notification and, for approvals, the my-increases
    history table.
    Query param `scope=delegate` returns the pending requests routed to the
    CALLER as a delegatee (i.e. their email appears in some HOD's
    department_hod_mapping.delegated_to). Returns an empty list if the
    caller is not a delegatee for anyone — this does NOT require is_hod.
    """
    from store.budget_store import get_pending_budget_requests, resolve_delegating_hods_for

    force_hod_scope      = scope and scope.strip().lower() == "hod"
    force_mine_scope     = scope and scope.strip().lower() == "mine"
    force_delegate_scope = scope and scope.strip().lower() == "delegate"

    caller_email = (current_user.get("email") or "").strip().lower()

    def _encrypt_request_pii(rows: list) -> list:
        _pii_keys = ("hod_email", "requester_email", "requester_name", "approved_by", "approved_by_name", "delegated_to")
        return [
            {k: (encrypt_pii(v) if k in _pii_keys else v) for k, v in row.items()}
            for row in rows
        ]

    if force_mine_scope:
        caller_id = str(
            current_user.get("sub")
            or current_user.get("user_id")
            or current_user.get("id")
            or ""
        )
        if not caller_id:
            return {"requests": []}
        return {"requests": _encrypt_request_pii(get_pending_budget_requests(target_user_id=caller_id))}
    if force_delegate_scope:
        if not caller_email or not resolve_delegating_hods_for(caller_email):
            return {"requests": []}
        return {"requests": _encrypt_request_pii(get_pending_budget_requests(approver_email=caller_email))}
    if force_hod_scope and is_hod(current_user):
        return {"requests": _encrypt_request_pii(get_pending_budget_requests(hod_email=caller_email))}
    if is_admin(current_user):
        return {"requests": _encrypt_request_pii(get_pending_budget_requests(include_approved=True))}
    if is_hod(current_user):
        return {"requests": _encrypt_request_pii(get_pending_budget_requests(hod_email=caller_email))}
    return {"requests": []}


@router.get("/budget/requests/{request_id}")
def get_request_status(
        request_id: str,
        current_user: dict = Depends(get_current_user),
):
    """
    Return the resolved/live status of a request group (all fanned-out HOD
    rows sharing request_id) — powers the Inbox live-status check so a HOD
    who didn't act first sees "already approved/rejected by X" instead of
    stale action buttons.

    Authorisation: 404 (not 403 — avoid confirming a request_id's existence
    to unauthorised callers) unless the caller is
      - the requester themselves (target_user_id == caller id), or
      - one of the fanned-out HODs (their email is in the request's hod list), or
      - one of the HOD's nominated delegatees (their email is in the
        request's delegated_to list), or
      - an admin (read-only visibility for oversight).
    Anyone else — including senior approvers in unrelated departments — gets
    a 404, mirroring the "request not found" branch to prevent enumeration
    of valid request UUIDs.
    """
    from store.budget_store import get_request_group
    rows = get_request_group(request_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Request not found")

    caller_id    = str(
        current_user.get("sub")
        or current_user.get("user_id")
        or current_user.get("id")
        or ""
    )
    caller_email = (current_user.get("email") or "").strip().lower()
    target_uid   = str(rows[0].get("user_id") or "")
    routed_hods       = {(r.get("hod_email") or "").lower() for r in rows if not r.get("delegated_to")}
    routed_delegatees = {(r.get("delegated_to") or "").lower() for r in rows if r.get("delegated_to")}

    is_requester       = bool(caller_id) and caller_id == target_uid
    is_routed_hod      = is_hod(current_user) and caller_email in routed_hods
    is_routed_delegate = bool(caller_email) and caller_email in routed_delegatees
    is_admin_reader    = is_admin(current_user)

    if not (is_requester or is_routed_hod or is_routed_delegate or is_admin_reader):
        # 404 instead of 403: don't confirm the request_id exists.
        raise HTTPException(status_code=404, detail="Request not found")

    resolved = next((r for r in rows if r["status"] != "pending"), None)
    overall_status = resolved["status"] if resolved else "pending"
    first = rows[0]
    return {
        "request_id":               request_id,
        "status":                   overall_status,
        "user_id":                  first.get("user_id", ""),
        "requester_email":          encrypt_pii(first.get("requester_email", "")),
        "requester_name":           encrypt_pii(first.get("requester_name", "")),
        "requester_department":     first.get("requester_department", ""),
        "requested_extra_cost_usd": first.get("requested_extra_cost_usd"),
        "justification":            first.get("justification", ""),
        "current_base_cost_usd":    first.get("current_base_cost_usd"),
        "current_extra_cost_usd":   first.get("current_extra_cost_usd"),
        "hod_emails":               [encrypt_pii(e) for e in sorted(routed_hods)],
        "delegatee_emails":         [encrypt_pii(e) for e in sorted(routed_delegatees)],
        "approved_by":              encrypt_pii(resolved.get("approved_by")) if resolved else None,
        "approved_by_name":         encrypt_pii(resolved.get("approved_by_name")) if resolved else None,
        "resolved_at":              resolved.get("resolved_at") if resolved else None,
        "new_limit_usd":            resolved.get("new_limit_usd") if resolved else None,
    }


@router.post("/budget/requests/{request_id}/approve")
def approve_request(
        request_id: str,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """
    Approve a budget-increase request.

    Authorisation: an approver is either
      - the HOD the request was routed to (hod_email match with
        delegated_to = NULL on their row), or
      - a delegatee the HOD has nominated on department_hod_mapping
        (delegated_to match, hod_email = the HOD).
    Admins and senior approvers may VIEW the request (see
    GET /budget/requests) but CANNOT approve or reject — the approval is the
    department HOD's or their delegatee's call.

    Self-approval is blocked at both the store layer and here — a delegatee
    who happens to also be the requester of this ticket cannot approve their
    own request. Route it back to the HOD or another delegatee.
    """
    from store.budget_store import approve_budget_request
    (target_user_id, _requested_extra, hod_emails, delegatee_emails,
     requester_email, _is_resolved) = _load_request_group_meta(request_id)
    if not target_user_id:
        raise HTTPException(status_code=404, detail="Request not found")

    actor_email = (current_user.get("email") or "").strip().lower()
    routed_hods       = [h.lower() for h in hod_emails]
    routed_delegatees = [d.lower() for d in delegatee_emails]

    is_hod_actor       = is_hod(current_user) and actor_email in routed_hods
    is_delegatee_actor = bool(actor_email) and actor_email in routed_delegatees

    if not (is_hod_actor or is_delegatee_actor):
        raise HTTPException(
            status_code=403,
            detail=("Only the HOD this request was routed to (or one of their "
                    "nominated delegatees) may approve it."),
        )

    # Self-approval guard (defence in depth — also enforced in the store).
    if requester_email and actor_email == requester_email:
        raise HTTPException(
            status_code=403,
            detail="You cannot approve your own budget-increase request.",
        )

    # is_hod_actor=True for both branches: the store already accepts either
    # a hod_email or delegated_to match and always charges the row's hod_email
    # (the ORIGINAL HOD) cap, not the acting delegatee's.
    result = approve_budget_request(
        request_id,
        acting_hod_email=actor_email,
        acting_hod_name=current_user.get("name", "") or "",
        is_hod_actor=True,
    )
    if not result.get("success"):
        raise HTTPException(status_code=409 if "already" in result.get("error", "") else 404,
                             detail=result.get("error", "Not found"))
    _audit_hod_action(current_user, result.get("user_id", ""), _HOD_ACTION_APPROVE_REQUEST)
    # Notify the requester — approvers' stale inbox cards are LEFT IN PLACE.
    # The pending-requests UI already hides Approve/Reject buttons whenever
    # status != "pending" (see PendingRequests component), and hitting the
    # already-resolved endpoint returns a 409 with "already approved by X".
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            user_id=result["user_id"],
            type="budget_approved",
            title="Budget increase approved",
            body=(
                f"Your request was approved: +${result['requested_extra_usd']:.2f} added to your "
                f"budget (base ${result['new_base_cost_usd']:.2f} + extra "
                f"${result['new_extra_cost_usd']:.2f} = ${result['new_cost_usd']:.2f} total)."
            ),
        )
    except Exception as e:
        logger.warning(f"budget_router: failed to send approval inbox notification: {e}")
    _invalidate_roster_cache()
    result["approved_by"]      = encrypt_pii(result.get("approved_by"))
    result["approved_by_name"] = encrypt_pii(result.get("approved_by_name"))
    return result


@router.get("/budget/model-rates")
def get_model_rates(current_user: dict = Depends(get_current_user)):
    """Return current model cost rates from ModelRateTable.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing the
    platform's internal LLM pricing table to any anonymous caller.
    Fix: added `current_user: dict = Depends(get_current_user)` as a
    function parameter so FastAPI rejects unauthenticated requests with
    401 before the handler runs. Deliberately not admin-only (`require_role`)
    — any authenticated user may still view it, since pricing context is
    needed by every user's budget UI. No other logic changed.
    """
    try:
        from db.database import SessionLocal
        from db.models import ModelRateTable
        db = SessionLocal()
        try:
            rates = db.query(ModelRateTable).order_by(ModelRateTable.effective_from.desc()).all()
            seen = set()
            result = []
            for r in rates:
                if r.model_id not in seen:
                    seen.add(r.model_id)
                    result.append({
                        "model_id":            r.model_id,
                        "provider":            r.provider,
                        "input_cost_per_1k":   float(r.input_cost_per_1k),
                        "output_cost_per_1k":  float(r.output_cost_per_1k),
                        "is_free":             r.is_free,
                        "effective_from":      r.effective_from.isoformat() if r.effective_from else None,
                    })
            return {"rates": result}
        finally:
            db.close()
    except Exception as e:
        logger.error(f"budget_router get_model_rates: {e}")
        return {"rates": [], "error": str(e)}


@router.get("/budget/summary")
def budget_summary(current_user: dict = Depends(get_current_user)):
    """Admin: cumulative usage across all users."""
    from auth.rbac import is_admin
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")
    from store.budget_store import get_all_usage_totals, DEFAULT_COST_LIMIT_USD, DEFAULT_TOKEN_LIMIT, DEFAULT_REQUEST_LIMIT
    try:
        usage = get_all_usage_totals(limit=50)
    except Exception:
        usage = []
    return {
        "users":                     usage,
        "default_cost_limit_usd":    DEFAULT_COST_LIMIT_USD,
        "default_token_limit":       DEFAULT_TOKEN_LIMIT,
        "default_request_limit":     DEFAULT_REQUEST_LIMIT,
    }


@router.get("/budget/band-defaults")
def get_band_defaults(current_user: dict = Depends(get_current_user)):
    """Return band-level budget defaults from BudgetConfig table.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing the
    platform's internal per-band budget-tiering policy (allocation amounts
    and model allowlists per band) to any anonymous caller.
    Fix: added `current_user: dict = Depends(get_current_user)` as a
    function parameter so FastAPI rejects unauthenticated requests with
    401. Deliberately not admin-only (`require_role`) — any authenticated
    user may still view it, as this is reference data every user's budget
    UI needs, same as /budget/model-rates. No other logic changed.
    """
    try:
        from db.database import SessionLocal
        from db.models import BudgetConfig
        db = SessionLocal()
        try:
            configs = db.query(BudgetConfig).filter(
                BudgetConfig.band_level.isnot(None),
                BudgetConfig.user_id.is_(None),
            ).order_by(BudgetConfig.band_level).all()
            return {
                "band_defaults": [
                    {
                        "band_level":           c.band_level,
                        "total_allocation_usd": float(c.monthly_limit_usd),
                        "model_allowlist":      c.model_allowlist,
                    }
                    for c in configs
                ]
            }
        finally:
            db.close()
    except Exception as e:
        logger.error(f"budget_router get_band_defaults: {e}")
        return {"band_defaults": [], "error": str(e)}


def _get_band_allocation(ad_level: int) -> float:
    """Fetch total budget allocation for an ad_level (0-6) from BudgetConfig table.

    Explicit per-band template rows (budget_configs where user_id IS NULL and
    band_level == ad_level) take precedence. When none exists, every ad_level
    defaults to $50 — per-band differences must be configured via budget_configs,
    not hardcoded.
    """
    try:
        from db.database import SessionLocal
        from db.models import BudgetConfig
        db = SessionLocal()
        try:
            cfg = db.query(BudgetConfig).filter(
                BudgetConfig.band_level == ad_level,
                BudgetConfig.user_id.is_(None),
                ).first()
            return float(cfg.monthly_limit_usd) if cfg else 50.0
        finally:
            db.close()
    except Exception:
        return 50.0


@router.post("/budget/requests/{request_id}/reject")
def reject_request(
        request_id: str,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """
    Reject a budget-increase request. A rejection by ANY one routed approver
    kills the request for all fanned-out approvers — see
    store.budget_store.reject_budget_request.

    Authorisation: either the HOD the request was routed to, OR one of the
    delegatees the HOD has nominated on department_hod_mapping. Admins may
    view the request but not act on it. A delegatee-requester cannot reject
    their own request — that self-approval guard is enforced here and at the
    store layer.
    """
    from store.budget_store import reject_budget_request
    (target_user_id, _requested_extra, hod_emails, delegatee_emails,
     requester_email, _is_resolved) = _load_request_group_meta(request_id)
    if not target_user_id:
        raise HTTPException(status_code=404, detail="Request not found")

    actor_email = (current_user.get("email") or "").strip().lower()
    routed_hods       = [h.lower() for h in hod_emails]
    routed_delegatees = [d.lower() for d in delegatee_emails]

    is_hod_actor       = is_hod(current_user) and actor_email in routed_hods
    is_delegatee_actor = bool(actor_email) and actor_email in routed_delegatees

    if not (is_hod_actor or is_delegatee_actor):
        raise HTTPException(
            status_code=403,
            detail=("Only the HOD this request was routed to (or one of their "
                    "nominated delegatees) may reject it."),
        )

    if requester_email and actor_email == requester_email:
        raise HTTPException(
            status_code=403,
            detail="You cannot reject your own budget-increase request.",
        )

    result = reject_budget_request(request_id, acting_hod_email=actor_email, is_hod_actor=True)
    if not result.get("success"):
        raise HTTPException(status_code=409 if "already" in result.get("error", "") else 404,
                             detail=result.get("error", "Not found"))
    _audit_hod_action(current_user, target_user_id, _HOD_ACTION_REJECT_REQUEST)
    try:
        if target_user_id:
            from store.inbox_store import publish_inbox_item
            publish_inbox_item(
                user_id=target_user_id,
                type="budget_rejected",
                title="Budget top-up request rejected",
                body="Your budget top-up request was not approved. Contact your admin for details.",
            )
    except Exception as e:
        logger.warning(f"budget_router: failed to send rejection inbox notification: {e}")
    return result


# ── Phase 9: Chargeback / per-product billing endpoints ────────────────────────

class ProductBudgetAssign(BaseModel):
    product_id: str


def _require_admin_or_operator(current_user: dict = Depends(get_current_user)) -> dict:
    role = current_user.get("role", "")
    if role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Admin or operator access required")
    return current_user


def _pg_conn():
    import psycopg2
    from core.config import postgres_dsn
    return psycopg2.connect(postgres_dsn())


@router.get("/budget/chargeback")
def get_chargeback_summary(current_user: dict = Depends(_require_admin_or_operator)):
    """Admin/operator: per-product cost summary for the current calendar month."""
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                mu.product_id::text,
                COALESCE(p.name, 'unassigned')    AS product_name,
                mu.model,
                COUNT(*)                           AS total_calls,
                COALESCE(SUM(mu.input_tokens), 0)  AS total_tokens_in,
                COALESCE(SUM(mu.output_tokens), 0) AS total_tokens_out,
                COALESCE(SUM(mu.cost_usd), 0.0)   AS total_cost_usd
            FROM model_usages mu
            LEFT JOIN products p ON p.id = mu.product_id
            WHERE mu.created_at >= date_trunc('month', NOW())
            GROUP BY mu.product_id, p.name, mu.model
            ORDER BY total_cost_usd DESC
        """)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        items = [dict(zip(cols, row)) for row in rows]
        grand_total = sum(float(r["total_cost_usd"] or 0) for r in items)
        cur.close()
        conn.close()
        return {
            "month": __import__("datetime").datetime.utcnow().strftime("%Y-%m"),
            "products": items,
            "grand_total_cost_usd": round(grand_total, 6),
        }
    except Exception as e:
        logger.error(f"budget chargeback summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/budget/chargeback/{product_id}")
def get_product_chargeback(product_id: str, current_user: dict = Depends(get_current_user)):
    """Per-product detailed chargeback breakdown."""
    role     = current_user.get("role", "")
    ad_level = int(current_user.get("ad_level", 6) or 6)
    is_admin = role == "admin"

    from core.config import APPROVAL_AD_LEVEL as _APPROVAL_LEVEL
    if not is_admin and ad_level > _APPROVAL_LEVEL:
        raise HTTPException(
            status_code=403,
            detail=f"Seniority level {_APPROVAL_LEVEL} or above (or admin) required for product chargeback",
        )
    try:
        conn = _pg_conn()
        cur = conn.cursor()

        if not is_admin:
            user_dept = current_user.get("department", "") or ""
            cur.execute(
                "SELECT 1 FROM dept_product_mappings WHERE product_id = %s::uuid AND department_name = %s LIMIT 1",
                (product_id, user_dept),
            )
            if not cur.fetchone():
                cur.close()
                conn.close()
                raise HTTPException(status_code=403, detail="You do not have access to chargeback data for this product")

        cur.execute("""
            SELECT mu.model,
                   COUNT(*)                           AS total_calls,
                   COALESCE(SUM(mu.input_tokens), 0)  AS total_tokens_in,
                   COALESCE(SUM(mu.output_tokens), 0) AS total_tokens_out,
                   COALESCE(SUM(mu.cost_usd), 0.0)   AS total_cost_usd
            FROM model_usages mu
            WHERE mu.product_id = %s::uuid
              AND mu.created_at >= date_trunc('month', NOW())
            GROUP BY mu.model
            ORDER BY total_cost_usd DESC
        """, (product_id,))
        model_rows = cur.fetchall()
        model_cols = [d[0] for d in cur.description]
        model_breakdown = [dict(zip(model_cols, row)) for row in model_rows]

        cur.execute("""
            SELECT mu.user_id::text, u.email, u.name,
                   COUNT(*)                           AS total_calls,
                   COALESCE(SUM(mu.input_tokens), 0)  AS total_tokens_in,
                   COALESCE(SUM(mu.output_tokens), 0) AS total_tokens_out,
                   COALESCE(SUM(mu.cost_usd), 0.0)   AS total_cost_usd
            FROM model_usages mu
            LEFT JOIN users u ON u.id = mu.user_id::uuid
            WHERE mu.product_id = %s::uuid
              AND mu.created_at >= date_trunc('month', NOW())
            GROUP BY mu.user_id, u.email, u.name
            ORDER BY total_cost_usd DESC
        """, (product_id,))
        user_rows = cur.fetchall()
        user_cols = [d[0] for d in cur.description]
        user_breakdown = [dict(zip(user_cols, row)) for row in user_rows]
        for _row in user_breakdown:
            if "email" in _row:
                _row["email"] = encrypt_pii(_row["email"])
            if "name" in _row:
                _row["name"] = encrypt_pii(_row["name"])

        cur.execute("SELECT name, code FROM products WHERE id = %s::uuid", (product_id,))
        prod_row = cur.fetchone()
        product_name = prod_row[0] if prod_row else "unknown"
        product_code = prod_row[1] if prod_row else ""

        grand_total = sum(float(r["total_cost_usd"] or 0) for r in model_breakdown)
        cur.close()
        conn.close()
        return {
            "product_id":      product_id,
            "product_name":    product_name,
            "product_code":    product_code,
            "month":           __import__("datetime").datetime.utcnow().strftime("%Y-%m"),
            "model_breakdown": model_breakdown,
            "user_breakdown":  user_breakdown,
            "total_cost_usd":  round(grand_total, 6),
        }
    except Exception as e:
        logger.error(f"budget chargeback product {product_id} error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/budget/users/{user_id}/product")
def assign_product_budget(
        user_id: str,
        body: ProductBudgetAssign,
        current_user: dict = Depends(get_current_user),
):
    """Set default product_id for a user's usage tracking (chargeback assignment)."""
    caller_id = str(current_user.get("sub", current_user.get("user_id", "")))
    role = current_user.get("role", "")
    if role != "admin" and caller_id != user_id:
        raise HTTPException(status_code=403, detail="You may only update your own product assignment unless you are an admin")
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM products WHERE id = %s::uuid", (body.product_id,))
        if not cur.fetchone():
            cur.close()
            conn.close()
            raise HTTPException(status_code=404, detail=f"Product '{body.product_id}' not found")
        cur.execute(
            "UPDATE users SET default_product_id = %s::uuid WHERE id = %s::uuid RETURNING id",
            (body.product_id, user_id),
        )
        updated = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        if not updated:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        return {"success": True, "user_id": user_id, "product_id": body.product_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"assign_product_budget error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════════════════════════════════════════
# ADMIN: HOD MONTHLY-CAP MANAGEMENT
#
# - GET  /budget/admin/hods                  → list every HOD (from
#                                              department_hod_mapping) with
#                                              their cap row (if any) and
#                                              current-period consumption.
# - PUT  /budget/admin/hods/{email}/cap      → upsert monthly_cap_usd in
#                                              ainxt.hod_allocation_caps.
#
# Visibility: admin only (403 otherwise). HODs continue to use
# GET /budget/hod/cap-status for their own banner.
#
# Note: this endpoint deliberately does NOT fall back to the
# HOD_DEFAULT_MONTHLY_CAP_USD env var. If an HOD has no row in
# hod_allocation_caps the UI surfaces "Max cap not yet configured"
# so the admin can explicitly create one.
# ════════════════════════════════════════════════════════════════════════════

class HodCapUpsert(BaseModel):
    monthly_cap_usd: float
    notes:           Optional[str] = None

    @field_validator("monthly_cap_usd")
    @classmethod
    def _validate_cap(cls, v: float) -> float:
        if v is None or v <= 0:
            raise ValueError("monthly_cap_usd must be > 0")
        return float(v)

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > 1000:
            raise ValueError("notes must be ≤ 1000 characters")
        return v


def _admin_hod_period_and_reset():
    """Inline equivalents of governor._current_period / _first_of_next_month.

    Duplicated here (rather than importing private symbols) to keep the
    governor module's surface clean.
    """
    from datetime import datetime, timezone, date
    now = datetime.now(timezone.utc)
    period = now.strftime("%Y-%m")
    today  = now.date()
    year, month = (today.year + (today.month // 12)), (today.month % 12) + 1
    resets_on = date(year, month, 1)
    return period, resets_on.isoformat()


@router.get("/budget/admin/hods")
def admin_list_hods(request: Request, current_user: dict = Depends(get_current_user)):
    """
    Admin-only: list every HOD with cap status and current-period consumption.

    Response: { "hods": [ { hod_email, hod_name, departments,
                            has_cap_row, monthly_cap_usd, consumed_usd,
                            remaining_usd, period_yyyymm, resets_on,
                            is_active, updated_at, updated_by,
                            enforcement } ] }
    """
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")

    enforce_rate_limit_with_behaviour(request, BUDGET_ADMIN)

    import os
    from sqlalchemy import text
    from db.database import SessionLocal

    period, resets_on = _admin_hod_period_and_reset()
    enforcement_on = os.getenv("HOD_CAP_ENFORCEMENT_ENABLED", "false") \
        .strip().lower() in ("1", "true", "yes", "on")

    db = SessionLocal()
    try:
        # 1) Authoritative HOD list (dedup by lower(email))
        hod_rows = db.execute(text("""
            SELECT lower(hod_email)                  AS email,
                   MAX(hod_name)                     AS hod_name,
                   array_agg(DISTINCT department_name) AS departments
            FROM ainxt.department_hod_mapping
            WHERE hod_email IS NOT NULL AND hod_email <> ''
            GROUP BY lower(hod_email)
            ORDER BY lower(hod_email)
        """)).fetchall()

        # 2) Cap rows keyed by lower(email)
        cap_rows = db.execute(text("""
            SELECT lower(hod_email) AS email,
                   monthly_cap_usd, is_active, updated_at, updated_by
            FROM ainxt.hod_allocation_caps
        """)).fetchall()
        caps = {r.email: r for r in cap_rows}

        # 3) Current-period consumption keyed by lower(email).
        #    Two independent totals live on this table and must both be added:
        #      - consumed_after_usd  : running allocation total (any action
        #                              EXCEPT 'endpoint_spend', which never sets it)
        #      - endpoint_spend_usd  : running managed-endpoint cloud spend,
        #                              carried on the single action='endpoint_spend'
        #                              row per (hod, period) — see
        #                              services/endpoint_budget_governor.py.
        #    Without the second term, cloud spend incurred via managed endpoints
        #    never shows up here even though it's gated against the same cap.
        cons_rows = db.execute(text("""
            SELECT lower(hod_email) AS email,
                   COALESCE(MAX(consumed_after_usd), 0)
                   + COALESCE(MAX(CASE WHEN action = 'endpoint_spend'
                                        THEN endpoint_spend_usd END), 0) AS consumed
            FROM ainxt.hod_allocation_ledger
            WHERE period_yyyymm = :period AND shadow_mode = FALSE
            GROUP BY lower(hod_email)
        """), {"period": period}).fetchall()
        cons = {r.email: float(r.consumed or 0) for r in cons_rows}

        # 4) User count per HOD from users.hod_email — single source of truth.
        hod_user_count_rows = db.execute(text("""
            SELECT lower(hod_email) AS email, COUNT(*) AS user_count
            FROM   users
            WHERE  hod_email IS NOT NULL AND hod_email <> ''
              AND  is_active = TRUE
            GROUP  BY lower(hod_email)
        """)).fetchall()
        hod_user_counts = {r.email: int(r.user_count or 0) for r in hod_user_count_rows}

        out = []
        for h in hod_rows:
            email = h.email
            consumed = float(cons.get(email, 0.0))
            cap_row = caps.get(email)
            depts = list(h.departments or [])
            total_users = hod_user_counts.get(email, 0)

            if cap_row is not None:
                cap = float(cap_row.monthly_cap_usd or 0)
                remaining = max(0.0, cap - consumed)
                out.append({
                    "hod_email":              email,
                    "hod_name":               h.hod_name,
                    "departments":            depts,
                    "total_users":            total_users,
                    "has_cap_row":            True,
                    "monthly_cap_usd":        cap,
                    "consumed_usd":           consumed,
                    "remaining_usd":          remaining,
                    "period_yyyymm":          period,
                    "resets_on":              resets_on,
                    "is_active":              bool(cap_row.is_active),
                    "updated_at":             cap_row.updated_at.isoformat() if cap_row.updated_at else None,
                    "updated_by":             cap_row.updated_by,
                    "enforcement":            enforcement_on,
                })
            else:
                out.append({
                    "hod_email":              email,
                    "hod_name":               h.hod_name,
                    "departments":            depts,
                    "total_users":            total_users,
                    "has_cap_row":            False,
                    "monthly_cap_usd":        None,
                    "consumed_usd":           consumed,
                    "remaining_usd":          None,
                    "period_yyyymm":          period,
                    "resets_on":              resets_on,
                    "is_active":              None,
                    "updated_at":             None,
                    "updated_by":             None,
                    "enforcement":            enforcement_on,
                })

        # Unconfigured rows first, then most-consumed first, then alpha.
        out.sort(key=lambda r: (r["has_cap_row"], -float(r["consumed_usd"] or 0), r["hod_email"]))
        for r in out:
            r["hod_email"]  = encrypt_pii(r["hod_email"])
            r["hod_name"]   = encrypt_pii(r["hod_name"])
            r["updated_by"] = encrypt_pii(r["updated_by"])
        return {"hods": out}
    except Exception as e:
        logger.error(f"admin_list_hods error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/budget/admin/hods/{hod_email}/cap")
def admin_upsert_hod_cap(
    hod_email:    str,
    body:         HodCapUpsert,
    request:      Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Admin-only: insert or update the monthly cap for an HOD.

    - 200 → { success, hod_email, monthly_cap_usd, has_cap_row,
              created, updated_at, updated_by }
    - 403 if caller is not admin
    - 404 if hod_email is not present in department_hod_mapping
    - 422 from Pydantic validation
    """
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")

    enforce_rate_limit_with_behaviour(request, BUDGET_ADMIN)

    email_lc = (hod_email or "").strip().lower()
    if not email_lc:
        raise HTTPException(status_code=400, detail="hod_email is required")

    actor = (current_user.get("email") or "").lower() or None

    from sqlalchemy import text
    from db.database import SessionLocal

    db = SessionLocal()
    try:
        # Guard: refuse to seed caps for unknown HODs.
        exists = db.execute(text("""
            SELECT 1 FROM ainxt.department_hod_mapping
            WHERE lower(hod_email) = :e
            LIMIT 1
        """), {"e": email_lc}).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail=f"HOD '{email_lc}' not found in department_hod_mapping")

        # Detect add vs edit so we can return `created` and audit-log correctly.
        prior = db.execute(text("""
            SELECT 1 FROM ainxt.hod_allocation_caps
            WHERE lower(hod_email) = :e
            LIMIT 1
        """), {"e": email_lc}).fetchone()
        created = prior is None

        result = db.execute(text("""
            INSERT INTO ainxt.hod_allocation_caps
              (hod_email, monthly_cap_usd, is_active, notes, created_at, updated_at, updated_by)
            VALUES
              (:email, :cap, TRUE, :notes, now(), now(), :actor)
            ON CONFLICT (hod_email) DO UPDATE
              SET monthly_cap_usd = EXCLUDED.monthly_cap_usd,
                  notes           = COALESCE(EXCLUDED.notes, ainxt.hod_allocation_caps.notes),
                  is_active       = TRUE,
                  updated_at      = now(),
                  updated_by      = EXCLUDED.updated_by
            RETURNING monthly_cap_usd, updated_at, updated_by
        """), {
            "email": email_lc,
            "cap":   body.monthly_cap_usd,
            "notes": body.notes,
            "actor": actor,
        }).fetchone()
        db.commit()

        try:
            logger.info(
                "hod_cap_admin_%s actor=%s target=%s new_cap=%.2f",
                "create" if created else "update",
                actor or "unknown",
                email_lc,
                float(body.monthly_cap_usd),
            )
        except Exception:
            pass

        return {
            "success":         True,
            "hod_email":       encrypt_pii(email_lc),
            "monthly_cap_usd": float(result.monthly_cap_usd or 0),
            "has_cap_row":     True,
            "created":         created,
            "updated_at":      result.updated_at.isoformat() if result.updated_at else None,
            "updated_by":      encrypt_pii(result.updated_by),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"admin_upsert_hod_cap error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()



# ════════════════════════════════════════════════════════════════════════════
# HOD ALLOCATION AUDIT — who increased which user's budget, by how much
#
# GET /budget/admin/hod-audit
#   Reads the append-only ainxt.hod_allocation_ledger (written by
#   services/hod_budget_governor.reserve_and_record) so admins can audit HOD
#   cap spend across everyone, and each HOD can review their own allocations.
#
# Visibility:
#   - Admin              → all HOD ledger rows (optional ?hod_email filter).
#   - HOD (non-admin)    → forced to their own hod_email (any ?hod_email is
#                          ignored so an HOD cannot read another HOD's spend).
#   - Anyone else        → 403.
#
# Row scope: only "charged" rows (amount_usd > 0) that are live (shadow_mode
# = FALSE) — i.e. real budget increases actually counted against the cap.
# ════════════════════════════════════════════════════════════════════════════

@router.get("/budget/admin/hod-audit")
def hod_allocation_audit(
    request:      Request,
    hod_email:    Optional[str] = None,   # admin-only filter; overridden for HODs
    period:       Optional[str] = None,   # 'YYYY-MM'; defaults to current period
    limit:        int = 200,
    current_user: dict = Depends(get_current_user),
):
    """List HOD budget-increase allocations with a per-HOD rollup."""
    _admin = is_admin(current_user)
    _hod   = is_hod(current_user)
    if not (_admin or _hod):
        raise HTTPException(status_code=403, detail="Admin or HOD access required")

    enforce_rate_limit_with_behaviour(request, BUDGET_ADMIN)

    # HODs are strictly scoped to their own email; admins may filter freely.
    if _hod:
        scoped_email = (current_user.get("email") or "").strip().lower()
        if not scoped_email:
            raise HTTPException(status_code=403, detail="HOD email unavailable")
    else:
        scoped_email = (hod_email or "").strip().lower() or None

    # Clamp limit to a sane range.
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200

    default_period, _ = _admin_hod_period_and_reset()
    period = (period or default_period).strip()
    # Validate period format — guards the parameter reaching the ledger query.
    from datetime import datetime as _dt
    try:
        _dt.strptime(period, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")

    from sqlalchemy import text
    from db.database import SessionLocal

    sql = """
        SELECT l.id::text                              AS id,
               lower(l.hod_email)                      AS hod_email,
               l.period_yyyymm                         AS period_yyyymm,
               l.target_user_id::text                  AS target_user_id,
               COALESCE(l.target_user_email, u.email)  AS target_email,
               u.name                                  AS target_name,
               u.department                            AS target_department,
               l.action                                AS action,
               l.amount_usd                            AS amount_usd,
               l.previous_limit_usd                    AS previous_limit_usd,
               l.new_limit_usd                         AS new_limit_usd,
               l.cap_at_time_usd                       AS cap_at_time_usd,
               l.consumed_after_usd                    AS consumed_after_usd,
               l.justification                         AS justification,
               l.created_at                            AS created_at
        FROM ainxt.hod_allocation_ledger l
        LEFT JOIN ainxt.users u ON u.id = l.target_user_id
        WHERE l.amount_usd > 0
          AND l.status = 'approved'
          AND l.shadow_mode = FALSE
          AND l.period_yyyymm = :period
          {hod_filter}
        ORDER BY l.created_at DESC
        LIMIT :limit
    """.format(hod_filter="AND lower(l.hod_email) = :hod_email" if scoped_email else "")

    params = {"period": period, "limit": limit}
    if scoped_email:
        params["hod_email"] = scoped_email

    db = SessionLocal()
    try:
        rows = db.execute(text(sql), params).fetchall()

        entries = []
        rollup: dict = {}
        for r in rows:
            amount = float(r.amount_usd or 0)
            entries.append({
                "id":                 r.id,
                "hod_email":          encrypt_pii(r.hod_email),
                "period_yyyymm":      r.period_yyyymm,
                "target_user_id":     r.target_user_id,
                "target_email":       encrypt_pii(r.target_email or ""),
                "target_name":        encrypt_pii(r.target_name or ""),
                "target_department":  r.target_department or "",
                "action":             r.action,
                "amount_usd":         amount,
                "previous_limit_usd": float(r.previous_limit_usd) if r.previous_limit_usd is not None else None,
                "new_limit_usd":      float(r.new_limit_usd) if r.new_limit_usd is not None else None,
                "cap_at_time_usd":    float(r.cap_at_time_usd or 0),
                "consumed_after_usd": float(r.consumed_after_usd or 0),
                "justification":      r.justification or "",
                "created_at":         r.created_at.isoformat() if r.created_at else None,
            })

            agg = rollup.setdefault(r.hod_email, {
                "total_increased_usd": 0.0,
                "allocation_count":    0,
                "_users":              set(),
            })
            agg["total_increased_usd"] += amount
            agg["allocation_count"]    += 1
            agg["_users"].add(r.target_user_id)

        # Finalise rollup — resolve distinct-user sets to counts and round money.
        rollup_out = {
            encrypt_pii(email): {
                "total_increased_usd": round(v["total_increased_usd"], 2),
                "allocation_count":    v["allocation_count"],
                "distinct_users":      len(v["_users"]),
            }
            for email, v in rollup.items()
        }

        return {
            "period":   period,
            "scope":    "own" if _hod else "all",
            "entries":  entries,
            "rollup":   rollup_out,
        }
    except Exception as e:
        logger.error(f"hod_allocation_audit error: {e}")
        # Log details server-side; return a generic message so we don't
        # leak SQL/column names or internal exception state to the client.
        raise HTTPException(status_code=500, detail="Failed to load HOD allocation audit")
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# MY BUDGET — increase history (justification + approver) for the logged-in
# user themselves, mirroring what their HOD/admin can already see for them
# via GET /budget/admin/hod-audit. Only genuinely APPROVED increases —
# pending/rejected/superseded requests are not shown here.
# ════════════════════════════════════════════════════════════════════════════

@router.get("/budget/my-increases")
def my_budget_increases(
    request:      Request,
    current_user: dict = Depends(get_current_user),
):
    """Return the calling user's own approved budget-increase history."""
    enforce_rate_limit_with_behaviour(request, BUDGET_ADMIN)

    user_id = str(current_user.get("sub") or current_user.get("user_id") or current_user.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="Could not resolve caller user_id")

    from sqlalchemy import text
    from db.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id::text, hod_email, approved_by, approved_by_name,
                   amount_usd, previous_limit_usd, new_limit_usd,
                   justification, resolved_at
            FROM   ainxt.hod_allocation_ledger
            WHERE  target_user_id = :uid
              AND  action = 'approve_request'
              AND  status = 'approved'
              AND  amount_usd > 0
            ORDER  BY resolved_at DESC
        """), {"uid": user_id}).fetchall()

        entries = [{
            "id":                 r[0],
            "hod_email":          encrypt_pii(r[1]),
            "approved_by":        encrypt_pii(r[2] or r[1]),
            "approved_by_name":   encrypt_pii(r[3] or ""),
            "amount_usd":         float(r[4] or 0),
            "previous_limit_usd": float(r[5]) if r[5] is not None else None,
            "new_limit_usd":      float(r[6]) if r[6] is not None else None,
            "justification":      r[7] or "",
            "resolved_at":        r[8].isoformat() if r[8] else None,
        } for r in rows]

        return {"user_id": user_id, "entries": entries}
    except Exception as e:
        logger.error(f"my_budget_increases error: {e}")
        # Server-side logging retains the traceback; the API surface stays
        # generic so column names / SQL fragments aren't leaked to callers.
        raise HTTPException(status_code=500, detail="Failed to load budget increase history")
    finally:
        db.close()


# ============================================================
# MONTHLY RESET — admin manual trigger (backfills / testing)
# ============================================================

@router.post("/budget/admin/run-monthly-reset")
def admin_run_monthly_reset(
    period: str,                                       # 'YYYY-MM'
    current_user: dict = Depends(get_current_user),
):
    """
    Admin-only. Manually trigger the monthly snapshot+reset for the given
    period. Gated by BUDGET_MONTHLY_RESET_ENABLED — returns 503 if the
    feature is disabled.

    The same service is also invoked automatically by the
    `budget-reset-cron` thread in workers/start_workers.py.
    """
    import os
    from datetime import datetime as _dt

    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="admin only")

    if os.getenv("BUDGET_MONTHLY_RESET_ENABLED", "false").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        raise HTTPException(status_code=503, detail="feature disabled")

    # Validate period format strictly — guards against arbitrary input being
    # forwarded into the ledger query.
    try:
        _dt.strptime(period, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")

    from services.budget_audit_service import snapshot_and_reset_all_budgeted_users
    out = snapshot_and_reset_all_budgeted_users(period)
    _invalidate_roster_cache()
    return out


@router.post("/budget/admin/send-reset-warning")
def admin_send_reset_warning(
    period: str,                                       # 'YYYY-MM' (the month about to close)
    user_id: Optional[str] = None,                     # Optional: send to one user (dry-run)
    current_user: dict = Depends(get_current_user),
):
    """
    Admin-only. Send the pre-reset warning email for the given period.

      - Without `user_id`: send to every user with a per-user BudgetConfig.
      - With `user_id`   : send only to that user — useful for previewing
                           the template before the monthly cron fires.

    Gated by BUDGET_MONTHLY_RESET_ENABLED — returns 503 if disabled.
    Tip: set BUDGET_RESET_EMAIL_TEST_OVERRIDE=<your-email> in the env to
    route every outgoing email to a single inbox for testing.
    """
    import os
    from datetime import datetime as _dt

    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="admin only")

    if os.getenv("BUDGET_MONTHLY_RESET_ENABLED", "false").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        raise HTTPException(status_code=503, detail="feature disabled")

    try:
        _dt.strptime(period, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")

    from services.budget_audit_service import send_pre_reset_warnings_for_all
    return send_pre_reset_warnings_for_all(period, only_user_id=user_id)


# ── Platform-wide utilization (admin) ────────────────────────────────────────

def _resolve_util_window(period: str, reference_date: str) -> tuple:
    """
    Resolve (window_start, window_end_exclusive) date objects for the given
    period, anchored so the window *contains* reference_date — mirroring the
    Cloud Usage dashboard's _resolve_window (llm_spend_report_router.py):

      - "day"   → the reference date itself
      - "week"  → the Mon–Sun week the reference date falls in
      - "month" → the calendar month the reference date falls in

    window_end is exclusive (start of the day AFTER the last included day) so
    the SQL filter is a clean half-open range: created_at >= ws AND < we.
    """
    import calendar as _cal
    from datetime import date as _date, timedelta as _td

    try:
        ref = _date.fromisoformat(reference_date)
    except Exception:
        raise HTTPException(status_code=400, detail="reference_date must be YYYY-MM-DD")

    if period == "day":
        return ref, ref + _td(days=1)
    if period == "week":
        monday = ref - _td(days=ref.weekday())
        return monday, monday + _td(days=7)
    if period == "month":
        ws = ref.replace(day=1)
        last_day = _cal.monthrange(ref.year, ref.month)[1]
        return ws, ws + _td(days=last_day)
    raise HTTPException(status_code=400, detail="period must be 'month', 'week', or 'day'")


@router.get("/budget/admin/platform-utilization")
def get_platform_utilization(
    dimension: str = "channel",
    period: str = "month",
    reference_date: str = "",
    current_user: dict = Depends(get_current_user),
):
    """
    Admin: platform-wide spend + unique user count broken down by channel or
    model for a window that lands around the caller-chosen reference date.

    Query params
    ------------
    dimension      : "channel" | "model"   (default: "channel")
    period         : "month" | "week" | "day"  (default: "month")
    reference_date : YYYY-MM-DD anchor for the window (default: today).
                     The resolved window CONTAINS this date — e.g. period=month
                     with reference_date=2026-08-13 covers all of August 2026.

    The query runs against model_usages which is HASH-partitioned by id (128
    partitions). Date filters cannot prune partitions and PostgreSQL prefers a
    seq scan across all partitions when the window covers a large fraction of
    rows, so we keep the window as tight as the requested period allows to
    minimise the rows aggregated.

    Uses the shared SQLAlchemy pool (SessionLocal) — the same connection path
    as every other query in this router — so it inherits the app's DB
    credentials, statement_timeout, and search_path, and returns the
    connection to the pool on the way out.
    """
    from auth.rbac import is_admin
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")

    if dimension not in ("channel", "model"):
        raise HTTPException(status_code=400, detail="dimension must be 'channel' or 'model'")
    if period not in ("month", "week", "day"):
        raise HTTPException(status_code=400, detail="period must be 'month', 'week', or 'day'")

    from datetime import date as _date, timedelta as _td
    ref_str = reference_date or _date.today().isoformat()
    window_start, window_end = _resolve_util_window(period, ref_str)

    # Build the GROUP BY key expression.
    # For channel: use the same normalisation as _breakdown_for_users so that
    #   'cli', 'CLI', '' all collapse to 'CLI' / 'UNKNOWN'.
    # For model: reuse _MODEL_KEY_SQL which strips friendly wrappers and local:
    #   prefixes so the same underlying model always aggregates into one slice.
    if dimension == "channel":
        key_expr = f"COALESCE({_CHANNEL_KEY_SQL}, 'UNKNOWN')"
    else:
        key_expr = f"COALESCE({_MODEL_KEY_SQL.strip()}, 'UNKNOWN')"

    # Half-open range [window_start, window_end) passed as bound parameters —
    # the window dates are never string-interpolated into the SQL.
    sql = f"""
        SELECT
            {key_expr}                      AS key,
            COALESCE(SUM(cost_usd), 0)      AS cost_usd,
            COUNT(*)                        AS requests,
            COALESCE(SUM(total_tokens), 0)  AS tokens,
            COUNT(DISTINCT user_id)         AS unique_users
        FROM model_usages
        WHERE created_at >= :ws AND created_at < :we
        GROUP BY 1
        ORDER BY cost_usd DESC
    """

    from sqlalchemy import text as _text
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        rows = db.execute(
            _text(sql), {"ws": window_start, "we": window_end}
        ).fetchall()
    except Exception as exc:
        logger.error("platform_utilization error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load platform utilization")
    finally:
        db.close()

    breakdown = [
        {
            "key":          m.get("key") or "UNKNOWN",
            "cost_usd":     round(float(m.get("cost_usd") or 0), 6),
            "requests":     int(m.get("requests") or 0),
            "tokens":       int(m.get("tokens") or 0),
            "unique_users": int(m.get("unique_users") or 0),
        }
        for m in (row._mapping for row in rows)
    ]

    # For model breakdowns, apply the alias map so shorthand names (tera, luna,
    # deep, haiku, …) and structural variants (date suffixes, inline comments)
    # merge into their canonical model ids from core.model_registry.
    if dimension == "model":
        breakdown = _normalise_model_breakdown(breakdown)

    total_cost_usd     = round(sum(r["cost_usd"]     for r in breakdown), 6)
    total_unique_users = sum(r["unique_users"] for r in breakdown)

    return {
        "dimension":          dimension,
        "period":             period,
        "reference_date":     ref_str,
        # window_end is stored exclusive; report the last INCLUDED day
        "window_start":       window_start.isoformat(),
        "window_end":         (window_end - _td(days=1)).isoformat(),
        "breakdown":          breakdown,
        "total_cost_usd":     total_cost_usd,
        "total_unique_users": total_unique_users,
    }
