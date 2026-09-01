# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Shared corpus for the scheduled-email body-leak diagnostic.
#
# Consumed by BOTH:
#   - tests/workers/test_cowork_email_body.py   (pytest gate)
#   - scripts/diagnose_email_body_leak.py       (human-readable report)
#
# Kept in one place so the committed test and the operator-facing
# diagnostic can never drift apart.
#
# WHY the helpers are loaded via ast/exec:
#   workers/cowork_task_worker.py imports sqlalchemy, core.logger and
#   (lazily) the orchestrator. The four functions under test are pure
#   string helpers with no such needs, so we extract just those defs.
#   That keeps this suite runnable in CI with no DB / Redis / LLM.
# ============================================================
from __future__ import annotations

import ast
import json
import os
import re

_WORKER_REL = os.path.join("workers", "cowork_task_worker.py")

# The pure string helpers this corpus exercises directly. Their private
# collaborators (e.g. _header_label_match) are picked up automatically — see
# load_helpers, which takes every undecorated module-level def.
_REQUIRED = ("_sanitize_email_body", "_compose_email_body", "_looks_like_leaky_output")


def _repo_root() -> str:
    """Walk up from this file until we find workers/cowork_task_worker.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(here, _WORKER_REL)):
            return here
        here = os.path.dirname(here)
    raise RuntimeError(f"could not locate {_WORKER_REL} above {__file__}")


def load_helpers() -> dict:
    """Return a namespace containing the worker's pure string helpers.

    Takes every module-level ``def`` plus every module-level literal constant.
    Nothing else is evaluated, so no import in cowork_task_worker.py (sqlalchemy,
    core.logger, the orchestrator) is ever touched — this runs offline in CI.

    Functions that need heavier globals are still exec'd; they simply raise if
    called, which no test in this corpus does.
    """
    root = _repo_root()
    src = open(os.path.join(root, _WORKER_REL), encoding="utf-8").read()
    tree = ast.parse(src)

    ns: dict = {"json": json, "re": re, "os": os}

    # Module-level assignments are taken when they are side-effect free: a literal
    # constant, or an expression built only from literals / already-defined names
    # (e.g. _NARRATION_PREFIX_RE, which concatenates regex fragments).
    def _is_safe_value(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Call, ast.Await, ast.Yield, ast.Lambda,
                                ast.ListComp, ast.DictComp, ast.SetComp,
                                ast.GeneratorExp, ast.Attribute, ast.Subscript)):
                return False
        return True

    nodes: list[ast.stmt] = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            nodes.append(n)
        elif isinstance(n, ast.Assign) and all(
            isinstance(t, ast.Name) for t in n.targets
        ) and _is_safe_value(n.value):
            nodes.append(n)

    # Exec in source order so helpers that call each other resolve.
    for node in nodes:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<worker-helpers>", "exec"), ns)

    missing = [n for n in _REQUIRED if n not in ns]
    if missing:
        raise RuntimeError(
            f"required helper(s) not found in {_WORKER_REL}: {missing}"
        )
    return ns


# ──────────────────────────────────────────────────────────────────────────────
# LEAKY_OUTPUTS — realistic agent outputs that must NEVER reach a recipient
# with the to/subject metadata still embedded in the body.
#
# These are the shapes office mode actually produces when the
# {"subject","body"} JSON envelope fails to parse.
# ──────────────────────────────────────────────────────────────────────────────
RECIPIENT = "adarsh@example.com"
SUBJECT = "Daily Status Update"

LEAKY_OUTPUTS: dict[str, str] = {
    # ── The 5 shapes confirmed in the original diagnostic ──────────────────
    "instruction_echo_verbatim":
        f'Send an email to {RECIPIENT} with subject "{SUBJECT}" and '
        f'body: "Hi Adarsh, please share the daily status update."',

    "labelled_block_at_start":
        f"To: {RECIPIENT}\nSubject: {SUBJECT}\n\n"
        f"Hi Adarsh,\n\nPlease share the daily status update.\n\nRegards",

    "first_person_narration":
        f'I have sent an email to {RECIPIENT} with subject "{SUBJECT}" and '
        f'body: Hi Adarsh, please share the daily status update.',

    "prose_then_labelled_block":
        f"Here is the email I composed:\n\nTo: {RECIPIENT}\nSubject: {SUBJECT}\n\n"
        f"Hi Adarsh,\nPlease share the status.",

    "markdown_bold_labels":
        f"**To:** {RECIPIENT}\n**Subject:** {SUBJECT}\n\n"
        f"Hi Adarsh, please share the status.",

    # ── Narration variants ─────────────────────────────────────────────────
    "contracted_narration":
        f"I've drafted an email to {RECIPIENT} with subject \"{SUBJECT}\" and "
        f"body: Hi Adarsh, kindly share today's status.",

    "heres_the_email":
        f"Here's the email:\n\nTo: {RECIPIENT}\nSubject: {SUBJECT}\n\n"
        f"Hi Adarsh, kindly share today's status.",

    "sent_successfully_prefix":
        f"Email sent successfully:\nTo: {RECIPIENT}\nSubject: {SUBJECT}\n\n"
        f"Hi Adarsh, please confirm receipt.",

    "prepared_narration":
        f"I have prepared the following message for {RECIPIENT} with subject "
        f"\"{SUBJECT}\":\n\nHi Adarsh, please share the status.",

    # ── Label-format variants ──────────────────────────────────────────────
    "markdown_heading_labels":
        f"### To: {RECIPIENT}\n### Subject: {SUBJECT}\n\n"
        f"Hi Adarsh, please share the status.",

    "recipients_and_subject_line_labels":
        f"Recipients: {RECIPIENT}\nSubject Line: {SUBJECT}\n\n"
        f"Hi Adarsh, please share the status.",

    "to_cc_block":
        f"To: {RECIPIENT}\nCc: manager@example.com\nSubject: {SUBJECT}\n\n"
        f"Hi Adarsh, please share the status.",

    "crlf_labelled_block":
        f"To: {RECIPIENT}\r\nSubject: {SUBJECT}\r\n\r\n"
        f"Hi Adarsh, please share the status.",

    # ── JSON residue ───────────────────────────────────────────────────────
    "bare_json_envelope":
        json.dumps({"subject": SUBJECT, "body": "Hi Adarsh, please share the status."}),

    "prose_then_fenced_json":
        "Here is the email I composed:\n\n```json\n"
        + json.dumps({"subject": SUBJECT, "body": "Hi Adarsh, please share the status."})
        + "\n```",

    "json_with_to_field":
        json.dumps({
            "to": RECIPIENT,
            "subject": SUBJECT,
            "body": "Hi Adarsh, please share the status.",
        }),
}


# ──────────────────────────────────────────────────────────────────────────────
# CLEAN_BODIES — legitimate bodies that must pass through UNCHANGED.
#
# This is the regression guard against over-aggressive scrubbing. Several
# deliberately contain an email address or the word "subject" mid-sentence:
# those are real content, not metadata, and must survive.
# ──────────────────────────────────────────────────────────────────────────────
CLEAN_BODIES: dict[str, str] = {
    "plain_greeting":
        "Hi Adarsh,\n\nPlease share the daily status update.\n\nRegards,\nTeam",

    "address_mid_body":
        "Hi Adarsh,\n\nPlease copy finance@example.com on the reply so they can "
        "track the invoice.\n\nThanks",

    "word_subject_mid_sentence":
        "Hi team,\n\nThe subject of the audit is vendor onboarding. Please review "
        "the attached checklist before Friday.\n\nRegards",

    "subject_in_later_line":
        "Hi Adarsh,\n\nTwo points for the review:\n\n"
        "1. The subject: vendor onboarding is still open.\n"
        "2. Please confirm the timeline.\n\nRegards",

    "markdown_emphasis_in_prose":
        "Hi team,\n\nThis is **important** — the __deadline__ moved to Friday.\n\n"
        "Regards",

    "numbered_list_body":
        "Hi Adarsh,\n\nStatus for this week:\n\n1. UPI reconciliation complete\n"
        "2. Settlement report pending\n3. Audit sign-off scheduled\n\nRegards",

    "to_as_english_word":
        "Hi Adarsh,\n\nWe need to confirm the settlement window before Friday.\n\n"
        "Regards",

    "signature_with_address":
        "Hi Adarsh,\n\nPlease share the status.\n\nRegards,\nRavi Kumar\n"
        "ravi.kumar@example.com",

    # ── First-line stress cases for the header-block scan ──────────────────
    # These begin with label-adjacent wording. The scan must NOT treat them as
    # email headers, because they are the opening line of a real message.
    "first_line_to_do_list":
        "To do: confirm the settlement window before Friday.\n\n"
        "Everything else is on track.",

    "first_line_subject_to_approval":
        "Subject to approval, the release will ship on Friday.\n\n"
        "Please review the checklist.",

    "first_line_starts_with_to_verb":
        "To confirm: the reconciliation batch completed at 02:00.\n\n"
        "No action needed.",

    "no_trailing_newline_single_line":
        "Please share the daily status update.",

    # ── Digest-style bodies ────────────────────────────────────────────────
    # "Summarize my open MRs weekly and email me" is a common Cowork task, and
    # its output legitimately opens with "Here is the summary…". These must NOT
    # be mistaken for narration ABOUT an email, or the digest is never sent.
    "digest_here_is_the_summary":
        "Here is the summary of your open merge requests:\n\n"
        "1. Fix auth timeout\n2. Update dependencies",

    "digest_here_is_the_report":
        "Here is the weekly status report:\n\n"
        "All settlement batches completed on time.",

    "digest_here_are_the_updates":
        "Here are the key updates for this week:\n\n- UPI reconciliation done",

    "digest_i_have_prepared":
        "I have prepared the weekly digest:\n\nAll items are on track.",

    "digest_i_have_reviewed":
        "I have reviewed the open tickets:\n\nThree remain unassigned.",

    "digest_email_sent_metric":
        "Email sent count for the week: 42 messages processed.",
}


# ──────────────────────────────────────────────────────────────────────────────
# Leak detection
# ──────────────────────────────────────────────────────────────────────────────
_ADDR_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_MARKUP_RE = re.compile(r"^[\s>*_#`\-]+", re.MULTILINE)
_LABEL_RE = re.compile(
    r"^\s*(?:to|cc|bcc|recipient|recipients|subject|subject\s+line)\s*[:\-]",
    re.IGNORECASE | re.MULTILINE,
)
_JSON_RESIDUE_RE = re.compile(r'"(?:subject|body|saveToSentItems|toRecipients)"\s*:')


def find_leaks(body: str, recipient: str = RECIPIENT, subject: str = SUBJECT) -> list[str]:
    """Return the list of leak kinds present in `body` (empty == clean).

    A "leak" is email metadata that belongs in the Graph payload's own fields
    (to / subject) but has ended up inside the human-readable message body.
    """
    leaks: list[str] = []
    if not body:
        return leaks

    # Strip line-leading markup so **To:** is judged the same as To:
    probe = _MARKUP_RE.sub("", body)

    if recipient and recipient.lower() in body.lower():
        leaks.append("RECIPIENT_ADDR")
    elif _ADDR_RE.search(body):
        leaks.append("STRAY_ADDR")

    if _LABEL_RE.search(probe):
        leaks.append("HEADER_LABEL")

    if _JSON_RESIDUE_RE.search(body):
        leaks.append("JSON_RESIDUE")

    # The literal instruction phrasing should never survive into the body.
    if re.search(r"\b(?:send|sent|drafted|composed|prepared)\b[^\n]{0,40}\bemail\b",
                 body, re.IGNORECASE):
        leaks.append("INSTRUCTION_ECHO")

    return leaks
