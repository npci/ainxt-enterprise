# SPDX-License-Identifier: MIT
# ============================================================
# ORM MODELS — all platform entities
# Additive: does NOT touch memory/postgres_memory.py tables
# ============================================================

import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey,
    Index, Integer, Numeric, SmallInteger, String, Text, UniqueConstraint, func, text
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

# pgvector — graceful import (requires pgvector Python package + PG extension)
try:
    from pgvector.sqlalchemy import Vector as _Vector
    _VECTOR_TYPE = _Vector(768)   # nomic-embed-text / text-embedding-3-small dimensionality
    _PGVECTOR_AVAILABLE = True
except ImportError:
    _Vector = None
    _VECTOR_TYPE = Text           # fallback: store as JSON text if pgvector not installed
    _PGVECTOR_AVAILABLE = False

from db.database import Base, DB_SCHEMA


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.utcnow()


def _now_ist():
    """
    IST (Asia/Kolkata, fixed UTC+5:30) wall-clock time, naive (no tzinfo) —
    for columns that should display/store in IST rather than UTC (currently
    ModelUsage.created_at, per user-facing chargeback/audit requirements).
    Kept separate from `_now()` (still UTC, used by every other table's
    `created_at`) so this is an intentional, scoped change rather than a
    platform-wide timezone switch.
    """
    from core.time_utils import now_ist
    return now_ist()


def _now_utc():
    """Timezone-aware UTC ``now`` for columns backed by ``TIMESTAMP WITH TIME ZONE``.

    A naive ``datetime.utcnow()`` inserted into a ``timestamptz`` Postgres column
    is (per SQL rules) interpreted in the server's session TimeZone — which on
    an IST host silently shifts the stored moment by +5:30 h. On read-back the
    value comes with a ``+05:30`` tzinfo, so ``.timestamp()`` yields a POSIX
    second-count for a moment 5:30 h earlier than "now" actually was, and the
    frontend renders it as such (a "GMT-ish" clock reading with an IST label).
    Using ``datetime.now(timezone.utc)`` supplies an aware value so Postgres
    stores the correct absolute moment regardless of session TimeZone.
    """
    return datetime.now(timezone.utc)


# ============================================================
# USERS
# ============================================================

class User(Base):
    __tablename__ = "users"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    email           = Column(String(255), unique=True, nullable=False, index=True)
    name            = Column(String(255), nullable=False)
    role            = Column(String(50), nullable=False, default="user")   # admin | user
    org_id          = Column(String(255), nullable=True)
    hashed_password = Column(Text, nullable=True)
    sso_provider    = Column(String(100), nullable=True)   # keycloak | azure_ad | None
    sso_subject     = Column(String(255), nullable=True)   # external user identifier
    is_active       = Column(Boolean, nullable=False, default=True)
    # Operational fields
    last_login_at   = Column(DateTime, nullable=True)
    account_status  = Column(String(50), nullable=False, default="active")  # active | suspended | pending_verification
    email_verified  = Column(Boolean, nullable=False, default=False)
    created_at      = Column(DateTime, nullable=False, default=_now)
    # ABAC fields (populated by nightly org_tree sync)
    ad_level         = Column(Integer,     nullable=False, default=6)  # 0=most senior exec, 6=junior; can_approve = ad_level<=3
    ad_username      = Column(String(255), nullable=True)          # sAMAccountName
    employee_id      = Column(String(100), nullable=True)          # numeric employeeID from AD
    ad_dn            = Column(Text,        nullable=True)           # full LDAP distinguished name
    ad_title         = Column(String(255), nullable=True)          # raw title from AD
    department       = Column(String(255), nullable=True)
    manager_dn       = Column(Text,        nullable=True)           # manager DN from AD
    # Email of the user's assigned HOD (Head of Department). Referenced by
    # auth/dependencies.py, auth/rbac.py and routers/budget_router.py, which all
    # query `users.hod_email` directly -- but it was never declared here, so it
    # existed only in databases where a DBA had added it out of band. On a fresh
    # install GET /ainxt/v1/api/budget/admin/hods returned 500 with
    # `column "hod_email" does not exist`.
    hod_email        = Column(String(255), nullable=True, index=True)
    gitlab_username  = Column(String(255), nullable=True)
    is_security_team   = Column(Boolean,     nullable=False, default=False)  # IS/security team member
    last_ad_sync       = Column(DateTime,    nullable=True)
    # GAP-6: set True when a temporary password is issued via forgot-password flow.
    # Cleared to False when user changes password via Profile → Change Password.
    # Directory-backed safe: LDAP users never have hashed_password → always False for them.
    is_temp_password   = Column(Boolean,     nullable=False, default=False)
    default_product_id = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    # Custom Instructions — ChatGPT-style persona injection. Two text blobs:
    #   custom_about_user      — "What should AiNxt know about you?"
    #   custom_response_style  — "How should AiNxt respond?"
    # Both prepended to the system prompt on every /ask for this user.
    custom_about_user     = Column(Text, nullable=True)
    custom_response_style = Column(Text, nullable=True)

    # relationships
    chats    = relationship("Chat", back_populates="user", cascade="all, delete-orphan")
    projects = relationship("ProjectRecord", back_populates="owner", cascade="all, delete-orphan")
    usages   = relationship("ModelUsage", back_populates="user", cascade="all, delete-orphan")


# ============================================================
# CHATS
# ============================================================

class Chat(Base):
    __tablename__ = "chats"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id       = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    title         = Column(String(500), nullable=False, default="New Chat")
    session_id    = Column(String(255), nullable=True, index=True)
    agent_id      = Column(String(255), nullable=True)
    project_id    = Column(String(255), nullable=True, index=True)   # project persistence
    is_pinned     = Column(Boolean, nullable=False, default=False)
    client_source = Column(String(50), nullable=False, default="platform", index=True)  # platform|cli|ide-vscode|ide-jetbrains|api
    rag_mode      = Column(String(8), nullable=False, default="off")   # off | auto | on — KB retrieval mode for /ask
    # Per-chat KB scope (server-derived, server-validated against user's
    # accessible products at /ask time — kn_rewrite.md §7 server-derived rule).
    # /ask gateway reads these on every request and injects them into
    # _user_ctx['scope_filter'] (+ _user_ctx['kb_doc_id']) so the existing
    # Phase 1–5 retrieval machinery fires for every chat-time RAG request.
    product_id    = Column(UUID(as_uuid=False), nullable=True, index=True)
    domain        = Column(String(100), nullable=True)
    spec_version  = Column(String(50),  nullable=True, index=True)
    kb_doc_id     = Column(UUID(as_uuid=False), nullable=True)
    # Links this chat to a managed endpoint (when created via endpoint proxy)
    endpoint_slug = Column(String(100), nullable=True, index=True)
    created_at    = Column(DateTime, nullable=False, default=_now)
    updated_at    = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    user     = relationship("User", back_populates="chats")
    messages = relationship("ChatMessage", back_populates="chat", cascade="all, delete-orphan")

class MessageVersion(Base):
    """Version of a chat message — supports the Edit + Branch flow.
    Editing a past user turn creates a new row with the same root_id
    and a new parent_id pointing to the prior version. The 'active'
    branch is the row with is_active=True for that root_id."""
    __tablename__ = "message_versions"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    message_id  = Column(UUID(as_uuid=False), nullable=False, index=True)
    chat_id     = Column(UUID(as_uuid=False), nullable=False, index=True)
    parent_id   = Column(UUID(as_uuid=False), nullable=True)
    root_id     = Column(UUID(as_uuid=False), nullable=False, index=True)
    role        = Column(String(20), nullable=False)
    content     = Column(Text, nullable=False)
    version     = Column(Integer, nullable=False, default=1)
    is_active   = Column(Boolean, nullable=False, default=True)
    created_at  = Column(DateTime, nullable=False, default=_now)


class ChatArtifact(Base):
    """Self-contained block (HTML / React / SVG / Markdown / Mermaid /
    code) generated inside a chat. Surfaced in a side-pane (Canvas)."""
    __tablename__ = "chat_artifacts"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    chat_id       = Column(UUID(as_uuid=False), nullable=False, index=True)
    message_id    = Column(UUID(as_uuid=False), nullable=True)
    title         = Column(String(200), nullable=False, default="Untitled")
    artifact_type = Column(String(20), nullable=False)   # html | react | svg | markdown | mermaid | code
    language      = Column(String(40), nullable=True)
    content       = Column(Text, nullable=False)
    version       = Column(Integer, nullable=False, default=1)
    created_by    = Column(UUID(as_uuid=False), nullable=True)
    created_at    = Column(DateTime, nullable=False, default=_now)
    updated_at    = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class ChatShare(Base):
    """Public read-only snapshot of a chat (share link)."""
    __tablename__ = "chat_shares"

    token       = Column(String(64), primary_key=True)
    chat_id     = Column(UUID(as_uuid=False), nullable=False, index=True)
    owner_id    = Column(UUID(as_uuid=False), nullable=False, index=True)
    snapshot    = Column(JSONB, nullable=False)
    created_at  = Column(DateTime, nullable=False, default=_now)
    expires_at  = Column(DateTime, nullable=True)


class PromptTemplate(Base):
    """Saved prompt template for the chat input "/" quick-insert menu."""
    __tablename__ = "prompt_templates"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id     = Column(UUID(as_uuid=False), nullable=False, index=True)
    name        = Column(String(120), nullable=False)
    body        = Column(Text, nullable=False)
    scope       = Column(String(10), nullable=False, default="private")  # private | org
    created_at  = Column(DateTime, nullable=False, default=_now)
    updated_at  = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    chat_id        = Column(UUID(as_uuid=False), ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    role           = Column(String(50), nullable=False)      # user | assistant | system
    content        = Column(Text, nullable=False)
    model_used     = Column(String(100), nullable=True)
    tokens_used              = Column(Integer, nullable=True)
    cost_usd                 = Column(Float, nullable=True)
    selected_model           = Column(String(255), nullable=True)
    attachment_ids           = Column(JSONB, default=list)
    token_usage_deprecated   = Column(Integer, default=0)   # deprecated duplicate of tokens_used — do not use
    cost                     = Column(Float, default=0.0)
    in_tok         = Column(Integer, nullable=True)   # prompt/input tokens
    out_tok        = Column(Integer, nullable=True)   # completion/output tokens
    latency        = Column(Float, nullable=True)     # response time in seconds
    language       = Column(String(20), nullable=True)   # en | hi | ta | mixed | unknown
    # Phase 3 transparency (kn_rewrite.md §8x) — the coverage tier decision
    # for this assistant turn (escalation, sections examined, badge text).
    # Persisted so the UI badge survives page reloads, not just live streams.
    # NULL for user messages and for assistant messages produced before
    # Phase 1 scope was wired into chat.
    coverage_trace = Column(JSONB, nullable=True)
    rag_mode       = Column(String(8), nullable=True)    # off | auto | on — rag_mode at write time (context isolation)
    created_at     = Column(DateTime, nullable=False, default=_now)

    chat = relationship("Chat", back_populates="messages")


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"

    id           = Column(String(36), primary_key=True, default=_uuid)
    # UUID, not String(36): Chat.id is UUID, and a varchar -> uuid foreign key is
    # rejected by Postgres. That mismatch made the fk_chat_attachments_chat_id
    # constraint in Part G impossible to create ("foreign key constraint ...
    # cannot be implemented"), so chat attachments had no referential integrity
    # to chats and orphan rows survived chat deletion.
    chat_id      = Column(UUID(as_uuid=False), nullable=False, index=True)
    # Owner of the upload — enables strict per-user ACL on byte retrieval
    # (mirrors GeneratedDocument.user_id). Nullable for backward compatibility
    # with rows created before this column existed; retrieval falls back to
    # created_by when user_id is NULL.
    user_id      = Column(String(255), nullable=True, index=True)
    file_name    = Column(String(512), nullable=False)
    file_type    = Column(String(50), nullable=False)
    file_size    = Column(Integer, default=0)
    # Kind of asset: "document" (default) | "image". Lets the raw endpoint and
    # cleanup distinguish uploaded images from documents. Nullable for legacy rows.
    kind         = Column(String(16), nullable=True, default="document")
    storage_path = Column(Text, nullable=False)
    parsed_text  = Column(Text, nullable=True)
    # Persistent image context (multi-turn image memory). For image uploads,
    # image_description holds the full Gemini Vision description and image_caption
    # a short (≤600 char) summary that later turns replay into history cheaply,
    # without re-sending the image bytes. NULL for documents / legacy rows.
    image_description = Column(Text, nullable=True)
    image_caption     = Column(String(600), nullable=True)
    created_by   = Column(String(255), nullable=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, default=_now)


# ============================================================
# AGENTS
# ============================================================

class AgentRecord(Base):
    __tablename__ = "agents_pg"
    __table_args__ = (
        UniqueConstraint("name", "created_by", "org_id", name="uq_agents_name_owner_org"),
    )

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name          = Column(String(255), nullable=False, index=True)
    org_id        = Column(String(255), nullable=False, default="default", index=True)
    description   = Column(Text, nullable=True)
    system_prompt = Column(Text, nullable=True)
    tools         = Column(JSONB, nullable=False, default=list)
    skills        = Column(JSONB, nullable=False, default=list)
    version       = Column(String(50), nullable=False, default="1.0.0")
    stage         = Column(String(50), nullable=False, default="production")  # development|staging|production
    owner         = Column(String(255), nullable=True)
    enabled       = Column(Boolean, nullable=False, default=True)
    status        = Column(String(50), nullable=False, default="PRODUCTION", index=True)
    created_by    = Column(String(255), nullable=True)
    approved_by   = Column(String(255), nullable=True)
    approved_at   = Column(DateTime(timezone=True), nullable=True)
    is_production = Column(Boolean, default=True)
    visibility      = Column(String(10), nullable=False, default="private")  # public | private
    department      = Column(String(255), nullable=True)
    kb_namespace    = Column(String(255), nullable=True)   # e.g. "docs_kb:hr" — scopes retrieve tool
    preferred_model = Column(String(50),  nullable=True)   # auto|claude|gpt|ollama
    # Governance: template-instance provenance (ABStudio approval layer)
    source_template_id   = Column(String(255), nullable=True)
    source_template_hash = Column(String(64),  nullable=True)
    last_approved_hash   = Column(String(64),  nullable=True)
    created_at      = Column(DateTime, nullable=False, default=_now)

    versions = relationship("AgentVersion", back_populates="agent", cascade="all, delete-orphan")


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_version"),
    )

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    agent_id   = Column(UUID(as_uuid=False), ForeignKey("agents_pg.id", ondelete="CASCADE"), nullable=False)
    version    = Column(String(50), nullable=False)
    definition = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=_now)

    agent = relationship("AgentRecord", back_populates="versions")


# ============================================================
# SKILLS
# ============================================================

class SkillRecord(Base):
    __tablename__ = "skills_pg"
    __table_args__ = (
        UniqueConstraint("name", "created_by", "org_id", name="uq_skills_name_owner_org"),
    )

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name          = Column(String(255), nullable=False, index=True)
    org_id        = Column(String(255), nullable=False, default="default", index=True)
    code          = Column(Text, nullable=True)
    description   = Column(Text, nullable=True)
    input_schema  = Column(JSONB, nullable=False, default=dict)
    output_schema = Column(JSONB, nullable=False, default=dict)
    permissions   = Column(JSONB, nullable=False, default=list)
    tools         = Column(JSONB, nullable=False, default=list)
    tags          = Column(JSONB, nullable=False, default=list)
    status        = Column(String(50), nullable=False, default="PRODUCTION", index=True)
    created_by    = Column(String(255), nullable=True)
    approved_by   = Column(String(255), nullable=True)
    approved_at   = Column(DateTime(timezone=True), nullable=True)
    is_production = Column(Boolean, default=True)
    visibility    = Column(String(10), nullable=False, default="private")  # public | private
    department    = Column(String(255), nullable=True)
    skill_type    = Column(String(20), nullable=False, default="execution")  # execution | behavioral
    # Governance: template-instance provenance (ABStudio approval layer)
    source_template_id   = Column(String(255), nullable=True)
    source_template_hash = Column(String(64),  nullable=True)
    last_approved_hash   = Column(String(64),  nullable=True)
    created_at    = Column(DateTime, nullable=False, default=_now)


# ============================================================
# WORKFLOWS
# ============================================================

class WorkflowRecord(Base):
    __tablename__ = "workflows_pg"
    __table_args__ = (
        UniqueConstraint("name", "created_by", "org_id", name="uq_workflows_name_owner_org"),
    )

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name             = Column(String(255), nullable=False, index=True)
    org_id           = Column(String(255), nullable=False, default="default", index=True)
    project_id       = Column(String(255), nullable=True, index=True)   # project persistence
    description      = Column(Text, nullable=True)
    steps            = Column(JSONB, nullable=False, default=list)
    failure_handling = Column(JSONB, nullable=False, default=dict)
    # Governance lifecycle (Phase 19)
    status        = Column(String(50), nullable=False, default="PRODUCTION", index=True)
    created_by    = Column(String(255), nullable=True)
    approved_by   = Column(String(255), nullable=True)
    approved_at   = Column(DateTime(timezone=True), nullable=True)
    is_production = Column(Boolean, default=True)
    visibility    = Column(String(10), nullable=False, default="private")  # public | private
    department    = Column(String(255), nullable=True)
    # Governance: template-instance provenance (ABStudio approval layer)
    source_template_id   = Column(String(255), nullable=True)
    source_template_hash = Column(String(64),  nullable=True)
    last_approved_hash   = Column(String(64),  nullable=True)
    created_at       = Column(DateTime, nullable=False, default=_now)


# ============================================================
# WORKFLOW RUNS  (execution state — P1-11 Postgres persistence)
# ============================================================

class WorkflowRunRecord(Base):
    """
    Persists workflow execution state to Postgres so that paused / running
    workflows survive a Redis restart.  Redis is still the primary fast path;
    this table is the durable recovery fallback.
    """
    __tablename__ = "workflow_runs"

    workflow_id   = Column(String(255), primary_key=True)
    workflow_name = Column(String(255), nullable=False, index=True)
    # status: "running" | "paused" | "completed" | "failed"
    status        = Column(String(50), nullable=False, default="running", index=True)
    state_json    = Column(JSONB, nullable=True)   # serialised snapshot for resume
    started_at    = Column(DateTime(timezone=True), nullable=False, default=_now)
    ended_at      = Column(DateTime(timezone=True), nullable=True)
    updated_at    = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


# ============================================================
# MCP SERVERS
# ============================================================

class MCPServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("name", "org_id", name="uq_mcp_servers_name_org"),
    )

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name        = Column(String(255), nullable=False, index=True)
    org_id      = Column(String(255), nullable=False, default="default", index=True)
    endpoint    = Column(Text, nullable=True)
    tools       = Column(JSONB, nullable=False, default=list)
    auth_config = Column(JSONB, nullable=False, default=dict)
    enabled     = Column(Boolean, nullable=False, default=True)
    status      = Column(String(50), nullable=False, default="PRODUCTION", index=True)
    created_by  = Column(String(255), nullable=True)
    approved_by = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    is_production = Column(Boolean, default=True)
    is_critical = Column(Boolean, nullable=False, default=False)
    registered_by = Column(String(255), nullable=True)
    created_at  = Column(DateTime, nullable=False, default=_now)


# ============================================================
# PROJECTS
# ============================================================

class ProjectRecord(Base):
    __tablename__ = "projects_pg"

    id                  = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name                = Column(String(255), unique=True, nullable=False, index=True)
    description         = Column(Text, nullable=True)
    repo_name           = Column(String(255), nullable=True)
    default_branch      = Column(String(255), nullable=True)
    department          = Column(String(255), nullable=True)  # ABAC: dept-scoped visibility
    team                = Column(JSONB, nullable=False, default=list)
    custom_instructions = Column(Text, nullable=True)
    tags                = Column(JSONB, nullable=False, default=list)
    owner_id            = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    budget_limit_usd    = Column(Float, nullable=True)
    budget_used_usd     = Column(Float, nullable=False, default=0.0)
    created_at          = Column(DateTime, nullable=False, default=_now)
    updated_at          = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    owner = relationship("User", back_populates="projects")


# ============================================================
# MODEL USAGE (per-request cost + token tracking)
# ============================================================

class ModelUsage(Base):
    __tablename__ = "model_usages"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id       = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    agent_id      = Column(String(255), nullable=True)
    project_id    = Column(String(255), nullable=True)
    product_id    = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    endpoint      = Column(String(255), nullable=True)
    source_channel = Column(String(32), nullable=True, index=True)
    model         = Column(String(100), nullable=False)
    input_tokens  = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    total_tokens  = Column(Integer, nullable=False, default=0)
    latency_ms    = Column(Float, nullable=True)
    cost_usd      = Column(Float, nullable=True)
    request_id    = Column(String(255), nullable=True, index=True)
    # Prompt-cache token columns (Part T4, 2026-05-25) — always present on the
    # table (ALTER TABLE ... DEFAULT 0), mapped here so the ORM insert path
    # used by workers/kafka_consumer._handle_metrics can populate them the
    # same way memory.postgres_memory.PostgresMemory.create_model_usage's raw
    # SQL INSERT already does.
    cache_read_tokens  = Column(BigInteger, nullable=False, default=0)
    cache_write_tokens = Column(BigInteger, nullable=False, default=0)
    # IST (not UTC, unlike every other table's created_at — see _now_ist()
    # docstring) per chargeback/audit-facing requirement: users reading their
    # model_usages rows expect wall-clock IST timestamps, not UTC.
    created_at    = Column(DateTime, nullable=False, default=_now_ist)

    user = relationship("User", back_populates="usages")


# ============================================================
# SDLC PIPELINE RUNS
# ============================================================

class SDLCRun(Base):
    """
    Tracks one end-to-end SDLC pipeline execution.
    type: "feature" | "bug"
    state: CREATED | CLASSIFYING | ANALYZING | DESIGNING | SOLUTION_REVIEW |
           AWAITING_DESIGN_APPROVAL | CODING | REVIEWING | REVIEW_GATE |
           FIXING | TESTING | SLT_RUNNING | COMPLETION_REVIEW | COMMITTING |
           AWAITING_PR_APPROVAL | COMPLETE | FAILED
    """
    __tablename__ = "sdlc_runs"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    type          = Column(String(50), nullable=False)          # feature | bug
    jira_key      = Column(String(100), nullable=True, index=True)
    jira_summary  = Column(Text, nullable=True)
    repo          = Column(String(255), nullable=True)
    branch        = Column(String(255), nullable=True)
    pr_number     = Column(Integer, nullable=True)
    pr_url        = Column(Text, nullable=True)
    confluence_url = Column(Text, nullable=True)
    state         = Column(String(100), nullable=False, default="CREATED")
    current_stage = Column(String(100), nullable=True)
    context       = Column(JSONB, nullable=False, default=dict)  # all agent outputs
    error         = Column(Text, nullable=True)
    triggered_by       = Column(String(255), nullable=True)
    suspended_at_stage = Column(String(50), nullable=True)
    created_by         = Column(String(128), nullable=True)
    created_at         = Column(DateTime, nullable=False, default=_now)
    updated_at         = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    # HOD budget tracking — populated during the run; deducted at terminal state
    total_input_tokens  = Column(Integer, nullable=False, default=0)
    total_output_tokens = Column(Integer, nullable=False, default=0)
    total_cost_usd      = Column(Numeric(12, 6), nullable=False, default=0)
    hod_email           = Column(String(255), nullable=True)   # resolved at preflight
    hod_ledger_id       = Column(UUID(as_uuid=False), nullable=True)  # ainxt.hod_allocation_ledger.id
    # Workspace consistency — the exact commit this run's checkout is pinned to,
    # captured at first clone and re-checked-out by every later stage/instance that
    # re-materializes the run (gated by SDLC_REUSE_RUN_WORKSPACE).
    base_sha            = Column(String(64), nullable=True)
    # Governance approval evidence → linked Jira Change ticket (V7, 2026-08-04).
    # We do not capture anything new here — these anchor the EXPORT of already-
    # persisted governance approval evidence to a dedicated linked Jira Change ticket.
    governance_evidence_jira_key  = Column(String(100), nullable=True)  # change-ticket key = create/update anchor
    governance_evidence_posted_at = Column(DateTime, nullable=True)
    governance_evidence_sha       = Column(String(64), nullable=True)   # SHA-256 of last attached bundle

    events = relationship("SDLCRunEvent", back_populates="run", cascade="all, delete-orphan")


class SDLCRunEvent(Base):
    """Immutable audit trail of every state transition in an SDLC run."""
    __tablename__ = "sdlc_run_events"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    run_id      = Column(UUID(as_uuid=False), ForeignKey("sdlc_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    from_state  = Column(String(100), nullable=True)
    to_state    = Column(String(100), nullable=False)
    stage       = Column(String(100), nullable=True)
    actor       = Column(String(255), nullable=True)   # agent name | user email
    output      = Column(Text, nullable=True)           # agent output for this stage
    data        = Column(JSONB, nullable=False, default=dict)
    signature   = Column(Text, nullable=True)           # HMAC-SHA256 audit signature (Phase 18)
    created_at  = Column(DateTime, nullable=False, default=_now)

    run = relationship("SDLCRun", back_populates="events")

class SDLCGovernanceEvidenceLog(Base):
    """Exactly-once dedup ledger for governance approval-evidence exports to the
    linked Jira Change ticket. A UNIQUE event_key row is claimed BEFORE the Jira
    write so RQ retries + repeated resume/approve calls never double-post
    (see store/sdlc_governance_evidence.py:evidence_log_claim)."""
    __tablename__ = "sdlc_governance_evidence_log"

    # NOTE: this table is ORM-managed, so db/migrate.py's create_all() creates it
    # (before the raw _part_v7 DDL, whose CREATE TABLE IF NOT EXISTS then no-ops).
    # The DB defaults therefore MUST live on the ORM columns as server_default —
    # a Python-side `default=` alone is not emitted as a DB DEFAULT and raw-SQL
    # inserts (store/sdlc_governance_evidence.py) would then violate NOT NULL.
    id         = Column(UUID(as_uuid=False), primary_key=True,
                        server_default=text("gen_random_uuid()"), default=_uuid)
    run_id     = Column(String(64), nullable=False, index=True)
    event_key  = Column(Text, nullable=False)
    jira_key   = Column(String(100), nullable=True)
    kind       = Column(String(32), nullable=True)   # domain_decision | final
    posted_at  = Column(DateTime, nullable=False,
                        server_default=func.now(), default=_now)

    __table_args__ = (UniqueConstraint("event_key", name="uq_gov_evidence_event_key"),)


class SDLCRunRepo(Base):
    """
    Per-repo record for an SDLC run.

    A single-repo run has exactly one row (kind='primary'). A multi-repo run has
    one row per repo touched (kind='primary' for the Jira-issue repo,
    'editable' for repos that may be patched, 'compile-only' for read-only deps
    cloned only to produce a classpath).

    sdlc_runs.(repo, pr_url, pr_number) continue to reflect the PRIMARY repo
    for backward compatibility. Sibling repo state lives here.
    """
    __tablename__ = "sdlc_run_repos"
    __table_args__ = (
        UniqueConstraint("run_id", "repo", name="uq_sdlc_run_repos_run_repo"),
        Index("ix_sdlc_run_repos_run_id", "run_id"),
        Index("ix_sdlc_run_repos_state", "state"),
    )

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    run_id         = Column(UUID(as_uuid=False), ForeignKey("sdlc_runs.id", ondelete="CASCADE"), nullable=False)
    repo           = Column(String(500), nullable=False)        # gitlab namespace/project path
    ref            = Column(String(255), nullable=False)        # branch or tag supplied at trigger
    ref_sha        = Column(String(64),  nullable=True)         # commit SHA pinned at preflight
    kind           = Column(String(20),  nullable=False)        # 'primary' | 'editable' | 'compile-only'
    build_order    = Column(Integer, nullable=True)
    source         = Column(String(20),  nullable=True)         # 'user' | 'manifest' | 'build-file' | 'primary'
    pr_url         = Column(Text, nullable=True)
    pr_number      = Column(Integer, nullable=True)
    working_branch = Column(String(255), nullable=True)
    repo_ctx       = Column(JSONB, nullable=False, default=dict)   # {language, framework, test_framework}
    workspace_path = Column(Text, nullable=True)                # per-repo checkout root inside the run workspace
    state          = Column(String(50),  nullable=True)         # 'READY'|'BUILDING'|'PATCHED'|'FAILED'|'MR_OPENED'
    error          = Column(Text, nullable=True)
    created_at     = Column(DateTime, nullable=False, default=_now)
    updated_at     = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# AGENT EPISODIC MEMORY  (Phase 17)
# ============================================================

class AgentMemory(Base):
    """Cross-session key-value memory store per agent."""
    __tablename__ = "agent_memory"
    __table_args__ = (
        UniqueConstraint("agent_name", "key", name="uq_agent_memory_name_key"),
    )

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    agent_name = Column(String(255), nullable=False, index=True)
    key        = Column(String(500), nullable=False)
    value      = Column(Text, nullable=True)
    tags       = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# CREDENTIAL VAULT
# ============================================================

class CredentialVault(Base):
    """Encrypted credential store — AES-256-GCM (see store/credential_vault.py).

    SEC-F-020/032 (2026-08-26): migrated from Fernet (AES-128-CBC+HMAC-SHA256)
    to true AES-256-GCM. decrypt_value() still reads legacy Fernet rows for
    backward compatibility; new/updated rows are always AES-256-GCM ("v2:").
    """
    __tablename__ = "credential_vault"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name         = Column(String(255), unique=True, nullable=False, index=True)  # e.g. "gitlab_token"
    description  = Column(Text, nullable=True)
    category     = Column(String(100), nullable=False, default="api_key")  # api_key | oauth_token | password | certificate
    encrypted    = Column(Text, nullable=False)   # AES-256-GCM ("v2:" prefix) or legacy Fernet
    owner_id     = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tags         = Column(JSONB, nullable=False, default=list)
    last_rotated = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, nullable=False, default=_now)
    updated_at   = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# GOVERNANCE EVENTS  (Phase 19 — durable audit log)
# ============================================================

class GovernanceEvent(Base):
    """Immutable audit record for every governance lifecycle transition."""
    __tablename__ = "governance_events"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    entity_type = Column(String(50),  nullable=False, index=True)   # agents|skills|mcp|workflows
    name        = Column(String(255), nullable=False, index=True)   # entity name
    action      = Column(String(50),  nullable=False)               # submit|approve|reject|promote|deprecate
    from_status = Column(String(50),  nullable=True)
    to_status   = Column(String(50),  nullable=False)
    actor       = Column(String(255), nullable=False)               # user email or "system"
    reason      = Column(Text,        nullable=True)                # rejection reason etc.
    created_by  = Column(String(255), nullable=True, index=True)    # owner user id for owner-scoped queries
    signature   = Column(Text,        nullable=True)                # HMAC-SHA256 — same scheme as sdlc_run_events
    created_at  = Column(DateTime,    nullable=False, default=_now)


# ============================================================
# THREADS & MESSAGES
# ============================================================

class Thread(Base):
    __tablename__ = "threads_pg"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    title            = Column(String(500), nullable=False)
    description      = Column(Text, nullable=True, default="")
    project_id       = Column(String(255), nullable=True, default="")
    repo             = Column(String(255), nullable=True, default="")
    product_id       = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by       = Column(String(255), nullable=True, default="user")
    labels           = Column(JSONB, nullable=False, default=list)
    priority         = Column(String(50), nullable=False, default="Medium")
    status           = Column(String(50), nullable=False, default="open", index=True)
    # Thread lifecycle: open | resolved | archived | purged
    agent_status     = Column(String(50), nullable=True)   # pending | running | done | failed
    ainxt_run_id     = Column(String(255), nullable=True)  # linked SDLC run if @AiNxt triggered
    expires_at       = Column(DateTime, nullable=True)     # created_at + 180 days (set on create)
    department       = Column(String(255), nullable=True)  # ABAC: dept-scoped thread visibility
    created_at       = Column(DateTime, nullable=False, default=_now)
    updated_at       = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    messages = relationship("ThreadMessage", back_populates="thread",
                            cascade="all, delete-orphan", order_by="ThreadMessage.created_at")


class ThreadMessage(Base):
    __tablename__ = "thread_messages"

    id                = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    thread_id         = Column(UUID(as_uuid=False), ForeignKey("threads_pg.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    parent_message_id = Column(UUID(as_uuid=False), nullable=True, index=True)   # NULL = top-level
    content           = Column(Text, nullable=False)
    author            = Column(String(255), nullable=False, default="user")    # user_id or "ainxt"
    author_name       = Column(String(255), nullable=True)
    author_band       = Column(String(10), nullable=True)                       # A1, B2, C1...
    # message_type: text | ainxt_analysis | ainxt_action | system | merge_conflict
    message_type      = Column(String(50), nullable=False, default="text")
    # HITL state for ainxt_analysis messages: pending | approved | modified | rejected
    hitl_status       = Column(String(50), nullable=True)
    ainxt_run_id      = Column(String(255), nullable=True)   # links to sdlc_runs if SDLC triggered
    mentions          = Column(JSONB, nullable=False, default=list)
    reactions         = Column(JSONB, nullable=False, default=dict)  # {"👍":["uid1","uid2"],...}
    model_used        = Column(String(100), nullable=True)
    tokens_in         = Column(Integer, nullable=True)
    tokens_out        = Column(Integer, nullable=True)
    cost_usd          = Column(Float, nullable=True)
    latency_ms        = Column(Float, nullable=True)
    edited_at         = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, nullable=False, default=_now)

    thread = relationship("Thread", back_populates="messages")


# ============================================================
# INBOX ITEMS
# ============================================================

class InboxItem(Base):
    __tablename__ = "inbox_items"

    id        = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id   = Column(String(255), nullable=False, index=True)
    type      = Column(String(100), nullable=False, default="notification")
    title     = Column(String(500), nullable=False)
    body      = Column(Text, nullable=True, default="")
    source_id = Column(String(255), nullable=True, default="")
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict)
    read      = Column(Boolean, nullable=False, default=False)
    # Postgres column is TIMESTAMP WITH TIME ZONE. Store tz-aware UTC so that
    # a naive ``datetime.utcnow()`` isn't coerced into the server's local zone
    # (see ``_now_utc`` docstring) and reads come back as a correct UTC moment.
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now_utc)


# ============================================================
# DOCUMENT EMBEDDINGS  (Phase 19 — pgvector)
# ============================================================

class DocumentEmbedding(Base):
    """
    Vector store for RAG — migrated from ChromaDB SQLite to pgvector.
    Requires the pgvector Postgres extension: CREATE EXTENSION vector;

    RAG ACL classification levels:
      PUBLIC            — visible to all authenticated users
      INTERNAL          — all AiNxt employees (any role)
      CONFIDENTIAL      — developer+ (operator/admin/security)
      RESTRICTED        — operator/admin/security only
      PCI_SENSITIVE     — security/admin only + explicit user whitelist
    """
    __tablename__ = "document_embeddings"
    __table_args__ = (
        UniqueConstraint("repo", "file_path", "chunk_index", name="uq_doc_embed_chunk"),
    )

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    repo        = Column(String(255), nullable=False, index=True)
    file_path   = Column(Text,        nullable=False)
    chunk_index = Column(Integer,     nullable=False, default=0)
    content     = Column(Text,        nullable=False)
    # embedding column: Vector(768) when pgvector available, Text fallback
    embedding   = Column(_VECTOR_TYPE, nullable=True)
    metadata_   = Column("metadata",  JSONB, nullable=False, default=dict)
    # Dedup / change-detection (added Phase 21)
    content_hash = Column(String(64),  nullable=True,  index=True)  # SHA-256 of content
    line_start   = Column(Integer,     nullable=True)
    line_end     = Column(Integer,     nullable=True)
    # RAG Access Control
    classification = Column(String(50), nullable=False, default="INTERNAL", index=True)
    # PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED | PCI_SENSITIVE
    owner_team     = Column(String(255), nullable=True,  index=True)   # e.g. "payments", "security"
    org_id         = Column(String(255), nullable=True,  index=True)   # multi-tenant isolation
    uploaded_by    = Column(String(255), nullable=True)                 # user email / "indexer"
    allowed_roles  = Column(JSONB, nullable=False, default=list)        # [] = role-based gate only
    allowed_users  = Column(JSONB, nullable=False, default=list)        # [] = no user whitelist
    # Phase 2: product-scoped RAG ACL — NULL = platform-wide doc
    product_id     = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    # Phase 1 — Spec scope columns (stamped from KnowledgeDocument at activation time)
    domain         = Column(String(100), nullable=True, index=True)   # e.g. "Tech", "HR"
    spec_version   = Column(String(50),  nullable=True, index=True)   # e.g. "v3"
    # Phase 2 — Section-aware chunking + parent linkage
    parent_chunk_id   = Column(UUID(as_uuid=False), nullable=True, index=True)  # leaf → parent section row
    section_path      = Column(Text,    nullable=True)                          # e.g. "1. Intro > 1.2 Scope"
    is_section_parent = Column(Boolean, nullable=False, default=False, index=True)  # True = whole-section row
    # ── Part U11 (2026-06-08) — docx §8 hierarchy metadata on the chunk row ──
    # Denormalised so Fast-tier citation rendering + source_type filtering
    # don't need a cross-DB join into PGS01.knowledge_docs. All nullable —
    # NULL passes the CHECK and the partial index skips it.
    #   page_number  — 1-based page from the PDF parser; NULL for MD/code.
    #   section_name — leaf heading (last segment of section_path).
    #   doc_name     — copied from KnowledgeDocument.name at activate_doc time.
    #   source_type  — BRD/FSD/TPMC_DECISION/RBI_CIRCULAR/ARCHITECTURE/SPEC/OTHER
    #                  copied from KnowledgeDocument.source_type at activation.
    page_number       = Column(Integer, nullable=True)
    section_name      = Column(Text,    nullable=True)
    doc_name          = Column(Text,    nullable=True)
    source_type       = Column(String(32), nullable=True, index=True)
    # Phase 1 closure — chunk-level active-version filter
    # 'ACTIVE' = retrievable; 'DEPRECATED' = flipped by docs_store.activate_doc
    # when a newer spec version supersedes (deprecate_prior=True branch).
    # hybrid_search appends `AND status='ACTIVE'` so deprecation is instant —
    # no re-index, no delete, no stale chunks bleeding into retrieval.
    status         = Column(String(30), nullable=False, default="ACTIVE", index=True)
    # Department scoping — NULL or '' means visible to all departments
    department     = Column(String(255), nullable=True, index=True)
    # Branch scoping — NULL means unscoped (legacy rows); set during indexing
    branch         = Column(String(255), nullable=True, index=True)
    created_at     = Column(DateTime, nullable=False, default=_now)


# ── Eval Results ──────────────────────────────────────────────────────────────

class EvalResult(Base):
    """LLM-as-judge evaluation results for every critical execution point."""
    __tablename__ = "eval_results"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    eval_type   = Column(String(64),  nullable=False, index=True)   # e.g. "groundedness"
    score       = Column(Float,       nullable=False)                # 0.0–1.0
    reason      = Column(Text,        nullable=True)                 # one-sentence explanation
    session_id  = Column(String(255), nullable=True,  index=True)   # chat session
    run_id      = Column(String(255), nullable=True,  index=True)   # SDLC run
    question    = Column(Text,        nullable=True)                 # truncated question
    metadata_   = Column("metadata",  JSONB, nullable=False, default=dict)
    created_at  = Column(DateTime,    nullable=False, default=_now)
    # Platform tag — nullable so existing rows are unaffected (backward compatible).
    # Values: "chat" | "knowledge_base" | "my_workspace" | "agent_studio" |
    #         "cli"  | "ide_extension"  | "buddy_cowork" | "workflows"
    # NULL means the row was written before multi-platform support was added;
    # the API treats NULL rows as belonging to "all platforms".
    platform    = Column(String(64),  nullable=True,  index=True)
    # Source model that generated the answer being judged.
    # NULL for rows written before this column was added (backward compatible).
    # Example: "GPT-5 Mini (gpt-5-mini)" or "Kimi K2 (kimi-k2.7-code)"
    model       = Column(String(255), nullable=True,  index=True)
    # Model that acted as the LLM judge for this eval row.
    # NULL for rows written before this column was added (backward compatible).
    # Example: "GLM-5.2 FP8 (glm-5.2-fp8)" or "DeepSeek V4 Flash (deepseek-v4-flash)"
    judge_model = Column(String(255), nullable=True,  index=True)


# ============================================================
# KNOWLEDGE DOCUMENTS  (Document KB upload feature)
# ============================================================

class KnowledgeDocument(Base):
    """
    Uploaded enterprise documents for RAG.
    Phase 7: visibility governance, band-based ACL, content dedup, approval lifecycle.
    """
    __tablename__ = "knowledge_docs"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name             = Column(String(512), nullable=False)
    filename         = Column(String(512), nullable=False)
    namespace        = Column(String(128), nullable=False, index=True)
    content          = Column(Text, nullable=False)
    content_hash     = Column(String(64), nullable=True, index=True)   # SHA-256; dedup skip if exists
    chunks           = Column(JSONB, nullable=True)                    # raw text chunks — populated at upload, cleared after embedding on approval
    chunk_count      = Column(Integer, default=0)
    file_size        = Column(Integer, default=0)
    # Governance
    visibility       = Column(String(20), nullable=False, default="PUBLIC", index=True)
    # PUBLIC | PRIVATE
    min_band_level   = Column(Integer, nullable=False, default=1)   # 1=A1 … 9=E; only for PRIVATE
    product_id       = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="SET NULL"),
                              nullable=True, index=True)
    department_ids   = Column(JSONB, nullable=False, default=list)  # [] = all departments
    # Phase 1 — Spec scope metadata (product/domain/version)
    domain           = Column(String(100), nullable=True,  index=True)   # e.g. "Tech", "HR", "Finance"
    spec_version     = Column(String(50),  nullable=True,  index=True)   # e.g. "v3", "2025.1"
    version_date     = Column(DateTime,    nullable=True)                 # effective date of this version
    deprecate_prior  = Column(Boolean,     nullable=False, default=False) # True = mark prior versions deprecated on activation
    # Full doc body is stored on the local filesystem at
    # KB_DOC_STORAGE_PATH/<id>.md (kn_rewrite.md §6). The URI is implicit
    # from the doc_id — no column needed. Reader: store.kb_doc_cache.warm.
    parent_doc_id    = Column(UUID(as_uuid=False), nullable=True)         # prior version doc_id (lineage)
    # Phase 4 — Version scope cascade (explicit→as-of→active)
    valid_from       = Column(DateTime(timezone=True), nullable=True)     # when this version became authoritative
    valid_to         = Column(DateTime(timezone=True), nullable=True)     # NULL = still active
    # ── Part U13 (2026-06-08) — docx §8 hierarchy + §2/§13 retain originals ──
    # source_type pins the doc kind so retrieval can filter "only FSDs" and so
    # the citation footer can render a typed badge. Denormalised onto chunks
    # via document_embeddings.source_type (Part U11) for cross-DB-join-free
    # Fast-tier filtering. Enum is CHECK-enforced at DB level.
    source_type      = Column(String(32), nullable=True, index=True)
    # original_ext lets the citation footer link to the binary original at
    # KB_DOC_STORAGE_PATH/<id>.<ext>, written next to the canonical <id>.md
    # by docs_store.upload_doc. NULL = no original retained (legacy uploads).
    original_ext     = Column(String(16), nullable=True)
    # True when the PDF was uploaded as an image-only (scanned) document with no
    # embedded text. OCR (PaddleOCR via Docling) and compliance checks are deferred
    # to activate_doc() post-approval. False for all other document types.
    is_scanned_pdf          = Column(Boolean, nullable=False, default=False)
    # True when the PDF is a mixed document: some pages have selectable text
    # (born-digital) and other pages are image-only (scanned). Unlike is_scanned_pdf,
    # the upload succeeds with partial text. At activation, PaddleOCR runs on the
    # scanned pages and the results are merged with the digital pages' text.
    # Deferred compliance also runs on the merged output.
    has_mixed_scanned_pages = Column(Boolean, nullable=False, default=False, server_default="false")
    # Approval lifecycle: PENDING_APPROVAL | AUTO_APPROVED | APPROVED | INDEXING | ACTIVE | REJECTED | DEPRECATED | DELETING
    # INDEXING = approved, background kb_worker is running Docling parse + embed (not yet searchable)
    # ACTIVE   = fully indexed and RAG-searchable (set by kb_worker on success)
    # DELETING = deletion/cancellation is in progress; activation workers must stop
    status           = Column(String(30), nullable=False, default="PENDING_APPROVAL", index=True)
    compliance_pass  = Column(Boolean, nullable=True)   # None = not checked, True/False = result
    approved_by      = Column(String(255), nullable=True)
    approved_at      = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    # Stores the human-readable error from the last failed activate_doc() attempt
    # (e.g. "Embedding failed: ReadTimeout", "12 of 40 chunks returned zero vectors").
    # Set by kb_worker on rollback to PENDING_APPROVAL; cleared at the start of
    # each new activation attempt. Surfaced in the UI so approvers know why a doc
    # silently returned to PENDING_APPROVAL without digging through server logs.
    parse_error      = Column(Text, nullable=True)
    uploaded_by      = Column(String(255), nullable=True)
    uploaded_by_dept = Column(String(255), nullable=True, index=True)   # uploader's dept at upload time
    created_at       = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at       = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


# ============================================================
# KB DELETION HISTORY  (2026-08-06)
#
# Snapshot of a knowledge_docs row written at the moment it is hard-deleted,
# ONLY when the doc's status was ACTIVE (i.e. it had gone through approval +
# indexing and was RAG-searchable at some point). Docs deleted while still
# PENDING_APPROVAL or REJECTED never went live, so no history row is written
# for them — same behaviour as before this feature existed.
#
# Written inside the same DB transaction as the knowledge_docs row delete in
# store/docs_store.py::delete_doc(), so the two can never diverge (either both
# happen or neither does).
#
# Visibility of rows in this table is governed entirely at the API layer
# (routers/docs_router.py: GET /kb/deleted-history) per the ACL rule matrix:
#   - admin (role == "admin")            -> sees everything
#   - HOD (is_hod / get_hod_departments) -> sees PUBLIC deletions org-wide,
#                                            plus PRIVATE deletions for their
#                                            own department(s)
#   - everyone else                      -> sees only rows where they were the
#                                            uploader or the one who deleted it
# The visibility/department_ids/*_dept columns below exist so that filter can
# run without needing to join back to knowledge_docs (the row is gone by then).
# ============================================================

class KnowledgeDocDeletion(Base):
    """Immutable snapshot of an ACTIVE knowledge_docs row at hard-delete time."""
    __tablename__ = "knowledge_doc_deletions"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    doc_id           = Column(UUID(as_uuid=False), nullable=False, index=True)  # original knowledge_docs.id — not a FK, the row no longer exists
    name             = Column(String(512), nullable=False)
    filename         = Column(String(512), nullable=False)
    namespace        = Column(String(128), nullable=False, index=True)
    file_size        = Column(Integer, default=0)
    chunk_count      = Column(Integer, default=0)

    # Governance snapshot — needed to evaluate the ACL rule matrix on this table
    visibility       = Column(String(20), nullable=False, default="PUBLIC", index=True)
    department_ids   = Column(JSONB, nullable=False, default=list)
    product_id       = Column(UUID(as_uuid=False), nullable=True, index=True)
    domain           = Column(String(100), nullable=True)
    spec_version     = Column(String(50), nullable=True)
    source_type      = Column(String(32), nullable=True)
    original_ext     = Column(String(16), nullable=True)

    # Lifecycle snapshot at time of deletion — always "ACTIVE" by construction
    # (see delete_doc()), kept as a column rather than hardcoded for forward
    # compatibility and auditability.
    status           = Column(String(30), nullable=False, default="ACTIVE")
    uploaded_by      = Column(String(255), nullable=True, index=True)
    uploaded_by_dept = Column(String(255), nullable=True, index=True)
    approved_by      = Column(String(255), nullable=True)
    approved_at      = Column(DateTime, nullable=True)
    doc_created_at   = Column(DateTime(timezone=True), nullable=True)   # original knowledge_docs.created_at

    # Deletion event fields
    deleted_by       = Column(String(255), nullable=False, index=True)
    deleted_by_dept  = Column(String(255), nullable=True, index=True)
    deleted_at       = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)


# ============================================================
# MESSAGE FEEDBACK  (human preference / RL signal)
# ============================================================

class MessageFeedback(Base):
    """Thumbs-up / thumbs-down feedback on individual chat assistant messages."""
    __tablename__ = "message_feedback"

    id                 = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    message_id         = Column(String(255), nullable=False, index=True)  # chat_messages.id
    user_id            = Column(String(255), nullable=False, index=True)
    rating             = Column(Integer, nullable=False)   # +1 thumbs-up, -1 thumbs-down
    issue              = Column(String(255), nullable=True)  # thumbs-down category
    sub_issue          = Column(String(255), nullable=True)  # sub-category
    comment            = Column(Text, nullable=True)         # free-text from user
    user_prompt        = Column(Text, nullable=True)         # the question that triggered this response
    assistant_summary  = Column(Text, nullable=True)         # first 800 chars of the response
    created_at         = Column(DateTime, nullable=False, default=_now)


# ============================================================
# RAG ACCESS LOG  (immutable audit trail — PCI/DSS requirement)
# ============================================================

class RAGAccessLog(Base):
    """
    Immutable record of every RAG retrieval.  Written on every search call,
    whether the chunk was returned or silently filtered.
    Required for PCI/DSS audit trail — never deleted.
    """
    __tablename__ = "rag_access_log"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id        = Column(String(255), nullable=False, index=True)
    user_role      = Column(String(50),  nullable=False)
    org_id         = Column(String(255), nullable=True,  index=True)
    query_hash     = Column(String(64),  nullable=False)   # SHA-256 of query text (not stored raw)
    chunk_id       = Column(String(36),  nullable=False,   index=True)  # document_embeddings.id
    repo           = Column(String(255), nullable=False)
    file_path      = Column(Text,        nullable=False)
    classification = Column(String(50),  nullable=False)   # chunk's classification at access time
    access_granted = Column(Boolean,     nullable=False)   # True = returned to LLM, False = filtered
    deny_reason    = Column(String(255), nullable=True)    # e.g. "role_insufficient", "org_mismatch"
    session_id     = Column(String(255), nullable=True,    index=True)
    created_at     = Column(DateTime,    nullable=False, default=_now)


# ============================================================
# ENGINEER WORK CONTEXT  (cross-session continuity)
# ============================================================

class EngineerWorkContext(Base):
    """
    Persists the rolling work context for each engineer across sessions.
    Rebuilt after each session via LLM summarisation (max 500 tokens).
    Used to cold-start the next session with full context.
    """
    __tablename__ = "engineer_work_context"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_engineer_context_user"),
    )

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id         = Column(String(255), nullable=False, unique=True, index=True)
    summary         = Column(Text, nullable=True)          # LLM-generated rolling summary
    active_repos    = Column(JSONB, nullable=False, default=list)   # recently accessed repos
    active_tickets  = Column(JSONB, nullable=False, default=list)   # open Jira tickets
    recent_files    = Column(JSONB, nullable=False, default=list)   # recently touched files
    recent_decisions = Column(JSONB, nullable=False, default=list)  # key decisions made
    session_count   = Column(Integer, nullable=False, default=0)
    last_session_at = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, nullable=False, default=_now)
    updated_at      = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# CODE SYMBOLS  (symbol index for exact code entity lookup)
# ============================================================
class CodeSymbol(Base):
    """
    One row per named code entity extracted during indexing.
    Enables exact symbol lookup (class, method, interface) without
    relying on embeddings — critical for accurate code Q&A.
    """
    __tablename__ = "code_symbols"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    repo         = Column(Text, nullable=False, index=True)
    file_path    = Column(Text, nullable=False)
    symbol_name  = Column(Text, nullable=False, index=True)
    symbol_type  = Column(String(50), nullable=False)   # class|function|method|interface|enum|struct|trait|module
    language     = Column(String(30), nullable=False)
    line_start   = Column(Integer, nullable=True)
    line_end     = Column(Integer, nullable=True)
    signature    = Column(Text, nullable=True)           # full signature with params + return type
    parent_name  = Column(Text, nullable=True)           # containing class/module name for methods
    embedding_id = Column(Text, nullable=True)           # document_embeddings.id (for blending)
    created_at   = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        Index("idx_symbols_repo_name", "repo", "symbol_name"),
        Index("idx_symbols_name_lower", func.lower(text("symbol_name")), postgresql_using="btree"),
    )


# ============================================================
# REPO PERMISSIONS  (team-level repo access control)
# ============================================================
class RepoPermission(Base):
    """
    Controls which users/roles may query a specific indexed repo.
    Enforced at retrieval level in hybrid_search.py — not just UI.
    Empty = all authenticated users may access (default-open during rollout).
    """
    __tablename__ = "repo_permissions"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    repo       = Column(Text, nullable=False, index=True)
    user_id    = Column(String(255), nullable=True,  index=True)  # NULL = role-based
    user_role  = Column(String(50),  nullable=True)               # NULL = user-based
    granted    = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("repo", "user_id", "user_role", name="uq_repo_perm"),
    )


# ============================================================

# ============================================================
# USER TOKENS (per-user API keys — Local LLM, GitLab, Atlassian)
# ============================================================

class UserToken(Base):
    """
    User-managed API tokens stored AES-256-GCM-encrypted (SEC-F-020/032
    follow-up, 2026-08-26 — see routers/profile_router.py /
    core/platform_credentials.py). Legacy Fernet rows still decrypt
    transparently until re-written or bulk-migrated via
    scripts/migrate_all_to_aes_gcm.py.
    NOT fetched from credential_vault — user enters in Profile UI.
    token_type: local_llm | atlassian | gitlab
    """
    __tablename__ = "user_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "token_type", name="uq_user_token_type"),
    )

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid, server_default=text("gen_random_uuid()"))
    user_id         = Column(String(255), nullable=False, index=True)
    token_type      = Column(String(50),  nullable=False)
    encrypted_value = Column(Text,        nullable=False)
    label           = Column(String(255), nullable=True)
    is_active       = Column(Boolean,     nullable=False, default=True)
    created_at      = Column(DateTime,    nullable=False, default=_now)
    updated_at      = Column(DateTime,    nullable=False, default=_now, onupdate=_now)


# ============================================================
# USER HIERARCHY (AD reporting chain cache)
# ============================================================

class UserHierarchy(Base):
    """AD manager chain — rebuilt nightly by workers/ad_sync.py."""
    __tablename__ = "user_hierarchy"

    user_id        = Column(String(255), primary_key=True)
    manager_ids    = Column(JSONB, nullable=False, default=list)   # [L1, L2, L3, L4]
    report_ids     = Column(JSONB, nullable=False, default=list)   # direct reports
    last_synced_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# ============================================================
# PRODUCT ONTOLOGY
# ============================================================

class Product(Base):
    """
    Top-level organizing entity. Only C1+ users (band_level >= 5) may create.
    Products own repos, Jira projects, Confluence spaces, and members.
    """
    __tablename__ = "products"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name             = Column(String(255), nullable=False, unique=True)
    code             = Column(String(50),  nullable=False, unique=True)
    description      = Column(Text,        nullable=True)
    jira_project_key = Column(String(50),  nullable=True)
    confluence_space = Column(String(50),  nullable=True)
    jira_url         = Column(Text,        nullable=True)
    confluence_url   = Column(Text,        nullable=True)
    is_active        = Column(Boolean,     nullable=False, default=True, index=True)
    status           = Column(String(30),  nullable=False, default="ACTIVE", index=True)  # PENDING_APPROVAL | ACTIVE | REJECTED
    requested_by     = Column(String(255), nullable=True)
    reviewed_by      = Column(String(255), nullable=True)
    reviewed_at      = Column(DateTime,    nullable=True)
    review_note      = Column(Text,        nullable=True)
    created_by       = Column(String(255), nullable=False)
    created_at       = Column(DateTime,    nullable=False, default=_now)
    updated_at       = Column(DateTime,    nullable=False, default=_now, onupdate=_now)

    repos   = relationship("ProductRepo",   back_populates="product", cascade="all, delete-orphan")
    owners  = relationship("ProductOwner",  back_populates="product", cascade="all, delete-orphan")
    members = relationship("ProductMember", back_populates="product", cascade="all, delete-orphan")


class ProductRepo(Base):
    """Maps a product to one or more GitLab repositories."""
    __tablename__ = "product_repos"
    __table_args__ = (
        UniqueConstraint("product_id", "repo_name", name="uq_product_repo"),
    )

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    product_id = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    repo_name  = Column(String(255), nullable=False, index=True)
    branch     = Column(String(100), nullable=False, default="main")
    added_by   = Column(String(255), nullable=False)
    created_at = Column(DateTime,    nullable=False, default=_now)

    product = relationship("Product", back_populates="repos")


class GovernanceSuppression(Base):
    """Explicit, per-(product, repo) suppression of a governance finding
    (EA/IS/DPDP …). Content-fingerprint keyed (line-independent) so a triaged
    false positive does not resurface on unrelated edits. NEVER auto-created —
    always an explicit human triage with `reason` + `created_by` audit trail.
    See ``agents/sdlc_governance/schema.py:fingerprint`` for the key scheme.

    NOTE: ``product_id`` is nullable (a repo may be unmapped to any product). In
    Postgres, NULLs are DISTINCT in a UNIQUE constraint, so the ON CONFLICT
    upsert (routers/sdlc_router.py) does not de-dup rows with product_id IS NULL;
    that is acceptable — the suppression FILTER matches by tuple regardless of
    duplicate NULL-product rows."""
    __tablename__ = "sdlc_governance_suppressions"
    __table_args__ = (
        UniqueConstraint("product_id", "repo_name", "skill", "fingerprint", name="uq_gov_suppression"),
        Index("idx_gov_suppr_repo_skill", "repo_name", "skill"),
    )

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    product_id  = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True)
    repo_name   = Column(String(255), nullable=False, index=True)
    skill       = Column(String(64),  nullable=False)
    fingerprint = Column(String(128), nullable=False)
    # Skill-independent content correlation key (gvc1:… — see
    # agents/sdlc_governance/schema.py:content_fingerprint). Nullable for
    # backward compatibility: legacy rows match on the (skill, fingerprint)
    # tuple, new rows also match on (repo, content_key) across any domain.
    content_key = Column(String(128), nullable=True)
    rule        = Column(String(255), nullable=True)
    reason      = Column(Text,        nullable=True)
    created_by  = Column(String(255), nullable=False)
    active      = Column(Boolean,     nullable=False, default=True)
    created_at  = Column(DateTime,    nullable=False, default=_now)


class ProductOwner(Base):
    """C1+ users assigned as product owner or admin."""
    __tablename__ = "product_owners"
    __table_args__ = (
        UniqueConstraint("product_id", "user_id", name="uq_product_owner"),
    )

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    product_id = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id    = Column(String(255), nullable=False, index=True)
    role       = Column(String(50),  nullable=False, default="owner")   # owner | admin
    added_by   = Column(String(255), nullable=False)
    created_at = Column(DateTime,    nullable=False, default=_now)

    product = relationship("Product", back_populates="owners")


class ProductMember(Base):
    """Product membership — auto-populated via AD sync or manually added."""
    __tablename__ = "product_members"
    __table_args__ = (
        UniqueConstraint("product_id", "user_id", name="uq_product_member"),
    )

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    product_id = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id    = Column(String(255), nullable=False, index=True)
    source     = Column(String(50),  nullable=False, default="manual")  # manual | ad_sync
    added_at   = Column(DateTime,    nullable=False, default=_now)

    product = relationship("Product", back_populates="members")


# ============================================================
# INDEX REQUESTS (codebase indexing with approval gate)
# ============================================================

class IndexRequest(Base):
    """
    Request to index a GitLab repo into pgvector.
    Requires operator+ approval. Admin's git token used for cloning.
    status: pending | approved | rejected | running | done | failed
    """
    __tablename__ = "index_requests"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    repo_name    = Column(String(255), nullable=False, index=True)
    branch       = Column(String(100), nullable=False, default="main")
    product_id   = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    requested_by = Column(String(255), nullable=False, index=True)
    status       = Column(String(50),  nullable=False, default="pending", index=True)
    reviewed_by  = Column(String(255), nullable=True)
    review_note  = Column(Text,        nullable=True)
    reviewed_at  = Column(DateTime,    nullable=True)
    error_msg    = Column(Text,        nullable=True)
    # RQ job id for the enqueued workers.index_worker.index_repo_job run —
    # lets recover_stale_index_locks() (workers/index_worker.py) cross-check
    # a 'running' row against RQ's own job registry before touching it,
    # the same safe pattern codewiki_worker.py's recover_orphaned_codewiki_jobs
    # uses. Without this, a stale-vs-still-running distinction is impossible:
    # indexing legitimately takes anywhere from seconds to 10+ hours, so a
    # fixed time threshold alone would misidentify a genuinely long-running
    # job as crashed.
    job_id       = Column(String(64),  nullable=True)
    created_at   = Column(DateTime,    nullable=False, default=_now)
    updated_at   = Column(DateTime,    nullable=False, default=_now, onupdate=_now)


# ============================================================
# TOOL SUBMISSIONS  (Phase 8: IS team governance review)
# ============================================================

class ToolSubmission(Base):
    """
    A developer submits an MCP tool / skill / agent for IS team review.
    Auto-expires after 5 days without action (SLA).
    """
    __tablename__ = "tool_submissions"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    entity_type     = Column(String(50),  nullable=False, index=True)  # mcp_tool | skill | agent | agent_chain
    entity_name     = Column(String(255), nullable=False, index=True)
    visibility      = Column(String(20),  nullable=False, default="PUBLIC")  # PUBLIC | PRIVATE
    submitted_by    = Column(String(255), nullable=False, index=True)
    product_id      = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    # status: PENDING_IS_REVIEW | PENDING_C1_REVIEW | APPROVED | REJECTED | EXPIRED
    status          = Column(String(30),  nullable=False, default="PENDING_IS_REVIEW", index=True)
    requires_is_review = Column(Boolean, nullable=False, default=True)  # False = internal-only tools
    risk_score      = Column(Float,       nullable=True)   # CVSS-style 0.0–10.0
    risk_report     = Column(Text,        nullable=True)   # Security Assessment Agent output
    reviewed_by     = Column(String(255), nullable=True)
    review_note     = Column(Text,        nullable=True)
    reviewed_at     = Column(DateTime,    nullable=True)
    expires_at      = Column(DateTime,    nullable=True)   # submitted_at + 5 days
    created_at      = Column(DateTime,    nullable=False, default=_now)
    updated_at      = Column(DateTime,    nullable=False, default=_now, onupdate=_now)


# ============================================================
# MODEL RATE TABLE  (Phase 9: real cost tracking)
# ============================================================

class ModelRateTable(Base):
    """Versioned cost per model per 1K tokens — updated when providers change pricing."""
    __tablename__ = "model_rate_table"
    __table_args__ = (
        UniqueConstraint("model_id", "effective_from", name="uq_model_rate"),
    )

    id                    = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    model_id              = Column(String(100), nullable=False, index=True)   # e.g. claude-sonnet-4-6
    provider              = Column(String(50),  nullable=False)               # anthropic | openai | google | local_llm
    input_cost_per_1k     = Column(Float,       nullable=False, default=0.0)  # USD per 1K input tokens
    output_cost_per_1k    = Column(Float,       nullable=False, default=0.0)  # USD per 1K output tokens
    effective_from        = Column(DateTime,    nullable=False, default=_now)
    is_free               = Column(Boolean,     nullable=False, default=False) # True for Local LLM / Ollama
    created_at            = Column(DateTime,    nullable=False, default=_now)


# ============================================================
# BUDGET CONFIG  (Phase 9: band-based monthly limits)
# ============================================================

# Platform base budget allocation — the Python-side ORM default applied
# whenever a new BudgetConfig row is inserted without an explicit
# base_cost_usd. Configurable via BUDGET_BASE_COST_USD (default $50); mirrors
# store.budget_store.BASE_COST_USD. Note this does NOT change the real
# Postgres column DEFAULT set by db/migrate.py's DDL — that stays a static
# $50 fallback for rows inserted outside the ORM (see migrate.py comments).
_BASE_COST_USD_DEFAULT = float(os.getenv("BUDGET_BASE_COST_USD", "50.0"))


class BudgetConfig(Base):
    """
    Per-user budget configuration. Rows are only ever created for a real
    user_id — band-level template rows (user_id IS NULL) are not seeded, to
    avoid populating the table with dead data. monthly_limit_usd and
    base_cost_usd are always kept equal to the platform base allocation
    (configurable via BUDGET_BASE_COST_USD, default $50).
    """
    __tablename__ = "budget_configs"

    id                    = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id               = Column(String(255), nullable=True, unique=True, index=True)
    band_level            = Column(Integer,     nullable=True, index=True)   # 1=A1 … 9=E
    monthly_limit_usd     = Column(Float,       nullable=False, default=_BASE_COST_USD_DEFAULT)
    base_cost_usd         = Column(Numeric(12, 6), nullable=False, default=_BASE_COST_USD_DEFAULT)
    extra_cost_usd        = Column(Numeric(12, 6), nullable=False, default=0.0)
    # Optional: restrict which models this user/band can use
    model_allowlist       = Column(JSONB, nullable=False, default=list)  # [] = all allowed
    updated_by            = Column(String(255), nullable=True)
    created_at            = Column(DateTime,    nullable=False, default=_now)
    updated_at            = Column(DateTime,    nullable=False, default=_now, onupdate=_now)


# ============================================================
# USER USAGE TOTALS  (Postgres source of truth for budget)
# Redis `usage:{uid}:total` is a fast-path cache of this table.
# Falls back to this table when Redis is unavailable.
# ============================================================

class UserUsageTotal(Base):
    """Cumulative per-user LLM usage — Postgres-durable running total."""
    __tablename__ = "user_usage_totals"

    user_id        = Column(String(255), primary_key=True)
    tokens_used    = Column(BigInteger,  nullable=False, default=0)
    requests_made  = Column(BigInteger,  nullable=False, default=0)
    cost_usd_spent = Column(Numeric(12, 6), nullable=False, default=0.0)
    last_updated   = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# BUDGET PERIOD AUDIT  (monthly closing snapshot, written by cron)
# ============================================================

class BudgetPeriodAudit(Base):
    """
    One row per (user, closing month). Written by the monthly reset cron.
    Captures the user's budget state at month-end before the limit is
    reset to the band default and usage counters are zeroed.

    NOTE: user_id is plain String (no FK) to match BudgetConfig.user_id and
    UserUsageTotal.user_id conventions in this codebase (those tables also
    store User.id without a declared FK).
    """
    __tablename__ = "budget_period_audits"

    id                    = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id               = Column(String(255), nullable=False, index=True)
    period_yyyymm         = Column(String(7),   nullable=False, index=True)  # closing month, 'YYYY-MM'

    ad_level              = Column(Integer,        nullable=True)
    default_limit_usd     = Column(Numeric(12, 6), nullable=False)   # band default at reset time
    opening_limit_usd     = Column(Numeric(12, 6), nullable=False)   # limit at start of period (best-effort)
    closing_limit_usd     = Column(Numeric(12, 6), nullable=False)   # BudgetConfig.monthly_limit_usd pre-reset

    cost_used_usd         = Column(Numeric(12, 6), nullable=False, default=0)
    tokens_used           = Column(BigInteger,     nullable=False, default=0)
    requests_made         = Column(BigInteger,     nullable=False, default=0)
    unutilized_usd        = Column(Numeric(12, 6), nullable=False, default=0)  # max(0, closing_limit_usd - cost_used_usd)

    increase_count        = Column(Integer, nullable=False, default=0)
    increase_history_json = Column(JSONB,   nullable=False, default=list)  # list of ledger entries

    reset_to_usd          = Column(Numeric(12, 6), nullable=False)        # new limit after reset (= default_limit_usd)
    snapshot_at           = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "period_yyyymm", name="uq_budget_period_audit_user_period"),
        Index("ix_budget_period_audit_period", "period_yyyymm"),
    )


# ============================================================
# ORG TREE  (RBAC/ABAC Phase: AD hierarchy cache)
# ============================================================

class OrgTree(Base):
    """AD org hierarchy — rebuilt via POST /admin/sync/org-tree CSV upload."""
    __tablename__ = "org_tree"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    level          = Column(Integer, nullable=False)                  # 0=most senior, 6=junior
    node_id        = Column(String(255), nullable=True)
    parent_id      = Column(String(255), nullable=True)
    path           = Column(Text, nullable=True)
    dn             = Column(Text, nullable=True)
    department     = Column(String(255), nullable=True)
    description    = Column(Text, nullable=True)
    direct_reports = Column(Text, nullable=True)   # raw DN/name list from AD, semicolon-separated
    display_name   = Column(String(255), nullable=False)
    mail           = Column(String(255), nullable=True, index=True)
    manager        = Column(String(255), nullable=True)
    mobile         = Column(String(50), nullable=True)
    title          = Column(String(255), nullable=True)
    company        = Column(String(255), nullable=True)
    synced_at      = Column(DateTime, nullable=False, default=_now)


# ============================================================
# DEPT → PRODUCT MAPPINGS  (RBAC/ABAC Phase: dept-scoped product access)
# ============================================================

class DeptProductMapping(Base):
    """Maps a department to a product for dept-scoped product visibility."""
    __tablename__ = "dept_product_mappings"
    __table_args__ = (
        UniqueConstraint("product_id", "department", name="uq_dpm_product_dept"),
    )

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    product_id = Column(UUID(as_uuid=False), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    department = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)


# ============================================================
# USER LEVEL OVERRIDES  (manual ad_level override — survives nightly org_tree sync)
# ============================================================

class UserLevelOverride(Base):
    """
    Allows senior users (ad_level ≤ 2) to temporarily grant a lower ad_level
    (higher seniority access) to another user.  Applied after nightly sync so
    overrides survive the org_tree TRUNCATE+INSERT.

    Security rules:
      - granter must have ad_level ≤ 2
      - ad_level_override must be >= granter's own level (cannot grant higher seniority than self)
      - expires_at=NULL means permanent until next sync (or manual revoke)
    """
    __tablename__ = "user_level_overrides"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id          = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ad_level_override = Column(Integer, nullable=False)   # effective level to apply
    original_level   = Column(Integer, nullable=True)     # snapshotted AD level at override time
    overridden_by    = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    reason           = Column(Text, nullable=False)
    expires_at       = Column(DateTime, nullable=True)    # NULL = no expiry
    created_at       = Column(DateTime, nullable=False, default=_now)
    revoked_at       = Column(DateTime, nullable=True)
    is_active        = Column(Boolean, nullable=False, default=True)
    # Flat/admin-only mode (HOD_APPROVAL_ENABLED=false) only: an "Elevated"
    # (ad_level=0) grant also flips users.role to 'admin' for the duration of
    # the override, since ad_level alone doesn't unlock role=="admin"-gated
    # endpoints (user management, model governance writes, etc.) in that mode.
    # original_role snapshots the pre-grant role so revoke/expiry can restore
    # it exactly. Always NULL in HOD-hierarchy mode (role is never touched
    # there) and NULL for flat-mode "Standard" (ad_level=6) grants too.
    original_role    = Column(String(50), nullable=True)


# ============================================================
# USER FAVORITES  (Agent Catalog — per-user starred agents)
# ============================================================

class UserFavorite(Base):
    """
    User-specific starred/favorited entities (agents, skills, etc.)
    Persisted to Postgres — never Redis or localStorage.
    entity_type: 'agent' (future: 'skill', 'workflow')
    entity_id: agent name (matches agents_pg.name)
    """
    __tablename__ = "user_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "entity_type", "entity_id", name="uq_user_favorite"),
    )

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id     = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type = Column(String(50),  nullable=False)    # 'agent'
    entity_id   = Column(String(255), nullable=False)    # agent name
    created_at  = Column(DateTime, nullable=False, default=_now)


# ============================================================
# AGENT KB DOCS  (Agent-scoped Knowledge Base attachments)
# ============================================================

class AgentKbDoc(Base):
    """
    Links a KnowledgeDocument to a specific agent.
    Docs are staged at upload time and activated (embedded into pgvector
    with repo='agent_kb:{agent_name}') when the agent is approved.
    """
    __tablename__ = "agent_kb_docs"
    __table_args__ = (
        UniqueConstraint("agent_id", "doc_id", name="uq_agent_kb_doc"),
    )

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    agent_id   = Column(String(255), nullable=False, index=True)   # agents_pg.name
    doc_id     = Column(UUID(as_uuid=False), nullable=False, index=True)  # knowledge_docs.id (no FK — cross-DB)
    created_at = Column(DateTime, nullable=False, default=_now)


# ============================================================
# REQUEST AUDIT LOG  (immutable — PCI/DSS + client traceability)
# ============================================================

class RequestAuditLog(Base):
    """
    One row per /ask (and /ide/chat) request.
    Records who asked what from which client, which model answered,
    latency, token cost, and whether compliance blocked the response.
    Immutable — never updated or deleted.
    """
    __tablename__ = "request_audit_log"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    request_id      = Column(String(64),  nullable=False, index=True)   # gateway request_id
    user_id         = Column(String(255), nullable=False, index=True)
    email           = Column(String(255), nullable=True)
    department      = Column(String(255), nullable=True)
    client_source   = Column(String(50),  nullable=False, index=True)   # platform|cli|ide-vscode|ide-jetbrains|api
    endpoint        = Column(String(255), nullable=False)               # /ask | /ide/chat | etc.
    question_hash   = Column(String(64),  nullable=True)                # SHA-256 — no raw text
    model_used      = Column(String(100), nullable=True)
    tokens_in       = Column(Integer,     nullable=True)
    tokens_out      = Column(Integer,     nullable=True)
    cost_usd        = Column(Float,       nullable=True)
    latency_ms      = Column(Integer,     nullable=True)
    cache_hit       = Column(String(50),  nullable=True)                # redis|semantic|none
    compliance_blocked = Column(Boolean,  nullable=False, default=False)
    error           = Column(Text,        nullable=True)
    created_at      = Column(DateTime,    nullable=False, default=_now, index=True)


# ============================================================
# CLI VERSION REGISTRY  (fleet visibility for pushing updates)
# ============================================================

class CliVersionRecord(Base):
    """
    One row per (user_id, install_id). Tracks which CLI build each engineer
    is running so ops can see who is stale and target update rollouts.

    install_id is a per-install UUID the CLI generates once and persists under
    ~/.ainxt/install-id — stable across sessions on the same box, distinct
    across machines / reinstalls. This makes each row a device fingerprint,
    not just a user aggregate (one engineer on laptop + desktop shows two
    rows). Purely telemetry data — no source paths, prompts, or filenames.

    Populated by POST /ainxt/v1/api/cli/heartbeat, fired once per session
    on REPL boot (and every 6h for long-running sessions) from the CLI.
    """
    __tablename__ = "cli_version_registry"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id         = Column(String(255), nullable=False, index=True)
    email           = Column(String(255), nullable=True,  index=True)
    install_id      = Column(String(64),  nullable=False, index=True)
    version         = Column(String(32),  nullable=False, index=True)   # e.g. 2.0.4-beta
    channel         = Column(String(16),  nullable=False, default="latest")  # latest|stable|dev
    binary_name     = Column(String(64),  nullable=True)                # ainxt-v2
    os              = Column(String(32),  nullable=True)                # linux|darwin|win32
    arch            = Column(String(16),  nullable=True)                # x64|arm64
    os_release      = Column(String(128), nullable=True)                # kernel / build string
    runtime         = Column(String(32),  nullable=True)                # bun|node
    runtime_version = Column(String(32),  nullable=True)                # e.g. 1.1.34
    session_count   = Column(Integer,     nullable=False, default=1)
    first_seen_at   = Column(DateTime,    nullable=False, default=_now)
    last_seen_at    = Column(DateTime,    nullable=False, default=_now, index=True)

    __table_args__ = (
        UniqueConstraint("user_id", "install_id", name="uq_cli_version_user_install"),
        Index("ix_cli_version_last_seen_version", "last_seen_at", "version"),
    )


# ============================================================
# GENERATED DOCUMENTS  (chat document generation feature)
# ============================================================

class GeneratedDocument(Base):
    """
    Audit record for every document generated via chat.
    Binary file lives in core.config.DOC_STORAGE_DIR/{id}.{format} on a
    persistent volume (NOT /tmp) so refresh-then-download keeps working
    across container restarts.
    content_md is stored permanently for audit trail.
    """
    __tablename__ = "generated_documents"

    id         = Column(String(36),  primary_key=True)       # UUID string = file_id
    job_id     = Column(String(36),  nullable=False, index=True)  # rq job id
    user_id    = Column(String(255), nullable=False, index=True)
    chat_id    = Column(String(36),  nullable=True,  index=True)
    format     = Column(String(10),  nullable=False)          # docx|pptx|pdf|xlsx|txt|md
    title      = Column(String(500), nullable=False)
    filename   = Column(String(512), nullable=False)
    file_path  = Column(Text,        nullable=False)          # /tmp/ainxt_docs/...
    content_md = Column(Text,        nullable=True)           # permanent audit trail
    # Iterative editing (Canvas/Pages parity): builds that share an artifact_id are
    # successive VERSIONS of one logical document. version starts at 1 and rises on
    # each revision. artifact_id defaults to the file_id (id) for one-shot docs.
    artifact_id = Column(String(36), nullable=True, index=True)
    version     = Column(Integer,    nullable=False, default=1)
    created_at = Column(DateTime,    nullable=False, default=_now)


# ============================================================
# GENERATED IMAGES  (chat image generation feature)
# ============================================================

class GeneratedImage(Base):
    """
    Audit record for every image generated via /chat/image-generate.
    Binary file lives in core.config.IMAGE_STORAGE_DIR/{user}/{chat}/{id}.png
    on a persistent volume. Purged by workers/image_purge.py after
    IMAGE_RETAIN_DAYS (default 2).
    """
    __tablename__ = "generated_images"

    id         = Column(String(36),  primary_key=True)       # UUID string = file_id
    user_id    = Column(String(255), nullable=False, index=True)
    chat_id    = Column(String(36),  nullable=True,  index=True)
    provider   = Column(String(20),  nullable=False)          # gemini|openai
    prompt     = Column(Text,        nullable=True)
    filename   = Column(String(512), nullable=False)
    file_path  = Column(Text,        nullable=False)
    mime_type  = Column(String(50),  nullable=False, default="image/png")
    size_bytes = Column(Integer,     nullable=True)
    created_at = Column(DateTime,    nullable=False, default=_now)


# ============================================================
# USER API KEYS  (IDE integrations — Kilo Code, Cursor, etc.)
# ============================================================

# ============================================================
# DEPARTMENT → HOD MAPPING
# Manually created by DBA in the `ainxt` schema. The application
# only READS from this table; it never creates, alters, or writes
# to it. Do NOT include in any Base.metadata.create_all() path
# or Alembic autogenerate run.
# ============================================================

class DepartmentHodMapping(Base):
    """
    Read-only ORM view of `ainxt.department_hod_mapping`.

    Manually created; do not auto-generate.

    Column names are quoted in raw SQL because they use snake_case
    that Postgres would otherwise fold to lowercase.

    Cardinality:
      - One HOD MAY own multiple departments (multiple rows / same hod_email).
      - Each department has exactly ONE HOD.

    Matching: case-sensitive exact match on `department_name` against
    `users.department`. Email match is case-insensitive.

    Only `department_name` and `hod_email` participate in business logic;
    `corrected_department_name` and `hod_name` are informational only.
    """
    __tablename__   = "department_hod_mapping"
    __table_args__  = (
        # Prevent Alembic / metadata.create_all from emitting DDL for this
        # table. The table is owned/maintained by the DBA.
        {"schema": DB_SCHEMA, "info": {"skip_autogenerate": True}},
    )

    # Composite PK: there is no real PK declared on the manual table;
    # SQLAlchemy requires at least one primary_key column per mapped class.
    # We pick (department_name, hod_email) which is unique by the cardinality
    # rule above. This declaration affects the ORM only — it does NOT alter
    # the underlying table.
    department_name           = Column("department_name",          String(255), primary_key=True, nullable=False)
    corrected_department_name = Column("corrected_department_name", String(255), nullable=True)
    hod_name                  = Column("hod_name",                 String(255), nullable=True)
    hod_email                 = Column("hod_email",                String(255), primary_key=True, nullable=False)


# ============================================================
# HOD ALLOCATION CAP & LEDGER
# Manually created by DBA in the `ainxt` schema. The application
# reads `hod_allocation_caps` and APPENDS to `hod_allocation_ledger`;
# it never alters either table. Do NOT include in any
# Base.metadata.create_all() path or Alembic autogenerate run.
# ============================================================

class HodAllocationCap(Base):
    """
    Read-only ORM view of ainxt.hod_allocation_caps.

    Manually created; do not auto-generate.

    One row per HOD. Cap applies across all departments the HOD owns.
    Match is case-insensitive on hod_email (mirrors DepartmentHodMapping).
    """
    __tablename__  = "hod_allocation_caps"
    __table_args__ = (
        {"schema": DB_SCHEMA, "info": {"skip_autogenerate": True}},
    )

    hod_email       = Column("hod_email",       String(255),    primary_key=True, nullable=False)
    monthly_cap_usd = Column("monthly_cap_usd", Numeric(12, 2), nullable=False)
    is_active       = Column("is_active",       Boolean,        nullable=False, default=True)
    notes           = Column("notes",           Text,           nullable=True)
    created_at      = Column("created_at",      DateTime(timezone=True), nullable=False, default=_now)
    updated_at      = Column("updated_at",      DateTime(timezone=True), nullable=False, default=_now)
    updated_by      = Column("updated_by",      String(255),    nullable=True)


class HodAllocationLedger(Base):
    """
    Audit + spend-tracking ledger for HOD allocations, AND (as of 2026-07-23)
    the durable store for budget-increase requests end-to-end.

    Historically append-only (app only INSERTed, never UPDATE/DELETE) for
    'allocate'/'approve_request' actions that were always already-resolved.

    Now `action='approve_request'` rows may also be inserted
    with status='pending' at request-submission time (one row per HOD when a
    department maps to multiple HODs, all sharing one `request_id`), and are
    later UPDATEd in place to 'approved' / 'rejected' / 'superseded' by
    routers/budget_router.py + store/budget_store.py — see those modules for
    the row-locking (SELECT ... FOR UPDATE on all rows sharing request_id)
    that guarantees only one HOD's approval is ever applied.

    `consumed_after_usd` is the running total AFTER this row was applied,
    materialised so `get_period_consumption` is O(1) lookup via the
    (hod_email, period_yyyymm) index. Only meaningful once status != 'pending'.

    The entire table is truncated by the monthly budget reset job
    (services/budget_audit_service.py) — see BudgetPeriodAudit for the
    durable historical snapshot that survives the wipe.
    """
    __tablename__  = "hod_allocation_ledger"
    __table_args__ = (
        {"schema": DB_SCHEMA, "info": {"skip_autogenerate": True}},
    )

    id                 = Column("id",                 UUID(as_uuid=False), primary_key=True, default=_uuid)
    hod_email          = Column("hod_email",          String(255),    nullable=False)
    period_yyyymm      = Column("period_yyyymm",      String(7),      nullable=False)   # 'YYYY-MM'
    target_user_id     = Column("target_user_id",     UUID(as_uuid=False), nullable=False)
    target_user_email  = Column("target_user_email",  String(255),    nullable=True)
    action             = Column("action",             String(32),     nullable=False)
    amount_usd         = Column("amount_usd",         Numeric(12, 2), nullable=True)
    previous_limit_usd = Column("previous_limit_usd", Numeric(12, 2), nullable=True)
    new_limit_usd      = Column("new_limit_usd",      Numeric(12, 2), nullable=True)
    request_id         = Column("request_id",         String(255),    nullable=True)
    cap_at_time_usd    = Column("cap_at_time_usd",    Numeric(12, 2), nullable=True)
    consumed_after_usd = Column("consumed_after_usd", Numeric(12, 2), nullable=True)
    shadow_mode        = Column("shadow_mode",        Boolean,        nullable=False, default=False)
    created_at         = Column("created_at",         DateTime(timezone=True), nullable=False, default=_now)
    justification      = Column("justification",      Text,           nullable=True)

    # ── Request-lifecycle columns (2026-07-23) ──────────────────────────────
    status                   = Column("status",                   String(20),     nullable=False, default="approved")
    requested_extra_cost_usd = Column("requested_extra_cost_usd", Numeric(12, 6), nullable=True)
    requester_email          = Column("requester_email",          String(255),    nullable=True)
    requester_name           = Column("requester_name",           String(255),    nullable=True)
    requester_department     = Column("requester_department",     String(255),    nullable=True)
    current_base_cost_usd    = Column("current_base_cost_usd",    Numeric(12, 6), nullable=True)
    current_extra_cost_usd   = Column("current_extra_cost_usd",   Numeric(12, 6), nullable=True)
    resolved_at              = Column("resolved_at",              DateTime(timezone=True), nullable=True)
    # Actual actor who resolved this row — usually == hod_email, except when
    # an admin approves/rejects on a HOD's behalf (admin override).
    approved_by              = Column("approved_by",              String(255),    nullable=True)
    approved_by_name         = Column("approved_by_name",         String(255),    nullable=True)

    # ── Managed-endpoint cloud spend ────────────────────────────
    # Running ACTUAL consumption by managed endpoints for this HOD/period.
    # Deliberately separate from amount_usd / consumed_after_usd, which track
    # APPROVED ALLOCATIONS at NUMERIC(12,2) — a sub-cent cloud request ($0.004)
    # would round to $0.00 there and be lost. NUMERIC(12,6) matches the precision
    # of sdlc_runs.total_cost_usd.
    #
    # Only meaningful on rows with action='endpoint_spend': exactly ONE carrier
    # row per (hod_email, period_yyyymm), created on the first cloud request and
    # incremented atomically via INSERT ... ON CONFLICT DO UPDATE against the
    # partial unique index uq_hal_endpoint_spend_period.
    # Written by services/endpoint_budget_governor.record_endpoint_spend().
    endpoint_spend_usd       = Column("endpoint_spend_usd",       Numeric(12, 6), nullable=False, default=0)


# ============================================================
# ENDPOINT → HOD MAPPING  (managed-endpoint cloud budget owner)
# App-owned 
# — unlike the DBA-owned hod_* tables above, so NO skip_autogenerate.
# ============================================================

class EndpointHodMapping(Base):
    """
    Maps a managed endpoint to the HOD whose monthly cap funds its cloud-model
    spend.

    Exactly ONE funding HOD per endpoint (uq_ehm_endpoint on endpoint_id).
    This deliberately resolves the multi-HOD ambiguity that exists in
    ainxt.department_hod_mapping (which has no PK/UNIQUE and where one
    department may map to several HODs): a budget must have a single owner.

    Required before an endpoint may serve cloud models — enforced at write time
    (422 in endpoint_mgmt_router) and at request time (503 in
    endpoint_proxy_router). Local-only endpoints need no row.

    hod_email is matched case-insensitively at read time (idx_ehm_hod_email_lc),
    consistent with services/hod_budget_governor.py.
    """
    __tablename__  = "endpoint_hod_mapping"
    __table_args__ = ({"schema": "ainxt"},)

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    endpoint_id = Column(UUID(as_uuid=False),
                         ForeignKey("managed_endpoints.id", ondelete="CASCADE"),
                         nullable=False, unique=True, index=True)
    hod_email   = Column(String(255), nullable=False)
    is_active   = Column(Boolean,     nullable=False, default=True)
    created_by  = Column(String(255), nullable=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at  = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class UserAPIKey(Base):
    """
    Per-user API keys for IDE integrations.
    Raw key format: {username_slug}-{uuid4}   e.g. kannan-f47ac10b-58a2-...
    Only the SHA-256 hex digest is stored; plaintext is returned ONCE at
    generation time and never stored.
    """
    __tablename__ = "user_api_keys"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id      = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    key_prefix   = Column(String(100), nullable=False)          # displayable hint, e.g. "kannan-f47ac10b"
    key_hash     = Column(String(64),  nullable=False, unique=True)  # SHA-256 hex — never the raw key
    label        = Column(String(255), nullable=True)            # optional user-given label
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    is_active    = Column(Boolean,     nullable=False, default=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, default=_now_utc)
    expires_at   = Column(DateTime(timezone=True), nullable=True)   # NULL = no expiry; set on create
    last_expiry_notified_at = Column(DateTime, nullable=True)
    revoked_at   = Column(DateTime(timezone=True), nullable=True)

# ============================================================
# EMAIL BROADCAST  (Admin-only HTML email broadcast feature)
# ============================================================

class EmailBroadcast(Base):
    """
    One row per broadcast send. Drives the admin Email Broadcast feature.
    status: draft | queued | sending | completed | failed | cancelled
    """
    __tablename__ = "email_broadcasts"

    id                 = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    created_by         = Column(UUID(as_uuid=False),
                                ForeignKey("users.id", ondelete="SET NULL"),
                                nullable=True, index=True)
    subject            = Column(String(998), nullable=False)
    html_body          = Column(Text, nullable=False)
    text_body          = Column(Text, nullable=True)
    enrich_name        = Column(Boolean, nullable=False, default=False)
    targeting_json     = Column(JSONB, nullable=False, default=dict)   # full targeting payload
    status             = Column(String(20), nullable=False, default="draft", index=True)
    total_count        = Column(Integer, nullable=False, default=0)
    success_count      = Column(Integer, nullable=False, default=0)
    failure_count      = Column(Integer, nullable=False, default=0)
    compliance_blocked = Column(Boolean, nullable=False, default=False)
    model_used         = Column(String(100), nullable=True)
    created_at         = Column(DateTime, nullable=False, default=_now, index=True)
    updated_at         = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class EmailBroadcastAttachment(Base):
    """
    Uploaded attachment for a broadcast. Files are stored on disk under
    BROADCAST_ATTACHMENT_DIR; only metadata + path live in this table.
    broadcast_id is nullable until the attachment is referenced by /broadcast/send.
    """
    __tablename__ = "email_broadcast_attachments"
    __table_args__ = (
        Index("ix_email_bcast_att_owner_created", "uploaded_by", "created_at"),
    )

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    broadcast_id = Column(UUID(as_uuid=False),
                          ForeignKey("email_broadcasts.id", ondelete="CASCADE"),
                          nullable=True, index=True)
    uploaded_by  = Column(UUID(as_uuid=False),
                          ForeignKey("users.id", ondelete="SET NULL"),
                          nullable=True)
    filename     = Column(String(512), nullable=False)
    mimetype     = Column(String(120), nullable=False, default="application/octet-stream")
    size_bytes   = Column(Integer, nullable=False, default=0)
    storage_path = Column(Text, nullable=False)
    created_at   = Column(DateTime, nullable=False, default=_now)


class EmailBroadcastRecipient(Base):
    """
    Per-recipient row, created up-front when the broadcast is enqueued.
    Enables idempotent retries, accurate counts, and a detail view.
    status: pending | sent | failed | skipped
    """
    __tablename__ = "email_broadcast_recipients"
    __table_args__ = (
        Index("ix_email_bcast_rcpt_bcast_status", "broadcast_id", "status"),
    )

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    broadcast_id = Column(UUID(as_uuid=False),
                          ForeignKey("email_broadcasts.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    user_id      = Column(UUID(as_uuid=False),
                          ForeignKey("users.id", ondelete="SET NULL"),
                          nullable=True)
    email        = Column(String(320), nullable=False)
    name         = Column(String(255), nullable=True)
    status       = Column(String(20), nullable=False, default="pending", index=True)
    error_text   = Column(Text, nullable=True)
    sent_at      = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, nullable=False, default=_now)


class EmailBroadcastAuditLog(Base):
    """
    Append-only audit trail of every broadcast action.
    action: created | queued | sent_one | completed | failed | cancelled | compliance_blocked
    """
    __tablename__ = "email_broadcast_audit_log"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    broadcast_id   = Column(UUID(as_uuid=False),
                            ForeignKey("email_broadcasts.id", ondelete="CASCADE"),
                            nullable=True, index=True)
    actor_user_id  = Column(UUID(as_uuid=False),
                            ForeignKey("users.id", ondelete="SET NULL"),
                            nullable=True)
    actor_email    = Column(String(320), nullable=True)
    action         = Column(String(50), nullable=False, index=True)
    detail_json    = Column(JSONB, nullable=False, default=dict)
    created_at     = Column(DateTime, nullable=False, default=_now, index=True)


# ============================================================
# MANAGED ENDPOINTS
# Named OpenAI-compatible proxy endpoints.
# Callers authenticate with a platform-generated API key stored in
# user_api_keys (same table as CLI keys). The LiteLLM backend key
# is selected based on the use_env_key toggle:
#   use_env_key=True  → os.getenv(env_key_name) forwarded to LiteLLM
#   use_env_key=False → global LOCAL_LLM_API_KEY forwarded to LiteLLM
# ============================================================

class ManagedEndpoint(Base):
    """
    A named proxy endpoint that exposes an OpenAI-compatible API surface.

    Auth (caller → platform):
      Callers pass a platform-generated API key as Authorization: Bearer <key>.
      The key is stored in user_api_keys (SHA-256 hash only) and referenced
      here via api_key_id.

    LiteLLM backend key (platform → LiteLLM):
      Controlled by use_env_key:
        True  → os.getenv(env_key_name) — team-specific LiteLLM virtual key
        False → global LOCAL_LLM_API_KEY from env

    Allowed models: stored in model_ids (JSONB list). When set, callers may only
    use models in that list. When null, LiteLLM's per-key model restrictions apply.
    The list may contain BOTH local (LiteLLM) and cloud (GPT/Claude/Gemini) model
    IDs — cloud entries may only be added by an admin via endpoint_mgmt_router.

    Cloud budget: when model_ids contains any cloud model, the endpoint MUST have
    a row in ainxt.endpoint_hod_mapping naming the HOD whose monthly cap funds
    that spend. Enforced at write time (422) and at request time (503).
    Cloud enablement is derived from model_ids ∩ cloud catalog — there is no
    separate boolean flag to drift out of sync.

    Proxy URL: POST /ainxt/v1/api/{slug}/v1/chat/completions
    Models URL: GET  /ainxt/v1/api/{slug}/v1/models
    """
    __tablename__ = "managed_endpoints"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name         = Column(String(255), nullable=False)
    slug         = Column(String(100), nullable=False, unique=True, index=True)
    org_id       = Column(String(255), nullable=False, default="default", index=True)
    description  = Column(Text, nullable=True)
    # FK to user_api_keys — the platform-generated key callers use for auth
    api_key_id   = Column(UUID(as_uuid=False),
                          ForeignKey("user_api_keys.id", ondelete="SET NULL"),
                          nullable=True, index=True)
    # LiteLLM backend key selection
    use_env_key  = Column(Boolean, nullable=False, default=False)
    env_key_name = Column(String(255), nullable=True)    # e.g. "TEAM_LITELLM_API_KEY" — used when use_env_key=True
    model_ids    = Column(JSONB, nullable=True)           # allowed model IDs (local AND cloud); NULL = no restriction
    # Model served when the caller requests an unrecognised model name. Must be a
    # member of model_ids. NULL = use the first LOCAL model in model_ids, so an
    # unknown model never silently escalates to paid cloud inference.
    fallback_model = Column(String(255), nullable=True)
    enabled      = Column(Boolean, nullable=False, default=True)
    tool_calls_enabled = Column(Boolean, nullable=False, default=True)
    # System user that owns this endpoint's usage in billing/audit
    system_user_id = Column(UUID(as_uuid=False),
                            ForeignKey("users.id", ondelete="SET NULL"),
                            nullable=True, index=True)
    created_by   = Column(String(255), nullable=True)
    created_at   = Column(DateTime, nullable=False, default=_now)
    updated_at   = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# WORKSPACE MESSAGES  (Option B — server-side project chat history)
# Partitioned table: PARTITION BY HASH(project_id), 128 partitions.
# DDL is managed entirely by migrate.py Part S23 — NOT via create_all().
# This ORM model is used only for query/insert operations in the store.
# ============================================================

class WorkspaceMessage(Base):
    """
    Server-side persistence for workspace (project) chat messages.
    Replaces localStorage as the source of truth.

    Partition key: project_id — all messages for a project land in the
    same partition, enabling efficient range scans by (project_id, created_at).

    Composite PK (id, project_id) is required by Postgres for partitioned tables
    where the partition key must be part of the primary key.

    User isolation is enforced at query layer (WHERE user_id = :uid) — not via FK.
    """
    __tablename__ = "workspace_messages"
    # Exclude from create_all() — partitioned table DDL lives in migrate.py Part S23.
    __table_args__ = {"extend_existing": True}

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    project_id = Column(String(255), primary_key=True, nullable=False)   # partition key
    user_id    = Column(String(255), nullable=False, index=True)
    role       = Column(String(50),  nullable=False)   # user | assistant
    content    = Column(Text,        nullable=False)
    # Optional metadata mirroring the frontend message shape
    model_label = Column(String(100), nullable=True)
    cost_usd    = Column(Float,       nullable=True)
    latency     = Column(Float,       nullable=True)
    in_tok      = Column(Integer,     nullable=True)
    out_tok     = Column(Integer,     nullable=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, default=_now)


# ============================================================
# CKMS (Centralized Key Management Service)
#
# Two tables backing core.ckms. Row provisioning is owned by ops tooling
# (requirement §"Out of Scope"); this code only READS them at boot.
# DDL lives in db/sql/prod_catchup_2026_06_08_ckms_keys.sql.
# ============================================================

class KeyRecord(Base):
    """One row per (key_type) — active or historical.

    ``dek`` holds the DEK encrypted under the KEK (HSM-wrapped),
    OR ``BASE:<base64-of-clear-dek>`` for dev / phased-rollout rows that
    skip the HSM. ``kek`` holds KEK_LMK hex and is ignored for ``BASE:`` rows.

    Only ``status='A'`` rows are loaded at boot. The unique partial index
    ``ux_keys_table_active_key_name`` enforces exactly-one-active per
    ``key_name``.
    """

    __tablename__ = "keys_table"

    key_name   = Column(String(64),  primary_key=True)
    dek        = Column(String(256), nullable=False)
    kek        = Column(String(256), nullable=False, default="")
    status     = Column(String(1),   nullable=False, default="A")  # 'A' | 'I'
    created_at = Column(DateTime,    nullable=False, default=_now)
    updated_at = Column(DateTime,    nullable=False, default=_now, onupdate=_now)


class KeyTypeMapping(Base):
    """Maps each protected env var to the ``key_type`` row that decrypts it.

    Env vars absent from this table fall back to ``KEY_CREDS`` at the
    application layer (requirement §"Default rule").
    """

    __tablename__ = "key_type_mapping"

    env_var    = Column(String(128), primary_key=True)
    key_type   = Column(String(64),  nullable=False, index=True)
    created_at = Column(DateTime,    nullable=False, default=_now)
    updated_at = Column(DateTime,    nullable=False, default=_now, onupdate=_now)


# ============================================================
# ENTERPRISE LLM SPEND TRACKING
#
# Populated nightly by services/llm_spend/orchestrator.py from the
# three provider billing APIs (OpenAI Costs, Anthropic Admin, GCP
# Billing BigQuery export for Vertex Gemini). Reports + exec emails
# read from llm_spend_daily. Digest jobs verify freshness via
# llm_spend_fetch_runs before sending.
#
# Schema migration: db/sql/prod_catchup_2026_06_17_llm_spend.sql
# ============================================================

class LLMSpendDaily(Base):
    """One row per (usage_date, provider, model, source).

    `source` distinguishes future fanout (e.g. Vertex vs AI Studio for
    Gemini) so the UPSERT key stays stable; report queries SUM across
    sources naturally via GROUP BY (provider, model).
    """

    __tablename__ = "llm_spend_daily"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    usage_date    = Column(DateTime,   nullable=False, index=True)   # PG DATE; SQLAlchemy DateTime is fine
    provider      = Column(String(20), nullable=False)               # 'openai' | 'anthropic' | 'gemini'
    model         = Column(String(120), nullable=False)              # canonical model id or 'other'
    cost_usd      = Column(Numeric(14, 6), nullable=False, default=0)
    input_tokens  = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    request_count = Column(BigInteger, nullable=False, default=0)
    source        = Column(String(30), nullable=False)               # 'openai_costs_api'|'anthropic_admin'|'gcp_bq_export'
    fetched_at    = Column(DateTime,   nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "usage_date", "provider", "model", "source",
            name="uq_llm_spend_daily",
        ),
        Index("idx_llm_spend_daily_provider_model", "provider", "model", "usage_date"),
    )


class LLMSpendFetchRun(Base):
    """Audit log of every nightly / on-demand fetch attempt.

    The digest jobs (daily/weekly/monthly/quarterly) check this table for
    at least one ``status='ok'`` row per (provider, day-in-range) before
    sending an exec email. Missing coverage triggers the on-call alert
    instead of an inaccurate digest.
    """

    __tablename__ = "llm_spend_fetch_runs"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    run_started   = Column(DateTime,   nullable=False, default=_now)
    run_finished  = Column(DateTime,   nullable=True)
    provider      = Column(String(20), nullable=False)
    window_start  = Column(DateTime,   nullable=False)               # PG DATE
    window_end    = Column(DateTime,   nullable=False)               # PG DATE
    status        = Column(String(20), nullable=False)               # 'ok'|'partial'|'failed'
    rows_upserted = Column(Integer,    nullable=False, default=0)
    error_text    = Column(Text,       nullable=True)

    __table_args__ = (
        Index("idx_llm_spend_fetch_runs_provider_window", "provider", "window_end"),
        Index("idx_llm_spend_fetch_runs_status",          "status",   "window_end"),
    )

class LLMSpendAlertSent(Base):
    """Dedup guard for URGENT alert mails sent by services/llm_spend/alerts.py.

    The alert helpers (alert_missing_fetch, alert_failed_fetch,
    alert_partial_fetch) write a row
    here via INSERT ... ON CONFLICT DO NOTHING RETURNING id BEFORE calling
    SMTP. Exactly one of the racing workers / replicas gets a non-empty
    RETURNING and emails; all others see the conflict and no-op. This is
    what prevents the admin inbox from receiving 10+ identical mails for a
    single outage when multiple uvicorn workers or pods all fire their own
    APScheduler cron job at the same minute.

    A genuinely new outage on a different period produces a different
    (window_start, window_end) — and usually a different dedup_key — so
    legitimate future alerts still go through.

    Schema migration: db/sql/prod_catchup_2026_06_19_llm_spend_alerts_sent.sql
    """

    __tablename__ = "llm_spend_alerts_sent"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    kind          = Column(String(32),  nullable=False)   # 'missing_fetch' | 'failed_fetch' | 'partial_fetch' | 'digest_sent' | 'fetch_run'
    cadence       = Column(String(16),  nullable=False)   # 'daily'|'weekly'|'monthly'|'quarterly'|'nightly'
    window_start  = Column(DateTime,    nullable=False)   # PG DATE
    window_end    = Column(DateTime,    nullable=False)   # PG DATE
    # csv of missing ISO dates (missing_fetch) or sorted failing providers
    # (failed_fetch / partial_fetch) or the period_label (digest_sent) or the
    # window string (fetch_run — the multi-worker run-once guards).
    # Truncated to 255 chars by alerts.py.
    dedup_key     = Column(String(255), nullable=False)
    recipients    = Column(Integer,     nullable=False, default=0)
    smtp_ok       = Column(Boolean,     nullable=False, default=False)
    sent_at       = Column(DateTime,    nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "kind", "cadence", "window_start", "window_end", "dedup_key",
            name="uq_llm_spend_alerts_sent",
        ),
        Index("ix_llm_spend_alerts_sent_window", "window_end", "kind"),
    )





# ============================================================
# AiNxt COACH  (self-contained feature — coach_* tables)
# Independent: gated by ENABLE_COACH; nothing else references these.
# Observes every LLM interaction, scores AI-usage practice, surfaces a
# user dashboard + admin console + weekly digest. Redact-at-write: the
# coach_event row never holds raw prompt/completion text — only a hash +
# the redacted (and at-rest encrypted) prompt. See AINXT_COACH_REQUIREMENTS.md.
# ============================================================

class CoachEvent(Base):
    """One normalised per-interaction practice event. The firehose table.

    PK is composite (event_id, ts) so the table can be RANGE-partitioned by
    ts (monthly). Stores ONLY prompt_hash + prompt_redacted (encrypted at rest);
    raw prompt/completion text is never persisted."""
    __tablename__ = "coach_event"

    event_id        = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    ts              = Column(DateTime, nullable=False, default=_now, primary_key=True, index=True)
    user_id         = Column(String(255), nullable=False, index=True)
    channel         = Column(String(32),  nullable=False, index=True)  # web|cli|api|teams|mcp|voice|mobile|embed|workflow|agent
    workspace       = Column(String(255), nullable=True)
    project         = Column(String(255), nullable=True)
    thread_id       = Column(String(255), nullable=True, index=True)
    request_id      = Column(String(255), nullable=True, index=True)   # join key to ainxt.metrics
    model           = Column(String(128), nullable=True)
    prompt_hash     = Column(String(64),  nullable=True)
    prompt_redacted = Column(Text,        nullable=True)               # redactor output, encrypted at rest
    completion_hash = Column(String(64),  nullable=True)
    tokens_in       = Column(Integer,     nullable=False, default=0)
    tokens_out      = Column(Integer,     nullable=False, default=0)
    cost_usd        = Column(Float,       nullable=False, default=0.0)
    context_window_pct = Column(Float,    nullable=False, default=0.0)
    tool_calls      = Column(JSONB,       nullable=False, default=list)
    accepted        = Column(Boolean,     nullable=True)
    governance_flags = Column(JSONB,      nullable=False, default=list)
    compliance_flags = Column(JSONB,      nullable=False, default=list)
    pii_flags       = Column(JSONB,       nullable=False, default=list)
    secret_flags    = Column(JSONB,       nullable=False, default=list)
    latency_ms      = Column(Integer,     nullable=False, default=0)
    rule_hits       = Column(JSONB,       nullable=False, default=list)
    department      = Column(String(255), nullable=True, index=True)
    # ── EvalEngine (LLM-as-judge) results — populated asynchronously after
    #    the deterministic rule evaluator runs. Nullable: NULL means the eval
    #    has not completed yet (or EVAL_ENABLED=false). Never blocks ingestion.
    eval_score      = Column(Float,       nullable=True)   # 0.0–1.0 from judge
    eval_verdict    = Column(String(16),  nullable=True)   # "ACCEPT" | "REJECT"
    eval_issues     = Column(JSONB,       nullable=True)   # list[str] of specific issues

    __table_args__ = (
        Index("idx_coach_event_user_ts", "user_id", "ts"),
        Index("idx_coach_event_dept_ts", "department", "ts"),
    )


class CoachRuleHit(Base):
    """One row per rule that fired on an event. evidence holds the field values
    that triggered the rule (§14.5 explainability — every score is drill-down)."""
    __tablename__ = "coach_rule_hit"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    event_id    = Column(UUID(as_uuid=False), nullable=False, index=True)
    user_id     = Column(String(255), nullable=False, index=True)
    rule_id     = Column(String(64),  nullable=False, index=True)
    category    = Column(String(64),  nullable=False)   # prompt-quality|session-hygiene|review-discipline|tool-mastery|context-management|security
    severity    = Column(String(16),  nullable=False, default="low")  # low|medium|high|critical
    channel     = Column(String(32),  nullable=False)
    department  = Column(String(255), nullable=True, index=True)
    detail      = Column(JSONB,       nullable=False, default=dict)
    evidence    = Column(JSONB,       nullable=False, default=dict)
    muted       = Column(Boolean,     nullable=False, default=False)
    created_at  = Column(DateTime,    nullable=False, default=_now, index=True)


class CoachScoreSnapshot(Base):
    """Periodic per-user practice score (overall + per-category)."""
    __tablename__ = "coach_score_snapshot"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id         = Column(String(255), nullable=False, index=True)
    snapshot_date   = Column(DateTime,    nullable=False, default=_now, index=True)
    score_overall   = Column(Float,       nullable=False, default=0.0)
    score_prompt    = Column(Float,       nullable=False, default=0.0)
    score_session   = Column(Float,       nullable=False, default=0.0)
    score_review    = Column(Float,       nullable=False, default=0.0)
    score_tool      = Column(Float,       nullable=False, default=0.0)
    score_context   = Column(Float,       nullable=False, default=0.0)
    score_security  = Column(Float,       nullable=False, default=0.0)
    event_count     = Column(Integer,     nullable=False, default=0)
    hit_count       = Column(Integer,     nullable=False, default=0)
    department      = Column(String(255), nullable=True, index=True)
    created_at      = Column(DateTime,    nullable=False, default=_now)


class CoachRulePack(Base):
    """A versioned, publishable bundle of rules. mandatory rules cannot be muted."""
    __tablename__ = "coach_rule_pack"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    pack_id     = Column(String(128), nullable=False, index=True)
    version     = Column(String(32),  nullable=False, default="1.0.0")
    name        = Column(String(255), nullable=False)
    description = Column(Text,        nullable=True, default="")
    rules       = Column(JSONB,       nullable=False, default=list)
    mandatory   = Column(Boolean,     nullable=False, default=False)
    published   = Column(Boolean,     nullable=False, default=True)
    created_by  = Column(String(255), nullable=True)
    created_at  = Column(DateTime,    nullable=False, default=_now)
    updated_at  = Column(DateTime,    nullable=False, default=_now, onupdate=_now)


class CoachRuleMute(Base):
    """A per-user mute of a single rule. muted_until NULL = indefinite."""
    __tablename__ = "coach_rule_mute"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id     = Column(String(255), nullable=False, index=True)
    rule_id     = Column(String(64),  nullable=False, index=True)
    muted_until = Column(DateTime,    nullable=True)
    reason      = Column(Text,        nullable=True, default="")
    created_at  = Column(DateTime,    nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("user_id", "rule_id", name="uq_coach_mute_user_rule"),
    )


class CoachRuleDisabled(Base):
    """An admin disable of a rule, org-wide (department NULL) or per-department."""
    __tablename__ = "coach_rule_disabled"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    rule_id     = Column(String(64),  nullable=False, index=True)
    department  = Column(String(255), nullable=True, index=True)   # NULL = org-wide
    reason      = Column(Text,        nullable=True, default="")
    disabled_by = Column(String(255), nullable=True)
    created_at  = Column(DateTime,    nullable=False, default=_now)


class CoachAdminAudit(Base):
    """Immutable audit of every admin action against Coach data/rules."""
    __tablename__ = "coach_admin_audit"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    actor_id    = Column(String(255), nullable=False, index=True)
    actor_email = Column(String(255), nullable=True)
    action      = Column(String(64),  nullable=False, index=True)  # reset_score|purge_events|disable_rule|enable_rule|manual_coach|...
    target_user = Column(String(255), nullable=True, index=True)
    rule_id     = Column(String(64),  nullable=True)
    details     = Column(JSONB,       nullable=False, default=dict)
    reason      = Column(Text,        nullable=True, default="")
    created_at  = Column(DateTime,    nullable=False, default=_now, index=True)


class CoachManualNote(Base):
    """An admin → user coaching message (nudge / digest-now / one-on-one)."""
    __tablename__ = "coach_manual_note"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id     = Column(String(255), nullable=False, index=True)
    actor_id    = Column(String(255), nullable=False)
    actor_email = Column(String(255), nullable=True)
    kind        = Column(String(32),  nullable=False, default="nudge")  # nudge|digest_now|one_on_one
    subject     = Column(String(255), nullable=True)
    body        = Column(Text,        nullable=True, default="")
    delivered   = Column(Boolean,     nullable=False, default=False)
    created_at  = Column(DateTime,    nullable=False, default=_now, index=True)


class CoachWeeklyMailOptOut(Base):
    """A per-user opt-out of the weekly Coach digest mail."""
    __tablename__ = "coach_weekly_mail_opt_out"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id      = Column(String(255), nullable=False, unique=True, index=True)
    opted_out_by = Column(String(255), nullable=True)
    reason       = Column(Text,        nullable=True, default="")
    created_at   = Column(DateTime,    nullable=False, default=_now, index=True)


# ============================================================
# P10 — PROMPT VERSIONING + A/B TESTING
# ============================================================

class PromptVersion(Base):
    """
    Versioned prompt registry with A/B testing support.

    prompt_key: logical name (e.g. "react_system_prompt")
    version:    integer version number (auto-incremented per key)
    content:    the full prompt text
    is_active:  True = this version is served (only one active per key)
    is_control: True = this is the control version for A/B tests
    traffic_pct: % of traffic routed to this version (100.0 = all traffic)
    eval_score: average eval score from EvalResult (updated by feedback loop)
    author:     who created this version
    """
    __tablename__ = "prompt_versions"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    prompt_key  = Column(String(255), nullable=False, index=True)
    version     = Column(Integer,     nullable=False, default=1)
    content     = Column(Text,        nullable=False)
    is_active   = Column(Boolean,     nullable=False, default=False)
    is_control  = Column(Boolean,     nullable=False, default=False)
    traffic_pct = Column(Float,       nullable=False, default=100.0)
    eval_score  = Column(Float,       nullable=True)
    author      = Column(String(255), nullable=False, default="system")
    created_at  = Column(DateTime,    nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("prompt_key", "version", name="uq_prompt_key_version"),
    )


# ============================================================
# P11 — SCHEDULED WORKFLOWS
# ============================================================

class ScheduledWorkflow(Base):
    """
    Scheduled or event-driven workflow definitions.

    cron_expr:     standard cron expression (e.g. "0 9 * * 1" = Mon 9am)
    event_trigger: event name that triggers this workflow (e.g. "pr_merged")
    workflow_def:  JSONB workflow definition (steps, name, etc.)
    is_active:     False = paused
    last_run_at:   timestamp of last execution
    next_run_at:   computed next execution time (updated by scheduler)
    created_by:    user_id of creator
    """
    __tablename__ = "scheduled_workflows"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name          = Column(String(255), nullable=False, index=True)
    workflow_def  = Column(JSONB,       nullable=False, default=dict)
    cron_expr     = Column(String(100), nullable=True)
    event_trigger = Column(String(255), nullable=True, index=True)
    is_active     = Column(Boolean,     nullable=False, default=True)
    last_run_at   = Column(DateTime,    nullable=True)
    next_run_at   = Column(DateTime,    nullable=True, index=True)
    created_by    = Column(String(255), nullable=True)
    created_at    = Column(DateTime,    nullable=False, default=_now)


class DiscussionsBotRun(Base):
    """Discussions module (Apache Answer, separate service) — @AiNxt bot
    reply bookkeeping ONLY. Questions/answers/votes/tags/users all live in
    Apache Answer's own separate `ainxt_answer` database, not here.
    Status: pending -> running -> complete|error."""
    __tablename__ = "discussions_bot_runs"

    id                = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    answer_post_id    = Column(String(128), nullable=False, index=True)
    answer_post_type  = Column(String(16), nullable=False)   # question|answer|comment
    mention_author    = Column(String(255), nullable=True)
    status            = Column(String(16), nullable=False, default="pending", index=True)
    input_redacted    = Column(Boolean, nullable=False, default=False)
    output_redacted   = Column(Boolean, nullable=False, default=False)
    error_message     = Column(Text, nullable=True)
    reply_post_id     = Column(String(128), nullable=True)
    created_at        = Column(DateTime, nullable=False, default=_now)
    updated_at        = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class DiscussionsQuestion(Base):
    """Discussions module (third revision — native ai-ui frontend, Apache
    Answer as a headless engine at services/discussions_engine/). Mirror of a
    question created via the headless engine — written in the SAME gateway
    request that creates it (routers/discussions_router.py), not async."""
    __tablename__ = "discussions_questions"

    id                 = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    external_id        = Column(String(128), nullable=False)   # the engine's own question id
    author_user_id     = Column(String(255), nullable=False, index=True)
    title              = Column(String(500), nullable=False)
    content            = Column(Text, nullable=False)
    tags               = Column(JSONB, nullable=False, default=list)
    vote_count         = Column(Integer, nullable=False, default=0)
    answer_count       = Column(Integer, nullable=False, default=0)
    comment_count      = Column(Integer, nullable=False, default=0)
    accepted_answer_id = Column(UUID(as_uuid=False), nullable=True)
    created_at         = Column(DateTime, nullable=False, default=_now)
    updated_at         = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    answers = relationship("DiscussionsAnswer", back_populates="question", cascade="all, delete-orphan")


class DiscussionsAnswer(Base):
    """Mirror of an answer created via the headless engine."""
    __tablename__ = "discussions_answers"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    external_id    = Column(String(128), nullable=False)
    question_id    = Column(UUID(as_uuid=False),
                            ForeignKey("discussions_questions.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    author_user_id = Column(String(255), nullable=False, index=True)
    content        = Column(Text, nullable=False)
    vote_count     = Column(Integer, nullable=False, default=0)
    is_accepted    = Column(Boolean, nullable=False, default=False)
    comment_count  = Column(Integer, nullable=False, default=0)
    created_at     = Column(DateTime, nullable=False, default=_now)
    updated_at     = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    question = relationship("DiscussionsQuestion", back_populates="answers")


class DiscussionsVote(Base):
    """One row per (target, voter) — mirrors a vote cast via the headless engine."""
    __tablename__ = "discussions_votes"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    target_type = Column(String(16), nullable=False)   # question|answer
    target_id   = Column(UUID(as_uuid=False), nullable=False)
    user_id     = Column(String(255), nullable=False)
    direction   = Column(SmallInteger, nullable=False)  # 1 | -1
    created_at  = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("target_type", "target_id", "user_id", name="uq_discussions_vote"),
    )


class DiscussionsEvent(Base):
    """Append-only feedback-spine log — one row per meaningful Discussions
    action (question_asked, answer_posted, vote_cast, answer_accepted,
    ainxt_mentioned, ainxt_replied), regardless of what else happens on that
    write. This is the table a future self-improvement worker consumes —
    same shape as workers/skill_loop_worker.py's existing signal capture."""
    __tablename__ = "discussions_events"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    event_type    = Column(String(40), nullable=False, index=True)
    actor_user_id = Column(String(255), nullable=True, index=True)
    target_type   = Column(String(16), nullable=True)
    target_id     = Column(UUID(as_uuid=False), nullable=True)
    payload       = Column(JSONB, nullable=False, default=dict)
    created_at    = Column(DateTime, nullable=False, default=_now, index=True)


class DiscussionsComment(Base):
    """Mirror of a comment posted via the headless engine (on a question or
    an answer). Same write-then-mirror pattern as DiscussionsAnswer."""
    __tablename__ = "discussions_comments"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    external_id    = Column(String(128), nullable=False)
    target_type    = Column(String(16), nullable=False)   # question|answer
    target_id      = Column(UUID(as_uuid=False), nullable=False, index=True)
    author_user_id = Column(String(255), nullable=False, index=True)
    content        = Column(Text, nullable=False)
    created_at     = Column(DateTime, nullable=False, default=_now)
    # Added 2026-07-17 alongside the edit feature — bumped on every edit so
    # the UI's AuthorLine can render an "· edited …" affordance like it
    # already does for questions/answers. Back-filled to created_at for
    # existing rows via db/sql/prod_catchup_2026_07_17_discussions_comment_updated_at.sql.
    updated_at     = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# DISCUSSION NOTIFY GROUPS
# Per-department email notification groups for Discussions.
# When a user posts a discussion the backend looks up this table
# by the poster's department and notifies the configured addresses.
# Replaces the previous DISCUSSIONS_DEFAULT_NOTIFY_EMAILS .env var
# which was org-wide and not department-aware.
# Added 2026-07-31 via db/sql/prod_catchup_2026_07_31_discussion_notify_groups.sql
# ============================================================

class DiscussionNotifyGroup(Base):
    """Flat list of email addresses notified on every new discussion post,
    regardless of who posted or what department they belong to.

    One row per email address. Populated and managed by admins via direct
    DB insert/delete — no UI yet. If your email is in this table, you get
    a notification email (and an in-app inbox item if you're an internal user)
    whenever anyone posts a discussion.
    """
    __tablename__ = "discussion_notify_groups"
    __table_args__ = ({"schema": "ainxt", "info": {"skip_autogenerate": True}},)

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    notify_email = Column(String(255), nullable=False, unique=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ============================================================
# USER CONNECTOR PERMISSIONS
# Platform-wide per-user connector tool permission decisions.
# Used by the orchestrator (gate before connector_call) and the
# scheduled task worker (bypass per-task action_allowlist when
# always_allow=TRUE). Added 2026-07-27.
# ============================================================

class UserConnectorPermission(Base):
    """
    Stores a user's permission decision for a connector tool.

    Resolution logic (checked by ConnectorEngine._check_user_permission):
      - Specific tool row (tool_name='gitlab_list_projects') takes precedence
        over wildcard row (tool_name='*').
      - always_allow=TRUE  → skip gate, execute immediately
      - always_allow=FALSE → blocked (user explicitly denied)
      - No row found       → needs_prompt (ask the user)
    """
    __tablename__ = "user_connector_permissions"
    __table_args__ = (
        UniqueConstraint("user_id", "connector_name", "tool_name", name="uq_ucp_user_connector_tool"),
        {"schema": "ainxt"},
    )

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id        = Column(String(255), nullable=False, index=True)
    connector_name = Column(String(100), nullable=False)
    tool_name      = Column(String(100), nullable=False, default="*")
    always_allow   = Column(Boolean, nullable=False, default=False)
    created_at     = Column(DateTime, nullable=False, default=_now)
    updated_at     = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ============================================================
# LLM PROVIDER CONFIGURATION
#
# Admin-managed inventory of LLM provider accounts (Anthropic, OpenAI,
# Gemini, OpenRouter/other OpenAI-compatible services, Ollama/local) and
# the concrete models exposed under each. This is the single source of
# truth core.llm_provider_registry reads from — it replaces per-model
# env vars (OPENAI_CODING_MODEL, CLAUDE_PRIMARY_MODEL, ...) as the way
# new models become available to end users (chat dropdown, governance,
# managed endpoints). API keys are never stored here directly — they live
# in credential_vault, referenced by credential_id.
#
# install.sh seeds rows here via db/bootstrap_llm_providers.py; existing
# .env-only deployments are backfilled by the migrate.py Part AC1 function.
# ============================================================

class LLMProvider(Base):
    """One configured account/connection to an LLM backend.

    `family` selects which SDK/HTTP contract core/llm_provider_registry.py
    uses to talk to it: anthropic | openai | gemini | openai_compatible
    (covers OpenRouter, Together, Groq, Fireworks, and any other
    OpenAI-chat-completions-shaped API) | ollama. `slug` is the stable
    identifier used by install.sh's bootstrap script and by admin UI URLs.
    """
    __tablename__ = "llm_providers"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name         = Column(String(255), nullable=False)
    slug         = Column(String(100), nullable=False, unique=True, index=True)
    family       = Column(String(50), nullable=False)   # anthropic|openai|gemini|openai_compatible|ollama
    base_url     = Column(String(500), nullable=True)    # required for openai_compatible/ollama; override for others
    credential_id = Column(UUID(as_uuid=False),
                            ForeignKey("credential_vault.id", ondelete="SET NULL"),
                            nullable=True, index=True)
    org_id       = Column(String(255), nullable=False, default="default", index=True)
    enabled      = Column(Boolean, nullable=False, default=True)
    extra_config = Column(JSONB, nullable=False, default=dict)   # e.g. {"api_version": "...", "organization_id": "..."}
    last_verified_at   = Column(DateTime, nullable=True)
    last_verify_status = Column(String(20), nullable=True)   # ok|error|untested
    last_verify_error  = Column(Text, nullable=True)
    created_by   = Column(String(255), nullable=True)
    created_at   = Column(DateTime, nullable=False, default=_now)
    updated_at   = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    models = relationship("LLMModel", backref="provider", cascade="all, delete-orphan", passive_deletes=True)


class LLMModel(Base):
    """One model exposed under an LLMProvider.

    `model_id` is the exact string sent to the provider's API (e.g.
    "claude-sonnet-4-6", "gpt-5.4", "meta-llama/llama-3.1-70b-instruct" for
    OpenRouter, "llama3.2" for Ollama). `capabilities` carries everything
    that used to be a hardcoded literal keyed off the model-id string
    (context window, vision/tool-call support, tier/hint tags used by
    models/model_router.py, which UI/API channels may see it, and
    paid/free billing tier) — see core/llm_provider_registry.py for the
    read-side shape contract.
    """
    __tablename__ = "llm_models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_id", name="uq_llm_models_provider_model"),
    )

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    provider_id  = Column(UUID(as_uuid=False),
                           ForeignKey("llm_providers.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    model_id     = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    capabilities = Column(JSONB, nullable=False, default=dict)
    enabled      = Column(Boolean, nullable=False, default=True)
    is_default   = Column(Boolean, nullable=False, default=False)
    sort_order   = Column(Integer, nullable=False, default=0)
    source       = Column(String(20), nullable=False, default="discovered")   # discovered|manual|seed
    created_by   = Column(String(255), nullable=True)
    created_at   = Column(DateTime, nullable=False, default=_now)
    updated_at   = Column(DateTime, nullable=False, default=_now, onupdate=_now)
