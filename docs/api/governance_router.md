# Governance Router

## Overview

The **Governance Router** (`routers/governance_router.py`) is a FastAPI `APIRouter` (prefix `/governance`) that provides a unified **lifecycle-management and approval-workflow** layer for four categories of platform artifacts:

| Entity Type | Key | Source of Truth |
|---|---|---|
| `agents` | Standalone Agent Builder agents | `AgentRecord` (Postgres) + in-memory `agent_builder` singleton |
| `skills` | Build Studio / marketplace skills | `SkillRecord` (Postgres) + Redis skill store |
| `mcp` | MCP tools (marketplace) | `MCPServer` (Postgres) + Redis tool store + marketplace KV |
| `workflows` | Build Studio workflows | `WorkflowRecord` (Postgres) + Redis workflow store |

Every artifact moves through a governed state machine — **DRAFT → PENDING_APPROVAL → APPROVED → PRODUCTION** — with rejection, withdrawal, deprecation, and (for critical MCP tools) two-level IS/Security approval. All transitions are validated, persisted to a tamper-evident Postgres audit log, mirrored across Redis/in-memory/Postgres, and surfaced to approvers and submitters via the platform inbox and Slack/Teams notifications.

---

## Key Responsibilities

1. **Lifecycle state machine** — enforce valid transitions and reject illegal ones with HTTP 409.
2. **Multi-tier authorization** — role-based (`admin`, `platform_engineer`, `security`), org-hierarchy (`ad_level ≤ 3`), and department-scoped HOD approval.
3. **Dual storage synchronization** — keep Redis (live cache), Postgres (durable authority), and in-memory singletons consistent on every transition.
4. **Immutable audit trail** — every transition is signed (HMAC-SHA256) and written to `governance_events`.
5. **Approval routing & notifications** — route approval requests to the correct approver set (admins for public items; mapped HOD for private/department-scoped items) via inbox + Slack.
6. **Two-level MCP approval** — critical MCP tools require L1 (approver) then L2 (IS/AppSec/InfoSec team) sign-off.
7. **Publish-on-approval** — approved Build Studio workflows/agents are automatically published as shared catalog templates.
8. **SLA monitoring** — daily 5-day SLA reminders for stale `PENDING_APPROVAL` items, with manual trigger endpoints.

---

## Architecture Overview

```mermaid
graph TB
  subgraph Clients
    ABStudio["ABStudio Frontend<br/>(Build Studio)"]
    AIUI["ai-ui Frontend<br/>(Inbox / Governance)"]
    GW["Gateway"]
  end

  subgraph GovernanceRouter["governance_router (this module)"]
    EP["API Endpoints<br/>submit / approve / reject /<br/>promote / deprecate / withdraw"]
    SM["State Machine<br/>_VALID_TRANSITIONS"]
    GU["Guards<br/>_require_approver<br/>_require_scoped_approver"]
    TR["_transition handler"]
    KV["KV Helpers<br/>Redis DB=2 / DB=3"]
    PG["Postgres Helpers<br/>audit log + status sync"]
    MEM["In-Memory Helpers<br/>agent_builder / mcp_registry"]
    NOTIF["Notification & Routing<br/>inbox + Slack + HOD resolution"]
    SLA["SLA Monitor<br/>check_governance_sla_reminders"]
  end

  subgraph Storage
    Redis2[("Redis DB=2<br/>workflow KV — entity cache")]
    Redis3[("Redis DB=3<br/>registry KV — marketplace")]
    PGDB[("Postgres<br/>governance_events + entity tables")]
  end

  subgraph Dependencies
    AB["agent_builder singleton"]
    MR["mcp_registry singleton"]
    WR["workflow_repo<br/>(ABStudio)"]
    IS["inbox_store"]
    NT["core.notifications"]
    AS["core.audit_signer"]
    RBAC["auth.rbac / auth.dependencies"]
  end

  ABStudio --> GW
  AIUI --> GW
  GW --> EP
  EP --> GU --> TR
  TR --> SM
  TR --> KV --> Redis2
  TR --> PG --> PGDB
  TR --> MEM --> AB
  MEM --> MR
  EP --> NOTIF --> IS
  NOTIF --> NT
  PG --> AS
  EP --> WR
  GU --> RBAC
  SLA --> PGDB
  SLA --> NOTIF
```

---

## Entity Lifecycle State Machine

```mermaid
stateDiagram-v2
  [*] --> DRAFT: Create artifact

  DRAFT --> PENDING_APPROVAL: submit (any user)
  REJECTED --> PENDING_APPROVAL: re-submit after fixes

  PENDING_APPROVAL --> APPROVED: approve (scoped approver)
  PENDING_APPROVAL --> REJECTED: reject (scoped approver)
  PENDING_APPROVAL --> DRAFT: withdraw (owner / approver)

  PENDING_APPROVAL --> PENDING_L2: approve L1 (critical MCP only)
  PENDING_L2 --> APPROVED: approve L2 (IS/Security team)
  PENDING_L2 --> REJECTED: reject (scoped approver)
  PENDING_L2 --> DRAFT: withdraw (owner / approver)

  APPROVED --> PRODUCTION: promote (scoped approver)
  APPROVED --> DEPRECATED: deprecate (scoped approver)

  PRODUCTION --> DEPRECATED: deprecate (scoped approver)

  DEPRECATED --> [*]
```

### Transition Table

| Action | Allowed From | To Status | Who |
|---|---|---|---|
| `submit` | `DRAFT`, `REJECTED` | `PENDING_APPROVAL` | Any authenticated user |
| `approve` | `PENDING_APPROVAL`, `PENDING_L2` | `APPROVED` | Scoped approver (L2 for critical MCP) |
| `reject` | `PENDING_APPROVAL`, `PENDING_L2` | `REJECTED` | Scoped approver |
| `withdraw` | `PENDING_APPROVAL`, `PENDING_L2` | `DRAFT` | Owner or approver |
| `promote` | `APPROVED` | `PRODUCTION` | Scoped approver |
| `deprecate` | `PRODUCTION`, `APPROVED` | `DEPRECATED` | Scoped approver |

> **Critical MCP tools** insert an intermediate `PENDING_L2` state between `PENDING_APPROVAL` and `APPROVED`. L1 approval is performed by approver-role users; L2 approval requires IS/AppSec/InfoSec team membership (configurable via `IS_TEAM_DEPARTMENTS` env var, default: `IS,AppSec,InfoSec`) or admin role.

---

## Storage Strategy

The router maintains three layers of storage that are kept in sync on every transition. Redis is the fast live cache; Postgres is the durable authority that survives restarts; in-memory singletons serve runtime execution.

```mermaid
graph LR
  subgraph Read Path
    R1["1. Redis (DB=2)"] -->|miss| R2["2. In-memory agent_builder"]
    R2 -->|miss| R3["3. Postgres entity table"]
  end

  subgraph Write Path
    W1["_set_entity_governance"] --> W2["Update Redis (DB=2)"]
    W1 --> W3["Update in-memory agent_builder"]
    W1 --> W4["Update Postgres entity table"]
    W1 --> W5["Audit event → Postgres<br/>governance_events (signed)"]
  end

  subgraph MCP Marketplace
    M1["_sync_marketplace_status"] --> M2["Redis DB=3<br/>marketplace:tool:{name}"]
  end
```

### Redis Layout

| Purpose | DB | Prefix / Index |
|---|---|---|
| Entity governance cache | DB=2 (`RDB_WORKFLOW`) | `agent_builder:agent:`, `skill_store:`, `mcp:tool:`, `workflow_store:` |
| Entity name index | DB=2 | `agent_builder:index`, `skill_store:index`, `mcp:tool:index`, `workflow_store:index` |
| Marketplace tool status sync | DB=3 (`RDB_REGISTRY`) | `marketplace:tool:{name}` |

Redis is **best-effort**: if the KV client is unavailable, the router degrades gracefully to Postgres and in-memory sources. See [core_kv](../core_kv.md) for the KV client abstraction.

### Postgres Tables

| Table | Role |
|---|---|
| `governance_events` | Immutable, signed audit log of every transition |
| `AgentRecord` | Governance status mirror for agents |
| `SkillRecord` | Governance status mirror for skills |
| `MCPServer` | Governance status mirror for MCP tools |
| `WorkflowRecord` | Governance status mirror for workflows |
| `InboxItem` | Approval / status notifications (via `inbox_store`) |
| `User`, `DepartmentHodMapping` | HOD resolution and approver routing |

See [db_models](../db_models.md) for model definitions.

### Audit Signing

Every `_pg_record_event` call computes an HMAC-SHA256 signature over the canonical event dict via `core.audit_signer.sign_event` and stores it in `governance_events.signature`. This makes the audit log tamper-evident — the same scheme used for `sdlc_run_events`. See [core_audit_signer](../core_audit_signer.md).

---

## API Endpoints

| Method | Path | Handler | Auth | Description |
|---|---|---|---|---|
| `GET` | `/{entity_type}` | `list_governance` | Authenticated | List all entities with governance fields, pagination, status filter, visibility/department scoping |
| `GET` | `/{entity_type}/{name}` | `get_entity_status` | Authenticated | Current governance status of a single entity |
| `GET` | `/{entity_type}/{name}/graph` | `get_entity_graph` | Approver | Workflow graph preview for approver (Inbox) |
| `GET` | `/{entity_type}/{name}/config` | `get_entity_config` | Approver | Agent config preview (instructions, tools, skills) |
| `GET` | `/{entity_type}/{name}/source` | `get_entity_source` | Approver | Skill source code preview |
| `GET` | `/{entity_type}/{name}/history` | `get_history` | Authenticated | Durable audit-log history (always returns a list) |
| `POST` | `/{entity_type}/{name}/submit` | `submit_for_approval` | Authenticated | Submit for approval → `PENDING_APPROVAL` |
| `POST` | `/{entity_type}/{name}/approve` | `approve_entity` | Scoped approver | Approve → `APPROVED` (or `PENDING_L2` for critical MCP) |
| `POST` | `/{entity_type}/{name}/reject` | `reject_entity` | Scoped approver | Reject → `REJECTED` (with reason) |
| `POST` | `/{entity_type}/{name}/withdraw` | `withdraw_entity` | Owner / approver | Cancel pending request → `DRAFT` |
| `POST` | `/{entity_type}/{name}/promote` | `promote_entity` | Scoped approver | Promote → `PRODUCTION` |
| `POST` | `/{entity_type}/{name}/deprecate` | `deprecate_entity` | Scoped approver | Deprecate → `DEPRECATED` |
| `GET` | `/sla/overdue` | `get_sla_overdue` | Admin / operator / approver | List items exceeding 5-day SLA |
| `POST` | `/sla/remind` | `trigger_sla_reminders` | Admin | Manually trigger SLA reminder notifications |

All endpoints accept an optional `owner_id` query parameter for **owner-scoped** operations. This is critical because multiple users can create same-named artifacts; the `owner_id` ensures Redis cache entries and Postgres rows are correctly attributed.

---

## Core Components

### Constants & Configuration

| Constant | Description |
|---|---|
| `ENTITY_TYPES` | `{"agents", "skills", "mcp", "workflows"}` — valid entity types |
| `APPROVER_ROLES` | `{"admin", "platform_engineer", "security"}` — roles allowed to approve/reject/promote/deprecate |
| `_VALID_TRANSITIONS` | State-machine map: action → (allowed from-statuses, to-status) |
| `_IS_TEAM_DEPTS` | Departments for L2 MCP approval (env: `IS_TEAM_DEPARTMENTS`, default `IS,AppSec,InfoSec`) |
| `_REDIS_PREFIXES` / `_REDIS_INDICES` | Redis key prefixes and set-index names per entity type |

### KV Helpers

- **`_get_redis()`** — KV client for governance entity cache (DB=2). Returns `None` on KV failure.
- **`_get_marketplace_kv()`** — KV client for marketplace tool registry (DB=3).
- **`_sync_marketplace_status(name, new_status)`** — best-effort update of `marketplace:tool:{name}` status in DB=3.
- **`_load_entity_redis(entity_type, name, owner_id)`** — load entity from Redis; verifies `created_by` matches `owner_id` to prevent cross-user cache collisions.
- **`_save_entity_redis(entity_type, name, data)`** — write entity JSON + add name to index set.
- **`_list_entities_redis(entity_type)`** — enumerate all entities of a type from Redis index.

### Postgres Helpers

- **`_pg_record_event(...)`** — persist a signed `GovernanceEvent` row (fire-and-forget via `_bg`).
- **`_pg_update_status(entity_type, name, updates, owner_id)`** — sync governance fields to the entity's Postgres table (`AgentRecord`, `SkillRecord`, `MCPServer`, `WorkflowRecord`).
- **`_pg_get_history(entity_type, name, owner_id)`** — fetch ordered audit-log entries.

### In-Memory Helpers

- **`_get_entity_status(entity_type, name, owner_id)`** — three-tier status lookup: Redis → in-memory `agent_builder` → Postgres.
- **`_set_entity_governance(entity_type, name, updates, owner_id)`** — apply updates to all three layers (Redis + in-memory + Postgres). If no Redis copy exists, updates Postgres directly.
- **`_get_agent_builder()`** / **`_get_mcp_registry()`** — lazy imports of the `agent_builder` and `mcp_registry` singletons.

### Notification & Approval Routing

These functions form the **single source of truth** for who receives approval requests and who is authorized to act on them:

- **`_resolve_hod_user_ids(department)`** — query `DepartmentHodMapping` → active `User` IDs for the department's HOD(s).
- **`_resolve_admin_user_ids()`** — all active admin user IDs (fallback when no HOD is mapped).
- **`_resolve_approval_recipients(department, visibility)`** — routing logic:
  - `public` → admins only
  - `private` → mapped HOD(s) if present, else admins
- **`_resolve_sent_to_label(visibility, department)`** — human-readable label for inbox message body.
- **`_governance_notify(...)`** — push inbox items to approvers (on submit) or entity creator (on approve/reject/promote/deprecate/withdraw).
- **`_notify_is_team_for_l2(entity_type, name, l1_actor)`** — notify IS/Security team users when a critical MCP tool needs L2 approval.
- **`_notify_approvers(...)`** — fire-and-forget Slack notification via `core.notifications.notify`.
- **`_get_entity_owner_id(...)`** — resolve the creator's user ID from the Postgres entity record.
- **`_get_entity_department(...)`** / **`_get_entity_visibility(...)`** — read department and visibility from the governance mirror record.

### Publish-on-Approval

- **`_publish_as_template(entity_type, artifact_name, owner_id)`** — after approval of a workflow or agent, reads visibility + department from the governance record and publishes the artifact as a shared catalog template via `workflow_repo.publish_workflow_as_template` / `publish_agent_as_template`.
- **`_delete_published_source_agent(artifact_name, owner_id)`** — after an agent is published as a template, removes the source agent row, deregisters triggers, and deletes chat threads so it lives only in the Templates catalog. Mirrors the ABStudio `DELETE /agents/{id}` cleanup.

### Guards & Authorization

- **`_require_approver(current_user)`** — allows `admin`, `platform_engineer`, `security` roles, or `ad_level ≤ 3` (Director+). Used for preview endpoints.
- **`_require_scoped_approver(entity_type, name, current_user)`** — scope-aware guard for approve/reject/promote/deprecate:
  - `admin` → can approve anything (global override)
  - `public` items → admin only
  - `private` items → HOD of the creator's department (if mapped), else admin only
  - Uses `auth.rbac.is_admin`, `is_hod`, `get_hod_departments` for HOD identity (case-insensitive department match)
- **`_actor(current_user)`** — extract actor email or user ID for audit logging.

### Generic Transition Handler

**`_transition(entity_type, name, action, actor, ...)`** is the central function that:

1. Validates the entity type.
2. Looks up the current status (three-tier).
3. Checks the transition against `_VALID_TRANSITIONS` (HTTP 409 on illegal transition).
4. Builds update dict (`status`, `{action}_by`, `{action}_at`, optional `reason`).
5. Calls `_set_entity_governance` to sync all storage layers.
6. Fires `_bg(_pg_record_event, ...)` for the audit log (non-blocking).
7. Returns `(from_status, to_status)` for the endpoint to use in notifications.

### SLA Monitoring

- **`check_governance_sla_reminders()`** — queries `governance_events` for items in `PENDING_APPROVAL` for > 5 days with no subsequent approve/reject. For each stale item, resolves approver recipients (same visibility-aware routing) and pushes inbox + Slack notifications. Called daily by the cron scheduler at 09:00 IST.
- **`_query_overdue_items()`** — raw Postgres query helper shared by the endpoint and cron.
- **`get_sla_overdue`** — `GET /governance/sla/overdue` endpoint for admin/operator/approver.
- **`trigger_sla_reminders`** — `POST /governance/sla/remind` endpoint for manual admin trigger.

---

## Process Flows

### Submit for Approval

```mermaid
sequenceDiagram
  participant U as User (any)
  participant EP as submit_for_approval
  participant TR as _transition
  participant ST as _set_entity_governance
  participant PG as _pg_record_event
  participant N as _governance_notify
  participant IS as inbox_store

  U->>EP: POST /{entity_type}/{name}/submit?owner_id=...
  EP->>TR: _transition("submit", actor)
  TR->>TR: Validate current status ∈ {DRAFT, REJECTED}
  TR->>ST: Update Redis + in-memory + Postgres → PENDING_APPROVAL
  TR-->>EP: (from_status, to_status)
  EP->>EP: Sync marketplace KV (if MCP)
  EP-->>PG: _bg → sign + persist GovernanceEvent
  EP-->>N: _bg → resolve recipients
  N->>N: public → admins; private → HOD or admins
  N->>IS: publish_inbox_item per recipient
  N->>IS: Also notify submitter
  EP-->>U: {status: PENDING_APPROVAL}
```

### Approve (Standard + Two-Level MCP)

```mermaid
sequenceDiagram
  participant A as Approver
  participant EP as approve_entity
  participant GU as _require_scoped_approver
  participant TR as _transition
  participant PUB as _publish_as_template
  participant KB as _activate_agent_kb_docs
  participant N as _governance_notify

  A->>EP: POST /{entity_type}/{name}/approve

  alt Critical MCP tool (PENDING_APPROVAL)
    EP->>EP: Check marketplace KV is_critical
    EP->>EP: Verify L1 approver (role / ad_level)
    EP->>EP: Transition → PENDING_L2
    EP-->>N: _bg → notify IS team for L2
    EP-->>A: {status: PENDING_L2}
  else PENDING_L2 (critical MCP)
    EP->>EP: Verify L2 (IS team dept or admin)
    EP->>GU: Scoped approver check
    EP->>TR: _transition("approve") → APPROVED
    EP-->>N: _bg → notify creator
    EP-->>A: {status: APPROVED}
  else Standard (non-critical)
    EP->>GU: Scoped approver check
    EP->>TR: _transition("approve") → APPROVED
    alt entity is workflow or agent
      EP-->>PUB: _bg → publish as template
    end
    alt entity is agent
      EP-->>KB: _bg → activate linked KB docs
    end
    EP-->>N: _bg → notify creator
    EP-->>A: {status: APPROVED}
  end
```

### Reject / Withdraw

```mermaid
sequenceDiagram
  participant A as Approver / Owner
  participant EP as reject_entity / withdraw_entity
  participant GU as _require_scoped_approver
  participant TR as _transition
  participant N as _governance_notify

  alt Reject
    A->>EP: POST .../reject {reason}
    EP->>GU: Scoped approver check
    EP->>TR: _transition("reject", reason) → REJECTED
    EP-->>N: _bg → notify creator with reason
  else Withdraw
    A->>EP: POST .../withdraw
    EP->>EP: Verify owner or approver
    EP->>TR: _transition("withdraw") → DRAFT
    EP-->>N: _bg → notify creator
  end
  EP-->>A: Response
```

### Promote to Production

```mermaid
sequenceDiagram
  participant A as Approver
  participant EP as promote_entity
  participant GU as _require_scoped_approver
  participant TR as _transition
  participant AB as agent_builder
  participant N as _governance_notify

  A->>EP: POST .../promote
  EP->>GU: Scoped approver check
  EP->>TR: _transition("promote") → PRODUCTION
  alt entity is agent
    EP-->>AB: _bg → reload_from_db(name) (hot-reload)
  end
  EP-->>N: _bg → notify creator
  EP-->>A: {status: PRODUCTION}
```

### SLA Reminder Flow

```mermaid
sequenceDiagram
  participant CRON as Cron Scheduler (daily 09:00 IST)
  participant SLA as check_governance_sla_reminders
  participant DB as Postgres
  participant RR as _resolve_approval_recipients
  participant IS as inbox_store
  participant NT as core.notifications

  CRON->>SLA: trigger
  SLA->>DB: Query PENDING_APPROVAL > 5 days
  DB-->>SLA: Overdue items
  loop For each overdue item
    SLA->>RR: Resolve recipients (visibility-aware)
    RR-->>SLA: approver user IDs
    loop For each recipient
      SLA->>IS: publish_inbox_item (SLA Overdue)
    end
    SLA->>NT: notify (Slack/Teams)
  end
  SLA-->>CRON: {overdue_count, notified}
```

---

## Authorization Model

```mermaid
flowchart TD
  REQ["Approve / Reject / Promote / Deprecate request"] --> ADMIN{"is_admin?"}
  ADMIN -->|Yes| ALLOW["Allow (global override)"]

  ADMIN -->|No| VIS{"visibility?"}
  VIS -->|public| DENY1["403: Admin only for public items"]

  VIS -->|private| HODMAP{"HOD mapped for<br/>creator's department?"}
  HODMAP -->|No| DENY2["403: Admin only<br/>(no HOD mapped)"]
  HODMAP -->|Yes| ISHOD{"Caller is HOD of<br/>creator's department?"}
  ISHOD -->|Yes| ALLOW
  ISHOD -->|No| DENY3["403: Requires HOD of<br/>creator's department"]
```

### Approval Routing vs. Authorization

The routing logic (`_resolve_approval_recipients`) and the authorization guard (`_require_scoped_approver`) share the **same rules** to ensure notifications and enforcement are aligned:

| Visibility | Notification Recipients | Authorized Approvers |
|---|---|---|
| `public` | All admins | Admin only |
| `private` (HOD mapped) | Department HOD(s) | Department HOD(s) |
| `private` (no HOD mapped) | All admins | Admin only |

> Deliberately, broad `ad_level ≤ 3` seniors are **not** used for routing or authorization — only the mapped HOD or an admin. This prevents approval requests from leaking to unrelated departments.

---

## Component Interaction Diagram

```mermaid
graph TB
  subgraph Endpoints
    list_governance
    get_entity_status
    get_entity_graph
    get_entity_config
    get_entity_source
    get_history
    submit_for_approval
    approve_entity
    reject_entity
    withdraw_entity
    promote_entity
    deprecate_entity
    get_sla_overdue
    trigger_sla_reminders
  end

  subgraph Core
    _transition
    _set_entity_governance
    _get_entity_status
    _require_approver
    _require_scoped_approver
    _governance_notify
    _resolve_approval_recipients
    _publish_as_template
    check_governance_sla_reminders
  end

  subgraph Storage
    _load_entity_redis
    _save_entity_redis
    _list_entities_redis
    _pg_record_event
    _pg_update_status
    _pg_get_history
    _sync_marketplace_status
  end

  submit_for_approval --> _transition
  approve_entity --> _transition
  reject_entity --> _transition
  withdraw_entity --> _transition
  promote_entity --> _transition
  deprecate_entity --> _transition

  _transition --> _get_entity_status
  _transition --> _set_entity_governance
  _transition --> _pg_record_event

  _set_entity_governance --> _load_entity_redis
  _set_entity_governance --> _save_entity_redis
  _set_entity_governance --> _pg_update_status

  approve_entity --> _require_scoped_approver
  reject_entity --> _require_scoped_approver
  promote_entity --> _require_scoped_approver
  deprecate_entity --> _require_scoped_approver

  get_entity_graph --> _require_approver
  get_entity_config --> _require_approver
  get_entity_source --> _require_approver

  submit_for_approval --> _governance_notify
  approve_entity --> _governance_notify
  approve_entity --> _publish_as_template
  _governance_notify --> _resolve_approval_recipients

  get_sla_overdue --> _pg_get_history
  trigger_sla_reminders --> check_governance_sla_reminders
  check_governance_sla_reminders --> _resolve_approval_recipients
```

---

## Data Model

### `GovernanceEvent` (Postgres)

| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `entity_type` | String(50) | `agents`, `skills`, `mcp`, or `workflows` |
| `name` | String(255) | Entity name |
| `action` | String(50) | `submit`, `approve`, `approve_l1`, `reject`, `promote`, `deprecate`, `withdraw` |
| `from_status` | String(50) | Previous status (nullable) |
| `to_status` | String(50) | New status |
| `actor` | String(255) | User email or `"system"` |
| `reason` | Text | Rejection reason (nullable) |
| `created_by` | String(255) | Owner user ID for owner-scoped queries |
| `signature` | Text | HMAC-SHA256 signature |
| `created_at` | DateTime | Timestamp |

### Pydantic Models

| Model | Fields | Usage |
|---|---|---|
| `RejectBody` | `reason: str = ""` | Request body for `POST .../reject` |

---

## Dependencies

### Internal Modules

| Module | Usage |
|---|---|
| [auth_dependencies](../auth_dependencies.md) | `get_current_user` — JWT/API-key/cookie authentication for all endpoints |
| [auth_rbac](../auth_rbac.md) | `is_admin`, `is_hod`, `get_hod_departments` — HOD-scoped authorization |
| [core_kv](../core_kv.md) | `get_kv`, `KVError` — Redis KV client for entity cache and marketplace sync |
| [core_logger](../core_logger.md) | `logger` — structured logging |
| [core_config](../infrastructure/core_config.md) | `REDIS_HOST`, `REDIS_PORT`, `RDB_WORKFLOW`, `RDB_REGISTRY` — KV configuration |
| [core_notifications](../core_notifications.md) | `notify` — Slack/Teams/email notifications |
| [core_audit_signer](../core_audit_signer.md) | `sign_event` — HMAC-SHA256 audit event signing |
| [db_models](../db_models.md) | `GovernanceEvent`, `AgentRecord`, `SkillRecord`, `MCPServer`, `WorkflowRecord`, `User`, `DepartmentHodMapping`, `InboxItem` |
| [store_inbox_store](../store_inbox_store.md) | `publish_inbox_item` — inbox notification delivery |
| [agents_agent_builder](../agents_agent_builder.md) | `agent_builder` singleton — in-memory agent cache and hot-reload |
| [mcp_registry](../mcp_registry.md) | `mcp_registry` singleton — in-memory MCP tool/skill registry |
| [app_core_workflow_repo](../app_core_workflow_repo.md) | `publish_workflow_as_template`, `publish_agent_as_template`, `get_workflow_by_name`, `get_agent_by_name`, `get_skill`, `delete_agent`, trigger cleanup |
| [app_services_trigger_scheduler](../app_services_trigger_scheduler.md) | `deregister_trigger` — trigger cleanup on published-agent deletion |
| [app_api_agent_chat](../app_api_agent_chat.md) | Agent chat store — thread cleanup on published-agent deletion |

### External Libraries

| Library | Usage |
|---|---|
| `fastapi` | `APIRouter`, `HTTPException`, `Depends`, `Query` |
| `pydantic` | `BaseModel` for request validation |
| `sqlalchemy` | Raw SQL text queries for SLA checks |
| `threading` | `_bg` fire-and-forget daemon threads |

---

## Integration Points

### ABStudio Build Studio

The governance router is the **approval gateway** for artifacts created in ABStudio's Build Studio. When a user clicks "Deploy" on a workflow or agent, the frontend calls `POST /governance/{entity_type}/{name}/submit`. Upon approval, the artifact is published as a shared template via `workflow_repo`. See [api_governance](api_governance.md) for the ABStudio-side governance API wrappers.

### ai-ui Inbox

The ai-ui frontend's `Inbox` component consumes governance inbox items (`type = "governance_approval"`) to render approval cards with inline actions. The metadata fields (`entity_type`, `entity_name`, `status`, `current_status`, `owner_id`) drive the UI's approve/reject buttons. The preview endpoints (`/graph`, `/config`, `/source`) provide approvers with artifact details without needing owner-scoped access.

### Cron Scheduler

`check_governance_sla_reminders()` is invoked daily at 09:00 IST (03:30 UTC) by the platform's cron scheduler (see [worker_orchestration](../workers/worker_orchestration.md)). It can also be triggered manually via `POST /governance/sla/remind`.

### Agent KB Activation

When an agent is approved, any linked KnowledgeDocuments are activated (embedded into pgvector under `repo='agent_kb:{name}'`) in a background thread. This makes the agent's knowledge base immediately RAG-searchable upon approval.

### Agent Hot-Reload on Promote

When an agent is promoted to `PRODUCTION`, `agent_builder.reload_from_db(name)` is called in a background thread so the new/updated system prompt takes effect immediately without a server restart.

---

## Error Handling & Resilience

| Scenario | Behavior |
|---|---|
| Redis unavailable | `_get_redis()` returns `None`; reads fall through to in-memory/Postgres; writes skip Redis |
| Postgres audit write fails | Logged as warning; API response is not blocked (fire-and-forget via `_bg`) |
| Postgres status sync fails | Logged as warning; Redis/in-memory still updated |
| Marketplace KV sync fails | Logged as warning; no-op |
| Illegal state transition | HTTP 409 with descriptive message |
| Entity not found | HTTP 404 |
| Insufficient privileges | HTTP 403 with detailed reason |
| Notification failure | Logged as warning; never blocks API response |

The `_bg(fn, *args, **kwargs)` helper wraps every non-critical side-effect (audit logging, notifications, template publishing, KB activation, hot-reload) in a daemon thread that logs and swallows exceptions, ensuring the caller is never blocked.
