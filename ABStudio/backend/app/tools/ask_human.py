# SPDX-License-Identifier: Apache-2.0
"""
ask_human — built-in Human-in-the-Loop tool.

This tool is *declarative*: it does not execute anything. When the LLM
emits an ``ask_human`` tool call, the native engine intercepts the call
by name in ``_run_agent`` and routes it to the HITL interrupt branch —
the call is what triggers the pause, the engine never invokes ``.call()``
through the normal tool dispatch path.

The same surface (``name``, ``description``, ``call``, ``to_function_spec``)
as ``_CatalogTool`` / ``_PythonFunctionTool`` so the engine can list it
in the tool spec sent to the LLM without any special-casing during
prompt construction.

Registered only for agents with ``hitlMode != "off"``; non-HITL agents
never see this tool in their function spec.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


ASK_HUMAN_TOOL_NAME = "ask_human"


class AskHumanTool:
    """In-engine HITL tool. Never actually executed."""

    name = ASK_HUMAN_TOOL_NAME
    description = (
        "Pause the workflow and ask the human invoker for a decision or "
        "clarification before continuing. Use this whenever you need a "
        "judgement call you should not make alone (approvals, missing "
        "facts, ambiguous routing). Provide a clear question and 2–5 "
        "short options the human can pick from."
    )

    async def call(self, arguments: Dict[str, Any]) -> str:
        # Defensive fallback: if something ever dispatches this through
        # the normal tool path (it should not — the engine intercepts by
        # name), surface a clear no-op string rather than crashing.
        question = (arguments or {}).get("question", "")
        return json.dumps({
            "status": "pending_human",
            "question": question,
        })

    def to_function_spec(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The question to put to the human. Be specific "
                            "and self-contained — the human may not have "
                            "the full conversation context in front of them."
                        ),
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "2–5 short option labels the human can pick. "
                            "Omit for a free-form answer."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional one-paragraph background the human "
                            "should read before answering."
                        ),
                    },
                },
                "required": ["question"],
            },
        }


def extract_ask_human_payload(arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalise the ``ask_human`` arguments into a payload safe to send
    to the frontend HITL card. Robust against missing/malformed fields so
    a sloppy LLM doesn't crash the interrupt path."""
    args = arguments or {}
    question = str(args.get("question") or "").strip() or "The agent needs your input."
    raw_options = args.get("options")
    if isinstance(raw_options, list):
        options: List[str] = [str(o).strip() for o in raw_options if str(o).strip()]
    else:
        options = []
    context = str(args.get("context") or "").strip()
    return {
        "question": question,
        "options": options[:5],
        "context": context,
    }
