# SPDX-License-Identifier: Apache-2.0
"""
Structured Agent Handoff — context carrier for agent-to-agent delegation.

When one agent delegates to another via call_agent(), it can serialize a
HandoffContext so the receiving agent:
  - skips the retrieval step (chunks already provided)
  - inherits domain classification + intent
  - knows what prior agents already attempted

Usage (calling side):
    from agents.handoff import HandoffContext
    ctx = HandoffContext(
        question        = state.question,
        retrieved_chunks= context_chunks,
        intent          = "code_review",
        domain          = "gitlab",
        session_id      = session_id,
        user_ctx        = state.user_ctx,
        complexity      = "complex",
        prior_outputs   = [{"agent": "triage", "answer": "..."}],
    )
    call_agent(agent_name="code_reviewer", message=question, context_json=ctx.to_json())

Usage (receiving side — handled automatically by AgentRunner._run_inner):
    handoff = HandoffContext.from_json(context_json)
    # AgentRunner skips _run_tools() and injects handoff.retrieved_chunks directly
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class HandoffContext:
    """
    Structured context passed from one agent to another.

    Fields:
        question         Original user question being answered.
        retrieved_chunks List of text chunks from prior retrieval (skip re-retrieval).
        intent           Classified intent string (e.g. "code_review", "jira_triage").
        domain           Domain/system this relates to (e.g. "gitlab", "jira", "payments").
        session_id       Shared session ID for memory continuity.
        user_ctx         User context dict from JWT (user_id, department, ad_level).
        complexity       Query complexity: "simple" | "medium" | "complex".
        prior_outputs    List of {"agent": str, "answer": str} from earlier agents.
        metadata         Arbitrary extra data.
    """
    question:          str
    retrieved_chunks:  List[str]             = field(default_factory=list)
    intent:            str                   = ""
    domain:            str                   = ""
    session_id:        Optional[str]         = None
    user_ctx:          Dict[str, Any]        = field(default_factory=dict)
    complexity:        str                   = "medium"
    prior_outputs:     List[Dict[str, str]]  = field(default_factory=list)
    metadata:          Dict[str, Any]        = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "HandoffContext":
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def build_context_text(self) -> str:
        """Render retrieved_chunks + prior_outputs as a context block for the LLM prompt."""
        parts = []

        if self.retrieved_chunks:
            parts.append("## Retrieved Context\n" + "\n\n".join(self.retrieved_chunks))

        if self.prior_outputs:
            prior_lines = []
            for po in self.prior_outputs:
                prior_lines.append(f"**{po.get('agent', 'agent')}**: {po.get('answer', '')}")
            parts.append("## Prior Agent Outputs\n" + "\n".join(prior_lines))

        return "\n\n".join(parts) if parts else ""
