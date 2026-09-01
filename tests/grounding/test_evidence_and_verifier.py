# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Wave 3 — EvidencePack / RRF fusion / claim-grounding verifier
# ============================================================
# All pure — no model, no infra. NLI is injected as a deterministic stub.
# ============================================================

from grounding.evidence import Chunk, EvidencePack, rrf_fuse
from grounding.verifier import (
    CONTRADICTED,
    SUPPORTED,
    UNSUPPORTED,
    decompose_claims,
    verify,
)


# ── RRF fusion ───────────────────────────────────────────────────────────────

def test_rrf_rewards_agreement_across_lists():
    # doc "b" appears high in both lists → should win despite "a" being rank-0 once.
    vector = ["a", "b", "c"]
    keyword = ["b", "d", "a"]
    fused = rrf_fuse([vector, keyword])
    assert fused[0] == "b"
    assert set(fused) == {"a", "b", "c", "d"}


def test_rrf_is_scale_free_and_deterministic():
    # identical inputs → identical output; order stable for ties.
    lists = [["x", "y"], ["y", "x"]]
    assert rrf_fuse(lists) == rrf_fuse(lists)


def test_rrf_top_n_and_empty():
    assert rrf_fuse([["a", "b", "c"]], top_n=2) == ["a", "b"]
    assert rrf_fuse([]) == []
    assert rrf_fuse([[]]) == []


def test_rrf_ignores_empty_ids():
    assert "" not in rrf_fuse([["a", "", "b"]])


# ── EvidencePack ──────────────────────────────────────────────────────────────

def test_evidence_pack_chunk_ids():
    pack = EvidencePack(
        query="q",
        chunks=[Chunk(id="c1", text="t1"), Chunk(id="c2", text="t2")],
    )
    assert pack.chunk_ids() == ["c1", "c2"]
    assert pack.as_dict()["query"] == "q"


# ── claim decomposition ───────────────────────────────────────────────────────

def test_decompose_splits_and_filters_trivial():
    claims = decompose_claims("The sky is blue today. Ok. Water boils at 100 degrees celsius.")
    # "Ok." is too short → filtered
    assert any("sky is blue" in c for c in claims)
    assert any("Water boils" in c for c in claims)
    assert all(len(c) > 12 for c in claims)


def test_decompose_empty():
    assert decompose_claims("") == []
    assert decompose_claims(None) == []  # type: ignore[arg-type]


# ── verifier with a deterministic NLI stub ────────────────────────────────────

def _keyword_nli(claim: str, evidence_text: str) -> str:
    """Deterministic stand-in for a model NLI: SUPPORTED if evidence shares a
    strong keyword, CONTRADICTED on the word 'not', else UNSUPPORTED."""
    c = claim.lower()
    e = evidence_text.lower()
    if "not" in e and any(w in e for w in c.split()):
        return CONTRADICTED
    shared = set(c.split()) & set(e.split())
    return SUPPORTED if len(shared) >= 2 else UNSUPPORTED


def test_verify_marks_supported_and_unsupported():
    evidence = [
        Chunk(id="e1", text="The payment client uses exponential backoff on retries."),
        Chunk(id="e2", text="Timeouts are set to thirty seconds."),
    ]
    answer = "The payment client uses exponential backoff. The moon is made of cheese."
    report = verify(answer, evidence, nli=_keyword_nli)
    assert report.verdicts
    # first claim aligns to e1 and shares keywords → supported
    assert any(v.label == SUPPORTED and v.evidence_id == "e1" for v in report.verdicts)
    # cheese claim has no matching evidence → unsupported
    assert any(v.label == UNSUPPORTED for v in report.verdicts)
    assert 0.0 <= report.grounding_confidence <= 1.0


def test_verify_never_raises_and_defaults_unsupported():
    def _boom(_c, _e):
        raise RuntimeError("nli down")

    evidence = [Chunk(id="e1", text="some evidence text here")]
    report = verify("A claim that will fail verification cleanly.", evidence, nli=_boom)
    # nli raised → claim recorded UNSUPPORTED, never a false CONTRADICTED, never raises
    assert all(v.label == UNSUPPORTED for v in report.verdicts)
    assert report.grounding_confidence == 0.0


def test_verify_empty_answer_is_empty_report():
    report = verify("", [Chunk(id="e1", text="x")], nli=_keyword_nli)
    assert report.verdicts == []
    assert report.grounding_confidence == 0.0


def test_fully_grounded_flag():
    evidence = [Chunk(id="e1", text="alpha beta gamma delta present here")]
    report = verify("alpha beta gamma present.", evidence, nli=_keyword_nli)
    assert report.is_fully_grounded is True


def test_contradiction_penalized_worse_than_unsupported():
    # one supported + one contradicted → confidence lower than one supported +
    # one merely unsupported.
    ev = [Chunk(id="e1", text="alpha beta gamma delta refutes not present")]

    def nli_contra(_c, _e):
        return CONTRADICTED

    def nli_unsup(_c, _e):
        return UNSUPPORTED

    r_contra = verify("alpha beta gamma. delta epsilon zeta.", ev, nli=nli_contra)
    r_unsup = verify("alpha beta gamma. delta epsilon zeta.", ev, nli=nli_unsup)
    assert r_contra.grounding_confidence <= r_unsup.grounding_confidence
    assert r_contra.grounding_confidence >= 0.0  # clamped, never negative


def test_str_enum_labels_compare_as_strings():
    # Label is a str-Enum → equals the plain string used by injected NLI stubs.
    assert SUPPORTED == "supported"
    assert UNSUPPORTED == "unsupported"


def test_chunk_from_dict_adapts_live_retriever_shape():
    ch = Chunk.from_dict({"chunk_id": "x9", "text": "t", "score": "0.7", "file_path": "a.py"})
    assert ch.id == "x9" and ch.text == "t" and ch.score == 0.7 and ch.source == "a.py"
