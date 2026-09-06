# SPDX-License-Identifier: MIT
# ============================================================
# RequestContext — Wave 1 strangler-fig capture object
# ============================================================
#
# One typed object that captures what the /ask handler (gateway.py:2198
# `ask_ai`) already computes in stages 0-3 (tracing, guards, identity,
# product scoping, intent pre-pass). See docs/architecture/03-request-lifecycle.md
# (step L1) and 04-conversation-intelligence-layer.md.
#
# WAVE 1 CONTRACT (important):
#   - This is a SHADOW-POPULATE object: gateway builds it behind the
#     default-OFF `PIPELINE_V2` flag and NOTHING downstream reads it yet.
#   - It is written-but-never-read so its presence is provably inert in prod.
#   - Later waves will start reading it and move stage logic here.
#
# This module imports ONLY the standard library so it is importable in a bare
# test environment (gateway.py itself cannot be imported — it pulls in HSM /
# redis at import time). Matches the @dataclass house style of
# models/doc_intent.py:41 (DocIntent) and core/rate_limiter.py:113.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle; resolver + cil are pure too
    from profiles.resolver import EffectivePolicy
    from cil.state import ConversationState
    from pipeline.dispatch import DispatchDecision


@dataclass
class RequestContext:
    """Captures the identity/intent facts the /ask handler derives per request.

    Field groups map 1:1 to the lifecycle stages in
    docs/architecture/03-request-lifecycle.md. All fields have safe defaults so a
    partially-populated context (e.g. a failed capture) is never invalid.
    """

    # ── stage 0 — tracing / timing (gateway.py:2201-2212) ──────────────
    request_id: str = ""
    start_time: float = 0.0

    # ── stage 2 — identity (gateway.py:2242-2313) ─────────────────────
    # user_ctx holds the 11 keys built at gateway.py:2266-2277:
    #   user_id, user_role, ad_level, department, is_admin, can_approve,
    #   org_id, session_id, name, ad_username, email  (+ product_ids later)
    user_id: str = ""
    user_dept: str = ""
    user_ctx: Dict[str, Any] = field(default_factory=dict)
    auth_method: str = ""  # "jwt" | "api_key" | ""

    # ── stage 2b — product scoping (gateway.py:2320-2341) ─────────────
    product_ids: List[str] = field(default_factory=list)

    # ── stage 3 — intent pre-pass ─────────────────────────────────────
    chat_id: str = ""                       # gateway.py:2346
    rag_mode: str = "off"                    # resolved from q.rag_mode
    doc_intent: Optional[Dict[str, Any]] = None  # DocIntent.raw / __dict__ copy

    # ── L2/PR3 forward slot — resolved policy (unused/read in Wave 1) ──
    policy: Optional["EffectivePolicy"] = None

    # ── Wave 2 — Conversation Intelligence Layer state (shadow) ───────
    conv_state: Optional["ConversationState"] = None

    # ── Phase 3 — dispatch lane/fork decision (shadow, then driving) ──
    dispatch: Optional["DispatchDecision"] = None

    def snapshot(self) -> Dict[str, Any]:
        """Flat dict for span attributes / test comparison.

        Excludes bulky/nested values that are not useful as telemetry
        attributes; keeps only scalars that describe the request shape.
        """
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "user_dept": self.user_dept,
            "auth_method": self.auth_method,
            "product_id_count": len(self.product_ids),
            "chat_id": self.chat_id,
            "rag_mode": self.rag_mode,
            "has_doc_intent": self.doc_intent is not None,
            "profile_id": getattr(self.policy, "profile_id", None),
            "has_conv_state": self.conv_state is not None,
        }

    def as_dict(self) -> Dict[str, Any]:
        """Full serialization (for debugging / golden fixtures)."""
        return asdict(self)
