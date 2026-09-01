# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BLOCK CONFIDENCE SCORER  —  core/confidence_scorer.py
# ============================================================
#
# Computes a single confidence score in [0.0, 1.0] for any
# user prompt that has been blocked by the safety stack
# (HardBlock engine, Compliance/PCI/PII engine).
#
# This scorer is intentionally deterministic and additive — it
# operates ONLY on the evidence the gateway already collected
# at block time (no extra LLM calls, no IO, no model lookups).
# That keeps the cost ~O(n_findings) and safe to call inline
# on the API thread.
#
# Algorithm: weighted noisy-OR ensemble.
#
#   p_block = 1 - Π (1 - w_i)
#
# Where each contributing signal i has an independent weight w_i:
#
#   1. HardBlock engine (regex/keyword, deterministic)
#        base       = 0.85
#        +0.05      per additional matched phrase (capped at +0.10)
#        +0.05      if multiple HardBlock categories matched
#
#   2. Compliance findings (per type, from compliance_engine)
#        High-precision regex (PAN, Aadhaar, secrets, keys):  0.95
#        Strong PII (IFSC, account no, passport, SSN):        0.90
#        Standard PII (email, mobile, dob, gst):              0.70
#        Anything else flagged as blocked:                    0.60
#
#   3. Severity boost (per finding)
#        CRITICAL   → +0.05
#        HIGH       → +0.03
#        (capped via the noisy-OR combination)
#
# Final score is clamped to [0.0, 1.0] and rounded to 4 dp.
#
# A prompt with even a single HardBlock match starts at 0.85
# confidence; a HardBlock + PCI PAN double-hit will land at
# ≈ 0.99. A solitary "standard PII" mobile-number match sits
# at 0.70.
#
# This shape gives downstream monitoring (Grafana / SIEM) a
# meaningful spread so analysts can triage false positives
# (low confidence) separately from clear-cut violations (≥ 0.90).
# ============================================================

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


# ── Per-type base weights for compliance findings ────────────
# Tunable: any key not present falls back to _DEFAULT_TYPE_WEIGHT.
# Keep keys lowercase; the scorer normalizes inputs.
_TYPE_WEIGHTS: Dict[str, float] = {
    # HardBlock (handled separately, but included for completeness)
    "hardblock":              0.85,
    "hardblock_engine_error": 0.90,   # fail-closed path: high confidence the block is intentional

    # High-precision PCI / secrets
    "pci_pan":                0.95,
    "pci_aadhaar":            0.95,
    "pci_credit_card":        0.95,
    "pci_cvv":                0.95,
    "pci_track_data":         0.95,
    "secret_api_key":         0.95,
    "secret_token":           0.95,
    "secret_private_key":     0.97,
    "key_leak":               0.95,
    "aws_access_key":         0.97,
    "aws_secret_key":         0.97,

    # Strong PII
    "pii_ifsc":               0.90,
    "pii_account_number":     0.90,
    "pii_passport":           0.90,
    "pii_ssn":                0.92,
    "pii_voter_id":           0.88,
    "pii_driving_license":    0.88,

    # Standard PII
    "pii_email":              0.70,
    "pii_mobile":             0.70,
    "pii_phone":              0.70,
    "pii_dob":                0.70,
    "pii_gst":                0.72,
    "pii_address":            0.65,
    "pii_name":               0.55,

}

_DEFAULT_TYPE_WEIGHT     = 0.60
_HARDBLOCK_BASE          = 0.85
_HARDBLOCK_EXTRA_PHRASE  = 0.05
_HARDBLOCK_EXTRA_CAT     = 0.05
_HARDBLOCK_EXTRA_CAP     = 0.10
_SEVERITY_BOOST: Dict[str, float] = {
    "CRITICAL": 0.05,
    "HIGH":     0.03,
    "MEDIUM":   0.0,
    "LOW":      0.0,
}


def _clamp01(x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return x


def _noisy_or(weights: Iterable[float]) -> float:
    """
    Combine independent weights with noisy-OR:
        p = 1 - Π (1 - w_i)
    Weights are clamped to [0, 1) before combination (a single 1.0 would
    saturate the product to 0, which we instead want to mean "near-certain"
    — handled by the final clamp).
    """
    prod = 1.0
    saw_any = False
    for w in weights:
        try:
            wf = float(w)
        except (TypeError, ValueError):
            continue
        if wf <= 0.0:
            continue
        saw_any = True
        # Cap individual contributions just below 1.0 so the product math
        # stays meaningful when several near-1.0 signals stack.
        if wf >= 0.999:
            wf = 0.999
        prod *= (1.0 - wf)
    return 0.0 if not saw_any else (1.0 - prod)


def _normalize_findings(findings: Any) -> List[Dict[str, Any]]:
    """Defensive: accept None, a single dict, or a list of dicts."""
    if not findings:
        return []
    if isinstance(findings, dict):
        return [findings]
    if isinstance(findings, list):
        return [f for f in findings if isinstance(f, dict)]
    return []


def _hardblock_signal(hb_findings: List[Dict[str, Any]]) -> Optional[float]:
    """
    Returns a single weight for the entire HardBlock evidence bundle,
    or None when there is no HardBlock signal.

    Phrase/category counts modestly inflate confidence — many matches
    indicate the prompt is unambiguously in the blocked category.
    """
    hb = [
        f for f in hb_findings
        if str(f.get("type", "")).upper() in ("HARDBLOCK", "HARDBLOCK_ENGINE_ERROR")
    ]
    if not hb:
        return None

    # Count distinct matched phrases and categories across all HB findings.
    phrases: set = set()
    categories: set = set()
    for f in hb:
        val = f.get("value")
        if isinstance(val, list):
            for v in val:
                if v:
                    phrases.add(str(v))
        elif val:
            phrases.add(str(val))
        cat = f.get("category")
        if cat:
            categories.add(str(cat))

    w = _HARDBLOCK_BASE
    # +0.05 per phrase beyond the first, capped.
    extra_phrases = max(0, len(phrases) - 1)
    w += min(extra_phrases * _HARDBLOCK_EXTRA_PHRASE, _HARDBLOCK_EXTRA_CAP)
    # +0.05 if multiple categories matched.
    if len(categories) > 1:
        w += _HARDBLOCK_EXTRA_CAT

    # Severity boost from the strongest finding.
    sev = max(
        (_SEVERITY_BOOST.get(str(f.get("severity", "")).upper(), 0.0) for f in hb),
        default=0.0,
    )
    w += sev

    return _clamp01(w)


def _compliance_signals(
    findings: List[Dict[str, Any]],
    blocked_types: Iterable[str],
) -> List[float]:
    """
    Yields one weight per compliance finding whose `type` is in the
    authoritative `blocked_types` list. HardBlock findings are excluded
    (they are scored separately by _hardblock_signal).
    """
    blocked_set = {str(t).lower() for t in (blocked_types or []) if t}
    weights: List[float] = []
    for f in findings:
        ftype_raw = str(f.get("type", ""))
        ftype = ftype_raw.lower()
        if not ftype:
            continue
        if ftype in ("hardblock", "hardblock_engine_error"):
            continue
        # Only count findings the compliance engine declared blocking.
        if blocked_set and ftype not in blocked_set:
            continue

        base = _TYPE_WEIGHTS.get(ftype, _DEFAULT_TYPE_WEIGHT)
        sev = _SEVERITY_BOOST.get(str(f.get("severity", "")).upper(), 0.0)
        weights.append(_clamp01(base + sev))
    return weights


def compute_block_confidence(
    *,
    hardblock_findings: Optional[List[Dict[str, Any]]] = None,
    compliance_findings: Optional[List[Dict[str, Any]]] = None,
    blocked_types:        Optional[List[str]]         = None,
) -> float:
    """
    Compute a confidence score in [0.0, 1.0] for a blocked prompt.

    Inputs are exactly the evidence gateway already has at block time:

      hardblock_findings  – the gateway's `_hb_findings` list (HARDBLOCK
                            and HARDBLOCK_ENGINE_ERROR entries).
      compliance_findings – `_ask_chk["findings"]` from compliance_engine.
      blocked_types       – `_ask_chk["blocked_types"]` (authoritative
                            list of finding types that caused the block).

    Returns a float clamped to [0.0, 1.0], rounded to 4 decimals.
    Returns 0.0 when there is no block evidence at all (callers should
    not log a "blocked" record in that case).

    Notes:
      - This function is pure and side-effect free.
      - It never raises; on internal error it returns 0.0 and the caller
        treats the score as "unavailable" via the audit logger's None
        normalization rules.
    """
    try:
        hb = _normalize_findings(hardblock_findings)
        comp = _normalize_findings(compliance_findings)

        signals: List[float] = []

        hb_w = _hardblock_signal(hb)
        if hb_w is not None:
            signals.append(hb_w)

        signals.extend(_compliance_signals(comp, blocked_types or []))

        score = _noisy_or(signals)
        return round(_clamp01(score), 4)
    except Exception:
        # Never let scoring failure affect the user response or audit.
        return 0.0


__all__ = ["compute_block_confidence"]
