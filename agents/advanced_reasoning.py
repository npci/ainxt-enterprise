# SPDX-License-Identifier: MIT
"""
agents/advanced_reasoning.py — P7: Advanced loop techniques.

Implements three reasoning strategies beyond ReAct + Reflexion:

  TreeOfThoughts     — parallel branch exploration for HIGH-risk complex queries
  SelfConsistency    — majority-vote sampling for LOW-risk factual queries
  ChainOfVerification — claim-level verification when confidence < 0.75

All strategies are gated by ADVANCED_REASONING_ENABLED=true (default false).
Individual strategies have their own env-var controls.

COST CONTROLS
-------------
- ToT: only for HIGH-risk + complex queries; n_branches=3, max_depth=2
- SC:  only for LOW-risk factual queries; n_samples=3
- CoVe: only when confidence < 0.75; verifies top-5 claims only
- Kill-switch: ADVANCED_REASONING_ENABLED=false disables all three

DESIGN
------
All strategies use the same LLM proxy pattern as react_orchestrator.py
(_run_with_fallback / _expand_query). No direct Anthropic SDK calls.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Callable

from core.logger import logger


def _advanced_reasoning_enabled() -> bool:
    return os.getenv("ADVANCED_REASONING_ENABLED", "false").lower() in ("1", "true", "yes")


def _llm_call(prompt: str, temperature: float = 0.0, model_hint: str = "simple") -> str:
    """Single LLM call via the model router. Returns "" on failure."""
    try:
        from models.model_router import get_router
        return get_router().generate(prompt, model_hint=model_hint, temperature=temperature).strip()
    except Exception as e:
        logger.debug(f"advanced_reasoning._llm_call failed: {e}")
        return ""


# ============================================================
# TREE OF THOUGHTS
# ============================================================

class TreeOfThoughts:
    """
    Parallel branch exploration for HIGH-risk complex queries.

    Algorithm:
      1. Generate n_branches initial thoughts in parallel
      2. Score each thought (0.0–1.0) with a lightweight evaluator prompt
      3. Select the best thought, generate next level (up to max_depth)
      4. Return the best leaf answer

    Gated: ADVANCED_REASONING_ENABLED=true + TOT_ENABLED=true (default true when AR enabled)
    """

    def __init__(
        self,
        n_branches: int = None,
        max_depth: int = 2,
    ):
        self.n_branches = n_branches or int(os.getenv("TOT_N_BRANCHES", "3"))
        self.max_depth = max_depth

    def run(
        self,
        goal: str,
        system_prompt: str,
        executor: Optional[Callable] = None,
    ) -> str:
        """
        Run Tree of Thoughts on goal. Returns the best answer found.
        Falls back to a single direct LLM call on any error.
        """
        if not _advanced_reasoning_enabled():
            return _llm_call(f"{system_prompt}\n\n{goal}", model_hint="complex")

        try:
            logger.info(f"[ToT] starting n_branches={self.n_branches} max_depth={self.max_depth}")
            current_thoughts = [goal]

            for depth in range(self.max_depth):
                # Generate branches from each current thought in parallel
                branch_prompts = []
                for thought in current_thoughts:
                    for _ in range(self.n_branches):
                        branch_prompts.append(
                            f"{system_prompt}\n\n"
                            f"Think step by step about the following question. "
                            f"Provide a partial answer or reasoning path:\n\n{thought}"
                        )

                with ThreadPoolExecutor(
                    max_workers=min(len(branch_prompts), 6),
                    thread_name_prefix="tot-branch",
                ) as pool:
                    futures = [pool.submit(_llm_call, p, 0.7, "complex") for p in branch_prompts]
                    branches = [f.result() for f in as_completed(futures)]

                branches = [b for b in branches if b.strip()]
                if not branches:
                    break

                # Score each branch
                scored = []
                for branch in branches:
                    score = self._score_thought(goal, branch)
                    scored.append((score, branch))

                scored.sort(key=lambda x: x[0], reverse=True)
                logger.info(
                    f"[ToT] depth={depth+1} branches={len(scored)} "
                    f"best_score={scored[0][0]:.2f}"
                )

                # Keep top-2 thoughts for next depth level
                current_thoughts = [b for _, b in scored[:2]]

            # Final answer: expand the best thought into a complete response
            best_thought = current_thoughts[0] if current_thoughts else goal
            final_prompt = (
                f"{system_prompt}\n\n"
                f"Based on the following reasoning, provide a complete, final answer:\n\n"
                f"Reasoning: {best_thought}\n\n"
                f"Original question: {goal}\n\n"
                f"Final answer:"
            )
            answer = _llm_call(final_prompt, temperature=0.0, model_hint="complex")
            logger.info(f"[ToT] completed, answer length={len(answer)}")
            return answer or best_thought

        except Exception as e:
            logger.error(f"[ToT] failed ({e}), falling back to direct call")
            return _llm_call(f"{system_prompt}\n\n{goal}", model_hint="complex")

    def _score_thought(self, goal: str, thought: str) -> float:
        """Score a thought branch 0.0–1.0 using a lightweight evaluator prompt."""
        if not thought.strip():
            return 0.0
        prompt = (
            f"Rate the following reasoning on a scale of 0.0 to 1.0 for how well it "
            f"addresses the question. Output ONLY a number between 0.0 and 1.0.\n\n"
            f"Question: {goal[:300]}\n\n"
            f"Reasoning: {thought[:500]}\n\n"
            f"Score (0.0-1.0):"
        )
        try:
            raw = _llm_call(prompt, temperature=0.0, model_hint="simple")
            m = re.search(r"(\d+\.?\d*)", raw)
            if m:
                score = float(m.group(1))
                return max(0.0, min(1.0, score))
        except Exception:
            pass
        return 0.5


# ============================================================
# SELF-CONSISTENCY
# ============================================================

class SelfConsistency:
    """
    Majority-vote sampling for LOW-risk factual queries.

    Algorithm:
      1. Generate n_samples completions in parallel at temperature=0.7
      2. Cluster similar answers by string similarity
      3. Return the answer from the largest cluster

    Gated: ADVANCED_REASONING_ENABLED=true + SC_ENABLED=true (default true when AR enabled)
    """

    def __init__(self, n_samples: int = None):
        self.n_samples = n_samples or int(os.getenv("SELF_CONSISTENCY_N_SAMPLES", "3"))

    def run(self, goal: str, system_prompt: str) -> str:
        """
        Run Self-Consistency on goal. Returns the majority-vote answer.
        Falls back to a single direct LLM call on any error.
        """
        if not _advanced_reasoning_enabled():
            return _llm_call(f"{system_prompt}\n\n{goal}", model_hint="simple")

        try:
            logger.info(f"[SC] starting n_samples={self.n_samples}")
            prompt = f"{system_prompt}\n\n{goal}"

            with ThreadPoolExecutor(
                max_workers=self.n_samples,
                thread_name_prefix="sc-sample",
            ) as pool:
                futures = [
                    pool.submit(_llm_call, prompt, 0.7, "simple")
                    for _ in range(self.n_samples)
                ]
                samples = [f.result() for f in as_completed(futures)]

            samples = [s for s in samples if s.strip()]
            if not samples:
                return _llm_call(prompt, temperature=0.0, model_hint="simple")

            # Cluster by string similarity (simple overlap)
            best = self._majority_vote(samples)
            logger.info(f"[SC] completed, {len(samples)} samples, selected answer length={len(best)}")
            return best

        except Exception as e:
            logger.error(f"[SC] failed ({e}), falling back to direct call")
            return _llm_call(f"{system_prompt}\n\n{goal}", model_hint="simple")

    def _majority_vote(self, samples: List[str]) -> str:
        """Return the sample from the largest similarity cluster."""
        if len(samples) == 1:
            return samples[0]

        def _similarity(a: str, b: str) -> float:
            """Simple word-overlap Jaccard similarity."""
            wa = set(a.lower().split())
            wb = set(b.lower().split())
            if not wa or not wb:
                return 0.0
            return len(wa & wb) / len(wa | wb)

        # Build clusters: each sample starts its own cluster
        clusters: List[List[str]] = []
        for sample in samples:
            placed = False
            for cluster in clusters:
                if _similarity(sample, cluster[0]) >= 0.4:
                    cluster.append(sample)
                    placed = True
                    break
            if not placed:
                clusters.append([sample])

        # Return the first sample from the largest cluster
        largest = max(clusters, key=len)
        return largest[0]


# ============================================================
# CHAIN OF VERIFICATION
# ============================================================

class ChainOfVerification:
    """
    Claim-level verification when confidence < 0.75.

    Algorithm:
      1. Extract factual claims from the answer (LLM call)
      2. For each claim: targeted RAG search to verify
      3. Mark VERIFIED / UNVERIFIED / CONTRADICTED
      4. If CONTRADICTED: regenerate with correction context

    Gated: ADVANCED_REASONING_ENABLED=true + COV_ENABLED=true (default true when AR enabled)
    """

    def __init__(self, max_claims: int = 5):
        self.max_claims = max_claims

    def verify(
        self,
        goal: str,
        answer: str,
        observations: list,
        repo_filter: Optional[str] = None,
    ) -> str:
        """
        Verify factual claims in answer against RAG evidence.
        Returns the original answer if no contradictions found,
        or a regenerated answer with corrections if contradictions exist.
        """
        if not _advanced_reasoning_enabled():
            return answer

        try:
            logger.info("[CoVe] starting claim verification")
            claims = self._extract_claims(answer)
            if not claims:
                return answer

            logger.info(f"[CoVe] extracted {len(claims)} claims to verify")
            verified, contradicted = [], []

            for claim in claims[:self.max_claims]:
                status = self._verify_claim(claim, observations, repo_filter)
                if status == "CONTRADICTED":
                    contradicted.append(claim)
                    logger.info(f"[CoVe] CONTRADICTED: {claim[:80]}")
                else:
                    verified.append(claim)

            if not contradicted:
                logger.info(f"[CoVe] all {len(verified)} claims verified — no corrections needed")
                return answer

            # Regenerate with correction context
            correction_prompt = (
                f"The following answer contains contradicted claims that need correction:\n\n"
                f"Original answer: {answer}\n\n"
                f"Contradicted claims (these are WRONG — do not include them):\n"
                + "\n".join(f"- {c}" for c in contradicted)
                + f"\n\nOriginal question: {goal}\n\n"
                f"Please provide a corrected answer that removes or fixes the contradicted claims. "
                f"Keep all verified information. Be concise."
            )
            corrected = _llm_call(correction_prompt, temperature=0.0, model_hint="complex")
            if corrected:
                logger.info(
                    f"[CoVe] regenerated answer after {len(contradicted)} contradiction(s), "
                    f"length={len(corrected)}"
                )
                return corrected
            return answer

        except Exception as e:
            logger.error(f"[CoVe] failed ({e}), returning original answer")
            return answer

    def _extract_claims(self, answer: str) -> List[str]:
        """Extract factual claims from the answer using an LLM call."""
        if not answer.strip():
            return []
        prompt = (
            f"Extract the top {self.max_claims} specific factual claims from the following text. "
            f"Output ONLY a JSON array of strings, e.g. [\"claim1\", \"claim2\"]. "
            f"Focus on verifiable facts (numbers, names, behaviors, APIs). "
            f"No explanation.\n\nText: {answer[:1500]}"
        )
        try:
            raw = _llm_call(prompt, temperature=0.0, model_hint="simple")
            m = re.search(r'\[.*?\]', raw, re.DOTALL)
            if m:
                import json
                claims = json.loads(m.group())
                return [c.strip() for c in claims if isinstance(c, str) and c.strip()]
        except Exception:
            pass
        return []

    def _verify_claim(
        self,
        claim: str,
        observations: list,
        repo_filter: Optional[str] = None,
    ) -> str:
        """
        Verify a single claim against tool observations and RAG.
        Returns "VERIFIED", "UNVERIFIED", or "CONTRADICTED".
        """
        # First check against existing tool observations (free — no LLM call)
        evidence_text = " ".join(
            str(o.get("result_preview", "")) for o in (observations or []) if o.get("ok")
        ).lower()

        if evidence_text:
            # Simple keyword check: if claim keywords appear in evidence, likely verified
            claim_words = set(re.findall(r'\b[a-zA-Z][a-zA-Z0-9_]{2,}\b', claim.lower()))
            evidence_words = set(re.findall(r'\b[a-zA-Z][a-zA-Z0-9_]{2,}\b', evidence_text))
            overlap = len(claim_words & evidence_words)
            if overlap >= 3:
                return "VERIFIED"

        # RAG verification: search for the claim
        try:
            from models.hybrid_retriever import hybrid_retrieve_context
            rag_chunks = hybrid_retrieve_context(
                question=claim,
                repo_filter=repo_filter or "global",
                complexity="simple",
                max_chunks=3,
            )
            if not rag_chunks:
                return "UNVERIFIED"

            rag_text = " ".join(rag_chunks[:3])
            verify_prompt = (
                f"Does the following evidence support, contradict, or not mention this claim?\n\n"
                f"Claim: {claim}\n\n"
                f"Evidence: {rag_text[:800]}\n\n"
                f"Output ONLY one word: VERIFIED, CONTRADICTED, or UNVERIFIED."
            )
            result = _llm_call(verify_prompt, temperature=0.0, model_hint="simple").upper().strip()
            if "CONTRADICTED" in result:
                return "CONTRADICTED"
            if "VERIFIED" in result:
                return "VERIFIED"
        except Exception as e:
            logger.debug(f"[CoVe] claim verification RAG failed: {e}")

        return "UNVERIFIED"
