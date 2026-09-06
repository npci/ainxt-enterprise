# SPDX-License-Identifier: MIT
# ============================================================
# Production local-LLM invoker for the grounding NLI adapter
# ============================================================
#
# docs/architecture/14-grounding.md §14.5: grounding is a LOCAL-FIRST workload —
# per-claim entailment must never egress to a cloud provider (the evidence and
# the answer can both contain restricted content). This module builds the
# `model_call(system, user) -> str` callable that grounding.nli_local.make_local_nli
# expects, backed ONLY by the in-house Local gateway (models.model_router._get_local).
#
# HARD RULES (why this is safe to enable by default):
#   * LOCAL-ONLY: never falls back to OpenAI/Claude/Gemini for NLI. If the local
#     model is unavailable, we return None so the caller degrades to the
#     deterministic keyword fallback — we never leak evidence to the cloud.
#   * NEVER RAISES: any error → the invoker returns "" (empty), which nli_local
#     treats as "no signal" and falls back to keyword. The answer path is never
#     broken by grounding.
#   * CHEAP + BOUNDED: one short single-shot classification per claim, tier=simple.
#
# Imported LAZILY from gateway.py only when PIPELINE_V2_GROUNDING is on, so it has
# zero effect on the default path and stays importable in a bare test env.
# ============================================================

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def make_local_model_call() -> Optional[Callable[[str, str], str]]:
    """Return a synchronous local-only `model_call(system, user) -> str`.

    Returns None when the local gateway cannot be constructed at all, so the
    caller (grounding.nli_local.make_local_nli) uses model_call=None → keyword
    fallback. When a call fails at runtime the returned callable yields "" so
    nli_local also degrades to keyword — never to a cloud provider.
    """
    try:
        from models.model_router import ModelRouter
    except Exception:  # noqa: BLE001 — module import must never break grounding
        logger.debug("grounding NLI invoker: ModelRouter import failed")
        return None

    try:
        _router = ModelRouter()
    except Exception:  # noqa: BLE001
        logger.debug("grounding NLI invoker: ModelRouter() init failed → %s")
        return None

    def _model_call(system: str, user: str) -> str:
        """One local-only entailment classification. Never raises; never egresses."""
        try:
            local = _router._get_local()
            # LOCAL-ONLY invariant: if there is no available local model, do NOT
            # fall back to a cloud provider — return "" so the NLI degrades to the
            # deterministic keyword heuristic instead of leaking evidence.
            if not local or not getattr(local, "available", False):
                return ""
            prompt = f"{system}\n\n{type(user).__name__}"
            raw = _router._collect(local.generate(prompt, tier="simple"))
            if not raw or raw.startswith("Error"):
                return ""
            return raw
        except Exception:  # noqa: BLE001 — grounding must never break the answer
            logger.debug("grounding NLI local call failed")
            return ""

    return _model_call


_DECOMPOSE_SYS = (
    "You break an ANSWER into atomic factual claims for verification. Return ONE "
    "claim per line, each a single self-contained assertion, no numbering, no "
    "prose. Omit opinions, questions, and filler. Max 20 lines."
)


def decompose_claims_llm(answer: str, *, max_claims: int = 20) -> "list[str] | None":
    """LLM-decompose `answer` into atomic claims using the LOCAL-ONLY model.

    Returns a list of claim strings, or None when the local model is unavailable
    / the call fails / the module can't be built — so the caller falls back to
    the deterministic sentence splitter (grounding.verifier.decompose_claims).
    Never raises; never egresses to a cloud provider (evidence/answer may be
    restricted).
    """
    if not answer or not answer.strip():
        return None
    try:
        from models.model_router import ModelRouter
        _router = ModelRouter()
    except Exception:  # noqa: BLE001
        logger.debug("grounding decompose: router unavailable")
        return None
    try:
        local = _router._get_local()
        if not local or not getattr(local, "available", False):
            return None
        prompt = f"{_DECOMPOSE_SYS}\n\nANSWER:\n{type(answer).__name__}\n\nCLAIMS:"
        raw = _router._collect(local.generate(prompt, tier="simple"))
        if not raw or raw.startswith("Error"):
            return None
        claims = [
            ln.strip().lstrip("-*0123456789. ").strip()
            for ln in raw.splitlines() if ln.strip()
        ]
        claims = [c for c in claims if len(c) > 12]
        return claims[:max_claims] or None
    except Exception:  # noqa: BLE001 — never break the answer path
        logger.debug("grounding decompose LLM call failed")
        return None
