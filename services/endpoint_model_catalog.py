# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ENDPOINT MODEL CATALOG — cloud/local classification + cost for managed endpoints
#
# One place that answers three questions for the managed-endpoint feature:
#   1. Is this model CLOUD (paid) or LOCAL (in-house, free)?
#   2. What is the full catalog an admin may choose from?
#   3. What does a completed call cost in USD?
#
# WHY THIS MODULE EXISTS
#   The codebase has FIVE competing "is this in-house" predicates
#   (middleware/budget_middleware._is_inhouse_model, gateway._req_is_inhouse,
#   routers/messages_compat_router._is_in_house_model,
#   ABStudio governance._is_local_model, gateway_local_llm.is_local_model) and
#   FOUR cost functions with different unknown-model behaviour. Rather than add a
#   sixth/fifth, this module delegates to the AUTHORITATIVE source for each
#   question and is imported by both endpoint routers so they can never disagree
#   about what is billable.
#
# CLASSIFICATION STRATEGY (deliberately allowlist-based, not prefix-based)
#   cloud  := membership in the platform's own cloud catalog (feature-flag aware,
#             BLOCKED_MODELS filtered)
#   local  := gateway_local_llm.is_local_model() — the live LiteLLM catalog, so
#             names like "Kimi-k2.5" / "glm-5.2" that no prefix heuristic catches
#             are still recognised.
#   Anything in neither set is UNKNOWN and is never treated as free.
# ============================================================

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from core.logger import logger

# Conservative fallback rate for a model we cannot price: the gpt-5.4 rate, matching
# gateway._estimate_cost:315. Deliberately NOT (0,0) — an unpriced cloud model must
# over-bill rather than silently bill nothing (which is what
# messages_compat_router._compute_cost_usd and ABStudio's estimate_model_cost do).
_UNKNOWN_RATES = (2.00, 8.00)

# "local" appearing anywhere in the model name is the platform's de-facto $0 marker
# (gateway._estimate_cost:302). Kept for parity so labels like
# "Local (In-house) (Kimi-k2.5)" and "local:glm-5.2" price at zero.
_LOCAL_MARKER = "local"


# ── Cloud catalog ────────────────────────────────────────────────────────────

def get_cloud_models() -> List[str]:
    """
    Cloud models this platform exposes, honouring the ENABLE_* feature flags and
    excluding BLOCKED_MODELS.

    Mirrors routers/model_governance_router._all_model_ids() so the endpoint admin
    picker offers exactly the same cloud set as model governance.
    Returns [] on import failure (fail-closed: no cloud models selectable).
    """
    try:
        from core.model_registry import (
            BLOCKED_MODELS,
            CLAUDE_HAIKU, CLAUDE_OPUS_48_MODEL, CLAUDE_OPUS_5_MODEL,
            CLAUDE_OPUS_MODEL, CLAUDE_PRIMARY_MODEL, CLAUDE_SONNET_5_MODEL,
            ENABLE_CLI_OPUS_48, ENABLE_CLI_OPUS_5, ENABLE_GPT56_LUNA,
            ENABLE_GPT56_TERA, ENABLE_OPUS, ENABLE_SONNET_5,
            GEMINI_CODING_LITE_MODEL, GEMINI_IMAGE_MODEL, GEMINI_TEXT_MODEL,
            OPENAI_CODING_MODEL, OPENAI_LATEST_MODEL, OPENAI_LUNA_MODEL,
            OPENAI_SIMPLE_MODEL, OPENAI_TERA_MODEL,
        )
    except Exception as exc:
        logger.warning("endpoint_catalog: model_registry import failed → %s", exc)
        return []

    models = [
        OPENAI_CODING_MODEL,
        OPENAI_SIMPLE_MODEL,
        OPENAI_LATEST_MODEL,
        CLAUDE_PRIMARY_MODEL,
        CLAUDE_HAIKU,
        GEMINI_TEXT_MODEL,
        GEMINI_CODING_LITE_MODEL,
        GEMINI_IMAGE_MODEL,
    ]
    if ENABLE_OPUS:
        models.append(CLAUDE_OPUS_MODEL)
        if ENABLE_CLI_OPUS_48:
            models.append(CLAUDE_OPUS_48_MODEL)
    if ENABLE_CLI_OPUS_5:
        models.append(CLAUDE_OPUS_5_MODEL)
    if ENABLE_SONNET_5:
        models.append(CLAUDE_SONNET_5_MODEL)
    if ENABLE_GPT56_TERA:
        models.append(OPENAI_TERA_MODEL)
    if ENABLE_GPT56_LUNA:
        models.append(OPENAI_LUNA_MODEL)

    # Deduplicate (env overrides can collapse two constants onto one id) and drop
    # blocked models — e.g. gpt-5.5 is both OPENAI_LATEST_MODEL and BLOCKED.
    seen: set = set()
    out: List[str] = []
    for m in models:
        if m and m not in seen and m not in BLOCKED_MODELS:
            seen.add(m)
            out.append(m)
    return out


def get_local_models() -> List[str]:
    """Live local (LiteLLM) model catalog. [] when the proxy is unreachable."""
    try:
        from gateway_local_llm import get_local_gateway
        return list(get_local_gateway().list_models() or [])
    except Exception as exc:
        logger.warning("endpoint_catalog: local model list failed → %s", exc)
        return []


# ── Classification ───────────────────────────────────────────────────────────

def is_cloud_model(model: str) -> bool:
    """True only for models in the platform's cloud catalog (exact match)."""
    if not model:
        return False
    return model in set(get_cloud_models())


def is_local_model(model: str) -> bool:
    """
    True for in-house models served by the local LiteLLM fleet.

    Delegates to gateway_local_llm.is_local_model (live catalog, strips a
    "local:" prefix). Falls back to the "local" substring marker so labels the
    catalog doesn't know still classify correctly.
    """
    if not model:
        return False
    try:
        from gateway_local_llm import is_local_model as _iglm
        if _iglm(model):
            return True
    except Exception:
        pass
    m = model.lower()
    return m.startswith("local:") or _LOCAL_MARKER in m


def classify_model(model: str) -> str:
    """'cloud' | 'local' | 'unknown'. Cloud wins ties — never under-bill."""
    if is_cloud_model(model):
        return "cloud"
    if is_local_model(model):
        return "local"
    return "unknown"


def provider_of(model: str) -> str:
    """
    'openai' | 'claude' | 'gemini' | 'unknown' for a CLOUD model id.

    Delegates to services.llm_spend.approved_models's proven provider regexes
    (_normalise + _provider_of) rather than re-implementing prefix matching a
    sixth time — that module already normalises display-label wrapping
    ("GPT-5.4 (Coding) (gpt-5.4) [fallback]" -> "gpt-54") and provider-prefixes
    a third-party-verified way. Only used to pick which of the three
    /llm/{provider}-tools-stream endpoints a tool-call request should hit
    (services/cloud_tool_stream.py) — never for cost or cloud/local
    classification, which stay authoritative in this module via
    get_cloud_models()/is_cloud_model().

    Returns "unknown" for local models and anything unrecognised; callers must
    treat "unknown" as "cannot serve tool calls for this model" rather than
    guessing a provider.
    """
    if not model:
        return "unknown"
    try:
        from services.llm_spend.approved_models import _normalise, _provider_of
    except Exception as exc:
        logger.warning("endpoint_catalog: approved_models import failed → %s", exc)
        return "unknown"

    canon = _normalise(model)
    provider = _provider_of(canon)
    # approved_models uses "anthropic" as the bucket name; the tools-stream
    # endpoint and gateway.py's _model_hint conventions use "claude".
    if provider == "anthropic":
        return "claude"
    if provider in ("openai", "gemini"):
        return provider
    return "unknown"


def has_cloud_models(model_ids: Optional[List[str]]) -> bool:
    """True if any entry in an endpoint's allowlist is a paid cloud model."""
    if not model_ids:
        return False
    cloud = set(get_cloud_models())
    return any(m in cloud for m in model_ids)


def first_local_model(model_ids: Optional[List[str]]) -> Optional[str]:
    """
    First LOCAL model in an endpoint's allowlist, or None.

    Used as the fallback target for an unrecognised model so it resolves to free
    in-house inference instead of silently escalating to paid cloud (which is what
    the platform's own default routing does — see gateway._oai_model_hint
    returning None -> ModelRouter auto-route -> TIER_MEDIUM -> gpt-5.4).
    """
    for m in (model_ids or []):
        if is_local_model(m):
            return m
    return None


# ── Cost ─────────────────────────────────────────────────────────────────────

def _rates_for(model: str):
    """
    (input_usd, output_usd) per 1M tokens.

    Same resolution order as gateway._estimate_cost (the platform-standard
    implementation, and the only one that never returns $0 for an unknown cloud
    model): exact key, then substring scan so display labels like
    "GPT-5.4 (Coding) (gpt-5.4) [fallback]" still price correctly, then a
    conservative default.

    Replicated here rather than imported so the request path does not pull in the
    12k-line gateway.py module.
    """
    try:
        from core.model_registry import MODEL_COST_PER_1M
    except Exception:
        return _UNKNOWN_RATES

    rates = MODEL_COST_PER_1M.get(model)
    if rates is not None:
        return rates

    m = (model or "").lower()
    for mid, r in MODEL_COST_PER_1M.items():
        if mid and mid.lower() in m:
            return r
    return _UNKNOWN_RATES


def cheapest_cloud_model(model_ids: Optional[List[str]]) -> Optional[str]:
    """
    Cheapest CLOUD model in an endpoint's allowlist, or None if it contains no
    cloud models. Ranked by (input_per_1M + output_per_1M) from the same
    pricing table estimate_cost_usd/price_hint already use — ties broken by
    allowlist order (min() keeps the first-seen winner), so the result is
    deterministic for a given allowlist.

    This is the SECOND tier of the fallback an endpoint uses for an
    unrecognised model name: first_local_model() (free, preferred) is tried
    first by the caller; this is reached only when the allowlist has no local
    model at all. Computed here — never admin-set — so it can never drift out
    of sync with the endpoint's actual allowlist or the platform's current
    pricing.
    """
    cloud_ids = [m for m in (model_ids or []) if is_cloud_model(m)]
    if not cloud_ids:
        return None

    def _rank(m: str) -> float:
        rin, rout = _rates_for(m)
        return rin + rout

    return min(cloud_ids, key=_rank)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """
    USD cost of a call, as Decimal (callers persist NUMERIC(12,6)).

    In-house models are always free. Everything else is priced from
    MODEL_COST_PER_1M, defaulting to a conservative rate when unknown so an
    unpriced cloud model over-bills rather than escaping billing entirely.
    """
    if not model:
        return Decimal("0")

    m = model.lower()
    if _LOCAL_MARKER in m or is_local_model(model):
        return Decimal("0")

    rin, rout = _rates_for(model)
    cost = (
        (Decimal(str(input_tokens or 0))  * Decimal(str(rin)))
        + (Decimal(str(output_tokens or 0)) * Decimal(str(rout)))
    ) / Decimal("1000000")
    # 6 dp matches hod_allocation_ledger.endpoint_spend_usd / model_usages precision.
    return cost.quantize(Decimal("0.000001"))


def price_hint(model: str) -> Optional[Dict[str, float]]:
    """
    {"input_per_1m", "output_per_1m"} for the admin UI, or None for free models —
    so an admin sees the cost implication before enabling a cloud model.
    """
    if not model or classify_model(model) == "local":
        return None
    rin, rout = _rates_for(model)
    return {"input_per_1m": float(rin), "output_per_1m": float(rout)}
