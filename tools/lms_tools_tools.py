# SPDX-License-Identifier: Apache-2.0
"""
AiNxt Agentic Platform — lms_tools MCP tools.

Content catalog access + learning plan persistence. Used by UC-96 (training
material creation) and UC-100 (personalized learning tutor). The adaptive
pedagogy is the agent's job; these tools just supply catalog data and
store plans.

Functions exposed:
  list_modules         — list LMS modules, filterable by level / max duration
  save_learning_plan   — persist a per-learner plan as JSON
  get_learning_plan    — fetch a previously saved plan

Companion server: mcp/servers/lms_tools_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars):
  LMS_TOOLS_DATA_DIR     — root for the LMS module catalog (default ./data/lms)
  LMS_TOOLS_CATALOG_CSV  — CSV file with the module catalog (relative to
                            data_dir, default uc100_personalized_tutor/
                            content_catalog.csv)
  LMS_TOOLS_PLANS_DIR    — where saved learning plans land
                            (default ./outbox/learning_plans)
"""

import json
import os
from typing import List

import pandas as pd


# ── Configuration ────────────────────────────────────────────────────────────

_DATA_DIR    = os.getenv("LMS_TOOLS_DATA_DIR",    "./data/lms")
_CATALOG_CSV = os.getenv("LMS_TOOLS_CATALOG_CSV",
                         "uc100_personalized_tutor/content_catalog.csv")
_PLANS_DIR   = os.getenv("LMS_TOOLS_PLANS_DIR",   "./outbox/learning_plans")


# ── Tool functions ───────────────────────────────────────────────────────────

def list_modules(level: str = "", max_duration_min: int = 0) -> List[dict]:
    """List learning modules from the configured catalog, filterable by
    level and max duration (minutes)."""
    df = pd.read_csv(os.path.join(_DATA_DIR, _CATALOG_CSV))
    if level:
        df = df[df["level"] == level]
    if max_duration_min:
        df = df[df["duration_min"] <= max_duration_min]
    return df.to_dict("records")


def save_learning_plan(learner_id: str, plan: List[dict]) -> dict:
    """Persist a learning plan (list of {week, modules, milestone,
    quiz_topic}) for a learner."""
    os.makedirs(_PLANS_DIR, exist_ok=True)
    p = os.path.join(_PLANS_DIR, f"plan_{learner_id}.json")
    json.dump({"learner_id": learner_id, "plan": plan}, open(p, "w"), indent=2)
    return {"file": p, "weeks": len(plan)}


def get_learning_plan(learner_id: str) -> dict:
    """Fetch a previously saved learning plan for a learner."""
    p = os.path.join(_PLANS_DIR, f"plan_{learner_id}.json")
    if not os.path.exists(p):
        return {"learner_id": learner_id, "plan": None}
    return json.load(open(p))
