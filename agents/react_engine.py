# SPDX-License-Identifier: Apache-2.0
"""
ReACT Engine — plan → act → observe → reflect → repeat

Reusable iterative reasoning engine for Threads @AiNxt and SDLC pipeline.
- Reasoning iterations use Claude Sonnet 4.6 (cost control)
- Final synthesis uses "solution" model hint (Opus if ENABLE_OPUS=true, else Sonnet)
- Max 3 iterations (same ceiling as Orchestrator)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from core.logger import logger

# Gap #7 (7/7): the ReAct micro-loop ceiling is sourced from the UNIFIED loop
# policy so no engine keeps its own private constant. Fail-safe to 3.
try:
    from agents.loop_policy import REACT_ITERATIONS as MAX_REACT_ITERATIONS
except Exception:  # noqa: BLE001
    MAX_REACT_ITERATIONS = 3
CONFIDENCE_THRESHOLD  = 0.80


@dataclass
class ReactStep:
    action:     str   # retrieve | analyze | critique | synthesize
    query:      str
    result:     str   = ""
    confidence: float = 0.0


@dataclass
class ReactResult:
    answer:     str
    steps:      list  = field(default_factory=list)
    iterations: int   = 0
    model_used: str   = ""
    confidence: float = 0.0


class ReactEngine:
    """
    Iterative ReACT loop for tasks that require deep reasoning over a codebase.

    Args:
        task:                 Natural-language task (also used as the base retrieval query).
        retrieve_fn:          Callable[[query: str], list[str]] — returns text chunks.
        max_iterations:       Hard cap on reasoning loops (default 3).
        confidence_threshold: Stop early once this score is reached.
        synthesis_hint:       Model hint for the final answer ("solution" → Opus if ENABLE_OPUS).
        iteration_hint:       Model hint for mid-loop calls ("complex" → Sonnet — cost control).
    """

    def __init__(
        self,
        task:                 str,
        retrieve_fn:          Callable[[str], list[str]],
        max_iterations:       int   = MAX_REACT_ITERATIONS,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        synthesis_hint:       str   = "solution",
        iteration_hint:       str   = "complex",
    ):
        self.task                 = task
        self.retrieve_fn          = retrieve_fn
        self.max_iterations       = max_iterations
        self.confidence_threshold = confidence_threshold
        self.synthesis_hint       = synthesis_hint
        self.iteration_hint       = iteration_hint
        self._last_critique       = ""

    # ------------------------------------------------------------------
    def run(self) -> ReactResult:
        from models.model_router import model_router

        steps: list[ReactStep] = []
        gathered: list[str]    = []
        analysis:  str         = ""
        confidence: float      = 0.0

        for i in range(self.max_iterations):
            logger.info(f"[ReactEngine] iteration {i + 1}/{self.max_iterations}")

            # ── Retrieve ────────────────────────────────────────────────
            query = self._retrieval_query(i)
            try:
                chunks = self.retrieve_fn(query) or []
                seen = set(gathered)
                for c in chunks:
                    if c not in seen:
                        gathered.append(c)
                        seen.add(c)
                gathered = gathered[:8]
            except Exception as e:
                logger.warning(f"[ReactEngine] retrieve error: {e}")
            steps.append(ReactStep(
                action="retrieve", query=query[:120],
                result=f"{len(gathered)} chunks total",
            ))

            # ── Analyse ────────────────────────────────────────────────
            prompt = self._analysis_prompt(gathered, analysis, i)
            try:
                analysis = model_router.generate(prompt, model_hint=self.iteration_hint)
            except Exception as e:
                logger.warning(f"[ReactEngine] analysis LLM error: {e}")
                break
            steps.append(ReactStep(
                action="analyze", query=f"iter-{i + 1}",
                result=analysis[:150],
            ))

            # ── Confidence ─────────────────────────────────────────────
            confidence = self._confidence(gathered, analysis)
            logger.info(f"[ReactEngine] confidence={confidence:.2f}")
            if confidence >= self.confidence_threshold:
                break

            # ── Critique (only if more loops remain) ───────────────────
            if i < self.max_iterations - 1:
                try:
                    self._last_critique = model_router.generate(
                        self._critique_prompt(analysis),
                        model_hint=self.iteration_hint,
                    )
                    steps.append(ReactStep(
                        action="critique", query=f"iter-{i + 1}",
                        result=self._last_critique[:150],
                    ))
                except Exception as e:
                    logger.warning(f"[ReactEngine] critique error: {e}")

        # ── Synthesize — best model ─────────────────────────────────────
        try:
            answer = model_router.generate(
                self._synthesis_prompt(gathered, analysis),
                model_hint=self.synthesis_hint,
            )
        except Exception as e:
            logger.warning(f"[ReactEngine] synthesis fallback ({e}) — using last analysis")
            answer = analysis

        model_used = getattr(model_router, "last_model_label", self.synthesis_hint)
        steps.append(ReactStep(action="synthesize", query="final", result=answer[:150]))

        return ReactResult(
            answer=answer,
            steps=steps,
            iterations=sum(1 for s in steps if s.action == "analyze"),
            model_used=model_used,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    def _retrieval_query(self, iteration: int) -> str:
        if iteration == 0 or not self._last_critique:
            return self.task[:300]
        return f"{self.task[:200]}\n\nFocus: {self._last_critique[:200]}"

    def _analysis_prompt(self, chunks: list[str], prev: str, iteration: int) -> str:
        ctx = "\n\n".join(chunks[:6]) if chunks else "No code context available."
        p = (
            f"You are an expert engineering analyst.\n"
            f"Task: {self.task}\n\n"
            f"Retrieved Code Context:\n{ctx[:3000]}\n"
        )
        if prev and iteration > 0:
            p += f"\nPrevious analysis (refine, do not repeat):\n{prev[:800]}\n"
        p += "\nProvide a structured technical analysis. Reference exact file paths and function names from the context."
        return p

    def _critique_prompt(self, analysis: str) -> str:
        return (
            f"Review this engineering analysis and identify gaps:\n"
            f"1. What specific information is missing?\n"
            f"2. What assumptions were made without code evidence?\n"
            f"3. What should be retrieved next to strengthen the analysis?\n\n"
            f"Analysis:\n{analysis[:1500]}\n\n"
            f"Reply concisely — focus only on what is missing."
        )

    def _synthesis_prompt(self, chunks: list[str], analysis: str) -> str:
        ctx = "\n\n".join(chunks[:6]) if chunks else ""
        return (
            f"You are an expert engineering assistant producing the final answer.\n\n"
            f"Task: {self.task}\n\n"
            + (f"Code Context:\n{ctx[:2000]}\n\n" if ctx else "")
            + f"Reasoning from analysis iterations:\n{analysis[:2000]}\n\n"
            f"Produce a final structured response:\n"
            f"## Root Cause\n## Proposed Fix\n## Impact Assessment\n## Priority\n\n"
            f"Be precise — reference exact file paths and functions. No generic advice."
        )

    @staticmethod
    def _confidence(chunks: list[str], analysis: str) -> float:
        chunk_score    = min(len(chunks) / 6.0,     1.0)
        analysis_score = min(len(analysis) / 800.0,  1.0)
        return round(chunk_score * 0.6 + analysis_score * 0.4, 2)