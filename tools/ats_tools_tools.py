# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Agentic Platform — ats_tools MCP tools.

Read candidate pipeline + deterministic resume/JD scoring. Used by UC-62
(resume-to-JD matching), UC-63 (interview scheduling), UC-64 (candidate
follow-up sequences). The nuanced fit narrative is the agent's job —
score_keyword_overlap gives a reproducible quantitative signal to combine
with it.

Functions exposed:
  list_pipeline           — list candidates in the requisition pipeline
  score_keyword_overlap   — deterministic JD-vs-resume keyword score (0-100)
  propose_stage_update    — write a PROPOSED stage change to the outbox

Companion server: mcp/servers/ats_tools_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars):
  ATS_TOOLS_DATA_DIR     — root for pipeline CSV (default ./data/ats)
  ATS_TOOLS_PIPELINE_CSV — CSV file with the candidate pipeline (relative
                            to data_dir, default uc64_candidate_followups/
                            pipeline_candidates.csv)
  ATS_TOOLS_OUTBOX_DIR   — where proposed stage-change files are written
                            (default ./outbox/ats)
"""

import os
import re
from typing import List

import pandas as pd


# ── Configuration ────────────────────────────────────────────────────────────

_DATA_DIR      = os.getenv("ATS_TOOLS_DATA_DIR",     "./data/ats")
_PIPELINE_CSV  = os.getenv("ATS_TOOLS_PIPELINE_CSV",
                           "uc64_candidate_followups/pipeline_candidates.csv")
_OUTBOX_DIR    = os.getenv("ATS_TOOLS_OUTBOX_DIR",   "./outbox/ats")


# ── Tool functions ───────────────────────────────────────────────────────────

def list_pipeline(stage: str = "") -> List[dict]:
    """List candidates in the configured requisition pipeline, optionally
    filtered by stage."""
    df = pd.read_csv(os.path.join(_DATA_DIR, _PIPELINE_CSV))
    if stage:
        df = df[df["stage"] == stage]
    return df.to_dict("records")


def score_keyword_overlap(resume_text: str,
                          jd_must_have: List[str],
                          jd_nice_to_have: List[str]) -> dict:
    """Deterministic keyword-coverage score of a resume against JD
    requirement phrases (0-100)."""
    low = resume_text.lower()

    def hit(req: str) -> bool:
        return any(w in low for w in re.findall(r"[a-z]{4,}", req.lower()))

    must = [r for r in jd_must_have if hit(r)]
    nice = [r for r in jd_nice_to_have if hit(r)]
    score = round(
        70 * len(must) / max(len(jd_must_have), 1)
        + 30 * len(nice) / max(len(jd_nice_to_have), 1)
    )
    return {
        "score":             score,
        "must_have_hits":    must,
        "must_have_misses":  [r for r in jd_must_have if r not in must],
        "nice_to_have_hits": nice,
    }


def propose_stage_update(candidate_id: str, new_stage: str, rationale: str) -> dict:
    """Write a PROPOSED stage change to the outbox for recruiter
    confirmation (no direct ATS write — instant tier)."""
    os.makedirs(_OUTBOX_DIR, exist_ok=True)
    f = os.path.join(_OUTBOX_DIR, f"proposed_{candidate_id}_{new_stage}.txt")
    open(f, "w").write(
        f"candidate: {candidate_id}\nproposed_stage: {new_stage}\nrationale: {rationale}\n"
    )
    return {"status": "proposal_created", "file": f}
