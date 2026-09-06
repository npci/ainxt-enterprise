# SPDX-License-Identifier: MIT
# ============================================================
# Regression gate — scheduled Cowork emails must never carry the
# recipient address or subject line inside the MESSAGE BODY.
#
# Bug this pins down:
#   A scheduled email arrived with "To: x@y.com / Subject: ..." embedded
#   in the body, while the same request sent interactively through Buddy
#   was clean. Cause: the scheduler re-derives {to, subject, body} from
#   the agent's free-form TEXT (the interactive path carries them as
#   separate connector_call params). When the {"subject","body"} JSON
#   envelope failed to parse, the scheduler fell back to the agent's
#   whole narrative as the body, and the regex safety net only matched
#   a narrow set of shapes.
#
# The suite has two halves that pull in OPPOSITE directions — that
# tension is the point:
#   PART 1  leaky agent outputs must be scrubbed
#   PART 2  legitimate bodies must pass through byte-identical
#
# Passing only one half is easy; passing both is the actual fix.
#
# Pure string helpers only — no DB / Redis / LLM / httpx. See
# tests/workers/email_body_corpus.py for how the helpers are loaded.
# ============================================================
from __future__ import annotations

import os
import sys

if __name__ == "__main__":  # pragma: no cover - standalone convenience
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover - offline hosts without pytest
    # Minimal shim so this file remains importable and runnable via
    # `python tests/workers/test_cowork_email_body.py` on a box with no pytest.
    # Under real pytest this branch is never taken.
    class _FixtureShim:
        @staticmethod
        def __call__(*_a, **_kw):
            def deco(fn):
                return fn
            return deco

    class _MarkShim:
        @staticmethod
        def parametrize(*_a, **_kw):
            def deco(fn):
                return fn
            return deco

    class _PytestShim:
        mark = _MarkShim()

        @staticmethod
        def fixture(*_a, **_kw):
            def deco(fn):
                return fn
            return deco

        @staticmethod
        def fail(msg=""):
            raise AssertionError(msg)

    pytest = _PytestShim()  # type: ignore[assignment]

from tests.workers.email_body_corpus import (
    CLEAN_BODIES,
    LEAKY_OUTPUTS,
    RECIPIENT,
    SUBJECT,
    find_leaks,
    load_helpers,
)


@pytest.fixture(scope="module")
def helpers() -> dict:
    return load_helpers()


@pytest.fixture(scope="module")
def sanitize(helpers):
    return helpers["_sanitize_email_body"]


@pytest.fixture(scope="module")
def compose(helpers):
    fn = helpers.get("_compose_email_body")
    if fn is None:
        pytest.fail("_compose_email_body missing from workers/cowork_task_worker.py")
    return fn


@pytest.fixture(scope="module")
def looks_leaky(helpers):
    fn = helpers.get("_looks_like_leaky_output")
    if fn is None:
        pytest.fail("_looks_like_leaky_output missing from workers/cowork_task_worker.py")
    return fn


# ──────────────────────────────────────────────────────────────────────────────
# PART 1 — the sanitizer must strip email metadata out of the body
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(LEAKY_OUTPUTS))
def test_sanitizer_strips_metadata_from_body(sanitize, name):
    """No agent-output shape may leave to/subject metadata in the body."""
    raw = LEAKY_OUTPUTS[name]
    out = sanitize(raw)
    leaks = find_leaks(out)
    assert not leaks, (
        f"shape {name!r} leaked {leaks} into the message body\n"
        f"  input : {raw!r}\n"
        f"  output: {out!r}"
    )


@pytest.mark.parametrize("name", sorted(LEAKY_OUTPUTS))
def test_sanitizer_never_returns_empty(sanitize, name):
    """Existing contract: scrubbing must never empty the body."""
    assert sanitize(LEAKY_OUTPUTS[name]).strip(), f"{name} sanitized to empty"


def test_sanitizer_preserves_the_actual_message(sanitize):
    """Scrubbing removes the envelope, not the message the recipient must read."""
    out = sanitize(
        f"Here is the email I composed:\n\nTo: {RECIPIENT}\nSubject: {SUBJECT}\n\n"
        f"Hi Adarsh,\nPlease share the status."
    )
    assert "Please share the status." in out
    assert "Hi Adarsh" in out


# ──────────────────────────────────────────────────────────────────────────────
# PART 2 — legitimate bodies must survive byte-identical
#
# This is the guard against "fixing" the leak by over-scrubbing. Several
# of these contain an address or the word "subject" as real content.
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(CLEAN_BODIES))
def test_clean_bodies_pass_through_unchanged(sanitize, name):
    body = CLEAN_BODIES[name]
    out = sanitize(body)
    assert out == body, (
        f"legitimate body {name!r} was altered by the sanitizer\n"
        f"  before: {body!r}\n"
        f"  after : {out!r}"
    )


@pytest.mark.parametrize("name", sorted(CLEAN_BODIES))
def test_clean_bodies_are_not_flagged_leaky(looks_leaky, name):
    """The detector must not hold back a perfectly good body."""
    assert not looks_leaky(CLEAN_BODIES[name]), (
        f"legitimate body {name!r} was misclassified as leaky — it would be "
        f"withheld from sending"
    )


def test_sanitizer_is_idempotent(sanitize):
    """Sanitizing twice equals sanitizing once (it runs on more than one path)."""
    for raw in LEAKY_OUTPUTS.values():
        once = sanitize(raw)
        assert sanitize(once) == once


# ──────────────────────────────────────────────────────────────────────────────
# PART 3 — the leak detector
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(LEAKY_OUTPUTS))
def test_detector_flags_every_leaky_shape(looks_leaky, name):
    assert looks_leaky(LEAKY_OUTPUTS[name]), (
        f"shape {name!r} was NOT flagged as leaky — it would be sent as-is"
    )


def test_detector_handles_empty_input(looks_leaky):
    assert not looks_leaky("")
    assert not looks_leaky(None)


# ──────────────────────────────────────────────────────────────────────────────
# PART 4 — composition priority and send confidence
#
# The core correctness property: a narrative that failed to parse must be
# HELD (routed to the outbox), never auto-sent.
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(LEAKY_OUTPUTS))
def test_narrative_output_is_never_confident(compose, name):
    """R1/R2: the raw-output fallback must not be treated as a sendable body."""
    _body, _source, confident = compose("", "", LEAKY_OUTPUTS[name])
    assert not confident, (
        f"shape {name!r} was marked confident and would auto-send a malformed email"
    )


def test_user_dictated_body_wins_verbatim(compose):
    """Priority 1: an explicitly dictated body is used as-is and is sendable."""
    prompt = (
        'Send an email to a@b.com with subject "Status" and '
        'body: "Hi there, please confirm the settlement window."'
    )
    body, source, confident = compose(prompt, "ignored llm body", "ignored narrative")
    assert confident
    assert source == "prompt"
    assert body == "Hi there, please confirm the settlement window."


def test_parsed_envelope_body_is_confident(compose):
    """Priority 2: a body from a parsed {"subject","body"} envelope is sendable."""
    body, source, confident = compose("", "Hi Adarsh, please share the status.", "narrative")
    assert confident
    assert source == "llm_json"
    assert body == "Hi Adarsh, please share the status."


def test_clean_plain_text_output_is_usable(compose):
    """A genuinely clean plain-text answer is still deliverable."""
    clean = "Hi Adarsh,\n\nHere is this week's status: all items are on track.\n\nRegards"
    body, source, confident = compose("", "", clean)
    assert confident, "a clean plain-text output should be sendable"
    assert source == "raw_output"
    assert body == clean


def test_no_content_yields_no_body(compose):
    body, source, confident = compose("", "", "")
    assert not body
    assert not confident
    assert source == "none"


def test_prompt_body_takes_priority_over_envelope(compose):
    """Ordering is prompt > envelope > raw output."""
    prompt = 'email to a@b.com body: "Dictated text."'
    body, _source, _confident = compose(prompt, "Envelope text.", "Narrative text.")
    assert body == "Dictated text."


# ──────────────────────────────────────────────────────────────────────────────
# PART 5 — parity with the interactive Buddy send
#
# "Works when sent directly through Buddy" is the user's reference
# behaviour, so parity is the real acceptance criterion. The interactive
# path carries {to, subject, body} as discrete connector_call params; the
# scheduled path must produce an identical dict.
# ──────────────────────────────────────────────────────────────────────────────
def test_scheduled_params_match_interactive_params(compose, sanitize):
    recipient = "adarsh@example.com"
    subject = "Daily Status Update"
    message = "Hi Adarsh,\n\nPlease share the daily status update.\n\nRegards"

    # Interactive Buddy: the planner emits these as separate params.
    interactive = {"to": recipient, "subject": subject, "body": message}

    # Scheduled: same message, but the agent wrapped it in a labelled block.
    narrative = f"To: {recipient}\nSubject: {subject}\n\n{message}"
    body, _source, _confident = compose("", message, narrative)
    scheduled = {"to": recipient, "subject": subject, "body": sanitize(body)}

    assert scheduled == interactive, (
        "scheduled send params diverge from the interactive send params\n"
        f"  interactive: {interactive!r}\n"
        f"  scheduled  : {scheduled!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Standalone runner — `python tests/workers/test_cowork_email_body.py`
#
# Mirrors pytest's parametrize/fixture behaviour closely enough to run this
# gate on a host without pytest installed (offline build boxes). CI still
# collects this file as a normal pytest module.
# ──────────────────────────────────────────────────────────────────────────────
def _run_standalone() -> int:  # pragma: no cover - dev/offline convenience
    import inspect
    import traceback

    ns = load_helpers()
    provided = {
        "helpers": ns,
        "sanitize": ns.get("_sanitize_email_body"),
        "compose": ns.get("_compose_email_body"),
        "looks_leaky": ns.get("_looks_like_leaky_output"),
    }
    missing = [k for k, v in provided.items() if v is None]
    if missing:
        print(f"FAIL — helper(s) not yet implemented in the worker: {missing}")
        return 1

    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]

    passed = failed = 0
    failures: list[tuple[str, str]] = []

    for name, fn in tests:
        params = list(inspect.signature(fn).parameters)
        # Expand the "name" parameter over the corpus the test targets.
        if "name" in params:
            corpus = CLEAN_BODIES if "clean" in name else LEAKY_OUTPUTS
            cases = [{"name": k} for k in sorted(corpus)]
        else:
            cases = [{}]

        for case in cases:
            kwargs = dict(case)
            for p in params:
                if p in provided:
                    kwargs[p] = provided[p]
            label = f"{name}[{case['name']}]" if case else name
            try:
                fn(**kwargs)
                passed += 1
            except AssertionError as exc:
                failed += 1
                failures.append((label, str(exc)))
            except Exception:
                failed += 1
                failures.append((label, traceback.format_exc()))

    print(f"\n{passed} passed, {failed} failed")
    for label, msg in failures:
        print(f"\n--- FAILED {label}\n{msg}")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_run_standalone())
