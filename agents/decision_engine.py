# SPDX-License-Identifier: Apache-2.0
import json

from core.logger import logger


class DecisionEngine:

    def __init__(self, llm):
        self.llm = llm

    def decide(self, state):

        prompt = f"""
You are an autonomous code assistant agent.

Decide which tools to use.

Return ONLY JSON.

Available tools:
- rewrite
- retrieve
- analyze
- compliance
- local_llm
- generate

Question:
{state.question}

Context available: {"YES" if state.context else "NO"}

JSON format:
{{
 "rewrite": true/false,
 "retrieve": true/false,
 "analyze": true/false,
 "compliance": true/false,
 "local_llm": true/false,
 "generate": true/false
}}
"""

        response = self.llm.complete(prompt)

        try:

            text = response.strip()

            start = text.find("{")
            end = text.rfind("}") + 1

            decision = json.loads(text[start:end])

            logger.info(f"AGENT DECISION: {decision}")

            return decision

        except Exception as e:

            logger.error(f"Decision parse failed: {e}")

            return {
                "rewrite": False,
                "retrieve": True,
                "analyze": False,
                "compliance": False,
                "local_llm": False,
                "generate": True
            }
