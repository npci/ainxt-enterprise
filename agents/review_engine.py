# SPDX-License-Identifier: Apache-2.0
"""
agents/review_engine.py — P12: Review engine.

Phase 1: Multi-model consensus review (pure LLM, no external tools).
Phase 2: Static analysis (Bandit/Semgrep via subprocess in sandbox).
Phase 3: Architecture review (KB-based pattern matching).

DESIGN
------
Phase 1 (M):
  - Run same review prompt on 2 models in parallel (ThreadPoolExecutor)
  - Default: [CLAUDE_HAIKU, GEMINI_FLASH] (cost-efficient)
  - Parse both into structured ReviewResult
  - Flag claims in one but not the other
  - Return {agreed: [...], disagreed: [...], consensus_score: float}

Phase 2 (L):
  - Register bandit_scan and semgrep_scan as ToolDefinition entries
  - Execute via subprocess in existing sandbox/ container
  - Parse JSON output → [{rule, severity, line, message}]
  - Requires: Bandit/Semgrep installed in Docker image

Phase 3 (L):
  - Retrieve architecture patterns from KB (hybrid_retriever, tag="architecture")
  - LLM review: "Does this code follow the architecture patterns?"
  - Return {violations: [], suggestions: [], score: float}

WHAT IS NOT BUILT
-----------------
- Real-time streaming review
- Review of non-code content (docs, configs) — SDLC-only
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional

from core.logger import logger


@dataclass
class ReviewResult:
    """Structured result from a single model's code review."""
    model:    str
    issues:   List[dict] = field(default_factory=list)
    # Each issue: {severity: "HIGH"|"MEDIUM"|"LOW", message: str, line: int|None}
    summary:  str = ""
    score:    float = 1.0  # 0.0 = many issues, 1.0 = clean


@dataclass
class ConsensusResult:
    """Result of multi-model consensus review."""
    agreed:          List[dict]   # issues both models flagged
    disagreed:       List[dict]   # issues only one model flagged
    consensus_score: float        # 0.0–1.0 (1.0 = full agreement)
    model_results:   List[ReviewResult] = field(default_factory=list)
    combined_score:  float = 1.0  # average of model scores


class ReviewEngine:
    """
    Multi-model consensus code review engine.

    Phase 1: Multi-model consensus (pure LLM)
    Phase 2: Static analysis (Bandit/Semgrep via sandbox subprocess)
    Phase 3: Architecture review (KB-based)
    """

    # ── Phase 1: Multi-model consensus ──────────────────────────────────────

    def multi_model_consensus(
        self,
        code: str,
        review_type: str = "security",
        models: Optional[List[str]] = None,
        language: str = "python",
    ) -> ConsensusResult:
        """
        Run the same review prompt on 2 models in parallel and compute consensus.

        review_type: "security" | "quality" | "performance" | "general"
        models: list of model identifiers (default: [CLAUDE_HAIKU, GEMINI_FLASH])

        Returns ConsensusResult with agreed/disagreed issues and consensus_score.
        """
        if models is None:
            try:
                # GEMINI_FLASH does not exist in core.model_registry, so this
                # import raised ImportError on EVERY call and the fallback below
                # always won -- the review engine used gemini-2.0-flash whatever
                # the deployment had configured. GEMINI_TEXT_MODEL is the real
                # constant.
                from core.model_registry import CLAUDE_HAIKU, GEMINI_TEXT_MODEL
                models = [CLAUDE_HAIKU, GEMINI_TEXT_MODEL]
            except Exception:
                # Last-resort fallback: read the same env vars the registry uses.
                # No hardcoded model IDs — if the vars are unset the list is
                # filtered to non-empty strings so callers get an empty list
                # rather than a broken model name.
                import os as _os
                models = [
                    m for m in (
                        _os.getenv("CLAUDE_HAIKU", ""),
                        _os.getenv("GEMINI_TEXT_MODEL", ""),
                    ) if m
                ]

        prompt = self._build_review_prompt(code, review_type, language)

        # Run both models in parallel
        results: List[ReviewResult] = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="review-model") as pool:
            futures = {
                pool.submit(self._run_review, model, prompt): model
                for model in models[:2]
            }
            for future in as_completed(futures):
                model = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.warning(f"ReviewEngine: model {model} failed: {e}")
                    results.append(ReviewResult(model=model, issues=[], summary=f"Review failed: {e}", score=0.5))

        if not results:
            return ConsensusResult(agreed=[], disagreed=[], consensus_score=0.0)

        if len(results) == 1:
            return ConsensusResult(
                agreed=results[0].issues,
                disagreed=[],
                consensus_score=1.0,
                model_results=results,
                combined_score=results[0].score,
            )

        # Compute consensus
        agreed, disagreed = self._compute_consensus(results[0].issues, results[1].issues)
        total_issues = len(agreed) + len(disagreed)
        consensus_score = len(agreed) / max(total_issues, 1)
        combined_score = sum(r.score for r in results) / len(results)

        logger.info(
            f"ReviewEngine: consensus agreed={len(agreed)} disagreed={len(disagreed)} "
            f"score={consensus_score:.2f} combined={combined_score:.2f}"
        )

        return ConsensusResult(
            agreed=agreed,
            disagreed=disagreed,
            consensus_score=consensus_score,
            model_results=results,
            combined_score=combined_score,
        )

    def _build_review_prompt(self, code: str, review_type: str, language: str) -> str:
        """Build a structured review prompt."""
        focus = {
            "security":    "security vulnerabilities, injection risks, authentication issues, data exposure",
            "quality":     "code quality, maintainability, naming, complexity, duplication",
            "performance": "performance bottlenecks, inefficient algorithms, memory leaks, N+1 queries",
            "general":     "overall code quality, security, performance, and maintainability",
        }.get(review_type, "overall code quality")

        return (
            f"Review the following {language} code for {focus}.\n\n"
            f"Output a JSON object with this exact structure:\n"
            f'{{"issues": [{{"severity": "HIGH|MEDIUM|LOW", "message": "...", "line": null}}, ...], '
            f'"summary": "...", "score": 0.0-1.0}}\n\n'
            f"score: 1.0 = clean code, 0.0 = many critical issues.\n"
            f"Output ONLY the JSON — no markdown, no explanation.\n\n"
            f"Code:\n```{language}\n{code[:3000]}\n```"
        )

    def _run_review(self, model: str, prompt: str) -> ReviewResult:
        """Run a review prompt on a single model."""
        try:
            from models.model_router import get_router
            raw = get_router().generate(prompt, model_hint="simple", temperature=0.0).strip()

            # Parse JSON response
            import json
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                data = json.loads(m.group())
                issues = data.get("issues", [])
                summary = data.get("summary", "")
                score = float(data.get("score", 1.0))
                return ReviewResult(model=model, issues=issues, summary=summary, score=score)
        except Exception as e:
            logger.warning(f"ReviewEngine._run_review failed for {model}: {e}")

        return ReviewResult(model=model, issues=[], summary="Parse failed", score=0.5)

    def _compute_consensus(
        self,
        issues_a: List[dict],
        issues_b: List[dict],
    ) -> tuple:
        """
        Compare two issue lists and return (agreed, disagreed).

        Two issues are considered the same if their messages have >50% word overlap.
        """
        def _words(msg: str) -> set:
            return set(re.findall(r'\b[a-zA-Z]{3,}\b', (msg or "").lower()))

        agreed = []
        disagreed = []
        matched_b = set()

        for issue_a in issues_a:
            words_a = _words(issue_a.get("message", ""))
            best_match = None
            best_score = 0.0
            for i, issue_b in enumerate(issues_b):
                if i in matched_b:
                    continue
                words_b = _words(issue_b.get("message", ""))
                if not words_a or not words_b:
                    continue
                overlap = len(words_a & words_b) / len(words_a | words_b)
                if overlap > best_score:
                    best_score = overlap
                    best_match = i

            if best_score >= 0.5 and best_match is not None:
                matched_b.add(best_match)
                agreed.append({**issue_a, "_consensus": "agreed"})
            else:
                disagreed.append({**issue_a, "_consensus": "model_a_only"})

        # Issues in B not matched to A
        for i, issue_b in enumerate(issues_b):
            if i not in matched_b:
                disagreed.append({**issue_b, "_consensus": "model_b_only"})

        return agreed, disagreed

    # ── Phase 2: Static analysis ─────────────────────────────────────────────

    def run_static_analysis(
        self,
        code: str,
        language: str = "python",
        tool: str = "bandit",
    ) -> List[dict]:
        """
        Run static analysis via subprocess in the sandbox container.

        tool: "bandit" (Python) | "semgrep" (multi-language)

        Returns list of findings: [{rule, severity, line, message}]
        Requires: Bandit/Semgrep installed in Docker image.
        """
        # SEC-15: validate language against allowlist to prevent unexpected file extensions
        _ALLOWED_LANGUAGES = {"python", "javascript", "typescript", "java", "go", "ruby", "php", "c", "cpp"}
        safe_language = language.lower() if language.lower() in _ALLOWED_LANGUAGES else "python"
        if safe_language != language.lower():
            logger.warning(f"ReviewEngine: unsupported language {language!r}, defaulting to python")

        if safe_language != "python" and tool == "bandit":
            logger.info(f"ReviewEngine: bandit only supports Python, skipping for {language}")
            return []

        tmp_path = None
        try:
            import os as _os
            import tempfile
            import subprocess
            import json as _json

            # SEC-15: use delete=False but ensure cleanup in finally block
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=f".{safe_language}", delete=False
            ) as f:
                f.write(code)
                tmp_path = f.name

            if tool == "bandit":
                cmd = ["bandit", "-f", "json", "-q", tmp_path]
            elif tool == "semgrep":
                cmd = ["semgrep", "--json", "--config", "auto", tmp_path]
            else:
                return []

            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            output = proc.stdout or proc.stderr

            findings = []
            try:
                data = _json.loads(output)
                if tool == "bandit":
                    for r in data.get("results", []):
                        findings.append({
                            "rule":     r.get("test_id", ""),
                            "severity": r.get("issue_severity", "MEDIUM").upper(),
                            "line":     r.get("line_number"),
                            "message":  r.get("issue_text", ""),
                        })
                elif tool == "semgrep":
                    for r in data.get("results", []):
                        findings.append({
                            "rule":     r.get("check_id", ""),
                            "severity": r.get("extra", {}).get("severity", "MEDIUM").upper(),
                            "line":     r.get("start", {}).get("line"),
                            "message":  r.get("extra", {}).get("message", ""),
                        })
            except Exception as _pe:
                logger.warning(f"ReviewEngine: failed to parse {tool} output: {_pe}")

            logger.info(f"ReviewEngine: {tool} found {len(findings)} issues")
            return findings

        except FileNotFoundError:
            logger.info(f"ReviewEngine: {tool} not installed — skipping static analysis")
            return []
        except Exception as e:
            logger.warning(f"ReviewEngine.run_static_analysis failed: {e}")
            return []
        finally:
            # SEC-15: always clean up the temp file
            if tmp_path:
                try:
                    import os as _os2
                    _os2.unlink(tmp_path)
                except OSError:
                    pass

    # ── Phase 3: Architecture review ─────────────────────────────────────────

    def architecture_review(
        self,
        code: str,
        repo_filter: Optional[str] = None,
        language: str = "python",
    ) -> dict:
        """
        Review code against architecture patterns from the KB.

        Retrieves architecture patterns tagged "architecture" from the KB,
        then asks the LLM whether the code follows them.

        Returns: {violations: [], suggestions: [], score: float}
        """
        try:
            from models.hybrid_retriever import hybrid_retrieve_context
            arch_chunks = hybrid_retrieve_context(
                question=f"architecture patterns {language} best practices",
                repo_filter=repo_filter or "global",
                complexity="simple",
                max_chunks=4,
            )

            if not arch_chunks:
                return {"violations": [], "suggestions": [], "score": 1.0, "note": "no_arch_patterns_found"}

            arch_context = "\n\n".join(arch_chunks[:4])
            prompt = (
                f"Review the following {language} code against these architecture patterns:\n\n"
                f"Architecture patterns:\n{arch_context[:1500]}\n\n"
                f"Code to review:\n```{language}\n{code[:2000]}\n```\n\n"
                f"Output a JSON object:\n"
                f'{{"violations": ["..."], "suggestions": ["..."], "score": 0.0-1.0}}\n'
                f"score: 1.0 = fully compliant, 0.0 = many violations.\n"
                f"Output ONLY the JSON."
            )

            from models.model_router import get_router
            raw = get_router().generate(prompt, model_hint="simple", temperature=0.0).strip()

            import json as _json
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                data = _json.loads(m.group())
                return {
                    "violations":  data.get("violations", []),
                    "suggestions": data.get("suggestions", []),
                    "score":       float(data.get("score", 1.0)),
                }
        except Exception as e:
            logger.warning(f"ReviewEngine.architecture_review failed: {e}")

        return {"violations": [], "suggestions": [], "score": 1.0, "note": "review_failed"}
