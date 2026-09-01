# SPDX-License-Identifier: Apache-2.0
"""
Loop evaluator & controller — accurate, low-hallucination confidence scoring
plus hybrid loop-termination for Build Studio's loop nodes.

Why this module exists
----------------------
Build Studio's ``while``-mode loop previously trusted a single self-reported
``score`` value emitted by the body agent (see ``_build_loop_directive``).
LLMs are notoriously overconfident about their own outputs, so that signal
drifts and the loop either exits too early or runs forever.

This module replaces that single self-reported number with two layered
defences:

  1. ``LLMEvaluator``  — an INDEPENDENT LLM-as-judge call that scores the
     body's output against an explicit rubric. The judge runs at
     ``temperature=0`` with a strict JSON contract and is required to emit
     its reasoning BEFORE the numeric score so the score is anchored by
     chain-of-thought rather than gut feel.

  2. ``LoopController`` — a hybrid stop policy that combines four signals:

         a. Confidence threshold        (e.g. ``score >= 0.85``)
         b. Semantic similarity         (difflib ratio vs. previous output;
                                         no embedding API required)
         c. Regression detection        (current score < previous - delta
                                         → return the previous-best output)
         d. Hard ``maxIterations`` cap  (guards against runaway while-loops)

     The controller always tracks the highest-scoring iteration so the
     caller can return the BEST output even if a later iteration degrades.

Design notes
------------
* No new dependencies — uses stdlib ``difflib`` for similarity so the
  module works in air-gapped deployments without an embeddings endpoint.
* Pure-async, uses the same ``get_llm_client`` / ``Message`` types as the
  rest of the engine so the evaluator is wired through the same provider
  abstraction the generator uses (OpenAI / Ollama / vLLM / LiteLLM).
* Backwards compatible — callers that don't construct a controller keep
  their existing behavior. Only the new optional loop-config keys
  (``useLlmEvaluator``, ``confidenceThreshold``, ``similarityThreshold``,
  ``stopMode``, ``evaluatorRubric``) activate the new code path.
"""

from __future__ import annotations

import json

import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import (
    factory_model,
    fill_blank_llm_fields,
    openai_compatible_api_key, openai_compatible_base_url,
)
from ..llm_handler import Message, get_llm_client
from ..models import LLMConfig

from core.logger import logger
# ---------------------------------------------------------------------------
# Defaults — tuned for "loop until the artifact is good enough"
# ---------------------------------------------------------------------------

# A 0.85 confidence threshold sits above where the judge usually scores
# "acceptable but rough" drafts and below where it scores polished work.
# Tune per-deployment via the loop node's ``confidenceThreshold`` field.
DEFAULT_CONFIDENCE_THRESHOLD = 0.85

# 0.95 ratio on difflib.SequenceMatcher catches the common case where two
# successive iterations re-emit substantially the same text with cosmetic
# tweaks. Lowering this risks false convergence; raising it disables the
# convergence escape hatch entirely.
DEFAULT_SIMILARITY_THRESHOLD = 0.95

# If iteration N scores more than this much lower than iteration N-1, the
# controller treats it as a regression and returns the previous best output
# instead of letting the model keep degrading the artifact.
DEFAULT_REGRESSION_DELTA = 0.05

# Default rubric — 6 simple, universal criteria that work for prose, code,
# planning artifacts, and structured JSON alike. Deliberately generic so
# the judge can score any task without domain-specific knowledge.
# Workflows that need domain-specific criteria can override via the
# ``evaluatorRubric`` loop config key (string overrides the whole prompt,
# dict adds/overrides weighted criteria).
DEFAULT_RUBRIC: Dict[str, Dict[str, Any]] = {
    "relevance": {
        "weight": 0.20,
        "description": "The content matches the requested topic and stays on-scope.",
    },
    "accuracy": {
        "weight": 0.20,
        "description": (
            "Facts, figures, and technical claims are correct and internally "
            "consistent. No fabricated names, numbers, APIs, or citations."
        ),
    },
    "completeness": {
        "weight": 0.20,
        "description": (
            "Covers every key aspect of the topic. No silently dropped "
            "requirements, no TODOs, no placeholders."
        ),
    },
    "structure": {
        "weight": 0.15,
        "description": (
            "Clear organisation with introduction, body, and conclusion "
            "(or an equivalent structure suited to the artifact type)."
        ),
    },
    "coherence": {
        "weight": 0.15,
        "description": (
            "Logical flow between sections. Ideas connect smoothly; no "
            "abrupt jumps or contradictions."
        ),
    },
    "depth": {
        "weight": 0.10,
        "description": (
            "Sufficient technical detail — not too shallow, not padded "
            "with fluff. Each section earns its space."
        ),
    },
}


# ---------------------------------------------------------------------------
# Evaluator result types
# ---------------------------------------------------------------------------

@dataclass
class CriterionScore:
    """One row of the evaluator's rubric scorecard."""
    name: str
    score: float          # 0..1
    weight: float         # 0..1, sums to ~1 across all criteria
    reasoning: str        # short justification, surfaced in the UI


@dataclass
class EvaluationResult:
    """Output of a single ``LLMEvaluator.evaluate`` call."""
    score: float                           # weighted aggregate 0..1
    criteria: List[CriterionScore]
    reasoning: str                         # judge's overall reasoning (CoT)
    raw_response: str                      # for debugging / audit
    judged: bool = True                    # False if judge call failed and
                                           # we fell back to a neutral score


# ---------------------------------------------------------------------------
# LLMEvaluator — independent rubric-driven judge
# ---------------------------------------------------------------------------

class LLMEvaluator:
    """LLM-as-judge with a strict rubric and structured JSON output.

    Key practices that minimise hallucination on the SCORE itself:
      * Temperature is forced to 0 regardless of the generator's temperature.
      * The judge is required to produce its reasoning BEFORE the score
        (chain-of-thought-anchored evaluation — empirically less drifty than
        score-first).
      * Output must be a single JSON object that matches a schema; we parse
        it strictly and fall back to a neutral score on parse failure
        rather than guessing.
      * Each criterion is scored INDEPENDENTLY then weighted, so a single
        "vibe" assessment can't dominate the final number.
    """

    def __init__(
        self,
        llm_cfg: Dict[str, Any],
        rubric: Optional[Dict[str, Dict[str, Any]]] = None,
        custom_system_prompt: Optional[str] = None,
    ) -> None:
        """
        Args:
            llm_cfg: Same LLMConfig dict the generator agent uses. We clone
                it and force temperature=0 so the judge is deterministic
                without forcing the generator to be.
            rubric: Optional override / extension of ``DEFAULT_RUBRIC``.
                Pass a dict ``{criterion_name: {weight, description}}`` to
                replace the default criteria entirely. Weights are
                normalised to sum to 1.0 so users don't have to do the math.
            custom_system_prompt: Fully replaces the built-in system prompt
                when set. Use for advanced workflows that need to bake in
                domain knowledge the rubric alone can't express.
        """
        cfg = dict(llm_cfg or {})
        cfg["temperature"] = 0.0  # judges MUST be deterministic
        # Keep max_tokens generous — the judge needs room for per-criterion
        # reasoning. Without this, long rubrics get truncated and the JSON
        # fence at the end is malformed, killing the parse path.
        cfg.setdefault("max_tokens", 1024)
        self._llm_cfg = cfg

        raw_rubric = rubric or DEFAULT_RUBRIC
        self._rubric = self._normalise_rubric(raw_rubric)
        self._system_prompt = custom_system_prompt or self._build_system_prompt()

    # -- public API ----------------------------------------------------------

    async def evaluate(
        self,
        task: str,
        output: str,
        prior_output: Optional[str] = None,
    ) -> EvaluationResult:
        """Score ``output`` against the rubric for the given ``task``.

        ``prior_output`` is optional context: when present, the judge is
        told the previous iteration's text so it can spot regressions
        (e.g. "iteration N removed a required section that was present
        in iteration N-1"). Pass ``None`` on iteration 0.
        """
        try:
            client = get_llm_client(LLMConfig(**self._llm_cfg))
        except Exception as exc:
            # Mis-configured LLM — surface a neutral 0.5 so the loop can
            # still progress on similarity / max-iter signals rather than
            # crashing the whole run.
            logger.warning(f'[AGENT] Evaluator LLM init failed: {exc}')
            return self._neutral_result(f"evaluator init error: {exc}")

        messages: List[Message] = [
            Message(role="system", content=self._system_prompt),
            Message(
                role="user",
                content=self._build_user_prompt(task, output, prior_output),
            ),
        ]

        try:
            raw = await client.complete(messages)
        except Exception as exc:
            logger.warning(f'[AGENT] Evaluator LLM call failed: {exc}')
            return self._neutral_result(f"evaluator call error: {exc}")

        return self._parse_response(raw)

    # -- prompt construction ------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Compose the judge's system prompt from the active rubric."""
        criteria_lines = []
        for name, cfg in self._rubric.items():
            criteria_lines.append(
                f"- `{name}` (weight {cfg['weight']:.2f}): {cfg['description']}"
            )
        criteria_block = "\n".join(criteria_lines)

        # The JSON schema is described inline rather than via function-
        # calling because not every provider in the OpenAI-compatible
        # ecosystem supports tools reliably (Ollama with some models, older
        # vLLM builds). A strict prose contract works everywhere.
        return (
            "You are an impartial evaluator scoring the output of another "
            "AI assistant against a fixed rubric. You are NOT the assistant "
            "and you do NOT rewrite, fix, or improve the output — you only "
            "score it.\n\n"
            "## Rubric\n"
            "Each criterion is scored from 0.0 (completely fails) to 1.0 "
            "(fully satisfies). Score each criterion INDEPENDENTLY:\n\n"
            f"{criteria_block}\n\n"
            "## Output contract (STRICT)\n"
            "Respond with EXACTLY one JSON object and nothing else. No "
            "markdown fence, no preamble, no commentary after the JSON.\n\n"
            "The object MUST have this shape:\n"
            "{\n"
            '  "reasoning": "<2-4 sentences explaining your overall assessment '
            'BEFORE you commit to numbers>",\n'
            '  "criteria": {\n'
            '    "<criterion_name>": { "score": <0.0..1.0>, "reasoning": '
            '"<one-sentence justification>" },\n'
            "    ...\n"
            "  }\n"
            "}\n\n"
            "## Rules to avoid common evaluator hallucinations\n"
            "1. Write your overall `reasoning` FIRST — let the analysis "
            "drive the score, never the other way around.\n"
            "2. Score each criterion based ONLY on what is visible in the "
            "output. Do not assume missing context exists.\n"
            "3. If a criterion does not apply (e.g. `format_validity` for "
            "a request with no format constraints), give it 1.0 with "
            "reasoning \"not applicable\". Never silently drop criteria.\n"
            "4. Penalise hallucinated facts heavily under "
            "`factual_correctness`. When in doubt, score lower — false "
            "confidence is worse than honest uncertainty.\n"
            "5. If a previous-iteration output is provided and the current "
            "output is WORSE (dropped sections, regressed facts), reflect "
            "that drop in the relevant criterion's score.\n"
            "6. Never output anything except the single JSON object."
        )

    def _build_user_prompt(
        self,
        task: str,
        output: str,
        prior_output: Optional[str],
    ) -> str:
        parts = [
            "## Task the assistant was asked to perform",
            (task or "(no task description provided)").strip(),
            "",
            "## Current iteration's output (the candidate you must score)",
            (output or "(empty output)").strip(),
        ]
        if prior_output:
            # Cap the previous output so a giant artifact doesn't blow the
            # judge's context window. 4000 chars covers ~750 tokens which is
            # enough to spot dropped sections without dominating the prompt.
            parts.extend([
                "",
                "## Previous iteration's output (for regression detection only)",
                prior_output.strip()[:4000],
            ])
        parts.extend([
            "",
            "Now emit the JSON evaluation object per the system contract.",
        ])
        return "\n".join(parts)

    # -- response parsing ---------------------------------------------------

    _JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

    def _parse_response(self, raw: str) -> EvaluationResult:
        """Extract the JSON object from the judge's response.

        Tolerates a markdown fence in case the model ignored the contract,
        but never invents missing fields — a malformed response degrades to
        a neutral score rather than silently producing a confident-looking
        number from nothing.
        """
        if not raw or not raw.strip():
            return self._neutral_result("empty evaluator response")

        text = raw.strip()
        candidate: Optional[str] = None

        # Prefer a code-fenced block when present (most well-behaved models
        # still wrap JSON despite being asked not to).
        fence_match = self._JSON_FENCE_RE.search(text)
        if fence_match:
            candidate = fence_match.group(1)
        else:
            # Fall back to "first balanced {..} block" — same approach used
            # by resolve_routing_state for body-agent outputs, kept simple
            # so a stray brace in the reasoning prose doesn't trip parsing.
            start = text.find("{")
            if start != -1:
                depth = 0
                for i, ch in enumerate(text[start:], start):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            candidate = text[start:i + 1]
                            break

        if not candidate:
            return self._neutral_result(f"no JSON found in: {text[:200]}")

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            return self._neutral_result(
                f"JSON parse error: {exc}; raw={candidate[:200]}"
            )

        if not isinstance(parsed, dict):
            return self._neutral_result("evaluator JSON was not an object")

        overall_reasoning = str(parsed.get("reasoning") or "").strip()
        criteria_payload = parsed.get("criteria") or {}
        if not isinstance(criteria_payload, dict):
            return self._neutral_result("evaluator criteria field malformed")

        scores: List[CriterionScore] = []
        for name, cfg in self._rubric.items():
            entry = criteria_payload.get(name) or {}
            if not isinstance(entry, dict):
                # Missing criterion — count as 0 with explanatory reasoning
                # rather than skipping silently. The user sees which
                # criteria the judge dropped.
                scores.append(CriterionScore(
                    name=name,
                    score=0.0,
                    weight=cfg["weight"],
                    reasoning="evaluator did not score this criterion",
                ))
                continue
            raw_score = entry.get("score")
            try:
                score_val = float(raw_score)
            except (TypeError, ValueError):
                score_val = 0.0
            # Clamp into [0, 1] — guards against models that emit -0.1 or
            # 1.2 to signal extremes.
            score_val = max(0.0, min(1.0, score_val))
            scores.append(CriterionScore(
                name=name,
                score=score_val,
                weight=cfg["weight"],
                reasoning=str(entry.get("reasoning") or "").strip(),
            ))

        weighted = sum(s.score * s.weight for s in scores)
        return EvaluationResult(
            score=round(weighted, 4),
            criteria=scores,
            reasoning=overall_reasoning,
            raw_response=raw,
            judged=True,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _normalise_rubric(
        rubric: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Ensure rubric weights sum to 1.0 even if the user passed raw
        values like ``{"a": 3, "b": 1}`` (treated as 0.75 / 0.25).
        """
        if not rubric:
            return DEFAULT_RUBRIC
        cleaned: Dict[str, Dict[str, Any]] = {}
        total = 0.0
        for name, cfg in rubric.items():
            if not isinstance(cfg, dict):
                continue
            try:
                w = float(cfg.get("weight", 1.0))
            except (TypeError, ValueError):
                w = 1.0
            if w < 0:
                w = 0.0
            total += w
            cleaned[name] = {
                "weight": w,
                "description": str(cfg.get("description") or name),
            }
        if total <= 0:
            # All weights zero — degrade to equal weighting rather than
            # division-by-zero downstream.
            equal = 1.0 / max(1, len(cleaned))
            for cfg in cleaned.values():
                cfg["weight"] = equal
        else:
            for cfg in cleaned.values():
                cfg["weight"] = cfg["weight"] / total
        return cleaned or DEFAULT_RUBRIC

    def _neutral_result(self, note: str) -> EvaluationResult:
        """Return a 0.5 score with a note so the loop can still progress
        on similarity / iteration-cap signals when the judge is unhealthy.
        """
        logger.info(f'[AGENT] LLMEvaluator fallback: {note}')
        scores = [
            CriterionScore(
                name=name,
                score=0.5,
                weight=cfg["weight"],
                reasoning="evaluator unavailable",
            )
            for name, cfg in self._rubric.items()
        ]
        return EvaluationResult(
            score=0.5,
            criteria=scores,
            reasoning=f"Evaluator unavailable — neutral score returned. {note}",
            raw_response="",
            judged=False,
        )


# ---------------------------------------------------------------------------
# LoopController — hybrid stop policy
# ---------------------------------------------------------------------------

@dataclass
class IterationRecord:
    """Per-iteration bookkeeping captured by the controller."""
    index: int
    output: str
    score: float
    evaluation: Optional[EvaluationResult] = None
    similarity_to_prev: Optional[float] = None


@dataclass
class StopDecision:
    """Why the loop stopped this iteration (or didn't)."""
    stop: bool
    reason: str                       # machine-readable: threshold|converged|regression|max_iter|continue
    message: str                      # human-readable, surfaced in SSE summary
    best_record: Optional[IterationRecord] = None


class LoopController:
    """Hybrid termination policy for Build Studio's while-mode loops.

    Used like::

        controller = LoopController(
            confidence_threshold=0.85,
            similarity_threshold=0.95,
            max_iterations=5,
            stop_mode="adaptive",
        )

        for i in range(max_iter_hard_cap):
            output = await run_body_agent(...)
            evaluation = await evaluator.evaluate(task, output, prior_output)
            decision = controller.record(output, evaluation)
            if decision.stop:
                final = decision.best_record
                break

    The controller is intentionally stateful but framework-agnostic so it
    can be unit-tested without spinning up the whole engine.
    """

    def __init__(
        self,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        regression_delta: float = DEFAULT_REGRESSION_DELTA,
        max_iterations: int = 5,
        stop_mode: str = "adaptive",
    ) -> None:
        """
        Args:
            confidence_threshold: Exit when ``score >= this``. Primary
                stop signal when the evaluator is enabled.
            similarity_threshold: Exit when difflib ratio between this
                output and the previous one is >= this. Catches the case
                where the model has stopped improving but the judge keeps
                bouncing around the same score.
            regression_delta: If ``current_score < previous_score -
                regression_delta``, return the previous best instead of
                continuing. Prevents the loop from polishing-then-ruining.
            max_iterations: Hard cap. Always honoured.
            stop_mode: ``"fixed"`` runs exactly ``max_iterations`` rounds
                and ignores threshold/similarity (matches the legacy
                "Run fixed number of times" UX). ``"adaptive"`` honours
                every signal — recommended default.
        """
        self.confidence_threshold = float(confidence_threshold)
        self.similarity_threshold = float(similarity_threshold)
        self.regression_delta = float(regression_delta)
        self.max_iterations = int(max_iterations)
        self.stop_mode = stop_mode if stop_mode in ("fixed", "adaptive") else "adaptive"
        self.history: List[IterationRecord] = []

    # -- public API ---------------------------------------------------------

    def record(
        self,
        output: str,
        evaluation: Optional[EvaluationResult] = None,
    ) -> StopDecision:
        """Record a new iteration and decide whether to keep looping.

        The caller is responsible for actually running the body and the
        evaluator. This method handles the bookkeeping and policy
        decisions only, which makes it easy to test deterministically.
        """
        idx = len(self.history)
        score = evaluation.score if evaluation else 0.0
        prev_output = self.history[-1].output if self.history else None

        sim = None
        if prev_output is not None:
            sim = self._similarity(prev_output, output)

        record = IterationRecord(
            index=idx,
            output=output,
            score=score,
            evaluation=evaluation,
            similarity_to_prev=sim,
        )
        self.history.append(record)

        return self._decide()

    @property
    def best(self) -> Optional[IterationRecord]:
        """Highest-scoring iteration so far. Falls back to the most recent
        record when no scores are present (e.g. evaluator disabled).
        """
        if not self.history:
            return None
        with_scores = [r for r in self.history if r.score is not None]
        if not with_scores:
            return self.history[-1]
        return max(with_scores, key=lambda r: (r.score, r.index))

    # -- decision logic -----------------------------------------------------

    def _decide(self) -> StopDecision:
        if not self.history:
            return StopDecision(stop=False, reason="continue", message="no iterations yet")

        # Hard cap is always honoured first — protects against runaway
        # cost regardless of stop_mode.
        if len(self.history) >= self.max_iterations:
            return StopDecision(
                stop=True,
                reason="max_iter",
                message=(
                    f"Reached max_iterations={self.max_iterations}. "
                    f"Returning best-scoring iteration."
                ),
                best_record=self.best,
            )

        # Fixed mode: ignore quality signals and just run to the cap. This
        # matches the user-facing "Run fixed N times" toggle. We still
        # track best_record so callers can return the best output rather
        # than mechanically the last one — that's a free quality win that
        # doesn't change the iteration count.
        if self.stop_mode == "fixed":
            return StopDecision(
                stop=False,
                reason="continue",
                message=f"fixed-mode: {len(self.history)}/{self.max_iterations}",
            )

        current = self.history[-1]
        previous = self.history[-2] if len(self.history) > 1 else None

        # 1. Confidence threshold — primary exit signal.
        if current.score >= self.confidence_threshold:
            return StopDecision(
                stop=True,
                reason="threshold",
                message=(
                    f"Confidence {current.score:.3f} met threshold "
                    f"{self.confidence_threshold:.2f}."
                ),
                best_record=self.best,
            )

        # 2. Regression detection — return the previous best.
        # Only checked once we have a baseline AND the judge actually ran;
        # neutral fallback scores (judged=False) would otherwise trigger
        # false regressions on every iteration.
        if previous is not None and self._scored(current) and self._scored(previous):
            if current.score < previous.score - self.regression_delta:
                return StopDecision(
                    stop=True,
                    reason="regression",
                    message=(
                        f"Score regressed from {previous.score:.3f} to "
                        f"{current.score:.3f} (>{self.regression_delta:.2f} "
                        f"drop). Returning previous best."
                    ),
                    best_record=self.best,
                )

        # 3. Semantic-ish similarity convergence. difflib.SequenceMatcher
        # ratio handles whitespace, ordering, and small edits gracefully
        # without an embeddings API. Only meaningful when we have a prior
        # iteration to compare against.
        # Gated by ``_scored`` (judged=True) so the self-report path only
        # exits on the user-configured threshold or maxIter, never on a
        # hidden similarity signal. Only the LLM judge path — where every
        # score is a real independent rating — earns the similarity gate.
        if current.similarity_to_prev is not None and self._scored(current):
            if current.similarity_to_prev >= self.similarity_threshold:
                return StopDecision(
                    stop=True,
                    reason="converged",
                    message=(
                        f"Output converged "
                        f"(similarity={current.similarity_to_prev:.3f} >= "
                        f"{self.similarity_threshold:.2f}). Further "
                        f"iterations unlikely to change result."
                    ),
                    best_record=self.best,
                )

        return StopDecision(
            stop=False,
            reason="continue",
            message=(
                f"iter {current.index}: score={current.score:.3f}, "
                f"sim={current.similarity_to_prev}"
            ),
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _scored(rec: IterationRecord) -> bool:
        """True iff the evaluator actually ran for this record."""
        return rec.evaluation is not None and rec.evaluation.judged

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Difflib-based similarity ratio in [0, 1].

        We normalise whitespace first so trivial reformatting (e.g. extra
        blank lines on iteration N) doesn't tank the similarity score and
        defeat the convergence check. ``SequenceMatcher`` is O(n*m) in the
        worst case, so we also cap inputs at 8000 chars on each side —
        plenty to detect convergence on a typical artifact without blowing
        the per-iteration latency budget.
        """
        if not a or not b:
            return 0.0
        norm_a = re.sub(r"\s+", " ", a).strip()[:8000]
        norm_b = re.sub(r"\s+", " ", b).strip()[:8000]
        if not norm_a or not norm_b:
            return 0.0
        return SequenceMatcher(None, norm_a, norm_b).ratio()


# ---------------------------------------------------------------------------
# Factory helpers — read loop-node config into evaluator/controller objects
# ---------------------------------------------------------------------------

def build_evaluator_from_config(
    loop_cfg: Dict[str, Any],
    llm_cfg: Dict[str, Any],
) -> Optional[LLMEvaluator]:
    """Construct an ``LLMEvaluator`` from raw loop-node config.

    Returns ``None`` when the loop doesn't opt in (``useLlmEvaluator``
    falsy), which is the legacy path — body's self-reported score is used
    as-is. This keeps every existing workflow working untouched.
    """
    if not loop_cfg.get("useLlmEvaluator"):
        return None

    rubric_override = loop_cfg.get("evaluatorRubric")
    rubric: Optional[Dict[str, Dict[str, Any]]] = None
    custom_prompt: Optional[str] = None

    if isinstance(rubric_override, str) and rubric_override.strip():
        custom_prompt = rubric_override.strip()
    elif isinstance(rubric_override, dict) and rubric_override:
        rubric = rubric_override

    # Precedence (low → high): inherited body LLM cfg → OPENAI_COMPATIBLE_*
    # env defaults (only filling unset fields) → per-loop evaluatorLlmConfig.
    # Routing through the OpenAI-compatible gateway (not FACTORY_*) ensures
    # the judge uses the same endpoint as /llm/models, which is what the
    # "Judge model" dropdown is populated from — so any UI pick is routable.
    _openai_token = openai_compatible_api_key()
    evaluator_llm_cfg = fill_blank_llm_fields(
        dict(llm_cfg or {}),
        base_url=openai_compatible_base_url(),
        api_key=_openai_token,
        model_name=factory_model(),
    )

    judge_override = loop_cfg.get("evaluatorLlmConfig")
    if isinstance(judge_override, dict):
        # Skip empty-string overrides so an unset UI dropdown doesn't
        # wipe out the inherited model.
        cleaned_override = {
            k: v for k, v in judge_override.items()
            if not (isinstance(v, str) and not v.strip())
        }
        evaluator_llm_cfg.update(cleaned_override)

    return LLMEvaluator(
        llm_cfg=evaluator_llm_cfg,
        rubric=rubric,
        custom_system_prompt=custom_prompt,
    )


def build_controller_from_config(loop_cfg: Dict[str, Any]) -> LoopController:
    """Construct a ``LoopController`` from raw loop-node config.

    All keys are optional; missing keys fall back to the module defaults.
    ``maxIterations`` mirrors the existing loop-node field so users don't
    have to set it twice.

    When ``confidenceThreshold`` is absent from the payload (which happens
    when the LLM evaluator is disabled — workflowStore.js only forwards it
    inside the evaluator block), fall back to whatever numeric threshold
    the user typed into the LoopWhileEditor condition row (e.g.
    ``Confidence Score > 0.85`` → 0.85).
    """
    threshold = loop_cfg.get("confidenceThreshold")
    if threshold is None:
        score_fields = {"score", "confidence", "confidence_score", "quality"}
        for case in (loop_cfg.get("cases") or []):
            for cond in (case.get("conditions") or []):
                if str(cond.get("field") or "").strip().lower() in score_fields:
                    try:
                        threshold = max(0.0, min(1.0, float(cond.get("value"))))
                    except (TypeError, ValueError):
                        threshold = None
                    if threshold is not None:
                        break
            if threshold is not None:
                break
    if threshold is None:
        threshold = DEFAULT_CONFIDENCE_THRESHOLD
    return LoopController(
        confidence_threshold=float(threshold),
        similarity_threshold=float(
            loop_cfg.get("similarityThreshold", DEFAULT_SIMILARITY_THRESHOLD)
        ),
        regression_delta=float(
            loop_cfg.get("regressionDelta", DEFAULT_REGRESSION_DELTA)
        ),
        max_iterations=int(loop_cfg.get("maxIterations") or 5),
        stop_mode=str(loop_cfg.get("stopMode") or "adaptive").lower(),
    )


# ---------------------------------------------------------------------------
# Budget meter — token + wall-clock caps for the canvas Loop node
# ---------------------------------------------------------------------------
#
# A deliberately small, standalone accumulator (no Goal / ctx precedence like
# app/loop/runner.py::BudgetMeter — that one belongs to the governed outer
# loop). Token counts are ESTIMATED with the same len(text)//4 heuristic used
# throughout the codebase because the streaming LLM client doesn't surface a
# usage object per call. That's fine for a "stop runaway loops" guardrail.

@dataclass
class LoopBudget:
    """Accumulating token + wall-clock meter for one loop run."""
    tokens_cap: int
    wall_clock_cap_s: int
    tokens_used: int = 0
    wall_clock_s: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    def charge(self, text: str) -> int:
        """Add an iteration's output to the token tally (estimated).

        Returns the increment so callers can log / surface it. Mirrors the
        ``len(buf_text) // 4`` estimate in app/loop/runner.py:430.
        """
        inc = max(1, len(text or "") // 4)
        self.tokens_used += inc
        return inc

    def over_budget(self) -> Tuple[bool, str]:
        """Refresh wall-clock and report whether either cap has tripped.

        Returns ``(over, reason)`` where reason is ``"tokens"`` /
        ``"wall_clock"`` / ``""``. Tokens are checked first so the message is
        deterministic when both trip on the same tick.
        """
        self.wall_clock_s = time.monotonic() - self.started_at
        if self.tokens_used >= self.tokens_cap:
            return True, "tokens"
        if self.wall_clock_s >= self.wall_clock_cap_s:
            return True, "wall_clock"
        return False, ""

    def snapshot(self) -> Dict[str, Any]:
        """Plain dict for the ``budget_consumed`` SSE payload."""
        return {
            "tokens": self.tokens_used,
            "wall_clock_s": round(self.wall_clock_s, 2),
            "cap": {
                "tokens_cap": self.tokens_cap,
                "wall_clock_cap_s": self.wall_clock_cap_s,
            },
        }


def build_budget_from_config(loop_cfg: Dict[str, Any]) -> Optional[LoopBudget]:
    """Construct a ``LoopBudget`` from the loop node's ``budget`` block.

    Returns ``None`` when the node carries no ``budget`` config so the loop
    runs uncapped (the existing behavior for every workflow saved before this
    feature existed).
    """
    budget = loop_cfg.get("budget")
    if not isinstance(budget, dict) or not budget:
        return None
    try:
        tokens_cap = int(budget.get("tokens_cap") or 0)
    except (TypeError, ValueError):
        tokens_cap = 0
    try:
        wall_clock_cap_s = int(budget.get("wall_clock_cap_s") or 0)
    except (TypeError, ValueError):
        wall_clock_cap_s = 0
    # Neither cap set → no meter (uncapped). A single cap is fine; the unset
    # one is treated as effectively infinite so it never trips.
    if tokens_cap <= 0 and wall_clock_cap_s <= 0:
        return None
    return LoopBudget(
        tokens_cap=tokens_cap if tokens_cap > 0 else 2**62,
        wall_clock_cap_s=wall_clock_cap_s if wall_clock_cap_s > 0 else 2**62,
    )


def verifier_timeout_from_config(loop_cfg: Dict[str, Any]) -> Optional[float]:
    """Read ``verify.timeout_s`` — the per-iteration judge call timeout.

    Returns ``None`` when unset so the judge call is awaited without a wrapper
    (existing behavior). A positive value wraps the judge in asyncio.wait_for.
    """
    verify = loop_cfg.get("verify")
    if not isinstance(verify, dict):
        return None
    raw = verify.get("timeout_s")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


# ---------------------------------------------------------------------------
# Convenience: serialise records for SSE / persistence
# ---------------------------------------------------------------------------

def evaluation_to_dict(ev: Optional[EvaluationResult]) -> Optional[Dict[str, Any]]:
    """Render an evaluation into a plain dict for SSE payloads / DB rows.

    Returns ``None`` for ``None`` so the caller can pass-through without
    a null check. Caps the per-criterion reasoning strings so a chatty
    judge can't bloat the SSE stream.
    """
    if ev is None:
        return None
    return {
        "score": ev.score,
        "judged": ev.judged,
        "reasoning": (ev.reasoning or "")[:500],
        "criteria": [
            {
                "name": c.name,
                "score": c.score,
                "weight": round(c.weight, 4),
                "reasoning": (c.reasoning or "")[:300],
            }
            for c in ev.criteria
        ],
    }


def record_to_dict(rec: Optional[IterationRecord]) -> Optional[Dict[str, Any]]:
    """Render an iteration record for SSE payloads."""
    if rec is None:
        return None
    return {
        "index": rec.index,
        "score": rec.score,
        "similarity_to_prev": rec.similarity_to_prev,
        "evaluation": evaluation_to_dict(rec.evaluation),
        "output_preview": (rec.output or "")[:400],
    }


def decision_to_dict(decision: StopDecision) -> Dict[str, Any]:
    """Render a stop decision for SSE payloads."""
    return {
        "stop": decision.stop,
        "reason": decision.reason,
        "message": decision.message,
        "best_record": record_to_dict(decision.best_record),
    }
