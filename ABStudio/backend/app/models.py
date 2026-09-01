# SPDX-License-Identifier: Apache-2.0
"""
Data models, request/response schemas, and authentication.

This is the single source of truth for:
  - LLM provider config       (LLMProvider, LLMConfig)
  - Workflow node types        (AgentNode, ConditionNode, StartNode, …)
  - API request/response bodies(RunRequest, RunResponse, GenerateWorkflowResponse, …)
  - Authentication             (AuthenticatedUser, require_framework_access)

Authentication note: in standalone (local-dev) mode every request is
accepted automatically — no keys or tokens are required. To add real
authentication, replace get_current_user() with your identity provider logic.

Used by: main.py, native_engine.py, mcp_manager.py, workflow_repo.py
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import Depends
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# LLM provider config
# ---------------------------------------------------------------------------

class LLMProvider(str, Enum):
    CUSTOM = "custom"


class LLMConfig(BaseModel):
    provider:    LLMProvider = LLMProvider.CUSTOM
    api_key:     str         = ""
    model_name:  str         = ""
    temperature: float       = Field(default=0.7, ge=0, le=1)
    max_tokens:  int         = Field(default=2048, ge=1, le=32000)
    top_p:       float       = Field(default=1.0, ge=0, le=1)
    base_url:    Optional[str] = None

    @field_validator("provider", mode="before")
    @classmethod
    def coerce_provider(cls, v):
        """Coerce any legacy/unknown provider value (e.g. 'google') to 'custom'."""
        try:
            return LLMProvider(v)
        except ValueError:
            return LLMProvider.CUSTOM


# ---------------------------------------------------------------------------
# Workflow node types
# ---------------------------------------------------------------------------

class McpServerType(str, Enum):
    GITHUB   = "github"
    GITLAB   = "gitlab"
    REST_API = "rest_api"
    POSTGRES = "postgres"
    WEAVIATE = "weaviate"
    # `teams` is present in MCP_SERVER_REGISTRY but was missing from this enum
    # — added for parity so a `type: "mcp"` node with server_type "teams"
    # validates.
    TEAMS    = "teams"


class StartNode(BaseModel):
    id:   str
    type: Literal["start"]


class EndNode(BaseModel):
    id:   str
    type: Literal["end"]


class AgentNode(BaseModel):
    id:           str
    type:         Literal["agent"]
    name:         str
    instructions: str
    llm_config:   LLMConfig


class McpNode(BaseModel):
    id:          str
    type:        Literal["mcp"]
    server_type: McpServerType
    config:      dict = {}


class SingleCondition(BaseModel):
    id:       str = ""
    field:    str = ""
    operator: str = "=="
    value:    str = ""
    type:     str = "string"       # "string" | "number" | "boolean"


class ConditionCase(BaseModel):
    id:         str
    name:       str = ""
    label:      str = ""
    expression: str = ""           # legacy: raw expression string
    conditions: List[SingleCondition] = []   # structured format
    logic:      str = "AND"        # "AND" | "OR"


class ConditionNode(BaseModel):
    id:    str
    type:  Literal["condition"]
    cases: List[ConditionCase] = []


WorkflowNode = Union[StartNode, EndNode, AgentNode, ConditionNode, McpNode]


class Edge(BaseModel):
    source:       str
    target:       str
    sourceHandle: Optional[str] = None   # identifies which condition branch


class Workflow(BaseModel):
    nodes: List[dict]   # kept as raw dicts for frontend flexibility
    edges: List[Edge]
    # Workflow-level KB blob inherited by agent nodes with mode == 'none'.
    # Optional so legacy clients without the field still POST a valid payload.
    knowledge: Optional[dict] = None


# ---------------------------------------------------------------------------
# API request / response bodies
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    workflow:       Workflow
    user_input:     str
    workflow_id:    Optional[str] = None
    workflow_name:  Optional[str] = None
    thread_id:      Optional[str] = None
    # Chat-panel "Run settings" → workflow-wide subagent (swarm) opt-in.
    # None = client didn't send (older builds) → engine uses its existing
    # default. Per-node pins (`disable_subagents=True` for force-OFF or
    # `enable_subagents=True` for force-ON) always win.
    subagents_enabled: Optional[bool] = None
    # ----------------------------------------------------------------
    # Loop Engineering (P1) — placeholders. Accepted now so the API
    # contract is stable; honoured by the engine starting in P2 when
    # /run-stream learns to promote into LoopRunner. See
    # docs/loop-engineering/PHASE_1_FOUNDATIONS.md §9.
    # ----------------------------------------------------------------
    # Goal to evaluate after each outer iteration. When set on /run-stream
    # the route promotes the run into LoopRunner without requiring a
    # saved LoopRecord (D10 — both entry points, single backend).
    goal_id:        Optional[str] = None
    # Override budget caps for an ad-hoc loop run.
    # Shape: {"tokens": int, "wall_clock_s": int, "max_iterations": int}.
    # Stored loops carry their own stopping_condition; this only matters
    # for goal-mode /run-stream calls.
    budget:         Optional[Dict[str, Any]] = None
    # When invoking via /loops/{id}/run-stream the router supplies this
    # so the engine can label loop_runs rows and forward to LoopRunner.
    loop_id:        Optional[str] = None
    # Narrows credential-vault env-var injection (P3). Defaults to None =
    # current behaviour (every connection the user has access to is
    # exposed to the sandbox).
    allowed_connections: Optional[List[str]] = None
    # Structured document uploads for this run. Each entry mirrors the
    # /agent-runner/attachment extraction envelope:
    #   {file_name, file_type, parsed_text, char_count, page_count}
    # Sent by the workflow-preview chat panel instead of gluing the parsed
    # text into user_input. None/empty = text-only run (legacy behaviour).
    attachments: Optional[List[Dict[str, Any]]] = None


class ResumeRequest(BaseModel):
    workflow:      Workflow
    human_input:   str
    workflow_id:   Optional[str] = None
    workflow_name: Optional[str] = None
    thread_id:     str
    # User-edited tool-call list for `before_tool` pauses. When supplied
    # (and the decision parses as `approve`), the engine uses this list
    # instead of the snapshot's `pending_tool_calls` — letting the
    # reviewer drop unwanted calls or add new ones from the catalog
    # before the agent runs them. Per-turn only; persistence happens
    # separately via PUT /workflows/{id}. Defaults to None so older
    # clients keep working unchanged.
    pending_tool_calls_override: Optional[List[Dict[str, Any]]] = None
    # Mirror of RunRequest.subagents_enabled so a resumed flow keeps the
    # same swarm policy the original run was started with.
    subagents_enabled: Optional[bool] = None
    # Loop Engineering (P1) — placeholders so HITL resume of an outer-loop
    # run can rebind to the same goal / loop. Engine-side support arrives
    # in P2 when LoopRunner learns to resume.
    goal_id:  Optional[str] = None
    loop_id:  Optional[str] = None
    # Mirror of RunRequest.attachments so a resumed run keeps the uploaded
    # documents available to downstream agents after an HITL pause. Same
    # shape: [{file_name, file_type, parsed_text, char_count, page_count}].
    attachments: Optional[List[Dict[str, Any]]] = None


class ExecutionTrace(BaseModel):
    agent:  str
    output: str


class RunResponse(BaseModel):
    status:          Literal["success", "error"]
    output:          Optional[str]                 = None
    execution_trace: Optional[List[ExecutionTrace]] = None
    message:         Optional[str]                 = None
    thread_id:       Optional[str]                 = None


class GenerateInstructionsRequest(BaseModel):
    prompt: str


class GenerateInstructionsResponse(BaseModel):
    instructions: str


class McpTestRequest(BaseModel):
    server_type: McpServerType
    config:      dict = {}


class McpTestResponse(BaseModel):
    status:  Literal["success", "error"]
    tools:   Optional[List[dict]] = None
    message: Optional[str]        = None


class GenerateWorkflowRequest(BaseModel):
    prompt: str


class GenerateWorkflowResponse(BaseModel):
    name:       str
    graph_data: dict   # {nodes: [...], edges: [...]}


# ---------------------------------------------------------------------------
# Triggers (Routines) — scheduled workflow / agent execution
#
# Mirrors the scheduled-tasks "Routines" UX pattern. All times are stored and exchanged
# in IST (Asia/Kolkata). The scheduler service translates these schedules
# into APScheduler triggers and persists every run in trigger_executions.
# ---------------------------------------------------------------------------

class TriggerScheduleType(str, Enum):
    ONCE     = "once"
    HOURLY   = "hourly"
    DAILY    = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY   = "weekly"
    CUSTOM   = "custom"
    # FR-T0-3 (REQ-T1): event-driven triggers. Not time-scheduled — they fire
    # via the signed ingestion route (api/triggers.py) on an inbound webhook /
    # platform event (Jira issue_created, GitLab MR/push, Slack/Teams, …).
    WEBHOOK  = "webhook"
    EVENT    = "event"


class TriggerTargetKind(str, Enum):
    WORKFLOW = "workflow"
    AGENT    = "agent"


class TriggerSchedule(BaseModel):
    """Schedule descriptor. Only the fields relevant to ``type`` are used.

    Times are interpreted in IST (Asia/Kolkata) regardless of server tz.
    """
    type:        TriggerScheduleType
    run_at:      Optional[str] = None  # "once": ISO datetime in IST (e.g. 2026-05-26T01:44:00)
    at_minute:   Optional[int] = None  # "hourly": 0-59
    at_time:     Optional[str] = None  # "daily" | "weekdays" | "weekly": "HH:MM" (24h)
    day_of_week: Optional[str] = None  # "weekly": monday|tuesday|...|sunday
    cron:        Optional[str] = None  # "custom": 5-field cron in IST
    # FR-T0-3 (REQ-T1/T2): event-driven fields ("webhook" | "event").
    event_source: Optional[str] = None  # jira | gitlab | slack | teams | inbox | kb
    event_type:   Optional[str] = None  # e.g. issue_created | merge_request | push
    # HMAC secret used to verify the inbound signature. Never returned by the
    # read model (TriggerOut) — write-only.
    secret:       Optional[str] = None


class TriggerCreate(BaseModel):
    target_kind: TriggerTargetKind
    target_id:   str
    # When target_kind == 'workflow' the trigger can optionally bind to a
    # specific agent node inside the workflow. The scheduler then runs the
    # chain starting at that node so the output flows downstream as normal.
    node_id:     Optional[str] = None
    name:        Optional[str] = None
    schedule:    TriggerSchedule
    input_text:  Optional[str] = ""
    enabled:     bool = True


class TriggerUpdate(BaseModel):
    name:       Optional[str]            = None
    schedule:   Optional[TriggerSchedule] = None
    input_text: Optional[str]            = None
    enabled:    Optional[bool]           = None


class TriggerOut(BaseModel):
    id:           str
    target_kind:  TriggerTargetKind
    target_id:    str
    node_id:      Optional[str] = None
    name:         Optional[str]
    schedule:     TriggerSchedule
    input_text:   str
    enabled:      bool
    created_at:   str
    updated_at:   str
    next_run_at:  Optional[str] = None
    last_run_at:  Optional[str] = None
    last_status:  Optional[str] = None


class TriggerExecutionOut(BaseModel):
    id:          int
    trigger_id:  str
    target_kind: TriggerTargetKind
    target_id:   str
    target_name: Optional[str] = None
    started_at:  str
    finished_at: Optional[str] = None
    status:      str  # "running" | "success" | "error"
    input_text:  str
    output:      Optional[str] = None
    error:       Optional[str] = None
    seen:        bool = False
    # Download references for documents produced by the run, so the Inbox can
    # render download chips (mirrors the interactive run's generated_files).
    generated_files: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Authentication — local dev stub
# ---------------------------------------------------------------------------

class AuthenticatedUser(BaseModel):
    id:         str
    email:      str
    full_name:  str
    role:       str
    # Surfaced from the enriched JWT payload (auth/dependencies.py) for
    # pgvector PRIVATE-doc ACL filtering in workflow / agent-config KB runs.
    department: str = ""
    frameworks: List[str] = Field(default_factory=list)
    # Hierarchy fields — populated from the platform JWT / LDAP AD sync.
    # ad_level: 0 = most senior executive, 6 = junior (default 6 = most restricted).
    # is_hod: True when this user heads one or more departments.
    # is_security_team: True for IS/security team members (bypasses tool restrictions).
    ad_level:         int  = 6
    is_hod:           bool = False
    is_security_team: bool = False
    # Department names this user heads (empty for non-HODs). Needed for
    # HOD-scoped visibility checks so we can match the creator's department
    # against the departments the caller actually owns.
    hod_departments:  List[str] = Field(default_factory=list)


async def _get_current_user() -> AuthenticatedUser:
    """Standalone mode: always returns a local-dev admin user."""
    return AuthenticatedUser(
        id="local-dev-user",
        email="dev@localhost",
        full_name="Local Developer",
        role="admin",
        department="",
        frameworks=["agent-chain"],
    )


def require_framework_access(framework: str):
    """
    FastAPI dependency factory. Returns a Depends() that resolves to
    the current user. Replace _get_current_user() to enforce real auth.
    """
    async def _require(
        current_user: AuthenticatedUser = Depends(_get_current_user),
    ) -> AuthenticatedUser:
        return current_user

    return _require
