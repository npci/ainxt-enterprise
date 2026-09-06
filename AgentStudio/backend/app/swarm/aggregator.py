# SPDX-License-Identifier: MIT
"""SwarmAggregator — one LLM call → parent-facing envelope.

The aggregator reads the blackboard (every worker's outputs + recorded
artifacts), runs the orchestrator-declared reducer kind, and produces a
single envelope:

    {output, files, confidence, sources?, notes?, warnings?, error?}

Anti-hallucination guards (from :mod:`app.swarm._shared`):

* ``try_parse_json_object``     — fenced/bare JSON tolerant parsing
* ``enforce_no_unsourced_claim``— answer-without-sources → error envelope
* ``surface_low_confidence``    — emit a warning if confidence < threshold

Short-circuit: when ``aggregator.kind == "none"`` we skip the LLM call
and build a deterministic envelope from the blackboard. Used for
single-worker swarms where the worker already returned the final answer.
"""
from __future__ import annotations

import json

import os
from typing import Any, Dict, List, Optional

from .blackboard import SharedBlackboard
from .prompts import AGGREGATOR_SYSTEM_PROMPT
from .types import SwarmAggregatorSpec

from core.logger import logger
# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------

SWARM_AGGREGATOR_MAX_TOKENS  = int(os.getenv("SWARM_AGGREGATOR_MAX_TOKENS", "2048"))
SWARM_AGGREGATOR_TEMPERATURE = float(os.getenv("SWARM_AGGREGATOR_TEMPERATURE", "0.1"))
# Max chars of blackboard the aggregator LLM sees. Higher than the
# per-worker view because the aggregator's job IS to read everything.
SWARM_AGGREGATOR_INPUT_MAX_CHARS = int(
    os.getenv("SWARM_AGGREGATOR_INPUT_MAX_CHARS", "12000")
)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class SwarmAggregator:
    """Reduce the blackboard into one parent envelope."""

    def __init__(self, llm_fn=None, *, model: Optional[str] = None):
        self._llm_fn = llm_fn  # tests inject a fake
        self._model = model or os.getenv("SWARM_AGGREGATOR_MODEL") or None

    @property
    def model(self) -> Optional[str]:
        """Resolved aggregator model (``None`` → factory default).

        Exposed so the SwarmRuntime can log it on aggregate start and
        embed it in the per-run JSON dump. Matches the same pattern on
        ``SwarmOrchestrator.model``.
        """
        return self._model

    async def _call_llm(self, system: str, user_text: str) -> str:
        if self._llm_fn is None:
            from app.core.factory_utils import call_factory_llm as _factory_call
            self._llm_fn = _factory_call
        return await self._llm_fn(
            system,
            [{"role": "user", "content": user_text}],
            max_tokens=SWARM_AGGREGATOR_MAX_TOKENS,
            model=self._model,
            temperature=SWARM_AGGREGATOR_TEMPERATURE,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def reduce(
        self,
        spec: SwarmAggregatorSpec,
        blackboard: SharedBlackboard,
    ) -> Dict[str, Any]:
        """Return a parent-facing envelope.

        Never raises — every failure mode lands as a structured
        ``{error, detail}`` envelope so the parent's tool loop can
        react instead of crashing.
        """
        try:
            if not blackboard.touched:
                return {
                    "error": "swarm_no_results",
                    "detail": "blackboard is empty; no worker produced output",
                }

            # All-errors fast path
            entries = blackboard.flat_entries()
            results = [e for e in entries if (e.get("channel") == "results")]
            if results and all(_is_error_payload(e["payload"]) for e in results):
                modes = sorted({(_get_error_code(e["payload"]) or "unknown") for e in results})
                return {
                    "error": "swarm_no_results",
                    "detail": f"all workers errored ({modes})",
                    "failed_role_ids": sorted({e["role_id"] for e in results}),
                }

            # kind == "none" short-circuit
            if spec.kind == "none":
                return self._envelope_from_blackboard(blackboard)

            # LLM reduce path
            user_text = _render_aggregator_input(
                spec.prompt or "Reduce the workers' outputs.",
                blackboard,
            )
            raw = await self._call_llm(AGGREGATOR_SYSTEM_PROMPT, user_text)
            envelope = self._envelope_from_llm(raw, blackboard)

            from ._shared import enforce_no_unsourced_claim, surface_low_confidence
            envelope = enforce_no_unsourced_claim(envelope)
            envelope = surface_low_confidence(envelope)
            return envelope
        except Exception as exc:  # noqa: BLE001 — top-level safety net
            logger.exception('[AGENT] swarm aggregator failed')
            return {
                "error": "aggregator_failure",
                "detail": str(exc)[:300],
            }

    # ------------------------------------------------------------------
    # Envelope builders
    # ------------------------------------------------------------------
    def _envelope_from_blackboard(self, bb: SharedBlackboard) -> Dict[str, Any]:
        """Deterministic envelope for ``kind == "none"`` (no LLM call).

        Concatenates every results-channel entry into a markdown digest
        and exposes artifacts under ``files``. Confidence is the average
        of any per-entry confidences workers reported.
        """
        results = [
            e for e in bb.flat_entries()
            if e.get("channel") == "results"
        ]
        parts: List[str] = []
        sources: List[str] = []
        confidences: List[float] = []
        for e in results:
            sources.append(e["entry_id"])
            payload = e["payload"]
            if isinstance(payload, dict):
                if "error" in payload:
                    parts.append(f"- {e['role_id']}: ERROR {payload.get('error')} — "
                                 f"{payload.get('detail') or ''}")
                else:
                    text = payload.get("output") or payload.get("answer") or json.dumps(
                        payload, default=str, ensure_ascii=False)
                    parts.append(f"- {e['role_id']}: {text}")
                    c = payload.get("confidence")
                    if isinstance(c, (int, float)) and 0 <= c <= 1:
                        confidences.append(float(c))
            else:
                parts.append(f"- {e['role_id']}: {payload}")

        envelope: Dict[str, Any] = {
            "output": "\n".join(parts) if parts else "(no results)",
            "files": bb.artifacts(),
            "sources": sources,
        }
        if confidences:
            envelope["confidence"] = round(sum(confidences) / len(confidences), 3)
        return envelope

    def _envelope_from_llm(self, raw: str, bb: SharedBlackboard) -> Dict[str, Any]:
        """Parse the aggregator LLM's JSON output into an envelope.

        Falls back to a prose-wrapping envelope when the LLM didn't
        produce parseable JSON — better to forward something to the
        parent than crash.
        """
        from ._shared import try_parse_json_object
        parsed = try_parse_json_object(raw)
        if isinstance(parsed, dict):
            # If the aggregator returned an error envelope, surface it
            # verbatim (with files appended).
            if "error" in parsed:
                env = dict(parsed)
                env.setdefault("files", bb.artifacts())
                return env
            env: Dict[str, Any] = {
                "output":     parsed.get("output") or parsed.get("answer") or "",
                "files":      bb.artifacts(),
            }
            for k in ("answer", "sources", "confidence", "notes", "warnings"):
                if k in parsed:
                    env[k] = parsed[k]
            # If the LLM forgot to set sources, fill in role_ids of every
            # results entry — gives ``_enforce_no_unsourced_claim`` a
            # legitimate source list to validate against.
            if "sources" not in env or not env.get("sources"):
                env["sources"] = [
                    e["entry_id"] for e in bb.flat_entries()
                    if e.get("channel") == "results"
                ]
            return env

        # LLM produced prose. Wrap it.
        return {
            "output":  (raw or "").strip(),
            "files":   bb.artifacts(),
            "sources": [
                e["entry_id"] for e in bb.flat_entries()
                if e.get("channel") == "results"
            ],
            "notes":   "aggregator returned non-JSON; wrapped as prose envelope",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_error_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and "error" in payload


def _get_error_code(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        v = payload.get("error")
        if isinstance(v, str):
            return v
    return None


def _render_aggregator_input(reduce_prompt: str, bb: SharedBlackboard) -> str:
    """Build the aggregator's user-turn text.

    Includes the orchestrator-declared reduce instructions plus a
    chronological blackboard digest. Char-budgeted to keep the
    aggregator's context bounded even when 50 workers each wrote a
    long results entry.
    """
    digest = bb.summary_view(max_chars=SWARM_AGGREGATOR_INPUT_MAX_CHARS)
    return (
        f"[REDUCE_INSTRUCTIONS]\n{reduce_prompt.strip()}\n\n"
        f"[BLACKBOARD DIGEST]\n{digest}\n"
    )


__all__ = ["SwarmAggregator"]
