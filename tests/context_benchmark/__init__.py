# SPDX-License-Identifier: Apache-2.0
"""Chat-context benchmark harness (Phase C0).

Offline, deterministic evaluation of the *context-assembly* decision — which
turns / summaries get selected into the prompt for a given transcript and
question. It deliberately makes **no LLM calls**: a probe passes/fails purely on
whether the fact needed to answer it is present (or correctly absent) in the
assembled context. This isolates hallucination source (B) "context-layer
omission", the only source the tier design controls.

See docs/CHAT_CONTEXT_STRATEGY_DESIGN.md §6 (benchmark harness / ship gate).
"""
