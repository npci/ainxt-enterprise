# SPDX-License-Identifier: MIT
"""
agents/sdlc_governance/config.py — Step 1: governance config (call-time env readers).

All values are read fresh on every call (never cached at import time), so a
deploy-time env flip needs no restart — mirrors the idiom used across
agents/sdlc_cli_utils.py and agents/sdlc_cli_engine.py.

Import side-effect-free: only stdlib + core.logger (+ core.model_registry,
which is a cheap constants-only module — no Postgres/Redis/Docker/network
import) at module import time.

Governance model knobs
-----------------------
SDLC_GOVERNANCE_REVIEW_MODEL — concrete model id used by review_model() for the
    governance SCAN (analyzer skill sessions). Unset/empty → CLAUDE_PRIMARY_MODEL
    (the platform Sonnet workhorse).
SDLC_GOVERNANCE_FIX_MODEL — concrete model id used by fix_model() for the
    author-fix CLI session (run_governance_author_fix in agents/sdlc_pipeline.py).
    Unset/empty → the exact model the fixer used before this knob existed,
    cli_model_for("coder") from core/model_registry.py, so leaving it unset is a
    strict no-op. Must never resolve to a BLOCKED_MODELS entry.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Union

from agents.sdlc_cli_utils import _env_str, _env_int
from core.logger import logger

# core.model_registry only defines env-var-backed string constants (no heavy
# imports, no I/O) — a top-level import is safe and keeps this module simple.
from core.model_registry import (
    CLAUDE_PRIMARY_MODEL, CLAUDE_OPUS_MODEL, CLAUDE_OPUS_46_MODEL,
    BLOCKED_MODELS, cli_model_for, cli_model_for_tier,
)


def _env_bool(name: str, default: bool) -> bool:
    """Same bool-coercion idiom as CliEngineConfig.from_env() in sdlc_cli_engine.py."""
    default_str = "true" if default else "false"
    return _env_str(name, default_str).strip().lower() in ("1", "true", "yes")


def source() -> str:
    """SDLC_GOVERNANCE_SOURCE: "git" | "path" | "auto" (default)."""
    raw = _env_str("SDLC_GOVERNANCE_SOURCE", "auto").strip().lower()
    if raw not in ("git", "path", "auto"):
        return "auto"
    return raw


def git_url() -> str:
    return _env_str("SDLC_GOVERNANCE_GIT_URL", "")


def git_ref() -> str:
    """Empty string means pin-to-default-branch-HEAD (the resolved commit sha
    is recorded on the returned Bundle regardless)."""
    return _env_str("SDLC_GOVERNANCE_GIT_REF", "")


def bundle_path() -> str:
    return _env_str("SDLC_GOVERNANCE_PATH", "")


def pin_version() -> bool:
    return _env_bool("SDLC_GOVERNANCE_PIN", True)


def max_iters() -> int:
    return _env_int("SDLC_GOVERNANCE_MAX_ITERS", 3)


def convergence_stall_limit() -> int:
    """End-gate author-loop convergence guard (2026-07-23). Max consecutive
    auto-fix re-scan iterations where the open-fingerprint set does NOT strictly
    shrink (or repeats) before the loop STOPS auto-fixing and surfaces a
    "not converging" banner to the author. Bounds runaway LLM spend / stuck
    worker slots alongside max_iters(). SDLC_GOVERNANCE_CONVERGENCE_STALL_LIMIT
    (default 2)."""
    return _env_int("SDLC_GOVERNANCE_CONVERGENCE_STALL_LIMIT", 2)


def block_severity() -> str:
    return _env_str("SDLC_GOVERNANCE_BLOCK_SEVERITY", "high")


def review_turns() -> int:
    """Max CLI tool-call turns for one governance review session. A diff-only
    read that may also run a skill's helper scripts — a modest cap is enough."""
    return _env_int("SDLC_GOVERNANCE_REVIEW_TURNS", 40)


def awareness_enabled() -> bool:
    return _env_bool("SDLC_GOVERNANCE_AWARENESS", True)


def review_model() -> str:
    """Concrete model id for the governance SCAN reviewer.

    SDLC_GOVERNANCE_REVIEW_MODEL wins (any provider's concrete id). Unset →
    the "complex" workhorse tier via cli_model_for_tier, which honors the
    provider-agnostic SDLC_TIER_COMPLEX_MODEL override and the registry
    fallback, so this resolves on a harness with no Anthropic. Defaults to the
    platform Sonnet workhorse (CLAUDE_PRIMARY_MODEL) when nothing else is set."""
    return _env_str("SDLC_GOVERNANCE_REVIEW_MODEL", "").strip() or cli_model_for_tier("complex")


def fix_model() -> str:
    """SDLC_GOVERNANCE_FIX_MODEL: concrete model id for the governance
    author-fix CLI session (run_governance_author_fix in sdlc_pipeline.py).
    Unset/empty → cli_model_for("coder") from core.model_registry — the exact
    model the fixer used before this knob existed, so leaving it unset is a
    strict no-op.

    Guard: an env-supplied value still cannot resolve to a BLOCKED_MODELS entry.
    NOTE (conservative choice — ambiguity not covered by the plan): the shared
    guard helper cli_model_for_tier() (core/model_registry.py) takes a TIER
    name (e.g. "complex"), not a concrete model id — calling it with a raw env
    string would look up an unknown key and silently collapse to
    CLAUDE_PRIMARY_MODEL, discarding a legitimate override. So the guard
    predicate itself (BLOCKED_MODELS membership, ENABLE_OPUS-aware — mirrors
    cli_model_for_tier()) is replicated here against the raw concrete id
    instead of routing through it.
    """
    default_model = cli_model_for("coder")
    raw = _env_str("SDLC_GOVERNANCE_FIX_MODEL", "").strip()
    if not raw:
        logger.info("[SDLC-GOV] Resolved governance fixer model",
                     model=default_model, source="default")
        return default_model

    blocked = set(BLOCKED_MODELS)
    if os.getenv("ENABLE_OPUS", "true").strip().lower() not in ("true", "1", "yes"):
        blocked.add(CLAUDE_OPUS_MODEL)
        blocked.add(CLAUDE_OPUS_46_MODEL)

    model = default_model if raw in blocked else raw
    logger.info("[SDLC-GOV] Resolved governance fixer model", model=model, source="env")
    return model


def enabled() -> bool:
    return _env_bool("SDLC_GOVERNANCE_ENABLED", True)


# Per-phase skill selection (2026-07-17) — route DIFFERENT skills to PLAN vs
# IMPLEMENT vs the governance REVIEW. Each env var is a CSV of skill slugs; unset
# → no env override for that phase (fall back to the manifest `phases` tag, then
# to "applies to all phases"). This is the operator-simple path; the pluggable
# path is a `phases: [...]` field per skill in governance.manifest.(json|yml).
_PHASE_ENV = {
    "plan":      "SDLC_GOVERNANCE_PLAN_SKILLS",
    "implement": "SDLC_GOVERNANCE_IMPLEMENT_SKILLS",
    "review":    "SDLC_GOVERNANCE_REVIEW_SKILLS",
}

# Per-phase kill-switch (2026-08-16) — DISABLE the governance awareness block for
# a single CLI phase of the bug/feature pipeline, without touching the other
# phase. Scoped to the PLAN and IMPLEMENT phases ONLY: these are read via
# resolve_awareness(), the sole path used by the bug/feature pipeline's plan and
# implement CLI sessions. The governance REVIEW is deliberately excluded — it
# runs via select_skills(), not resolve_awareness(), and review with no skills
# would be meaningless, so it must never be disable-able here. Distinct from the
# per-phase skill SELECTION vars above: an empty selection CSV means "no override
# → all skills", so it cannot express "none". Each var is a bool (default false =
# awareness stays on).
_PHASE_DISABLE_ENV = {
    "plan":      "SDLC_GOVERNANCE_PLAN_DISABLED",
    "implement": "SDLC_GOVERNANCE_IMPLEMENT_DISABLED",
}


def phase_disabled(phase: str) -> bool:
    """True when the governance awareness block is disabled for a CLI phase
    ("plan"|"implement") via SDLC_GOVERNANCE_<PHASE>_DISABLED. Only these two
    phases are supported — "review" (and any unknown/empty phase) → False, so the
    governance review can never be disabled this way. Default False (awareness
    stays enabled)."""
    env = _PHASE_DISABLE_ENV.get((phase or "").strip().lower())
    if not env:
        return False
    return _env_bool(env, False)


def skills_for_phase(phase: str) -> Optional[list]:
    """Env-configured skill slugs for a phase ("plan"|"implement"|"review"), or
    None when no per-phase env var is set for it (→ caller falls back to the
    manifest `phases` tag, then to all skills)."""
    env = _PHASE_ENV.get((phase or "").strip().lower())
    if not env:
        return None
    return parse_subset(_env_str(env, ""))


def parse_subset(raw: Union[str, list, None]) -> Optional[list]:
    """Normalize a raw subset value (CSV string, list, or None) into a list of
    lowercase-stripped skill slugs, or None meaning "all skills"."""
    if raw is None:
        return None
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return None
    slugs = [str(item).strip().lower() for item in items if str(item).strip()]
    return slugs or None


def scan_turns() -> int:
    """Max CLI tool-call turns for one governance SCAN session (runs analyzers — slow).
    SDLC_GOVERNANCE_SCAN_TURNS (default 60)."""
    return _env_int("SDLC_GOVERNANCE_SCAN_TURNS", 60)


def scan_workers() -> int:
    """Max parallel CLI scan sessions (one thread per skill). 0 → unbounded (one per skill).
    SDLC_GOVERNANCE_SCAN_WORKERS (default 4)."""
    return _env_int("SDLC_GOVERNANCE_SCAN_WORKERS", 4)


def scan_profile() -> str:
    """CLI profile for governance scan sessions. Default 'govscan'.
    SDLC_GOVERNANCE_SCAN_PROFILE."""
    return _env_str("SDLC_GOVERNANCE_SCAN_PROFILE", "govscan")


def require_binaries() -> bool:
    """When True, a skill whose SKILL.md references a binary that is absent on
    the worker host causes the scan session to SUSPEND (fail-closed), never
    silently PASS. SDLC_GOVERNANCE_REQUIRE_BINARIES (default true)."""
    return _env_bool("SDLC_GOVERNANCE_REQUIRE_BINARIES", True)


def max_diff_files() -> int:
    """Hard cap on the number of changed files a single governance run will
    review/scan. A diff above this is SUSPENDED for manual governance review
    rather than attempted: it is too large for one meaningful automated pass and
    the (huge) diff would otherwise overflow the CLI ``--print`` argv token.
    SDLC_GOVERNANCE_MAX_DIFF_FILES (default 100). 0 or negative → cap disabled."""
    return _env_int("SDLC_GOVERNANCE_MAX_DIFF_FILES", 100)


def max_diff_bytes() -> int:
    """Hard cap on the unified-diff SIZE (bytes) a single governance run will
    review/scan — a secondary guard for a small-file-count but pathologically
    large diff. SDLC_GOVERNANCE_MAX_DIFF_BYTES (default 1_500_000 ≈ 1.5 MB, well
    under a typical ARG_MAX). 0 or negative → cap disabled."""
    return _env_int("SDLC_GOVERNANCE_MAX_DIFF_BYTES", 1_500_000)


def diff_cap_exceeded(changed_files, diff_text) -> Optional[str]:
    """Return a human-readable reason string when the diff is too large for an
    automated governance run (→ the caller SUSPENDS for manual review), or None
    when it is within limits. File count is checked first, then byte size. Both
    caps are call-time env-readable and independently disableable (≤0)."""
    n_files = len(changed_files or [])
    fcap = max_diff_files()
    if fcap > 0 and n_files > fcap:
        return (
            f"diff too large for automated governance review: {n_files} changed "
            f"files exceeds the cap of {fcap} (SDLC_GOVERNANCE_MAX_DIFF_FILES). "
            "Split the change into smaller merge requests, or request a manual "
            "governance review."
        )
    n_bytes = len((diff_text or "").encode("utf-8", errors="ignore"))
    bcap = max_diff_bytes()
    if bcap > 0 and n_bytes > bcap:
        return (
            f"diff too large for automated governance review: {n_bytes} bytes "
            f"exceeds the cap of {bcap} (SDLC_GOVERNANCE_MAX_DIFF_BYTES). Split the "
            "change into smaller merge requests, or request a manual governance review."
        )
    return None
