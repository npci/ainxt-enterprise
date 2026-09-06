# SPDX-License-Identifier: MIT
# ============================================================
# SDLC CLI-loop budget guard — thin layer over the existing HOD
# per-run budget tracker (services.sdlc_budget_tracker /
# services.hod_budget_governor).
#
# CONTEXT / DECISION: the plan's default was a new pair of env knobs
# (SDLC_CLI_TOKEN_BUDGET / SDLC_CLI_COST_BUDGET_USD). The user chose
# instead to REUSE the existing HOD per-run budget infrastructure —
# this module is intentionally NOT a second, parallel budgeting
# system. It only:
#   * records CLI-loop token/cost usage onto the SAME sdlc_runs
#     columns everything else writes to (via record_llm_cost), and
#   * derives a --max-turns ceiling from the run's remaining HOD
#     allowance, so a long CLI loop self-throttles before the HOD
#     cap is blown, without introducing a second source of truth.
#
# Public API (consumed by Steps 3/5/6 of the CLI-loop rework):
#   record_cli_usage(run_id, usage, cost) -> None
#   remaining_budget(run_id, stage="")    -> (tokens_remaining, cost_remaining_usd)
#   derive_max_turns(remaining)           -> int
#   is_exhausted(run_id, stage="")        -> bool
#
# All functions are best-effort: on any error they log and return a
# safe/fail-open default. Budget accounting must never crash a run.
# ============================================================

from __future__ import annotations

from typing import Optional

from core.logger import logger

# ── Tunables (heuristics, not new budget knobs — see module docstring) ───────

# CLI hard soft-cap: never let derive_max_turns propose more than this,
# regardless of how much HOD budget remains. Mirrors the CLI engine's own
# ceiling so this module can't out-propose what the adapter will accept.
_MAX_TURNS_CEILING = 300

# Default max_turns when there is no ceiling to derive from (HOD enforcement
# off / shadow mode / no cap row) — enough headroom for a real IMPLEMENT loop
# without being unbounded.
_DEFAULT_MAX_TURNS = 120

# IMPLEMENT / REVIEW fix-round turn budget. Rather than a fixed cap, the PLAN
# phase emits `implement_max_turns` — it sized the change (files_to_change,
# new_files_needed, complexity), so it sizes the coder's turn budget. That
# estimate is clamped to a sane band and then lowered by the per-run HOD budget
# (budget can only ever REDUCE it). When the plan emits no usable estimate (old
# plans / omitted), fall back to _IMPLEMENT_TURNS_FALLBACK. An operator hard
# ceiling can be pinned via SDLC_CLI_IMPLEMENT_MAX_TURNS (default = the global
# _MAX_TURNS_CEILING, i.e. no extra restriction beyond the plan's own estimate).
_IMPLEMENT_TURNS_MIN      = 10   # floor — even a tiny change needs some turns
_IMPLEMENT_TURNS_FALLBACK = 60   # used only when the plan emits no estimate
_FIX_ROUND_TURNS_MAX      = 30   # a REVIEW fix-round is small — never needs more
_IMPLEMENT_TURNS_PER_FILE = 12   # per-file allowance for the file-count floor
_IMPLEMENT_TURNS_BASE     = 20   # base overhead (build/compile/test iteration)

# PLAN-phase turn budget, keyed by the ticket's classified complexity
# (simple/medium/complex — see agents.sdlc_normalizer / classification stage).
# Unknown/missing complexity falls back to the medium value.
_PLAN_TURNS_BY_COMPLEXITY = {"simple": 60, "medium": 120, "complex": 180}


def _implement_turns_operator_ceiling() -> int:
    """Optional operator hard ceiling on IMPLEMENT turns. Default = the global CLI
    ceiling (no extra restriction). Env SDLC_CLI_IMPLEMENT_MAX_TURNS; invalid or
    non-positive → the global ceiling. A positive value may also RAISE the
    effective ceiling, up to _MAX_TURNS_CEILING. Read at call time (no worker
    restart)."""
    import os
    raw = os.getenv("SDLC_CLI_IMPLEMENT_MAX_TURNS")
    if raw is None or not raw.strip():
        return _MAX_TURNS_CEILING
    try:
        v = int(raw)
    except ValueError:
        logger.warning(
            f"[SDLC-CLI-BUDGET] invalid SDLC_CLI_IMPLEMENT_MAX_TURNS={raw!r} — "
            f"using {_MAX_TURNS_CEILING}"
        )
        return _MAX_TURNS_CEILING
    return v if v > 0 else _MAX_TURNS_CEILING


def _implement_turn_multiplier() -> float:
    """Multiplier applied to the PLAN phase's implement_max_turns estimate so a
    turn-hungry coder (Sonnet) gets headroom. Env SDLC_CLI_IMPLEMENT_TURN_MULTIPLIER
    (default 1.5); invalid or <= 0 → 1.0 (no scaling). Read at call time."""
    import os
    raw = os.getenv("SDLC_CLI_IMPLEMENT_TURN_MULTIPLIER")
    if raw is None or not raw.strip():
        return 1.5
    try:
        v = float(raw)
    except ValueError:
        logger.warning(f"[SDLC-CLI-BUDGET] invalid SDLC_CLI_IMPLEMENT_TURN_MULTIPLIER={raw!r} — using 1.5")
        return 1.5
    return v if v > 0 else 1.0


def _implement_turns_per_file() -> int:
    """Per-file turn allowance for the file-count-based floor. Env
    SDLC_CLI_IMPLEMENT_TURNS_PER_FILE (default 12); invalid or <=0 → default."""
    import os
    raw = os.getenv("SDLC_CLI_IMPLEMENT_TURNS_PER_FILE")
    if raw is None or not raw.strip():
        return _IMPLEMENT_TURNS_PER_FILE
    try:
        v = int(raw)
    except ValueError:
        logger.warning(f"[SDLC-CLI-BUDGET] invalid SDLC_CLI_IMPLEMENT_TURNS_PER_FILE={raw!r} — using {_IMPLEMENT_TURNS_PER_FILE}")
        return _IMPLEMENT_TURNS_PER_FILE
    return v if v > 0 else _IMPLEMENT_TURNS_PER_FILE


def _implement_base_turns() -> int:
    """Base turn overhead added to the per-file floor. Env
    SDLC_CLI_IMPLEMENT_BASE_TURNS (default 20); invalid or <0 → default."""
    import os
    raw = os.getenv("SDLC_CLI_IMPLEMENT_BASE_TURNS")
    if raw is None or not raw.strip():
        return _IMPLEMENT_TURNS_BASE
    try:
        v = int(raw)
    except ValueError:
        logger.warning(f"[SDLC-CLI-BUDGET] invalid SDLC_CLI_IMPLEMENT_BASE_TURNS={raw!r} — using {_IMPLEMENT_TURNS_BASE}")
        return _IMPLEMENT_TURNS_BASE
    return v if v >= 0 else _IMPLEMENT_TURNS_BASE


def fix_round_ceiling() -> int:
    """The REVIEW fix-round turn ceiling, WITH the same multiplier headroom IMPLEMENT
    gets. A fix round is nominally bounded by ``_FIX_ROUND_TURNS_MAX``, but — like
    IMPLEMENT — a turn-hungry coder (Sonnet) gets ~1.5x headroom on top so it is not
    starved mid-fix and forced to ``error_max_turns`` (the fix-round prompt's STOP
    contract makes it terminate early once the flagged issues are green, so this is a
    safety cap, not a target). Uses SDLC_CLI_IMPLEMENT_TURN_MULTIPLIER (default 1.5);
    never returns below the nominal base."""
    return max(_FIX_ROUND_TURNS_MAX, round(_FIX_ROUND_TURNS_MAX * _implement_turn_multiplier()))


def resolve_implement_turns(plan_estimate, remaining, *, ceiling: int = None, file_count: int = 0) -> int:
    """Resolve --max-turns for an IMPLEMENT / fix-round CLI session.

    Turns = the HIGHER of (a) the PLAN estimate × multiplier (or the fallback when the
    plan emits none) and (b) a file-count floor (base + per-file × file_count). Floored
    at _IMPLEMENT_TURNS_MIN, clamped by the operator/caller ceiling, then lowered by the
    budget-derived ceiling (HOD budget can only ever REDUCE). file_count defaults to 0
    (fix/continue rounds don't pass it — they rely on (a) + the small ceiling they pass).
    Always returns a concrete int >= 1."""
    op_ceiling = _implement_turns_operator_ceiling()
    hard_ceiling = op_ceiling if ceiling is None else min(op_ceiling, ceiling)
    try:
        est = int(plan_estimate) if plan_estimate is not None else 0
    except (TypeError, ValueError):
        est = 0
    # (a) estimate-based
    if est > 0:
        est_turns = max(1, round(est * _implement_turn_multiplier()))
    else:
        est_turns = _IMPLEMENT_TURNS_FALLBACK
    # (b) file-count floor (0 when caller doesn't know the count — fix rounds)
    try:
        fc = max(0, int(file_count))
    except (TypeError, ValueError):
        fc = 0
    file_turns = (_implement_base_turns() + fc * _implement_turns_per_file()) if fc > 0 else 0
    # HIGHER of the two, floored, clamped by the hard ceiling
    resolved = max(_IMPLEMENT_TURNS_MIN, est_turns, file_turns)
    resolved = min(resolved, hard_ceiling)
    budget_ceiling = derive_max_turns(remaining)
    final = max(1, min(resolved, budget_ceiling))
    logger.info(
        "[SDLC-CLI-BUDGET] implement turns resolved",
        plan_estimate=plan_estimate, multiplier=_implement_turn_multiplier(),
        est_turns=est_turns, file_count=fc, file_turns=file_turns,
        pre_budget_resolved=resolved, hard_ceiling=hard_ceiling,
        budget_ceiling=budget_ceiling, resolved=final,
    )
    return final


def resolve_plan_turns(complexity, remaining) -> int:
    """Resolve --max-turns for a PLAN CLI session.

    Primary source is the ticket's classified `complexity` (simple/medium/
    complex), mapped via _PLAN_TURNS_BY_COMPLEXITY. Unknown, missing, or
    non-str complexity defaults to the medium value. The mapped value is then
    lowered by the budget-derived ceiling from derive_max_turns(remaining) so
    the HOD cap can only reduce it, never inflate it. Always returns a
    concrete int, 1 <= result <= _MAX_TURNS_CEILING."""
    medium_turns = _PLAN_TURNS_BY_COMPLEXITY["medium"]
    try:
        key = str(complexity).strip().lower()
        mapped = _PLAN_TURNS_BY_COMPLEXITY.get(key, medium_turns)
    except Exception:
        mapped = medium_turns

    budget_ceiling = derive_max_turns(remaining)
    resolved = min(mapped, budget_ceiling)
    resolved = max(1, min(resolved, _MAX_TURNS_CEILING))

    logger.info(
        "[SDLC-CLI-BUDGET] plan turns resolved",
        complexity=complexity, mapped_turns=mapped,
        budget_ceiling=budget_ceiling, resolved=resolved,
    )
    return resolved

# Heuristic average cost/tokens "spent" per CLI turn, used only to convert a
# remaining-budget figure into a turn count. These are deliberately rough —
# a CLI turn (one tool-call round trip) is usually a few thousand tokens of
# prompt+completion at the Sonnet workhorse rate. Tune later against real
# W0-style telemetry if it proves off.
_ASSUMED_TOKENS_PER_TURN = 8_000
_ASSUMED_COST_PER_TURN_USD = 0.15

# Conservative fallback cost-per-1M rates (Sonnet workhorse), used ONLY when
# the caller passes cost=0 (the fake CLI reports 0) and we must estimate cost
# from tokens. Mirrors core.model_registry.tier_cost_per_1m("complex") /
# sdlc_state_machine._llm's char/4 + tier-rate estimate. Loaded lazily from
# the single source of truth below; this constant is only the last-resort
# fallback if that import fails.
_FALLBACK_RATE_IN_PER_1M = 3.0
_FALLBACK_RATE_OUT_PER_1M = 15.0


def _estimate_cost_usd(tokens_in: int, tokens_out: int) -> float:
    """
    Best-effort cost estimate from token counts when the CLI reports cost=0.

    Reuses core.model_registry.tier_cost_per_1m("complex") — the Sonnet
    workhorse tier that the CLI-loop coder/fixer/test stages route through
    (CLAUDE.md Model Policy / SDLC_STAGE_MODEL_DEFAULTS) — as the single
    source of truth for per-token rates, same pattern as
    agents.sdlc_state_machine._llm's own cost estimate. Falls back to a
    hardcoded conservative Sonnet rate if the registry import fails for any
    reason (should not happen in practice).
    """
    try:
        from core.model_registry import tier_cost_per_1m
        rate_in, rate_out = tier_cost_per_1m("complex")
    except Exception:
        rate_in, rate_out = _FALLBACK_RATE_IN_PER_1M, _FALLBACK_RATE_OUT_PER_1M
    return (tokens_in / 1_000_000 * rate_in) + (tokens_out / 1_000_000 * rate_out)


def record_cli_usage(run_id: str, usage: dict, cost: float) -> None:
    """
    Record one CLI-loop turn's token/cost usage onto the run's existing HOD
    budget counters. Delegates to services.sdlc_budget_tracker.record_llm_cost
    — the SAME atomic UPDATE every other SDLC LLM call site uses — so this
    never becomes a second, competing counter.

    `usage` is the CliResult.usage shape: input_tokens, output_tokens, and
    optionally cache_read_input_tokens / cache_creation_input_tokens (summed
    into the input-token count, since they were still tokens the run paid
    prompt-processing cost for).

    If `cost` is falsy (the fake CLI reports 0, or a real CLI segment omits
    it), cost is estimated from tokens at the Sonnet-workhorse rate via
    _estimate_cost_usd().

    Best-effort: never raises. No-op if run_id is empty.
    """
    if not run_id:
        return
    usage = usage or {}
    try:
        tokens_in = int(usage.get("input_tokens", 0) or 0)
        tokens_in += int(usage.get("cache_read_input_tokens", 0) or 0)
        tokens_in += int(usage.get("cache_creation_input_tokens", 0) or 0)
        tokens_out = int(usage.get("output_tokens", 0) or 0)
    except Exception as exc:
        logger.warning(f"[SDLC-CLI-BUDGET] malformed usage dict run_id={run_id}: {exc}")
        return

    cost_usd = float(cost) if cost else 0.0
    if not cost_usd and (tokens_in or tokens_out):
        cost_usd = _estimate_cost_usd(tokens_in, tokens_out)

    try:
        from services.sdlc_budget_tracker import record_llm_cost
        record_llm_cost(tokens_in, tokens_out, round(cost_usd, 6), run_id=run_id)
    except Exception as exc:
        # Best-effort — budget accounting must never crash a run.
        logger.warning(f"[SDLC-CLI-BUDGET] record_cli_usage failed run_id={run_id}: {exc}")


def remaining_budget(run_id: str, stage: str = "") -> tuple:
    """
    Compute (tokens_remaining, cost_remaining_usd) for this run's HOD budget.

    tokens_remaining is always None today — the HOD governor tracks USD only,
    not a token ceiling — so this dimension is reserved for a future token-cap
    knob and is never used to block. cost_remaining_usd is the run's HOD
    remaining allowance (HOD monthly cap minus consumed-this-period), read via
    services.hod_budget_governor.get_cap_status(hod_email) for the hod_email
    stamped onto this run's sdlc_runs row at preflight.

    Returns (None, None) — "no ceiling" — whenever we can't determine a real
    figure: no run_id, no hod_email on the run (preflight didn't map one, or
    HOD enforcement was off), HOD enforcement disabled/shadow-mode, or any
    lookup error. This mirrors check_hod_budget()'s own shadow-mode semantics:
    the CLI loop must never self-block when the HOD gate itself wouldn't.

    Best-effort: never raises.
    """
    if not run_id:
        return (None, None)

    try:
        from services.hod_budget_governor import get_cap_status, _enforcement_enabled
        from db.database import SessionLocal
        from sqlalchemy import text

        if not _enforcement_enabled():
            # Shadow mode / HOD gate off — no ceiling, matches check_hod_budget().
            return (None, None)

        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT hod_email FROM sdlc_runs WHERE id = :id"),
                {"id": run_id},
            ).first()
        finally:
            db.close()

        hod_email = (row[0] if row else None) or ""
        if not hod_email:
            # No HOD mapped on this run (dept/HOD unresolved at preflight,
            # or enforcement was off then) — nothing to cap against.
            return (None, None)

        status = get_cap_status(hod_email)
        cost_remaining = float(status.remaining_usd)

        if cost_remaining <= 0:
            logger.warning(
                "[SDLC-CLI-BUDGET] per-run budget exhausted",
                run_id=run_id, stage=stage,
                tokens_used=None, cost_used=float(status.consumed_usd),
            )

        return (None, cost_remaining)
    except Exception as exc:
        logger.warning(f"[SDLC-CLI-BUDGET] remaining_budget lookup failed run_id={run_id}: {exc}")
        return (None, None)


def derive_max_turns(remaining) -> int:
    """
    Derive a --max-turns cap from the (tokens_remaining, cost_remaining_usd)
    tuple returned by remaining_budget(). Never exceeds the CLI soft cap of
    300 turns.

    Heuristic: assume ~_ASSUMED_COST_PER_TURN_USD spent per CLI turn (a
    tool-call round trip at the Sonnet-workhorse rate) and
    ~_ASSUMED_TOKENS_PER_TURN tokens per turn; divide the remaining budget by
    that per-turn assumption to get a turn count. When both dimensions are
    None (no ceiling — HOD off/shadow-mode, or nothing to cap against),
    return a bounded default (_DEFAULT_MAX_TURNS) rather than an unbounded
    loop — enough for a real IMPLEMENT loop but not open-ended.

    Always returns a concrete int (never None) so callers don't need a
    second fallback branch.
    """
    if not remaining or (remaining[0] is None and remaining[1] is None):
        return _DEFAULT_MAX_TURNS

    tokens_remaining, cost_remaining = remaining
    candidates = []

    if tokens_remaining is not None and _ASSUMED_TOKENS_PER_TURN > 0:
        candidates.append(max(0, int(tokens_remaining // _ASSUMED_TOKENS_PER_TURN)))

    if cost_remaining is not None and _ASSUMED_COST_PER_TURN_USD > 0:
        candidates.append(max(0, int(cost_remaining // _ASSUMED_COST_PER_TURN_USD)))

    if not candidates:
        return _DEFAULT_MAX_TURNS

    # Most restrictive dimension wins (never let one dimension's slack excuse
    # exhaustion on the other).
    turns = min(candidates)
    return max(0, min(turns, _MAX_TURNS_CEILING))


def is_exhausted(run_id: str, stage: str = "") -> bool:
    """
    Convenience check: True if any ceiling in remaining_budget(run_id) is
    <= 0. False (never exhausted) whenever remaining_budget can't determine
    a real ceiling (fail-open — the endpoint per-user budget still applies
    independently). Best-effort: never raises.
    """
    try:
        tokens_remaining, cost_remaining = remaining_budget(run_id, stage=stage)
    except Exception as exc:
        logger.warning(f"[SDLC-CLI-BUDGET] is_exhausted check failed run_id={run_id}: {exc}")
        return False

    if tokens_remaining is not None and tokens_remaining <= 0:
        return True
    if cost_remaining is not None and cost_remaining <= 0:
        return True
    return False
