# SPDX-License-Identifier: Apache-2.0
"""The eval set: hand-built + synthetic multi-turn chats with labeled probes.

Kept small, explicit, and deterministic. Each case is designed to stress ONE
failure mode from docs §6. The synthetic long-context generator inflates a
transcript past the overflow trigger to exercise the summary path.
"""
from __future__ import annotations

from typing import List

from .model import Case, Probe, Turn


def _pair(u: str, a: str, tag: str = "", rag_mode: str = "off") -> List[Turn]:
    return [
        Turn(role="user", content=u, tag=tag, rag_mode=rag_mode),
        Turn(role="assistant", content=a, tag=tag, rag_mode=rag_mode),
    ]


def _filler_pairs(n: int, prefix: str = "topic") -> List[Turn]:
    """Generate n benign, low-signal Q/A pairs to bulk up a transcript.

    Each pair is long enough (~600 chars) that n pairs reliably crosses the
    overflow trigger for smaller windows in the harness.
    """
    turns: List[Turn] = []
    body = ("This is background discussion that carries no probe-critical fact. "
            "It exists purely to consume context tokens so the transcript grows "
            "toward the overflow threshold used by the assembly strategy. ") * 12
    for i in range(n):
        turns += _pair(
            f"Question {i} about {prefix} {i}: {body}",
            f"Answer {i} regarding {prefix} {i}: {body}",
            tag=f"{prefix}_{i}",
        )
    return turns


def build_cases() -> List[Case]:
    cases: List[Case] = []

    # ── 1. Recall (short chat, fits window) ──────────────────────────────────
    turns = []
    turns += _pair("My project budget is 4200 dollars.",
                   "Noted — your budget is 4200 dollars.", tag="budget")
    turns += _pair("The deployment target is us-east-2.",
                   "Got it, deploying to us-east-2.", tag="region")
    turns += _pair("Let's discuss the schema next.",
                   "Sure, what tables do you have?", tag="schema")
    cases.append(Case(
        name="recall_short",
        turns=turns,
        probes=[
            Probe(kind="recall", question="What is my budget?", answer_fact="4200"),
            Probe(kind="recall", question="What region?", answer_fact="us-east-2"),
        ],
    ))

    # ── 2. Override (short chat) — fresh must win ────────────────────────────
    turns = []
    turns += _pair("My budget is 4200 dollars.",
                   "Noted, 4200 dollars.", tag="budget_old")
    turns += _pair("Some unrelated planning talk here.",
                   "Understood.", tag="mid")
    turns += _pair("Actually, scratch that — the budget is now 9800 dollars.",
                   "Updated: budget is 9800 dollars.", tag="budget_new")
    cases.append(Case(
        name="override_short",
        turns=turns,
        probes=[
            Probe(kind="override", question="What is my current budget?",
                  answer_fact="9800", stale_fact="4200"),
        ],
    ))

    # ── 3. Distractor — one relevant turn among many similar ─────────────────
    turns = _filler_pairs(6, prefix="budget-lookalike")
    turns += _pair("The API key rotation interval is 90 days.",
                   "Recorded: rotate API keys every 90 days.", tag="rotation")
    turns += _filler_pairs(6, prefix="config-lookalike")
    cases.append(Case(
        name="distractor_short",
        turns=turns,
        probes=[
            Probe(kind="distractor", question="How often do we rotate keys?",
                  answer_fact="90 days"),
        ],
    ))

    # ── 4. Long-context recall — forces overflow/summary path ────────────────
    # Fact stated EARLY, then a huge filler tail. Under the summary path the
    # early specific value is at risk of being distilled away.
    turns = []
    turns += _pair("Remember: the license key is ZX-7788-QP.",
                   "Stored the license key ZX-7788-QP.", tag="license")
    turns += _filler_pairs(90, prefix="longctx")  # keeps total turns < _RAW_TURNS cap
    cases.append(Case(
        name="longctx_recall",
        turns=turns,
        probes=[
            Probe(kind="longctx", question="What is the license key?",
                  answer_fact="ZX-7788-QP"),
        ],
    ))

    # ── 5. Long-context override — fresh value near the end, stale early ─────
    turns = []
    turns += _pair("The primary contact is Alice.",
                   "Primary contact: Alice.", tag="contact_old")
    turns += _filler_pairs(90, prefix="longctx2")
    turns += _pair("Update: the primary contact is now Bob.",
                   "Primary contact updated to Bob.", tag="contact_new")
    cases.append(Case(
        name="longctx_override",
        turns=turns,
        probes=[
            Probe(kind="override", question="Who is the primary contact?",
                  answer_fact="Bob", stale_fact="Alice"),
        ],
    ))

    # ── 6. Overflow recall — fact stated EARLY in a transcript that exceeds
    # even the flat 150K trigger, so BOTH strategies take the summary path.
    # The early specific value (order id) is only preserved if it survives
    # distillation. This is the summary-loss failure the tiers target.
    # Fact placed ~30 turns from the end so it is INSIDE the 200-turn cap and
    # INSIDE the summary's "old" band (older than _SUMMARY_TURNS=20 recent),
    # isolating summary-distillation loss from the raw-cap-drop effect.
    turns = []
    turns += _filler_pairs(60, prefix="overflow-head")
    turns += _pair("Confirm my order id is ORD-55231 for the audit.",
                   "Confirmed, order id ORD-55231 recorded for the audit.",
                   tag="order")
    turns += _filler_pairs(25, prefix="overflow-tail")
    cases.append(Case(
        name="overflow_summary_recall",
        turns=turns,
        probes=[
            Probe(kind="longctx", question="What is my order id?",
                  answer_fact="ORD-55231"),
        ],
    ))

    # ── 7. Buried-fact overflow — the critical value sits DEEP inside a long
    # old turn (past the ~120-char distillation window). This models the real
    # summary-loss failure: the gist is kept but the specific value is dropped.
    # It should FAIL on any strategy that relies on distillation for old turns
    # (both current and c1) — exactly the case Tier 1 retrieval (C3) is meant
    # to rescue later. Recording it now proves the baseline weakness honestly.
    buried_prefix = ("We spent this turn reviewing the migration plan in detail, "
                     "covering rollback steps, staging order, and owner sign-off, "
                     "none of which is the probe fact and all of which pushes the "
                     "real value far past the distillation cutoff. ") * 2
    turns = []
    turns += _filler_pairs(60, prefix="buried-head")
    # Value appears ONLY inside the long user turn (no concise assistant echo),
    # so it lives past the distillation cutoff with nothing short to preserve it.
    turns += _pair(
        buried_prefix + "The production database password rotation code is PWD-ROT-4417.",
        "Acknowledged the full migration review and next steps.",
        tag="buried",
    )
    turns += _filler_pairs(25, prefix="buried-tail")
    cases.append(Case(
        name="overflow_buried_fact",
        turns=turns,
        probes=[
            Probe(kind="longctx", question="What is the rotation code?",
                  answer_fact="PWD-ROT-4417"),
        ],
    ))

    return cases
