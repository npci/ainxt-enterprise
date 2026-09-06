# SPDX-License-Identifier: MIT
# ============================================================
# MEMORY ROUTER — /memory
# Episodic cross-session agent memory + per-agent analytics
# ============================================================

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from auth.dependencies import get_current_user as _get_current_user
from auth.rbac import require_role as _require_role
from memory.postgres_memory import PostgresMemory as _PM
from core.logger import logger

router = APIRouter(prefix="/memory", tags=["memory"])


# ── Shared model-label normalizer (analytics) ──────────────────
# Used by BOTH /analytics/platform and /agents/{name}/analytics so the same
# raw model id always renders as the same display label on every tab. This
# used to be two independently-maintained near-copies (~200 lines apart)
# that had already drifted — e.g. only one of them matched "gpt5-mini".
def _norm_model(raw: str) -> str:
    if not raw or raw in ("auto", "unknown", ""):
        return "Ollama local (llama3.1)"
    m = raw.lower()
    if "llama" in m or "ollama" in m or "local" in m:
        return "Ollama local (llama3.1)"
    if "gpt-5-mini" in m or "gpt5-mini" in m:
        return "GPT-5 Mini"
    if "gpt-5-5" in m:
        return "GPT-5-5 (Latest)"
    if "gpt-5.4" in m or "gpt-5" in m or "gpt5" in m:
        return "GPT-5.4"
    if "claude" in m and ("opus" in m) and ("4-7" in m or "4.7" in m):
        return "Claude Opus 4.7"
    if "claude" in m and ("opus" in m) and ("4-6" in m or "4.6" in m):
        return "Claude Opus 4.6"
    if "claude" in m and ("sonnet" in m or "4-6" in m or "4.6" in m):
        return "Claude Sonnet 4.6"
    if "claude" in m:
        return "Claude"
    if "gemini" in m:
        return "Gemini 2.5 Flash"
    return raw

# Module-level singleton for cross-chat user memory.
# CRITICAL: the routes below must be registered BEFORE the
# /{agent_name} catch-all routes, otherwise "/memory/user" gets
# matched by /{agent_name} with agent_name="user" and the wrong
# handler runs. (FastAPI does order-based, not specificity-based,
# routing.)
_user_pm = _PM()


# ── Cross-chat USER memory (ChatGPT-style "Memories") ─────────
# Surfaced by Chat → Memory drawer (MemoryPanel.jsx).
# These MUST come before /{agent_name} below.

@router.get("/user")
def get_user_memory_entries(current_user: dict = Depends(_get_current_user)):
    """List the caller's saved cross-chat memory entries (newest first)."""
    user_id = current_user.get("sub") or current_user.get("user_id") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return {"entries": _user_pm.list_user_memory(user_id, limit=100)}


@router.delete("/user/{mem_id}")
def delete_user_memory_entry(
    mem_id: str,
    current_user: dict = Depends(_get_current_user),
):
    """Delete one cross-chat memory entry by id (scoped to the caller)."""
    user_id = current_user.get("sub") or current_user.get("user_id") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthenticated")
    ok = _user_pm.delete_user_memory(user_id, mem_id)
    if not ok:
        raise HTTPException(status_code=404, detail="memory entry not found")
    return {"deleted": True}


@router.delete("/user")
def clear_user_memory_entries(current_user: dict = Depends(_get_current_user)):
    """Clear ALL cross-chat memory for the caller. Returns count deleted."""
    user_id = current_user.get("sub") or current_user.get("user_id") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return {"deleted": _user_pm.clear_user_memory(user_id)}


# ── Request models ────────────────────────────────────────────

class MemoryWrite(BaseModel):
    key:   str
    value: str
    tags:  List[str] = []


# ── POST /memory/{agent_name} ─────────────────────────────────

@router.post("/{agent_name}")
def write_memory(agent_name: str, body: MemoryWrite):
    """Store a key-value memory for an agent (upsert)."""
    from store.episodic_memory import remember
    record = remember(agent_name, body.key, body.value, body.tags)
    return {"success": True, "record": record}


# ── GET /memory/{agent_name} ──────────────────────────────────

@router.get("/{agent_name}")
def read_all_memory(
    agent_name: str,
    current_user: dict = Depends(_get_current_user),
):
    """Return all memory entries for an agent.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, letting any
    anonymous caller dump an agent's entire cross-session episodic memory
    (which can include content synthesized from user conversations).
    Fix: added `current_user: dict = Depends(_get_current_user)` as a
    function parameter so FastAPI rejects unauthenticated requests with
    401 before the handler runs. Deliberately not admin-only — any
    authenticated user may still read it, same as the platform-wide agent
    analytics endpoints below. No other logic changed.
    """
    from store.episodic_memory import recall_all
    memories = recall_all(agent_name)
    return {"agent_name": agent_name, "memory": memories}


# ── GET /memory/{agent_name}/search ──────────────────────────

@router.get("/{agent_name}/search")
def search_memory(
    agent_name: str,
    tags: Optional[str] = Query(default=None, description="Comma-separated tags"),
    current_user: dict = Depends(_get_current_user),
):
    """Return memory entries matching the given tags.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    previously had no auth dependency, exposing an agent's episodic memory
    to any anonymous caller.
    Fix: added `current_user: dict = Depends(_get_current_user)` as a
    function parameter so FastAPI rejects unauthenticated requests with
    401. No other logic changed.
    """
    from store.episodic_memory import recall_by_tags
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    results = recall_by_tags(agent_name, tag_list)
    return {"agent_name": agent_name, "results": results}


# ── GET /memory/{agent_name}/{key} ────────────────────────────

@router.get("/{agent_name}/{key}")
def read_memory_key(
    agent_name: str,
    key: str,
    current_user: dict = Depends(_get_current_user),
):
    """Return the value for a specific memory key.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    previously had no auth dependency, exposing individual episodic-memory
    entries to any anonymous caller.
    Fix: added `current_user: dict = Depends(_get_current_user)` as a
    function parameter so FastAPI rejects unauthenticated requests with
    401. No other logic changed.
    """
    from store.episodic_memory import recall
    value = recall(agent_name, key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"Memory key '{key}' not found for agent '{agent_name}'")
    return {"agent_name": agent_name, "key": key, "value": value}


# ── DELETE /memory/{agent_name}/{key} ─────────────────────────

@router.delete("/{agent_name}/{key}")
def delete_memory_key(agent_name: str, key: str):
    """Delete a memory entry."""
    from store.episodic_memory import forget
    existed = forget(agent_name, key)
    if not existed:
        raise HTTPException(status_code=404, detail=f"Memory key '{key}' not found")
    return {"success": True}

# ── GET /agents/{name}/analytics ──────────────────────────────
# (registered on this router with a different prefix — included in gateway)

analytics_router = APIRouter(tags=["agents"])


@analytics_router.get("/agents/{name}/analytics")
def agent_analytics(name: str, current_user: dict = Depends(_require_role("admin"))):
    """
    Return per-agent analytics: total runs, avg latency, success rate, cost, model usage.
    Also returns recent 20 run events as live logs.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing internal
    per-agent cost/usage/latency figures and recent run logs to any anonymous
    caller.
    Access control: admin-only, matching the sidebar's "Observe" section
    (Monitoring/Analytics/Eval Observatory), which is restricted to
    admins. 

    All totals/averages/breakdowns are computed with SQL-side aggregation
    (COUNT/SUM/AVG/GROUP BY) — no `.all()` on ModelUsage. An agent like "cli"
    or "ide_direct" can have well over a million rows; materialising every one
    of them into a Python ORM object just to sum a handful of columns is the
    performance defect documented for the platform-wide endpoint below, and it
    hits this endpoint too (Tier 2/3 fallbacks previously loaded the whole
    table to compute a platform-wide average).
    """
    try:
        from db.database import SessionLocal
        from db.models import ModelUsage, SDLCRunEvent, AgentRecord
        from sqlalchemy import desc, func, case, or_

        session = SessionLocal()
        try:
            def _agg_usage(filter_clause):
                """One aggregate query: total_runs, avg_latency, total_cost, total_tokens, success_rate.

                avg_latency matches the original Tier-1 semantics: sum(latency_ms or 0
                across ALL rows) / total_runs — zero-latency rows pull the average down,
                same as the original `sum(...) / total_runs` Python loop.
                """
                row = session.query(
                    func.count(ModelUsage.id).label("total_runs"),
                    func.coalesce(func.sum(func.coalesce(ModelUsage.latency_ms, 0)), 0).label("latency_sum"),
                    func.coalesce(func.sum(func.coalesce(ModelUsage.cost_usd, 0)), 0).label("total_cost"),
                    func.coalesce(func.sum(func.coalesce(ModelUsage.total_tokens, 0)), 0).label("total_tokens"),
                    func.count(case((func.coalesce(ModelUsage.latency_ms, 0) > 0, 1))).label("success_count"),
                    func.coalesce(
                        func.sum(case((func.coalesce(ModelUsage.latency_ms, 0) > 0, ModelUsage.latency_ms), else_=0)),
                        0,
                    ).label("success_latency_sum"),
                ).filter(filter_clause).one()
                total_runs = row.total_runs or 0
                avg_latency = (row.latency_sum / total_runs) if total_runs else 0
                success_rate = (row.success_count / total_runs * 100) if total_runs else 0
                # Average latency of successful (latency_ms > 0) rows only — used by the
                # Tier-2 SDLC-actor-mapping branch below, which historically averaged only
                # over rows with recorded latency, not the whole table.
                avg_latency_success = (row.success_latency_sum / row.success_count) if row.success_count else 0
                return total_runs, avg_latency, float(row.total_cost or 0), int(row.total_tokens or 0), success_rate, avg_latency_success

            def _model_breakdown(filter_clause):
                rows = (
                    session.query(ModelUsage.model, func.count(ModelUsage.id))
                    .filter(filter_clause)
                    .group_by(ModelUsage.model)
                    .all()
                )
                counts: dict = {}
                for raw_model, cnt in rows:
                    m = _norm_model(raw_model or "")
                    counts[m] = counts.get(m, 0) + cnt
                return counts

            # ── Model usage: match by agent_id OR by endpoint recorded under this name ──
            # agent_id column is set to the agent name when agents run via AgentRunner.
            # For system-level callers (orchestrator, ide_direct) we fall back to those.
            usage_filter = ModelUsage.agent_id == name
            total_runs, avg_latency, total_cost, total_tokens, success_rate, _ = _agg_usage(usage_filter)
            has_direct_usage = total_runs > 0

            # If no direct agent_id match, try common aliases
            if not has_direct_usage:
                _aliases = {
                    "orchestrator": [None, "orchestrator"],
                    "ide_direct":   ["ide_direct"],
                }
                if name in _aliases:
                    filters = [ModelUsage.agent_id == a for a in _aliases[name] if a is not None]
                    if None in _aliases[name]:
                        filters.append(ModelUsage.agent_id.is_(None))
                    usage_filter = or_(*filters)
                    total_runs, avg_latency, total_cost, total_tokens, success_rate, _ = _agg_usage(usage_filter)
                    has_direct_usage = total_runs > 0

            # SDLC pipeline actor mapping: registered agent names → internal pipeline roles.
            # LLM calls from SDLC pipeline stages are stored under the actor name (e.g.
            # "ai-coder"), not the registered agent name ("sdlc-coding-agent"). Use
            # SDLCRunEvent counts as the authoritative total_runs, and derive cost/token
            # figures from platform-wide per-call averages.
            _SDLC_ACTOR_MAP = {
                "sdlc-coding-agent":       ["ai-coder"],
                "sdlc-feature-analyst":    ["ai-analyst"],
                "sdlc-bug-triager":        ["ai-analyst"],
                "sdlc-troubleshooter":     ["ai-troubleshooter"],
                "sdlc-feature-classifier": ["ai-classifier"],
                "sdlc-solution-reviewer":  ["ai-solution-reviewer", "ai-completion-reviewer"],
                "sdlc-pr-reviewer":        ["ai-pr-reviewer"],
                "sdlc-solution-designer":  ["ai-solutioning-agent"],
                "incident_responder":      ["ai-troubleshooter"],
                "code_reviewer":           ["ai-code-reviewer"],
                "code-reviewer":           ["ai-code-reviewer"],
                "general-assistant":       ["ai-slt-creator", "ai-slt-reviewer"],
            }

            _mapped_actors = []
            _sdlc_event_count = 0
            if not has_direct_usage and name in _SDLC_ACTOR_MAP:
                _mapped_actors = _SDLC_ACTOR_MAP[name]
                _sdlc_event_count = session.query(func.count(SDLCRunEvent.id)).filter(
                    SDLCRunEvent.actor.in_(_mapped_actors)
                ).scalar() or 0

            model_counts: dict = {}
            if has_direct_usage:
                model_counts = _model_breakdown(usage_filter)
            elif _sdlc_event_count > 0:
                # Derive per-event averages from platform-wide model_usages via one
                # aggregate query — NOT session.query(ModelUsage).all(). Original code
                # averaged latency only over rows with a recorded (>0) latency, so use
                # avg_latency_success (not the whole-table avg_latency) here.
                _plat_runs, _, _plat_total_cost, _plat_total_tokens, _, _plat_avg_lat_success = _agg_usage(
                    ModelUsage.id.isnot(None)
                )
                _plat_runs = _plat_runs or 1
                _avg_cost_per_run   = _plat_total_cost / _plat_runs
                _avg_tokens_per_run = _plat_total_tokens / _plat_runs

                total_runs   = _sdlc_event_count
                avg_latency  = _plat_avg_lat_success
                total_cost   = round(_sdlc_event_count * _avg_cost_per_run, 6)
                total_tokens = int(_sdlc_event_count * _avg_tokens_per_run)
                success_rate = 100.0
                # SDLC pipeline primarily uses Claude Sonnet for complex reasoning
                model_counts = {"Claude Sonnet 4.6": _sdlc_event_count}
            else:
                total_runs   = 0
                avg_latency  = 0
                total_cost   = 0
                total_tokens = 0
                success_rate = 0

            # ── Agent metadata from agents_pg ──────────────────────
            agent_meta = {}
            try:
                rec = session.query(AgentRecord).filter(AgentRecord.name == name).first()
                if rec:
                    agent_meta = {
                        "description":   rec.description or "",
                        "status":        getattr(rec, "status", ""),
                        "is_production": getattr(rec, "is_production", False),
                        "stage":         getattr(rec, "stage", ""),
                        "owner":         rec.owner or "",
                        "version":       rec.version or "",
                    }
            except Exception as _e:
                logger.warning(f"analytics: agent metadata load failed: {_e}")

            # ── Recent SDLC events as live logs ────────────────────
            recent_events = []
            try:
                # Try direct actor match first; fall back to SDLC actor mapping
                _actor_filter = SDLCRunEvent.actor == name
                if _mapped_actors:
                    from sqlalchemy import or_ as _or2
                    _actor_filter = _or2(
                        SDLCRunEvent.actor == name,
                        SDLCRunEvent.actor.in_(_mapped_actors),
                    )
                events = (
                    session.query(SDLCRunEvent)
                    .filter(_actor_filter)
                    .order_by(desc(SDLCRunEvent.created_at))
                    .limit(20)
                    .all()
                )
                recent_events = [
                    {
                        "stage":      e.stage,
                        "from_state": e.from_state,
                        "to_state":   e.to_state,
                        "output":     (e.output or "")[:200],
                        "created_at": e.created_at.isoformat() if e.created_at else "",
                    }
                    for e in events
                ]
            except Exception as _e:
                logger.warning(f"analytics: SDLC events load failed: {_e}")

            # ── Platform context for agents with 0 direct runs ────
            # One aggregate query for the totals plus one GROUP BY for the model
            # mix — never a full `.all()` of model_usages (can be ~1.7M+ rows).
            platform_context = {}
            if total_runs == 0:
                try:
                    _plat_runs, _, _plat_total_cost, _plat_total_tokens, _, _ = _agg_usage(
                        ModelUsage.id.isnot(None)
                    )
                    platform_context = {
                        "platform_total_calls":  _plat_runs,
                        "platform_total_tokens": _plat_total_tokens,
                        "platform_total_cost":   round(_plat_total_cost, 4),
                        "platform_model_dist":   _model_breakdown(ModelUsage.id.isnot(None)),
                    }
                except Exception:
                    pass

            return {
                "agent_name":        name,
                "total_runs":        total_runs,
                "avg_latency_ms":    round(avg_latency, 2),
                "success_rate_pct":  round(success_rate, 2),
                "total_cost_usd":    round(total_cost, 6),
                "total_tokens":      total_tokens,
                "model_usage":       model_counts,
                "live_logs":         recent_events,
                "agent_meta":        agent_meta,
                "platform_context":  platform_context,
            }

        finally:
            session.close()

    except Exception as e:
        logger.warning(f"agent_analytics failed for '{name}': {e}")
        return {
            "agent_name":       name,
            "total_runs":       0,
            "avg_latency_ms":   0,
            "success_rate_pct": 0,
            "total_cost_usd":   0,
            "total_tokens":     0,
            "model_usage":      {},
            "live_logs":        [],
            "agent_meta":       {},
            "note":             f"Analytics unavailable: {e}",
        }


def _resolve_platform_window(granularity: str, now):
    """Resolve (window_start, window_end, series_bucket, window_label) for a
    Platform Overview granularity tab — mirrors the Day/Week/Month/Quarter
    control already shipped on Cloud Usage, but with rolling (not calendar)
    week/quarter windows since this tab is a live operational view, not a
    billing-reconciliation one:

    - day:     last 24 hours,        hourly buckets
    - week:    last 7 days,          daily buckets
    - month:   current calendar month (1st @ 00:00 IST → now), daily buckets
    - quarter: last 3 months (rolling 90 days), daily buckets
    """
    from datetime import timedelta

    if granularity == "day":
        return now - timedelta(hours=24), now, "hour", "Last 24 hours"
    if granularity == "week":
        return now - timedelta(days=7), now, "day", "Last 7 days"
    if granularity == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now, "day", f"{now.strftime('%B %Y')} (month to date)"
    if granularity == "quarter":
        return now - timedelta(days=90), now, "day", "Last 3 months"
    raise HTTPException(400, f"invalid granularity: {granularity!r}")


@analytics_router.get("/analytics/platform")
def platform_analytics(
    granularity: str = Query("day", pattern="^(day|week|month|quarter)$"),
    current_user: dict = Depends(_require_role("admin")),
):
    """
    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing
    platform-wide request/token/cost totals, top agents, and model
    distribution to any anonymous caller.
    Access control: admin-only, matching the sidebar's "Observe" section
    (Monitoring/Analytics/Eval Observatory), which is restricted to
    admins.

    Platform-wide analytics dashboard, scoped to a Day/Week/Month/Quarter
    window selected by the caller (default: day = last 24 h) — mirroring the
    granularity control already on the Cloud Usage tab.

    - Requests/tokens/cost totals for the window, with a trend vs. the
      immediately preceding window of the same length.
    - Top agents by usage, model distribution, and SDLC pipeline summary —
      all scoped to the window.
    - A time series (hourly for "day", daily otherwise) across the window.

    Everything is computed with SQL-side aggregation (COUNT/SUM/AVG/GROUP BY,
    following the pattern already used correctly in
    routers/dept_metrics_router.py) and — critically — every query below is
    filtered on `created_at` so Postgres can prune `model_usages`'
    monthly partitions and use its indexes.

    The previous implementation ran `session.query(ModelUsage).all()` with no
    WHERE/LIMIT at all, and `top_agents`/`model_dist` had no date filter
    either — against ~1.7M rows that meant hydrating up to 1.7M ORM objects
    and then walking the resulting list 10+ times in Python (14 full passes
    for the 7-day chart alone) on *every* Analytics tab open, defeating
    partition pruning and all five indexes on the table. That took 60–80 s
    per request; this version issues small, bounded, partition-prunable
    aggregate queries scoped to only the selected window instead.
    """
    import json as _json
    from datetime import timedelta
    from core.time_utils import now_ist

    # A short cache collapses the "N open tabs polling every 30 s" amplifier
    # from §7 into a single query cycle per granularity. Cache is best-effort:
    # any failure to read/write it just falls through to a live (now fast)
    # query.
    _cache_key = f"analytics:platform:v3:{granularity}"
    _kv = None
    try:
        from core.kv import get_kv
        from core.config import RDB_CACHE
        _kv = get_kv(RDB_CACHE, decode_responses=True)
        _cached = _kv.get(_cache_key)
        if _cached:
            return _json.loads(_cached)
    except Exception:
        pass

    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text

        session = SessionLocal()
        try:
            # ModelUsage.created_at is stored as naive IST (db/models.py
            # _now_ist()), unlike every other table's UTC created_at. The
            # previous code computed these windows with datetime.utcnow(),
            # which silently shifted every time-bucketed figure on this tab
            # by 5h30m (§8 defect #1). Use IST "now" so windows line up with
            # what is actually stored.
            now = now_ist()
            window_start, window_end, series_bucket, window_label = _resolve_platform_window(granularity, now)

            # Previous window of the same length, for the trend pill —
            # matches the Cloud Usage tab's `_previous_window` pattern.
            span = window_end - window_start
            prev_end   = window_start
            prev_start = window_start - span

            # ── Current + previous window totals in ONE bounded query.
            # WHERE created_at >= prev_start keeps the whole query inside a
            # single scan that Postgres can still prune by partition; the two
            # FILTER clauses split it into "current" vs "previous" without a
            # second round trip. ──
            totals_row = session.execute(_text("""
                SELECT
                    COUNT(*) FILTER (WHERE created_at >= :window_start AND created_at < :window_end) AS cur_requests,
                    COALESCE(SUM(total_tokens) FILTER (WHERE created_at >= :window_start AND created_at < :window_end), 0) AS cur_tokens,
                    COALESCE(SUM(cost_usd)     FILTER (WHERE created_at >= :window_start AND created_at < :window_end), 0) AS cur_cost,
                    COUNT(*) FILTER (WHERE created_at >= :prev_start AND created_at < :prev_end) AS prev_requests,
                    COALESCE(SUM(total_tokens) FILTER (WHERE created_at >= :prev_start AND created_at < :prev_end), 0) AS prev_tokens,
                    COALESCE(SUM(cost_usd)     FILTER (WHERE created_at >= :prev_start AND created_at < :prev_end), 0) AS prev_cost
                FROM model_usages
                WHERE created_at >= :prev_start AND created_at < :window_end
            """), {
                "window_start": window_start, "window_end": window_end,
                "prev_start": prev_start, "prev_end": prev_end,
            }).one()

            def _pct_change(cur, prev):
                if not prev:
                    return None if not cur else 100.0
                return round((cur - prev) / prev * 100, 1)

            cur_requests = totals_row.cur_requests or 0
            cur_tokens   = float(totals_row.cur_tokens or 0)
            cur_cost     = float(totals_row.cur_cost or 0)
            prev_requests = totals_row.prev_requests or 0
            prev_tokens   = float(totals_row.prev_tokens or 0)
            prev_cost     = float(totals_row.prev_cost or 0)

            totals = {
                "requests": cur_requests,
                "tokens":   cur_tokens,
                "cost_usd": round(cur_cost, 6),
            }
            comparison = {
                "requests_pct_change": _pct_change(cur_requests, prev_requests),
                "tokens_pct_change":   _pct_change(cur_tokens, prev_tokens),
                "cost_pct_change":     _pct_change(cur_cost, prev_cost),
            }

            # ── Top agents (by request count, scoped to the window) ──
            top_rows = session.execute(_text("""
                SELECT
                    COALESCE(agent_id, 'orchestrator') AS agent,
                    COUNT(*)                           AS requests,
                    COALESCE(SUM(cost_usd), 0)          AS cost_usd
                FROM model_usages
                WHERE created_at >= :window_start AND created_at < :window_end
                GROUP BY COALESCE(agent_id, 'orchestrator')
                ORDER BY requests DESC
                LIMIT 10
            """), {"window_start": window_start, "window_end": window_end}).fetchall()
            top_agents = [
                {"agent": r.agent, "requests": r.requests, "cost_usd": round(float(r.cost_usd or 0), 4)}
                for r in top_rows
            ]

            # ── Model distribution (scoped to the window) — GROUP BY in
            # SQL, normalize only the small grouped result in Python rather
            # than every row. ──
            model_rows = session.execute(_text("""
                SELECT model, COUNT(*) AS requests
                FROM model_usages
                WHERE created_at >= :window_start AND created_at < :window_end
                GROUP BY model
            """), {"window_start": window_start, "window_end": window_end}).fetchall()
            model_dist: dict = {}
            for r in model_rows:
                m = _norm_model(r.model or "")
                model_dist[m] = model_dist.get(m, 0) + r.requests

            # ── Time series across the window — hourly buckets for "day",
            # daily buckets otherwise. Single GROUP BY query, gap-filled in
            # Python against the small (<=24 or <=~92 point) series, never a
            # per-bucket round trip. ──
            series = []
            if series_bucket == "hour":
                rows = session.execute(_text("""
                    SELECT date_trunc('hour', created_at) AS bucket, COUNT(*) AS requests
                    FROM model_usages
                    WHERE created_at >= :window_start AND created_at < :window_end
                    GROUP BY bucket
                """), {"window_start": window_start, "window_end": window_end}).fetchall()
                by_bucket = {r.bucket.strftime("%Y-%m-%d %H:00"): r.requests for r in rows}
                # Buckets run from the floor of window_start to the floor of
                # window_end inclusive — matches exactly what
                # date_trunc('hour', ...) can produce, so no partial first/
                # last hour of data is ever silently dropped from the chart.
                first_bucket = window_start.replace(minute=0, second=0, microsecond=0)
                last_bucket  = window_end.replace(minute=0, second=0, microsecond=0)
                n_hours = int((last_bucket - first_bucket).total_seconds() // 3600) + 1
                for h in range(n_hours):
                    bucket_dt = first_bucket + timedelta(hours=h)
                    key = bucket_dt.strftime("%Y-%m-%d %H:00")
                    series.append({"label": bucket_dt.strftime("%H:00"), "requests": by_bucket.get(key, 0)})
            else:
                rows = session.execute(_text("""
                    SELECT date_trunc('day', created_at) AS bucket,
                           COUNT(*) AS requests,
                           COALESCE(SUM(cost_usd), 0) AS cost_usd
                    FROM model_usages
                    WHERE created_at >= :window_start AND created_at < :window_end
                    GROUP BY bucket
                """), {"window_start": window_start, "window_end": window_end}).fetchall()
                by_bucket = {r.bucket.strftime("%Y-%m-%d"): (r.requests, float(r.cost_usd or 0)) for r in rows}
                n_days = (window_end.date() - window_start.date()).days + 1
                for d in range(n_days):
                    bucket_date = window_start.date() + timedelta(days=d)
                    if bucket_date > window_end.date():
                        break
                    key = bucket_date.strftime("%Y-%m-%d")
                    req, cost = by_bucket.get(key, (0, 0.0))
                    series.append({"label": bucket_date.strftime("%b %d"), "requests": req, "cost_usd": round(cost, 4)})

            # ── SDLC summary (scoped to the window) ─────────────
            sdlc_summary: dict = {}
            try:
                sdlc_rows = session.execute(_text("""
                    SELECT state, COUNT(*) AS cnt
                    FROM sdlc_runs
                    WHERE created_at >= :window_start AND created_at < :window_end
                    GROUP BY state
                """), {"window_start": window_start, "window_end": window_end}).fetchall()
                sdlc_summary = {r.state: r.cnt for r in sdlc_rows}
            except Exception:
                pass

            # ── Agent counts — small table (agents_pg), not time-series
            # data, so a plain COUNT is cheap regardless of window. ──
            agent_stats = {"total": 0, "production": 0}
            try:
                arow = session.execute(_text("""
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE is_production IS TRUE OR status = 'PRODUCTION') AS production
                    FROM agents_pg
                """)).one()
                agent_stats["total"]      = arow.total or 0
                agent_stats["production"] = arow.production or 0
            except Exception:
                pass

            result = {
                "granularity":        granularity,
                "window_start":       window_start.isoformat(),
                "window_end":         window_end.isoformat(),
                "window_label":       window_label,
                "totals":             totals,
                "comparison":         comparison,
                "top_agents":         top_agents,
                "model_dist":         model_dist,
                "series":             series,
                "series_granularity": series_bucket,
                "sdlc_summary":       sdlc_summary,
                "agent_stats":        agent_stats,
            }

            if _kv is not None:
                try:
                    _kv.set(_cache_key, _json.dumps(result), ex=60)
                except Exception:
                    pass

            return result

        finally:
            session.close()

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"platform_analytics failed: {e}")
        return {
            "granularity": granularity,
            "totals": {}, "comparison": {},
            "top_agents": [], "model_dist": {}, "series": [],
            "sdlc_summary": {}, "agent_stats": {},
            "note": f"Analytics unavailable: {e}",
        }


# ── Per-Agent tab — Agent Studio agents ─────────────────────────
# Agent Studio (AgentStudio/) persists its own agents in a dedicated `agents`
# table (see AgentStudio/backend/app/core/workflow_repo.py), separate from the
# legacy Agent Builder registry (`agents_pg` / AgentRecord). Both tables live
# in the same shared Postgres database/schema, so these endpoints join
# Agent Studio's `agents` table against `model_usages` directly — the same
# raw-SQL, window-scoped aggregation pattern as `platform_analytics` above.
# Agent Studio runs are tagged `source_channel = 'AGENT-STUDIO'` and
# `agent_id = <Agent Studio agent id>` by
# AgentStudio/backend/agent_factory/pipeline.py::_record_model_usage.

@analytics_router.get("/analytics/agent-studio-agents")
def agent_studio_agents(
    granularity: str = Query("day", pattern="^(day|week|month|quarter)$"),
    current_user: dict = Depends(_require_role("admin")),
):
    """List unique Agent Studio agent NAMES with a users/usage/cost badge.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing every
    Agent Studio agent name plus usage/cost figures to any anonymous
    caller.
    Access control: admin-only, matching the sidebar's "Observe" section
    (Monitoring/Analytics/Eval Observatory), which is restricted to
    admins.

    Agent Studio's `agents` table is name-unique only per owner — every user
    who clicks "Use Template" on e.g. "AppSec Reviewer" gets their own row
    with an identical name. Grouping by name here collapses all of those
    per-owner clones into a single list entry (`num_users` = how many
    distinct owners have a clone of this name; `total_runs`/`total_cost_usd`
    = summed across every one of those clones) so the list shows one line
    per template/agent name instead of one line per clone.
    """
    from core.time_utils import now_ist

    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text

        session = SessionLocal()
        try:
            now = now_ist()
            window_start, window_end, _bucket, window_label = _resolve_platform_window(granularity, now)

            rows = session.execute(_text("""
                SELECT a.name,
                       COUNT(DISTINCT a.owner_user_id)    AS num_users,
                       COUNT(DISTINCT a.id)                AS num_instances,
                       COALESCE(SUM(u.total_runs), 0)      AS total_runs,
                       COALESCE(SUM(u.total_cost_usd), 0)  AS total_cost_usd
                FROM agents a
                LEFT JOIN (
                    SELECT agent_id,
                           COUNT(*)                    AS total_runs,
                           COALESCE(SUM(cost_usd), 0)  AS total_cost_usd
                    FROM model_usages
                    WHERE source_channel = 'AGENT-STUDIO'
                      AND created_at >= :window_start AND created_at < :window_end
                    GROUP BY agent_id
                ) u ON u.agent_id = a.id
                GROUP BY a.name
                ORDER BY total_runs DESC, a.name ASC
                LIMIT 500
            """), {"window_start": window_start, "window_end": window_end}).fetchall()

            agents = [
                {
                    "name":            r.name,
                    "num_users":       r.num_users or 0,
                    "num_instances":   r.num_instances or 0,
                    "total_runs":      r.total_runs or 0,
                    "total_cost_usd":  round(float(r.total_cost_usd or 0), 4),
                }
                for r in rows
            ]
            return {"granularity": granularity, "window_label": window_label, "agents": agents}
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"agent_studio_agents failed: {e}")
        return {"granularity": granularity, "agents": [], "note": f"Agent Studio agents unavailable: {e}"}


@analytics_router.get("/analytics/agent-studio-agents/{name}")
def agent_studio_agent_detail(
    name: str,
    granularity: str = Query("day", pattern="^(day|week|month|quarter)$"),
    current_user: dict = Depends(_require_role("admin")),
):
    """Analytics for one unique Agent Studio agent NAME, scoped to a window.

    SECURITY (AppSec finding — Information Disclosure / CWE-200, CWE-306):
    this endpoint previously had no auth dependency at all, exposing a
    named agent's owner count, usage, and cost figures to any anonymous
    caller.
    Access control: admin-only, matching the sidebar's "Observe" section
    (Monitoring/Analytics/Eval Observatory), which is restricted to
    admins.

    Aggregates across every owner's clone of this name (see the list endpoint
    above for why there can be more than one underlying agent id per name).
    """
    from core.time_utils import now_ist

    try:
        from db.database import SessionLocal
        from sqlalchemy import text as _text, bindparam

        session = SessionLocal()
        try:
            now = now_ist()
            window_start, window_end, _bucket, window_label = _resolve_platform_window(granularity, now)

            instance_rows = session.execute(_text("""
                SELECT id, owner_user_id, created_at, description
                FROM agents WHERE name = :name
                ORDER BY created_at ASC
            """), {"name": name}).fetchall()
            if not instance_rows:
                raise HTTPException(status_code=404, detail=f"Agent Studio agent '{name}' not found")

            agent_ids = [r.id for r in instance_rows]
            num_users = len({r.owner_user_id for r in instance_rows})
            description = next((r.description for r in instance_rows if r.description), "")
            agent_meta = {
                "name":          name,
                "num_users":     num_users,
                "num_instances": len(instance_rows),
                "description":   description,
                "created_at":    instance_rows[0].created_at.isoformat() if instance_rows[0].created_at else None,
            }

            _usage_sql = _text("""
                SELECT
                    COUNT(*) AS total_runs,
                    COALESCE(SUM(COALESCE(latency_ms, 0)), 0) AS latency_sum,
                    COALESCE(SUM(COALESCE(cost_usd, 0)), 0)   AS total_cost,
                    COALESCE(SUM(COALESCE(total_tokens, 0)), 0) AS total_tokens,
                    COUNT(*) FILTER (WHERE COALESCE(latency_ms, 0) > 0) AS success_count
                FROM model_usages
                WHERE agent_id IN :agent_ids AND source_channel = 'AGENT-STUDIO'
                  AND created_at >= :window_start AND created_at < :window_end
            """).bindparams(bindparam("agent_ids", expanding=True))
            usage_row = session.execute(_usage_sql, {
                "agent_ids": agent_ids, "window_start": window_start, "window_end": window_end,
            }).one()

            total_runs   = usage_row.total_runs or 0
            avg_latency  = (usage_row.latency_sum / total_runs) if total_runs else 0
            # Success is a proxy, not an explicit status column: model_usages
            # has no success/error field, so a call is counted as successful
            # when it recorded a non-zero latency_ms (i.e. the LLM actually
            # returned a response instead of erroring before completion).
            success_rate = (usage_row.success_count / total_runs * 100) if total_runs else 0

            _model_sql = _text("""
                SELECT model, COUNT(*) AS cnt
                FROM model_usages
                WHERE agent_id IN :agent_ids AND source_channel = 'AGENT-STUDIO'
                  AND created_at >= :window_start AND created_at < :window_end
                GROUP BY model
            """).bindparams(bindparam("agent_ids", expanding=True))
            model_rows = session.execute(_model_sql, {
                "agent_ids": agent_ids, "window_start": window_start, "window_end": window_end,
            }).fetchall()
            model_usage: dict = {}
            for r in model_rows:
                m = _norm_model(r.model or "")
                model_usage[m] = model_usage.get(m, 0) + r.cnt

            return {
                "name":             name,
                "granularity":      granularity,
                "window_label":     window_label,
                "total_runs":       total_runs,
                "avg_latency_ms":   round(avg_latency, 2),
                "success_rate_pct": round(success_rate, 2),
                "total_cost_usd":   round(float(usage_row.total_cost or 0), 6),
                "total_tokens":     int(usage_row.total_tokens or 0),
                "model_usage":      model_usage,
                "agent_meta":       agent_meta,
            }
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"agent_studio_agent_detail failed for '{name}': {e}")
        return {
            "name":             name,
            "granularity":      granularity,
            "total_runs":       0,
            "avg_latency_ms":   0,
            "success_rate_pct": 0,
            "total_cost_usd":   0,
            "total_tokens":     0,
            "model_usage":      {},
            "agent_meta":       {},
            "note":             f"Analytics unavailable: {e}",
        }
