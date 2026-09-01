# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Claim-grounding verifier — per-claim entailment (Phase 14, Layer 3)
# ============================================================
#
# docs/architecture/14-grounding.md §14.5. The frontier gap: today confidence is
# BLOCK-level (is this unsafe?) and heuristic; there is no check that the
# specific CLAIMS in an answer are supported by the specific EVIDENCE. This
# module adds that as a pipeline:
#
#     decompose → align → entail (NLI) → label → confidence
#
# DESIGN FOR SAFETY + TESTABILITY:
#   - The NLI step is INJECTED (a callable). In production a caller passes a
#     local-model NLI fn; in tests we pass a deterministic stub. No model import
#     here, so this module is pure/importable in a bare env.
#   - The verifier is ADVISORY: it never blocks or mutates an answer. It returns
#     a report; callers (per Domain Profile) decide whether to hedge/cite. On any
#     error it degrades to "unverified" — never a false "contradicted".
#   - Grounding confidence is SEPARATE from block confidence
#     (core/confidence_scorer.py) — a deliberate category distinction (§14.8).
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional


class Label(str, Enum):
    """NLI verdict for a claim against its aligned evidence.

    str-Enum so values compare/serialize as the plain strings used across the
    platform, while giving callers a typed vocabulary.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"


# module-level aliases (back-compat + terse call sites / test stubs)
SUPPORTED = Label.SUPPORTED
UNSUPPORTED = Label.UNSUPPORTED
CONTRADICTED = Label.CONTRADICTED
_VALID_LABELS = {Label.SUPPORTED, Label.UNSUPPORTED, Label.CONTRADICTED}

# An NLI function: (claim, evidence_text) -> one of the three labels above.
NliFn = Callable[[str, str], str]

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class ClaimVerdict:
    claim: str
    label: Label = UNSUPPORTED
    evidence_id: Optional[str] = None
    score: float = 0.0


@dataclass
class GroundingReport:
    verdicts: List[ClaimVerdict] = field(default_factory=list)
    grounding_confidence: float = 0.0   # separate from block confidence
    unsupported: List[str] = field(default_factory=list)
    contradicted: List[str] = field(default_factory=list)

    @property
    def is_fully_grounded(self) -> bool:
        return bool(self.verdicts) and not self.unsupported and not self.contradicted


def decompose_claims(answer: str, *, max_claims: int = 20) -> List[str]:
    """Split an answer into atomic factual claims (sentence-level, cheap).

    A production caller may pass an LLM-decomposed list instead; this
    sentence splitter is the deterministic fallback (never raises).
    """
    if not answer:
        return []
    parts = [s.strip() for s in _SENT_SPLIT.split(answer.strip()) if s.strip()]
    # ignore trivial/non-assertive fragments
    claims = [p for p in parts if len(p) > 12]
    return claims[:max_claims]


def _best_evidence(claim: str, evidence, aligner: Optional[Callable] = None):
    """Return (evidence_id, evidence_text) most relevant to the claim.

    Default aligner = lexical token overlap (deterministic). A production caller
    can inject an embedding/rerank aligner. `evidence` is an iterable of objects
    with `.id` and `.text` (e.g. grounding.evidence.Chunk).
    """
    if aligner is not None:
        return aligner(claim, evidence)
    claim_toks = set(re.findall(r"\w+", claim.lower()))
    best, best_overlap = None, 0
    for ch in evidence or []:
        toks = set(re.findall(r"\w+", (getattr(ch, "text", "") or "").lower()))
        overlap = len(claim_toks & toks)
        if overlap > best_overlap:
            best, best_overlap = ch, overlap
    if best is None:
        return (None, "")
    return (getattr(best, "id", None), getattr(best, "text", ""))


def verify(
    answer: str,
    evidence,
    *,
    nli: NliFn,
    aligner: Optional[Callable] = None,
    claims: Optional[List[str]] = None,
) -> GroundingReport:
    """Verify each claim in `answer` against `evidence` using the injected `nli`.

    Never raises. On any per-claim failure that claim is recorded as UNSUPPORTED
    (fail toward "we couldn't confirm", never a false CONTRADICTED).
    """
    report = GroundingReport()
    claim_list = claims if claims is not None else decompose_claims(answer)
    if not claim_list:
        return report

    for claim in claim_list:
        verdict = ClaimVerdict(claim=claim)
        try:
            ev_id, ev_text = _best_evidence(claim, evidence, aligner)
            verdict.evidence_id = ev_id
            if ev_text:
                raw = nli(claim, ev_text)
                # injected NLI may return a Label or a bare string; coerce safely
                try:
                    label = Label(raw)
                except ValueError:
                    label = UNSUPPORTED
                verdict.label = label if label in _VALID_LABELS else UNSUPPORTED
            else:
                verdict.label = UNSUPPORTED
        except Exception:  # noqa: BLE001 — advisory; never break the answer
            verdict.label = UNSUPPORTED
        verdict.score = 1.0 if verdict.label == SUPPORTED else 0.0
        report.verdicts.append(verdict)
        if verdict.label == UNSUPPORTED:
            report.unsupported.append(claim)
        elif verdict.label == CONTRADICTED:
            report.contradicted.append(claim)

    # Confidence penalizes contradictions harder than mere gaps: an UNSUPPORTED
    # claim is "couldn't confirm"; a CONTRADICTED claim means the evidence
    # actively refutes it (worse). (supported - contradicted) / total, clamped.
    total = len(report.verdicts)
    supported = sum(1 for v in report.verdicts if v.label == SUPPORTED)
    contradicted = len(report.contradicted)
    report.grounding_confidence = round(max(0.0, (supported - contradicted) / total), 4)
    return report
