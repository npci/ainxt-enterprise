# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Phase 3 — local NLI adapter for the grounding verifier
# ============================================================

from grounding.evidence import Chunk
from grounding.nli_local import make_local_nli
from grounding.verifier import CONTRADICTED, SUPPORTED, UNSUPPORTED, verify


def test_keyword_fallback_when_no_model():
    nli = make_local_nli(model_call=None)
    assert nli("the sky is blue", "the sky is blue today") == SUPPORTED
    assert nli("quantum entanglement theory", "the cat sat on the mat") == UNSUPPORTED
    assert nli("", "anything") == UNSUPPORTED


def test_model_call_maps_verdicts():
    def model(_sys, user):
        u = user.lower()
        if "refut" in u or "contra" in u:
            return "contradicted"
        if "backoff" in u:
            return "supported"
        return "unsupported"

    nli = make_local_nli(model_call=model)
    assert nli("uses backoff", "evidence: backoff") == SUPPORTED
    assert nli("x refutes y", "evidence: refut") == CONTRADICTED
    assert nli("unrelated", "evidence: nothing") == UNSUPPORTED


def test_model_crash_falls_back_never_raises():
    def boom(_s, _u):
        raise RuntimeError("model down")

    nli = make_local_nli(model_call=boom)
    # falls back to keyword, never raises
    assert nli("shared words here", "shared words here too") in (SUPPORTED, UNSUPPORTED)


def test_end_to_end_verify_with_local_nli():
    ev = [Chunk(id="e1", text="The payment client uses exponential backoff on retries.")]

    def model(_sys, user):
        return "supported" if "backoff" in user.lower() else "unsupported"

    report = verify("The payment client uses exponential backoff.", ev,
                    nli=make_local_nli(model_call=model))
    assert report.grounding_confidence > 0.0
    assert not report.contradicted
