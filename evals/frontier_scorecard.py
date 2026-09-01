# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Frontier-pattern scorecard (Phase 2 §2.8 → post-PIPELINE_V2 reality)
# ============================================================
#
# docs/architecture/02-benchmark-frontier.md §2.8 defines the SEVEN architectural
# patterns that produce "frontier feel" — and §2.9 scored AiNxt against them at
# design time. This module re-scores them against the ACTUAL wired reality after
# the PIPELINE_V2 enablement work (Phases 1-6), so the "benchmark met" claim is
# expressed as an honest, inspectable scorecard rather than a vague assertion.
#
# HONESTY RULE (the whole point of this file): a pattern is only ✅ when the
# supporting behavior is genuinely wired AND on by default. Anything still
# partial stays ◐ with the exact remaining gap named. This is orchestration
# parity — it does NOT and cannot claim literal frontier-MODEL parity, which
# depends on model weights/compute, not the COS (see §2.9 and
# WORLD_CLASS_CHAT_ARCHITECTURE.md).
#
# Pure stdlib — importable in a bare env, no runtime side effects.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import List

FULL = "full"        # ✅ wired + default-on
PARTIAL = "partial"  # ◐ wired but conditional / incomplete
GAP = "gap"          # ✗ not yet wired

_MARK = {FULL: "✅", PARTIAL: "◐", GAP: "✗"}


@dataclass(frozen=True)
class PatternScore:
    n: int
    name: str
    status: str
    wired: str      # what is now genuinely wired
    gap: str = ""   # honest remaining gap (empty when FULL)

    @property
    def mark(self) -> str:
        return _MARK.get(self.status, "?")


# The seven patterns, re-scored against post-Phase-1..6 code. Each `wired`
# statement points to the concrete change that makes it true.
SCORECARD: List[PatternScore] = [
    PatternScore(
        1, "Two state systems", FULL,
        wired="Durable memory stays separate from ephemeral history; PIPELINE_V2 "
              "adds a unified RequestContext + ConversationState (cil.state) captured "
              "per turn (gateway shadow-capture, default-on).",
    ),
    PatternScore(
        2, "Reasoning is a phase", FULL,
        wired="Plan panel streamed (plan_event) + first-class tool events "
              "(ToolMarker → {tool}); AND reasoning deltas now stream LIVE as "
              "{reasoning} frames across all providers that expose reasoning text — "
              "Claude thinking_delta, OpenAI o-series delta.reasoning, Gemini 2.5 "
              "thought parts (STREAM_REASONING_DELTAS, default-on). Never "
              "fabricated: emitted only when the provider actually exposes it.",
    ),
    PatternScore(
        3, "Evidence → answer", FULL,
        wired="Per-claim grounding runs a real LOCAL NLI verifier with LLM claim "
              "decomposition (local-only, sentence-split fallback); AND the "
              "pre-flush grounding GATE is now default-on (GROUNDING_PREFLUSH_GATE) "
              "— the answer is buffered, grounded-checked, and a hedge is streamed "
              "BEFORE the answer when confidence is low, so the answer is a function "
              "of verified evidence. Trade-off: buffering removes token streaming "
              "(revertible via env). Fail-safe: answer always flushed.",
    ),
    PatternScore(
        4, "Recency is sacred", FULL,
        wired="Fit-first compaction with a hard flat-floor; PIPELINE_V2 sources the "
              "usable_fraction from the resolved DomainProfile (default-on) without "
              "ever compacting earlier than legacy behavior.",
    ),
    PatternScore(
        5, "Context size = routing", FULL,
        wired="CIL task_complexity drives the router tier for auto turns "
              "(PIPELINE_V2_ROUTING); AND context size is now a first-class routing "
              "dimension — route() promotes to a larger-window tier (deep 256K → "
              "gemini 1M) when a turn's estimated token footprint won't fit with "
              "headroom (CONTEXT_SIZE_ROUTING, default-on). Privacy floor still wins.",
    ),
    PatternScore(
        6, "Retrieve stable artifacts", FULL,
        wired="Hybrid (pgvector + BM25 + rerank) and symbol retrieval over KB/code; "
              "dialogue is never retrieved (fit-first), preserving the invariant.",
    ),
    PatternScore(
        7, "Agency = depth × verify × recover", FULL,
        wired="Dispatch promotes agentic turns to the orchestrator "
              "(PIPELINE_V2_DISPATCH); tool activity is visible (Phase 5); AND loop "
              "depth is now UNIFIED — agents/loop_policy is the single source of "
              "truth for agentic depth. orchestrator (iterations), "
              "react_orchestrator (verify_loops_for_risk) and react_engine "
              "(REACT_ITERATIONS) all derive their ceilings from it; the main loop "
              "is adaptive (ADAPTIVE_LOOP_DEPTH, default-on), floored at the "
              "historical value so it can only deepen, never regress, and capped "
              "to prevent runaway. Each engine fail-safes to its historical "
              "constant on any import error.",
    ),
]

# Privacy floor is not one of the 7 frontier 'feel' patterns but IS the #1
# enterprise invariant closed in Phase 4 — tracked here so the scorecard tells
# the whole enterprise-CIL story.
ENTERPRISE_INVARIANTS: List[PatternScore] = [
    PatternScore(
        0, "Privacy floor (enterprise)", FULL,
        wired="models.model_router now enforces a hard, fail-closed privacy floor: "
              "CONFIDENTIAL+ data is pinned to the local model and cloud fallback is "
              "suppressed (PRIVACY_FLOOR_ENFORCE, default-on).",
        gap="",
    ),
    PatternScore(
        0, "Skill-only doc-gen (enterprise)", FULL,
        wired="Document generation is sandbox-only (platform skillset) with NO "
              "fallback; the skill-gen loop now runs against a generous ~30-min "
              "budget (DOC_TOTAL_BUDGET_SEC) across self-repair attempts so a slow "
              "build succeeds via the skill rather than hard-failing fast.",
        gap="",
    ),
]


def summary() -> dict:
    """Aggregate counts across the 7 frontier patterns (not the enterprise adds)."""
    full = sum(1 for p in SCORECARD if p.status == FULL)
    partial = sum(1 for p in SCORECARD if p.status == PARTIAL)
    gap = sum(1 for p in SCORECARD if p.status == GAP)
    return {"total": len(SCORECARD), "full": full, "partial": partial, "gap": gap}


def render() -> str:
    """Render the scorecard as a markdown table. Pure — returns a string."""
    lines = [
        "| # | Pattern | Status | Now wired | Honest remaining gap |",
        "|---|---|:--:|---|---|",
    ]
    for p in SCORECARD:
        lines.append(f"| {p.n} | {p.name} | {p.mark} | {p.wired} | {p.gap or '—'} |")
    lines.append("")
    lines.append("**Enterprise invariants (beyond the 7 'feel' patterns):**")
    lines.append("")
    lines.append("| Invariant | Status | Now wired |")
    lines.append("|---|:--:|---|")
    for p in ENTERPRISE_INVARIANTS:
        lines.append(f"| {p.name} | {p.mark} | {p.wired} |")
    s = summary()
    lines.append("")
    lines.append(
        f"**Frontier-pattern parity:** {s['full']}/{s['total']} full, "
        f"{s['partial']} partial, {s['gap']} gap. This is *orchestration* parity, "
        f"NOT literal frontier-model parity (which depends on model weights/compute, "
        f"not the COS)."
    )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    print(render())
