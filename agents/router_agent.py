# SPDX-License-Identifier: MIT
"""
RouterAgent — LLM-powered semantic agent routing.

Falls back to this when keyword/@mention matching in _intent_route_question()
finds no match. Uses a lightweight LLM prompt (Ollama/Haiku) to classify
the query domain and select the best PRODUCTION agent from the DB.

Usage:
    from agents.router_agent import router_agent
    result = router_agent.route(question, user_ctx={"department": "payments"})
    # Returns: {"type": "agent", "name": "...", "confidence": 0.85} or None
"""

import json
import re
from typing import Optional

from core.logger import logger


_CONFIDENCE_THRESHOLD = 0.65   # below this → fall through to OrchestratorAgent
_MAX_CATALOG_AGENTS   = 20     # cap so prompt stays small


class RouterAgent:
    """
    Semantic agent router. Instantiated once at module level.

    route() is synchronous and safe to call from FastAPI endpoints.
    It never raises — all errors produce None (safe fall-through).
    """

    def route(self, question: str, user_ctx: Optional[dict] = None) -> Optional[dict]:
        """
        Returns {"type": "agent", "name": str, "confidence": float} or None.
        None means: no good match — let OrchestratorAgent handle it.
        """
        try:
            return self._route(question, user_ctx)
        except Exception as e:
            logger.debug(f"RouterAgent: routing failed → {e}")
            return None

    # ------------------------------------------------------------------ #

    def _route(self, question: str, user_ctx: Optional[dict] = None) -> Optional[dict]:
        agents = self._load_production_agents()
        if not agents:
            return None   # no production agents deployed yet

        dept = (user_ctx or {}).get("department", "")

        # ── Build a compact agent catalog for the LLM ──────────────────
        catalog_lines = []
        for a in agents[:_MAX_CATALOG_AGENTS]:
            tools_hint = ", ".join((a.get("tools") or [])[:5])
            catalog_lines.append(
                f"name={a['name']} | dept={a.get('department') or 'all'} "
                f"| tools=[{tools_hint}] | desc={a['description'][:100]}"
            )
        catalog = "\n".join(catalog_lines)

        dept_hint = f" The user is from department: {dept}." if dept else ""

        prompt = (
            f"You are a routing agent for AiNxt engineering platform.{dept_hint}\n"
            f"Pick the BEST agent for the question from the catalog, or NONE if no agent fits well.\n\n"
            f"CATALOG:\n{catalog}\n\n"
            f"QUESTION: {question[:400]}\n\n"
            f"Respond ONLY with valid JSON (no markdown):\n"
            f'{{"agent": "<name or NONE>", "confidence": 0.0, "reason": "<one sentence>"}}'
        )

        from models.model_router import model_router
        raw = model_router.generate(prompt, model_hint="simple")

        # Extract JSON from response (LLM may wrap in prose)
        m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if not m:
            return None

        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return None

        agent_name = str(data.get("agent", "NONE")).strip()
        confidence = float(data.get("confidence", 0.0))

        if agent_name.upper() == "NONE" or confidence < _CONFIDENCE_THRESHOLD:
            return None

        # Final sanity-check: agent must still be PRODUCTION in DB
        if not self._verify_production(agent_name):
            return None

        logger.info(
            f"RouterAgent: '{question[:60]}' → agent='{agent_name}' "
            f"confidence={confidence:.2f}"
        )
        return {"type": "agent", "name": agent_name, "confidence": confidence}

    # ------------------------------------------------------------------ #

    def _load_production_agents(self) -> list:
        """Load all enabled PRODUCTION agents from Postgres."""
        try:
            from db.database import SessionLocal
            from db.models import AgentRecord
            with SessionLocal() as db:
                rows = (
                    db.query(AgentRecord)
                    .filter(
                        AgentRecord.status  == "PRODUCTION",
                        AgentRecord.enabled == True,
                    )
                    .limit(_MAX_CATALOG_AGENTS)
                    .all()
                )
                return [
                    {
                        "name":        r.name,
                        "description": r.description or "",
                        "tools":       r.tools or [],
                        "department":  r.department,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.debug(f"RouterAgent._load_production_agents: {e}")
            return []

    def _verify_production(self, name: str) -> bool:
        try:
            from db.database import SessionLocal
            from db.models import AgentRecord
            with SessionLocal() as db:
                r = db.query(AgentRecord).filter(
                    AgentRecord.name   == name,
                    AgentRecord.status == "PRODUCTION",
                ).first()
                return r is not None
        except Exception:
            return False


# Module singleton — imported by gateway.py
router_agent = RouterAgent()
