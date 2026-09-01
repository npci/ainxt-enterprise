# SPDX-License-Identifier: Apache-2.0
# ============================================================
# doc-intent needs_topic guard — "ask, don't fabricate"
# ============================================================
#
# A document request with NO subject (e.g. "generate a pdf") must ASK what it
# should be about, not generate a filler "general-purpose" document. This tests
# the pure deterministic guard (_resolve_needs_topic) that decides whether to
# clarify — importantly, it must NEVER block a legitimate request that has a
# topic, an attachment, a prior artifact, or a non-generate intent.
# ============================================================

from models.doc_intent import _resolve_needs_topic


def _r(needs_topic=True, has_attachments=False, intent="generate",
       source_scope="none", target_artifact_id=None, topic=""):
    return _resolve_needs_topic(
        needs_topic, has_attachments=has_attachments, intent=intent,
        source_scope=source_scope, target_artifact_id=target_artifact_id, topic=topic,
    )


def test_topicless_generate_asks():
    # "make a pdf" — model flags needs_topic, nothing to work from → ASK.
    assert _r() is True


def test_topic_present_does_not_ask():
    # "make a pdf about UPI" — model extracted a topic → GENERATE.
    assert _r(topic="UPI architecture") is False


def test_attachment_does_not_ask():
    assert _r(has_attachments=True) is False


def test_non_generate_intent_does_not_ask():
    for intent in ("summarize", "convert", "extract", "compare", "revise"):
        assert _r(intent=intent) is False, intent


def test_prior_artifact_does_not_ask():
    assert _r(source_scope="artifact", target_artifact_id="abc123") is False
    assert _r(source_scope="uploaded") is False


def test_model_said_no_never_asks():
    # If the model didn't flag needs_topic, we never ask regardless.
    assert _r(needs_topic=False) is False


def test_guard_is_conservative():
    # The ONLY case that asks: needs_topic AND generate AND no source AND no topic.
    assert _r(needs_topic=True, has_attachments=False, intent="generate",
              source_scope="none", target_artifact_id=None, topic="") is True
