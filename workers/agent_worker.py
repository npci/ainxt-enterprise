# SPDX-License-Identifier: MIT
# ============================================================
# AGENT WORKER — rq job wrapper for agent runs
# ============================================================

from core.logger import logger


def run_agent_job(payload: dict) -> str:
    """rq job: run a named agent with a message."""
    agent_name = payload.get("agent_name", "")
    message    = payload.get("message", "")
    session_id = payload.get("session_id")

    try:
        from agents.agent_builder import agent_runner
        result = agent_runner.run(agent_name, message, session_id)
        return result.answer if result.success else f"[Agent error: {result.error}]"
    except Exception as e:
        logger.error(f"agent_worker: agent {agent_name!r} failed → {e}")
        raise
