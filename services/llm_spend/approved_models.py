# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.approved_models
#
# "Approved for tracking" = union of:
#   (1) every model id present in core.model_registry env defaults
#   (2) every model id observed in ainxt.model_usages in the trailing N days
#
# Anything outside this union is bucketed as 'other' when persisting
# llm_spend_daily rows. This deliberately does NOT consult
# dept_model_permissions — per the agreed plan, tracking is independent
# of who is allowed to use what. If a model has been used or is in the
# registry defaults, it gets tracked.
#
# Result is cached for the lifetime of one fetch run (cheap; runs ~5×/day).
# ============================================================

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Set

from sqlalchemy import text

from core.logger import logger
from db.database import SessionLocal


# Window for "recently observed" models in model_usages.
_RECENT_DAYS = int(os.getenv("LLM_SPEND_APPROVED_LOOKBACK_DAYS", "90"))


@dataclass
class ApprovedModels:
    """Whitelists keyed by provider."""
    openai:    Set[str] = field(default_factory=set)
    anthropic: Set[str] = field(default_factory=set)
    gemini:    Set[str] = field(default_factory=set)

    def bucket(self, provider: str, raw_model: str) -> str:
        """Return canonical model id if approved, else 'other'."""
        if not raw_model:
            return "other"
        norm = _normalise(raw_model)
        approved = self._for(provider)
        if norm in approved:
            return norm
        # Approved entries are normalised; raw could be a prefix match
        # (e.g. 'gpt-5.4-2026-06-01' vs registry 'gpt-5.4').
        for canon in approved:
            if norm.startswith(canon):
                return canon
        return "other"

    def _for(self, provider: str) -> Set[str]:
        return {
            "openai":    self.openai,
            "anthropic": self.anthropic,
            "gemini":    self.gemini,
        }.get(provider, set())


# ── normalisation ───────────────────────────────────────────────────────────

_NORMALISE_RE = re.compile(r"[^a-z0-9\-]")

# model_usages stores display-name strings like:
#   "GPT-5.4 (Coding) (gpt-5.4) [fallback]"
#   "Claude Opus 4.7 (claude-opus-4-7)"
#   "Gemini 3.5 Flash (Coding) (gemini-3.5-flash)"
# The actual model id is always the LAST parenthesised group. Extract it
# before normalising so we don't smash the display prefix into the id.
_DISPLAY_MODEL_RE = re.compile(r"\(([^()]+)\)\s*(?:\[.*\])?\s*$")


def _normalise(model_id: str) -> str:
    """Lowercase + strip provider prefixes + collapse dot/dash separators.

    If the input looks like a display-name string ("Display Name (model-id)"
    or "Display Name (model-id) [fallback]"), extract the model id from the
    last parenthesised group first.

    Providers emit the same family with either a dot or a dash between the
    family and the version digit (e.g. OpenAI returns "gpt-5.4-2026-06-01"
    on /costs but "gpt-5-4-2026-06-01" on /usage). Collapse '.' to '-' here
    so both variants resolve to a single canonical key and don't double-bucket
    in the model breakdown / no-spend-tracked footer.
    """
    s = model_id.strip()
    # Extract model id from display-name pattern: "Label (actual-model-id) [fallback]"
    m = _DISPLAY_MODEL_RE.search(s)
    if m:
        s = m.group(1).strip()
    s = s.lower()
    # Strip "[fallback]" suffix if still present (bare "model-id [fallback]" without parens)
    if s.endswith("[fallback]"):
        s = s[: -len("[fallback]")].rstrip()
    for prefix in ("openai/", "anthropic/", "google/", "vertex_ai/", "models/", "publishers/google/models/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.replace(".", "-")
    return _NORMALISE_RE.sub("", s)


# ── registry resolution ────────────────────────────────────────────────────

def _from_registry() -> ApprovedModels:
    """Pull defaults from core.model_registry env-resolved constants.

    We accept any uppercase str-valued attribute whose VALUE looks like a
    known provider model id (via _provider_of). This is more robust than
    matching attribute-name prefixes — e.g. CLAUDE_HAIKU and SOLUTION_MODEL
    are obviously model ids by value but don't follow the *_MODEL suffix
    convention.
    """
    approved = ApprovedModels()
    try:
        from core import model_registry as mr  # type: ignore
    except Exception as e:
        logger.warning(f"[llm_spend.approved_models] model_registry import failed: {e}")
        return approved

    seen: Set[str] = set()
    for attr in dir(mr):
        if not attr.isupper():
            continue
        # Skip non-model constants that happen to start with provider words.
        if attr.endswith("_DISPLAY") or attr.endswith("_PROVIDER"):
            continue
        val = getattr(mr, attr, None)
        if not isinstance(val, str) or not val:
            continue
        canon = _normalise(val)
        if canon in seen:
            continue
        seen.add(canon)
        provider = _provider_of(canon)
        if provider == "openai":
            approved.openai.add(canon)
        elif provider == "anthropic":
            approved.anthropic.add(canon)
        elif provider == "gemini":
            approved.gemini.add(canon)
        # Anything else (local-llm, display strings, etc.) is ignored.
    return approved


# ── usage-table resolution ─────────────────────────────────────────────────

_USAGE_SQL = text(
    """
    SELECT DISTINCT model
    FROM ainxt.model_usages
    WHERE created_at >= :cutoff
      AND model IS NOT NULL
      AND model <> ''
    """
)


def _from_usage_table(days: int = _RECENT_DAYS) -> ApprovedModels:
    """Anything we've actually called recently — auto-promote into tracking."""
    approved = ApprovedModels()
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        with SessionLocal() as session:
            rows = session.execute(_USAGE_SQL, {"cutoff": cutoff}).fetchall()
    except Exception as e:
        logger.warning(f"[llm_spend.approved_models] model_usages read failed: {e}")
        return approved

    for (model,) in rows:
        if not model:
            continue
        canon = _normalise(model)
        bucket = _provider_of(canon)
        if bucket == "openai":
            approved.openai.add(canon)
        elif bucket == "anthropic":
            approved.anthropic.add(canon)
        elif bucket == "gemini":
            approved.gemini.add(canon)
        # Unknown providers are silently dropped; they aren't billable here.
    return approved


import re as _re_provider

# Provider patterns — broadened to cover future model variants we haven't
# seen yet. Anchored to typical model-id shape (a digit-bearing token follows
# the family prefix) so we still reject display strings like "claudehaiku"
# or the bare word "gemini".
#
# OpenAI:
#   gpt-N…           — gpt-4, gpt-5, gpt-5-5, gpt-5.2 (post-normalise: gpt-5-2)
#   chatgpt-N…       — chatgpt-4o
#   o-series         — o1/o2/o3/…/o9 followed by '-' or end (covers future o5/o6/o7)
#   text-embedding-* — text-embedding-3-large, text-embedding-ada-002 (any token)
# Anthropic:
#   claude-<family>-N…  — claude-haiku-4-5, claude-opus-4-7, claude-sonnet-4-6
#   claude-N…           — claude-3-5-sonnet, claude-4-… (newer family-first naming)
# Gemini:
#   gemini-N…       — gemini-2-5-flash, gemini-3-flash, gemini-3-1-pro
def _build_provider_re(base: str, extra_env: str) -> "_re_provider.Pattern[str]":
    """Combine the built-in base pattern with an optional operator-supplied extra.

    Set SPEND_OPENAI_PATTERN_EXTRA / SPEND_ANTHROPIC_PATTERN_EXTRA /
    SPEND_GEMINI_PATTERN_EXTRA to classify custom model IDs to a provider
    without code changes.  The extra value is treated as a raw regex fragment
    and OR-ed with the base pattern.  An empty or unset env var is a no-op —
    existing behaviour is completely unchanged.
    """
    extra = os.getenv(extra_env, "").strip()
    if extra:
        return _re_provider.compile(f"(?:{base})|(?:{extra})")
    return _re_provider.compile(base)


_OPENAI_RE    = _build_provider_re(
    r"^(gpt-\d|chatgpt-\d|o\d(-|$)|text-embedding-)",
    "SPEND_OPENAI_PATTERN_EXTRA",
)
_ANTHROPIC_RE = _build_provider_re(
    r"^claude-([a-z]+-\d|\d)",
    "SPEND_ANTHROPIC_PATTERN_EXTRA",
)
_GEMINI_RE    = _build_provider_re(
    r"^gemini-\d",
    "SPEND_GEMINI_PATTERN_EXTRA",
)


def _provider_of(canon: str) -> str:
    """Best-effort provider attribution from a normalised model id.

    Requires a digit-bearing token after the family name so we don't match
    display strings (e.g. "claudehaiku", "gemini") that happen to share a
    prefix. If the prefix-pattern check fails, fall back to a loose
    substring check that mirrors what we observe in ainxt.model_usages —
    this catches future variants the static regexes don't yet cover.
    """
    if _OPENAI_RE.match(canon):
        return "openai"
    if _ANTHROPIC_RE.match(canon):
        return "anthropic"
    if _GEMINI_RE.match(canon):
        return "gemini"
    # Cross-check loose patterns. A canonical id that contains an explicit
    # provider family token AND has at least one digit somewhere is almost
    # certainly that provider's model even if the static prefix list hasn't
    # been updated yet (e.g. a hypothetical "gpt-7" before we add it).
    has_digit = any(ch.isdigit() for ch in canon)
    if has_digit:
        if "claude" in canon:
            return "anthropic"
        if "gemini" in canon:
            return "gemini"
        if "gpt" in canon or "chatgpt" in canon:
            return "openai"
    logger.warning(
        "spend_tracker: model_id=%r did not match any provider pattern — "
        "spend will not be attributed to a provider. "
        "Set SPEND_OPENAI_PATTERN_EXTRA, SPEND_ANTHROPIC_PATTERN_EXTRA, or "
        "SPEND_GEMINI_PATTERN_EXTRA to classify it.",
        canon,
    )
    return "unknown"


# ── public ─────────────────────────────────────────────────────────────────

# Process-local TTL cache so a single orchestrator/digest pass — which calls
# get_approved_models() from each fetcher (openai/anthropic/gemini) plus the
# digest builder — does ONE registry scan + ONE model_usages query instead
# of four. TTL is short enough that ad-hoc admin re-triggers still pick up
# newly-observed models within ~1 minute. Override via env for tests.
import time as _time
import threading as _threading

_CACHE_TTL_SECS = int(os.getenv("LLM_SPEND_APPROVED_CACHE_TTL", "60"))
_cache_lock = _threading.Lock()
_cache_value: "ApprovedModels | None" = None
_cache_expires_at: float = 0.0


def _build_approved_models() -> ApprovedModels:
    reg = _from_registry()
    obs = _from_usage_table()
    return ApprovedModels(
        openai    = reg.openai    | obs.openai,
        anthropic = reg.anthropic | obs.anthropic,
        gemini    = reg.gemini    | obs.gemini,
    )


def get_approved_models() -> ApprovedModels:
    """Union of registry defaults + recent model_usages.

    Cached for `_CACHE_TTL_SECS` seconds (default 60) so multiple callers in
    a single fetch run share one resolution. Call `clear_approved_models_cache()`
    in tests or after a manual model-registry edit to force a refresh.
    """
    global _cache_value, _cache_expires_at
    now = _time.monotonic()
    with _cache_lock:
        if _cache_value is not None and now < _cache_expires_at:
            return _cache_value
        value = _build_approved_models()
        _cache_value = value
        _cache_expires_at = now + _CACHE_TTL_SECS
        return value


def clear_approved_models_cache() -> None:
    """Drop the cached ApprovedModels so the next call rebuilds from source."""
    global _cache_value, _cache_expires_at
    with _cache_lock:
        _cache_value = None
        _cache_expires_at = 0.0
