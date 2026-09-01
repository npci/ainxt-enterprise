# SPDX-License-Identifier: Apache-2.0
"""
routers/review_router.py — P12: Code review API endpoints.

Exposes ReviewEngine (agents/review_engine.py) via REST:

  POST /review/code          — multi-model consensus review
  POST /review/static        — static analysis (Bandit/Semgrep)
  POST /review/architecture  — architecture pattern review
  POST /review/full          — all three phases combined

All endpoints require authentication.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.dependencies import get_current_user
from core.logger import logger

router = APIRouter(tags=["review"])


# ── Request / Response models ────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    code:        str              = Field(..., description="Source code to review")
    language:    str              = Field("python", description="Programming language")
    review_type: str              = Field("general", description="security|quality|performance|general")
    models:      Optional[List[str]] = Field(None, description="Override default model pair")
    repo_filter: Optional[str]   = Field(None, description="Repo filter for architecture review")


class StaticAnalysisRequest(BaseModel):
    code:     str = Field(..., description="Source code to analyse")
    language: str = Field("python", description="Programming language")
    tool:     str = Field("bandit", description="bandit|semgrep")


class FullReviewRequest(BaseModel):
    code:        str              = Field(..., description="Source code to review")
    language:    str              = Field("python", description="Programming language")
    review_type: str              = Field("general", description="security|quality|performance|general")
    static_tool: str              = Field("bandit", description="bandit|semgrep")
    repo_filter: Optional[str]   = Field(None, description="Repo filter for architecture review")
    models:      Optional[List[str]] = Field(None, description="Override default model pair")


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/review/code")
def review_code(
    req: ReviewRequest,
    current_user=Depends(get_current_user),
):
    """
    Multi-model consensus code review.

    Runs the same review prompt on 2 LLMs in parallel (default: Claude Haiku +
    Gemini Flash) and returns agreed/disagreed issues with a consensus score.
    """
    try:
        from agents.review_engine import ReviewEngine
        engine = ReviewEngine()
        result = engine.multi_model_consensus(
            code=req.code,
            review_type=req.review_type,
            models=req.models,
            language=req.language,
        )
        return {
            "agreed":          result.agreed,
            "disagreed":       result.disagreed,
            "consensus_score": result.consensus_score,
            "combined_score":  result.combined_score,
            "model_results": [
                {
                    "model":   r.model,
                    "issues":  r.issues,
                    "summary": r.summary,
                    "score":   r.score,
                }
                for r in result.model_results
            ],
        }
    except Exception as e:
        logger.error(f"review_code endpoint failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/review/static")
def review_static(
    req: StaticAnalysisRequest,
    current_user=Depends(get_current_user),
):
    """
    Static analysis via Bandit (Python) or Semgrep (multi-language).

    Requires Bandit/Semgrep installed in the Docker image.
    Returns an empty list if the tool is not installed.
    """
    try:
        from agents.review_engine import ReviewEngine
        engine = ReviewEngine()
        findings = engine.run_static_analysis(
            code=req.code,
            language=req.language,
            tool=req.tool,
        )
        return {
            "tool":     req.tool,
            "language": req.language,
            "findings": findings,
            "count":    len(findings),
        }
    except Exception as e:
        logger.error(f"review_static endpoint failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/review/architecture")
def review_architecture(
    req: ReviewRequest,
    current_user=Depends(get_current_user),
):
    """
    Architecture pattern review using KB-retrieved patterns.

    Retrieves architecture patterns from the knowledge base and asks the LLM
    whether the submitted code follows them.
    """
    try:
        from agents.review_engine import ReviewEngine
        engine = ReviewEngine()
        result = engine.architecture_review(
            code=req.code,
            repo_filter=req.repo_filter,
            language=req.language,
        )
        return result
    except Exception as e:
        logger.error(f"review_architecture endpoint failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/review/full")
def review_full(
    req: FullReviewRequest,
    current_user=Depends(get_current_user),
):
    """
    Full three-phase code review:
      Phase 1 — Multi-model consensus (LLM)
      Phase 2 — Static analysis (Bandit/Semgrep)
      Phase 3 — Architecture review (KB-based)

    All three phases run; failures in Phase 2 or 3 are non-fatal.
    """
    from agents.review_engine import ReviewEngine
    engine = ReviewEngine()

    # Phase 1: consensus
    try:
        consensus = engine.multi_model_consensus(
            code=req.code,
            review_type=req.review_type,
            models=req.models,
            language=req.language,
        )
        phase1 = {
            "agreed":          consensus.agreed,
            "disagreed":       consensus.disagreed,
            "consensus_score": consensus.consensus_score,
            "combined_score":  consensus.combined_score,
        }
    except Exception as e:
        logger.warning(f"review_full phase1 failed: {e}")
        phase1 = {"error": str(e)}

    # Phase 2: static analysis
    try:
        findings = engine.run_static_analysis(
            code=req.code,
            language=req.language,
            tool=req.static_tool,
        )
        phase2 = {"tool": req.static_tool, "findings": findings, "count": len(findings)}
    except Exception as e:
        logger.warning(f"review_full phase2 failed: {e}")
        phase2 = {"error": str(e), "findings": []}

    # Phase 3: architecture review
    try:
        arch = engine.architecture_review(
            code=req.code,
            repo_filter=req.repo_filter,
            language=req.language,
        )
        phase3 = arch
    except Exception as e:
        logger.warning(f"review_full phase3 failed: {e}")
        phase3 = {"error": str(e), "violations": [], "suggestions": [], "score": 1.0}

    # Aggregate overall score (weighted average: consensus 50%, arch 30%, static 20%)
    consensus_score = phase1.get("combined_score", 1.0) if "error" not in phase1 else 1.0
    arch_score      = phase3.get("score", 1.0)          if "error" not in phase3 else 1.0
    static_penalty  = max(0.0, 1.0 - len(phase2.get("findings", [])) * 0.05)
    overall_score   = round(
        0.50 * consensus_score + 0.30 * arch_score + 0.20 * static_penalty,
        3,
    )

    return {
        "overall_score": overall_score,
        "phase1_consensus":  phase1,
        "phase2_static":     phase2,
        "phase3_architecture": phase3,
    }
