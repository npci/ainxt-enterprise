# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Local-model NLI adapter for the grounding verifier
# ============================================================
#
# docs/architecture/14-grounding.md §14.5: grounding is a local-first workload.
# grounding.verifier.verify() takes an INJECTED nli(claim, evidence)->label so
# it stays pure/testable. This module provides the production adapter: a small
# local-LLM call that classifies entailment. It is imported LAZILY by the caller
# and only invoked when PIPELINE_V2_GROUNDING is on, so it never affects the
# default path or the bare test env.
#
# Design: one cheap structured local call per claim, hard-capped, with a
# keyword fallback so a model outage degrades to a heuristic rather than failing
# the answer (the verifier already treats any exception as UNSUPPORTED).
# ============================================================

from __future__ import annotations

import re
from typing import Callable

from grounding.verifier import CONTRADICTED, SUPPORTED, UNSUPPORTED

_NLI_SYS = (
    "You are a strict fact-checker. Given a CLAIM and EVIDENCE, answer with ONE "
    "word only: 'supported' if the evidence entails the claim, 'contradicted' "
    "if the evidence refutes it, or 'unsupported' if the evidence is silent. "
    "No other words."
)


def _keyword_fallback(claim: str, evidence: str) -> str:
    """Deterministic fallback when no model is available (mirrors the verifier's
    conservative posture: only assert support on strong overlap)."""
    c = set(re.findall(r"\w+", (claim or "").lower()))
    e = set(re.findall(r"\w+", (evidence or "").lower()))
    if not c or not e:
        return UNSUPPORTED
    overlap = len(c & e) / max(len(c), 1)
    return SUPPORTED if overlap >= 0.5 else UNSUPPORTED


def make_local_nli(model_call: Callable[[str, str], str] | None = None):
    """Return an nli(claim, evidence)->label callable for verifier.verify().

    `model_call(system, user)->str` is the local-LLM invoker; when None we use
    the keyword fallback (so this is usable/testable without a live model). The
    returned callable never raises — on any error it falls back to keyword.
    """

    def _nli(claim: str, evidence: str) -> str:
        if model_call is None:
            return _keyword_fallback(claim, evidence)
        try:
            user = f"CLAIM: {claim}\nEVIDENCE: {evidence}\nAnswer:"
            raw = (model_call(_NLI_SYS, user) or "").strip().lower()
            # Order matters: check the NEGATIVE labels first. "unsupported"
            # CONTAINS the substring "support", so a naive `"support" in raw`
            # check would misclassify "unsupported" as SUPPORTED. Test
            # contradiction and unsupported/silent before the bare support check.
            if "contradict" in raw or "refut" in raw:
                return CONTRADICTED
            if "unsupport" in raw or "not support" in raw or "silent" in raw:
                return UNSUPPORTED
            if "support" in raw or "entail" in raw:
                return SUPPORTED
            return _keyword_fallback(claim, evidence)
        except Exception:  # noqa: BLE001 — never break the answer path
            return _keyword_fallback(claim, evidence)

    return _nli
