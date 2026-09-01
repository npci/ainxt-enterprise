# SPDX-License-Identifier: Apache-2.0
"""Loop Engineering Pydantic models.

Mirrors the JSONB layout of ``loops_pg`` / ``goals`` / ``loop_runs``.
Field names follow the SRS v2 Appendix-A reference loop so an operator
who knows the spec can write a Loop record by hand without consulting
this file.

Pydantic version
----------------
ABStudio is on **Pydantic v2** (see ``backend/requirements.txt`` —
``pydantic==2.13.x``). This module uses ``field_validator`` /
``model_dump`` accordingly. **Do not** import ``validator`` /
``.dict()`` — they're v1-only and the CI lint will catch them.

Reuse
-----
* ``app/models.py::LLMConfig`` — coerce-on-decode validator pattern.
* ``app/models.py::Workflow``  — ``BaseModel`` config conventions.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ────────────────────────── 5-element declarative model ──────────────────────────


class TriggerSpec(BaseModel):
    """How the loop is started.

    Only ``manual`` and ``cron`` are honoured in v1 (D11). Other types
    are accepted by the schema so a forward-compatible LoopRecord can be
    persisted today and exercised once the scheduler gains support.
    """
    type: Literal["manual", "cron", "jira_webhook", "log_alert", "queue_event"] = "manual"
    # 5-field cron in IST. Required when ``type == 'cron'``; ignored otherwise.
    cron: Optional[str] = None
    # Convenience helper for "daily / weekly at HH:MM" — the scheduler
    # converts this into a cron string at registration time.
    at_time: Optional[str] = None
    # Free-form filter blob (e.g. ``{"project": "ABC", "status": "Open"}``
    # for jira_webhook).
    filter: Optional[Dict[str, Any]] = None


class ActionSpec(BaseModel):
    """The thing the loop runs each iteration.

    ``engine='workflow'`` references a ``workflows.id``; ``engine='agent'``
    points at a single agent — the LoopRunner synthesises a 1-node chain
    around it before handing off to NativeEngine.execute().
    """
    engine: Literal["workflow", "agent"] = "workflow"
    target_id: str
    # Free-text augment of the agent system prompt — injected when set.
    instructions: Optional[str] = None


class ProofCheck(BaseModel):
    """One declarative proof check.

    The set is fixed (D6 — no Docker, no new sandbox); new check types
    require an engine-side dispatcher branch in ``proof.py`` (P2).
    """
    type: Literal["test_suite", "coverage", "repro_check", "latency", "scanner", "llm_judge"]
    must_pass: bool = True
    # Coverage %, latency ms, or llm_judge score depending on ``type``.
    threshold: Optional[float] = None
    # Type-specific config (e.g. ``{"cmd": ["pytest", "-q"]}``).
    config: Dict[str, Any] = Field(default_factory=dict)


class MemorySpec(BaseModel):
    """Per-run vs persistent memory scope (P5 reflection writer uses this)."""
    scope: Literal["run", "persistent"] = "run"
    carry: List[str] = Field(default_factory=list)


class StoppingCondition(BaseModel):
    """FR-1.7 — every Loop MUST carry a finite stopping_condition.

    ``measure``        — human-readable predicate (LLM-judged).
    ``max_iterations`` — outer-loop cap. ``ge=1`` blocks 0/negative values
                         at validation time so no API path can persist a
                         no-stop loop.
    ``budget_tokens``  — token cap; ditto.
    ``wall_clock_s``   — optional wall-clock cap; falls through to the
                         env default when omitted (see
                         ``app.core.config.budget_defaults``).
    """
    measure: str = ""
    max_iterations: int = Field(..., ge=1, le=100)
    budget_tokens: int = Field(..., ge=1)
    wall_clock_s: Optional[int] = Field(default=None, ge=1)


class VerifySpec(BaseModel):
    """Independent pre-ship verifier switch. Honoured in P4 (VerifierAgent)."""
    independent_agent: bool = False
    criteria: Optional[str] = None
    # Overrides VERIFIER_MODEL when set.
    model: Optional[str] = None


class OnUnresolved(BaseModel):
    """Routing for non-shipped outcomes (P5 degradation router)."""
    route_to: Literal["triage_inbox", "drop"] = "triage_inbox"


# ────────────────────────── LoopRecord ──────────────────────────


class LoopStatus(str, Enum):
    DRAFT       = "DRAFT"
    DEPRECATED  = "DEPRECATED"


# Legal status transitions. Sourced here so both ``app/loop/repo.py`` and
# ``app/api/loops.py`` agree on the same graph. The approval/promotion
# governance ladder was removed — a loop is simply active (DRAFT) until it
# is retired (DEPRECATED).
LEGAL_TRANSITIONS: Dict[LoopStatus, set] = {
    LoopStatus.DRAFT:      {LoopStatus.DEPRECATED},
    LoopStatus.DEPRECATED: set(),
}


def is_legal_transition(src: LoopStatus, dst: LoopStatus) -> bool:
    """True iff the FR-6.1 state graph permits ``src → dst``."""
    return dst in LEGAL_TRANSITIONS.get(src, set())


class LoopRecord(BaseModel):
    """The declarative ``Loop`` row stored in ``loops_pg``.

    Every field below maps 1:1 to a column or JSONB key in §4 of
    PHASE_1_FOUNDATIONS.md. Optional fields are left ``None`` on
    create so the DB defaults take over.
    """
    id:          Optional[str] = None
    name:        str
    org_id:      str = "default"
    category:    str = "engineering"
    description: Optional[str] = None

    # 5-element declarative model
    trigger:            TriggerSpec       = Field(default_factory=TriggerSpec)
    action:             ActionSpec
    proof:              List[ProofCheck]  = Field(default_factory=list)
    memory:             MemorySpec        = Field(default_factory=MemorySpec)
    stopping_condition: StoppingCondition
    verify:             VerifySpec        = Field(default_factory=VerifySpec)
    on_unresolved:      OnUnresolved      = Field(default_factory=OnUnresolved)

    # Governance + RBAC (mirrors the ``agents`` table — see
    # ABSTUDIO_TECHNICAL_REFERENCE §3.1).
    version:       str        = "1.0.0"
    status:        LoopStatus = LoopStatus.DRAFT
    visibility:    Literal["public", "private"] = "private"
    department:    Optional[str] = None
    owner_user_id: Optional[str] = None
    created_by:    Optional[str] = None
    approved_by:   Optional[str] = None
    approved_at:   Optional[datetime] = None
    enabled:       bool = True
    created_at:    Optional[datetime] = None
    updated_at:    Optional[datetime] = None

    @field_validator("stopping_condition")
    @classmethod
    def _stop_cond_required(cls, v: StoppingCondition) -> StoppingCondition:
        """Belt-and-braces FR-1.7 enforcement.

        The inner ``StoppingCondition.max_iterations`` / ``budget_tokens``
        fields already carry ``ge=1`` constraints which raise at v2
        field-validation time. This validator gives the API a stable
        error message ("FR-1.7") regardless of which sub-field was the
        offender, so the frontend can route the message to a single
        inline error slot near the budget controls.
        """
        if v.max_iterations < 1 or v.budget_tokens < 1:
            raise ValueError(
                "Loop missing stopping_condition.max_iterations and/or "
                "stopping_condition.budget_tokens (FR-1.7)"
            )
        return v


# ────────────────────────── Goal ──────────────────────────


class Goal(BaseModel):
    """First-class predicate + stop condition + budget.

    Goals are referenced from ``RunRequest.goal_id`` (ad-hoc /run-stream
    promotion to a LoopRunner) and from ``LoopRecord`` runs that don't
    declare an inline ``measure``. CRUD ships in P1; the predicate is
    consumed by ``LoopRunner.execute()`` in P2.
    """
    id:             Optional[str] = None
    name:           str
    description:    Optional[str] = None
    predicate_kind: Literal["llm_judge", "rule"] = "llm_judge"
    predicate:      Dict[str, Any]    = Field(default_factory=dict)
    stop_condition: StoppingCondition
    owner_user_id:  Optional[str] = None
    department:     Optional[str] = None
    created_at:     Optional[datetime] = None
    updated_at:     Optional[datetime] = None


# ────────────────────────── P4 — Verifier models ──────────────────────────


class VerificationVerdict(str, Enum):
    """Top-level verdict the independent VerifierAgent returns.

    ``INCONCLUSIVE`` is treated as ``FAIL`` by the runner — anything other
    than an explicit ``PASSED`` keeps the worktree staged so a human can
    review. The enum keeps the three states distinct so the audit trail
    can tell "the verifier ran and was uncertain" apart from "the
    verifier said no".
    """
    PASSED       = "pass"
    FAIL         = "fail"
    INCONCLUSIVE = "inconclusive"


class RiskClass(str, Enum):
    """Risk band the verifier assigns to the staged change.

    ``CRITICAL`` is the prompt-injection / safety override: regardless of
    the verdict field, a CRITICAL risk class forces the runner to treat
    the run as failed and refuse to ship. PHASE_4_VERIFIER.md §6.3.
    """
    NONE     = "none"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class VerifierEvidence(BaseModel):
    """One piece of evidence the verifier inspected.

    Captured by the VerifierAgent (not the maker) so the audit trail
    records what the *verifier* actually looked at. ``sha256`` lets a
    post-hoc reviewer detect tampering between the verifier run and the
    forensic review window.
    """
    rel_path:   str
    sha256:     str
    size_bytes: int
    kind:       Literal["file", "log", "diff"] = "file"


class VerifierResult(BaseModel):
    """Structured output the VerifierAgent returns.

    Persisted to ``verification_gate_runs`` and surfaced via
    ``GET /loops/runs/{id}/verdict``. ``raw_response`` is captured only
    when ``VERIFIER_DEBUG=1`` is set — the API strips it from the
    response otherwise to avoid leaking the verifier's chain of thought
    into operator dashboards.
    """
    verdict:      VerificationVerdict
    risk_class:   RiskClass
    reasons:      List[str] = Field(default_factory=list)
    confidence:   float = Field(ge=0.0, le=1.0)
    evidence:     List[VerifierEvidence] = Field(default_factory=list)
    model:        str = ""
    temperature:  float = 0.0
    elapsed_ms:   int = 0
    tokens_in:    int = 0
    tokens_out:   int = 0
    raw_response: Optional[str] = None


# ────────────────────────── P5 — Triage / Reflection / Memory ──────────────────────────


class ReflectionKind(str, Enum):
    """Why this reflection was written. Each terminal outcome of an outer
    iteration produces at most one row keyed on this enum so a future
    operator can grep ``WHERE kind = 'verifier_fail'`` for fleet-wide
    failure-mode analysis without parsing free text.
    """
    PROOF_FAILED   = "proof_failed"
    VERIFIER_FAIL  = "verifier_fail"
    BUDGET_HALT    = "budget_halt"
    ERROR          = "error"


class Reflection(BaseModel):
    """Verbalised lesson row mirroring the ``reflections`` table.

    The DB schema for ``reflections`` is the P1 generic
    ``(scope_kind, scope_id, tag, content, source_run)`` shape. This
    Pydantic model is the *projection* loop-engineering uses on top —
    ``scope_kind='loop'``, ``scope_id=loop_id``, ``content=lesson``,
    ``source_run=loop_run_id``, ``tag=kind.value``. The repo layer is the
    single point of impedance match between the two so callers see only
    the loop-shaped view.
    """
    id: str
    loop_id: str
    loop_run_id: str
    outer_iteration: int = 0
    kind: ReflectionKind
    lesson: str = Field(min_length=8, max_length=2000)
    tags: List[str] = Field(default_factory=list, max_length=10)
    created_at: Optional[datetime] = None


class Lesson(BaseModel):
    """Read-only projection fed into the maker's context by ``MemoryReadHandler``.

    Kept separate from ``Reflection`` (which is the write-side row) so
    the read API never accidentally leaks insertion-only fields (``id``,
    ``loop_run_id``) into the prompt — the prompt only ever needs the
    lesson text + tags + freshness for retrieval ranking.
    """
    lesson: str
    tags: List[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class InboxItem(BaseModel):
    """One discovered work-item the triage skill is allowed to summarise.

    Only these ``source`` values currently feed inbox collection
    (``loop_runs_failure`` is the only one wired in v1 — the others
    are placeholders matching the SRS so the LLM prompt can reason about
    a future-source row without revalidation churn).
    """
    source: Literal["loop_runs_failure", "log_alert", "manual"]
    external_id: str
    title: str
    snippet: str = ""
    severity: Literal["low", "med", "high"] = "med"
    discovered_at: Optional[datetime] = None


class TriageProposal(BaseModel):
    """A Goal-shaped object the triage skill emits.

    Inserted into ``goals`` by the repo layer — the Pydantic model
    deliberately doesn't carry a ``status`` field so a buggy LLM response
    can't smuggle an unexpected status past the repo layer.
    """
    loop_id: str
    title: str = Field(max_length=200)
    description: str = Field(max_length=4000, default="")
    source_item: InboxItem
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


# ────────────────────────── Public exports ──────────────────────────

__all__ = [
    "TriggerSpec",
    "ActionSpec",
    "ProofCheck",
    "MemorySpec",
    "StoppingCondition",
    "VerifySpec",
    "OnUnresolved",
    "LoopStatus",
    "LoopRecord",
    "Goal",
    "LEGAL_TRANSITIONS",
    "is_legal_transition",
    # P4
    "VerificationVerdict",
    "RiskClass",
    "VerifierEvidence",
    "VerifierResult",
    # P5
    "ReflectionKind",
    "Reflection",
    "Lesson",
    "InboxItem",
    "TriageProposal",
]
