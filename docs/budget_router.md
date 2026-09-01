# Budget Router Module

## Introduction

The **Budget Router** (`routers/budget_router.py`) is the central API layer for all budget management, allocation, utilization tracking, and cost chargeback operations in the AiNxt platform. It exposes a comprehensive set of FastAPI endpoints under the `/budget` tag that serve three distinct user personas — **end users** (self-service budget views), **HODs / reporting managers** (team oversight and budget-increase approvals), and **admins** (global allocation, HOD cap management, 10x winner program, monthly resets).

The module implements a controlled allocation model where every user's cost budget is the sum of a **base allocation** ($50 default, $1000 for 10x winners) and an **extra allocation** (granted via approved HOD budget-increase requests). Both reset to defaults on the monthly reset cycle — nothing carries over. The only ways a user's budget changes are: (a) the HOD approval flow, and (b) the admin-only 10x-winner base-allocation action.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "Client Layer"
        FE_BM["BudgetManager.jsx<br/>(ai-ui frontend)"]
        FE_TBP["TeamBudgetPanel.jsx"]
        FE_UV["UtilizationView.jsx"]
    end

    subgraph "API Layer — budget_router.py"
        EP_SELF["Self-Service Endpoints<br/>/budget/me, /budget/me/utilization"]
        EP_TEAM["Team Endpoints<br/>/budget/team, /budget/team/utilization"]
        EP_USER["User-Scope Endpoints<br/>/budget/users/{id}/*"]
        EP_REQ["Increase Request Flow<br/>/budget/request-increase<br/>/budget/requests/*"]
        EP_ADMIN["Admin Endpoints<br/>/budget/admin/*"]
        EP_CB["Chargeback Endpoints<br/>/budget/chargeback/*"]
        EP_HOD["HOD Cap Endpoints<br/>/budget/hod/cap-status"]
    end

    subgraph "Business Logic Layer"
        GOV["hod_budget_governor.py<br/>Cap enforcement & reservation"]
        AUDIT["budget_audit_service.py<br/>Monthly snapshot & reset"]
        HIER["hierarchy_service.py<br/>Org-tree subtree resolution"]
    end

    subgraph "Data Layer"
        BS["budget_store.py<br/>Redis + Postgres read/write"]
        IS["inbox_store.py<br/>In-app notifications"]
        SMTP["smtp_service.py<br/>Email notifications"]
    end

    subgraph "Storage"
        REDIS[("Redis<br/>Fast-path cache")]
        PG[("PostgreSQL<br/>Source of truth")]
    end

    subgraph "Cross-Cutting"
        MW["BudgetMiddleware<br/>Per-request enforcement"]
        RBAC["auth/rbac.py<br/>Role & scope checks"]
        RL["rate_limiter.py<br/>BUDGET_REQUEST / BUDGET_ADMIN"]
        SV["security_validation.py<br/>Input sanitization"]
    end

    FE_BM --> EP_SELF & EP_TEAM & EP_USER & EP_REQ & EP_ADMIN & EP_HOD
    FE_TBP --> EP_TEAM
    FE_UV --> EP_SELF & EP_TEAM

    EP_SELF --> BS
    EP_TEAM --> BS & HIER
    EP_USER --> BS
    EP_REQ --> BS & IS & SMTP & GOV
    EP_ADMIN --> BS & AUDIT & IS & SMTP
    EP_CB --> PG
    EP_HOD --> GOV

    BS --> REDIS & PG
    GOV --> PG
    AUDIT --> PG & BS
    IS --> PG
    HIER --> PG

    MW -.->|"check_budget<br/>before LLM call"| BS
    EP_REQ -.-> RBAC & RL & SV
    EP_ADMIN -.-> RBAC & RL
    EP_USER -.-> RBAC
```

### Allocation Model

```mermaid
graph LR
    subgraph "User Total Budget"
        BASE["base_cost_usd<br/>$50 default<br/>$1000 for 10x winners"]
        EXTRA["extra_cost_usd<br/>$0 default<br/>Incremented via HOD approvals"]
    end
    TOTAL["max_cost_usd_total<br/>= base + extra"]

    BASE --> TOTAL
    EXTRA --> TOTAL

    MONTHLY["Monthly Reset"] -->|"base → $50"| BASE
    MONTHLY -->|"extra → $0"| EXTRA
    MONTHLY -->|"usage → 0"| USAGE["Cumulative Usage"]

    WINNER["Admin: Winner Allocation"] -->|"base → $1000"| BASE
    HOD_APPROVE["HOD: Approve Request"] -->|"extra += amount"| EXTRA
```

---

## Endpoint Groups

The router organizes ~30 endpoints into six functional groups. Each group has distinct authorization rules and data-access patterns.

```mermaid
graph TB
    subgraph "Self-Service (X-User-Id header)"
        S1["GET /budget/me"]
        S2["GET /budget/me/utilization"]
    end

    subgraph "Team View (auth token)"
        T1["GET /budget/team"]
        T2["GET /budget/team/utilization"]
    end

    subgraph "User-Scope (auth token + scope check)"
        U1["GET /budget/users"]
        U2["GET /budget/users/{id}"]
        U3["GET /budget/users/{id}/usage"]
        U4["GET /budget/users/{id}/utilization"]
        U5["POST /budget/users/{id}/reset-usage"]
        U6["POST /budget/users/{id}/product"]
    end

    subgraph "Increase Request Flow"
        R1["POST /budget/request-increase"]
        R2["GET /budget/requests"]
        R3["GET /budget/requests/{id}"]
        R4["POST /budget/requests/{id}/approve"]
        R5["POST /budget/requests/{id}/reject"]
        R6["GET /budget/my-increases"]
    end

    subgraph "Admin Operations"
        A1["GET /budget/summary"]
        A2["GET /budget/admin/hods"]
        A3["PUT /budget/admin/hods/{email}/cap"]
        A4["POST /budget/admin/users/{id}/winner-allocation"]
        A5["POST /budget/admin/winner-allocation/batch"]
        A6["GET /budget/admin/hod-audit"]
        A7["POST /budget/admin/run-monthly-reset"]
        A8["POST /budget/admin/send-reset-warning"]
    end

    subgraph "Chargeback & Reference"
        C1["GET /budget/chargeback"]
        C2["GET /budget/chargeback/{product_id}"]
        C3["GET /budget/model-rates"]
        C4["GET /budget/band-defaults"]
        C5["GET /budget/hod/cap-status"]
    end
```

---

## Authorization & Scope Model

The module enforces a layered authorization model. Every endpoint that touches a specific user's data passes through `_scope_or_403()`, which checks three tiers in order:

```mermaid
flowchart TD
    START["Incoming request for target_user_id"] --> ADMIN{"is_admin?"}
    ADMIN -->|"Yes"| PASS["✅ Allow — unrestricted"]
    ADMIN -->|"No"| HOD{"is_hod?"}
    HOD -->|"Yes"| HOD_SCOPE{"target ∈ HOD<br/>department scope?"}
    HOD_SCOPE -->|"Yes"| PASS
    HOD_SCOPE -->|"No"| RM{"Reporting manager?<br/>(hierarchy subtree)"}
    RM -->|"Yes"| PASS
    RM -->|"No"| DENY["❌ 403 Out of scope"]
    HOD -->|"No"| RM2{"Reporting manager?<br/>(hierarchy subtree)"}
    RM2 -->|"Yes"| PASS
    RM2 -->|"No"| DENY

    style PASS fill:#c8e6c9
    style DENY fill:#ffcdd2
```

### Role Helpers (from `auth/rbac.py`)

| Helper | Returns | Description |
|--------|---------|-------------|
| `is_admin(current_user)` | `bool` | True if `role == "admin"` |
| `is_hod(current_user)` | `bool` | True if `is_hod` flag is set; admin+HOD users retain the flag |
| `get_hod_departments(current_user)` | `List[str]` | Department names the HOD owns |
| `get_visible_user_filter(current_user, request)` | `Optional[Set[str]]` | `None` = admin (unrestricted); `set` = scoped user IDs; memoized per-request |

### Rate Limiting

Two rate-limit profiles from `core/rate_limiter.py` are applied:

- **`BUDGET_REQUEST`** — applied to `POST /budget/request-increase` (user-scoped via `user_id`).
- **`BUDGET_ADMIN`** — applied to team/admin endpoints (`GET /budget/team`, `GET /budget/admin/hods`, `PUT /budget/admin/hods/{email}/cap`, winner-allocation endpoints, `GET /budget/admin/hod-audit`, `GET /budget/my-increases`).

Both use `enforce_rate_limit_with_behaviour()`, which first checks for behaviour-based IP/user blocks (anomaly detection) before applying the sliding-window limiter.

---

## Budget Increase Request Flow

This is the core business workflow of the module — the only mechanism (besides admin winner-allocation) by which a user's budget grows.

```mermaid
sequenceDiagram
    participant U as End User
    participant R as Budget Router
    participant BS as budget_store
    participant GOV as hod_budget_governor
    participant IS as inbox_store
    participant SMTP as smtp_service
    participant HOD as HOD(s)
    participant PG as PostgreSQL

    U->>R: POST /budget/request-increase
    R->>R: validate_budget_request (XSS, sanitization)
    R->>R: Enforce caller_id == body.user_id (IDOR defense)
    R->>R: enforce_rate_limit (BUDGET_REQUEST)
    R->>BS: resolve_hod_emails_for_department(dept)
    alt No HOD mapped
        R-->>U: 422 "No HOD mapped to your department"
    end
    R->>BS: request_budget_increase()
    BS->>PG: INSERT pending rows (one per HOD, shared request_id)
    BS-->>R: { request_id, hod_emails, current_base, current_extra }

    loop For each HOD
        R->>IS: publish_inbox_item (type=budget_request)
        R->>SMTP: send_html_email (HOD approval notification)
    end
    R-->>U: { success, request_id, hod_emails }

    Note over HOD: HOD opens Inbox / Budget Manager

    HOD->>R: POST /budget/requests/{id}/approve
    R->>BS: _load_request_group_meta(request_id)
    R->>R: Verify actor is routed HOD (else 403)
    R->>BS: approve_budget_request()
    BS->>PG: SELECT ... FOR UPDATE (lock all rows sharing request_id)
    BS->>GOV: check_and_reserve_cap(cur, hod_email, amount)
    alt Cap exceeded & enforcement ON
        GOV-->>BS: HTTPException(409)
        BS->>PG: ROLLBACK
        BS-->>R: 409 Cap exceeded
    end
    BS->>PG: UPDATE winning row → 'approved' (stamp cap_at_time, consumed_after)
    BS->>PG: UPDATE sibling rows → 'superseded'
    BS->>PG: UPDATE budget_configs (extra_cost_usd += amount)
    BS->>PG: COMMIT
    BS->>REDIS: Best-effort cache sync (budget:{uid})
    BS-->>R: { success, new_base, new_extra, new_total }
    R->>IS: publish_inbox_item (type=budget_approved → requester)
    R->>R: _invalidate_roster_cache()
    R-->>HOD: { success, ... }
```

### Key Design Decisions

1. **Multi-HOD fan-out**: A department may map to multiple HODs. The request creates one `pending` ledger row per HOD, all sharing a single `request_id`. Whichever HOD acts first resolves it for everyone.

2. **Atomic approval**: The ledger status transition, HOD cap charge, and `budget_configs` update all happen inside a single Postgres transaction with `SELECT ... FOR UPDATE` row-level locks. If any step fails, everything rolls back.

3. **First-resolver wins**: A concurrent second action on an already-resolved group fails gracefully with `"already approved/rejected by X"`.

4. **IDOR defense**: The caller's authenticated `user_id` must match `body.user_id` — prevents submitting requests on behalf of other users.

5. **404 over 403 for status checks**: `GET /budget/requests/{id}` returns 404 (not 403) for unauthorized callers to prevent enumeration of valid request UUIDs.

---

## HOD Monthly Cap Management

HODs have a monthly allocation cap that limits the total extra budget they can approve across all their team members in a given period.

```mermaid
flowchart LR
    subgraph "Cap Lifecycle"
        ADMIN_SET["Admin sets cap<br/>PUT /budget/admin/hods/{email}/cap"] --> CAP_ROW["hod_allocation_caps row<br/>{monthly_cap_usd, is_active}"]
        CAP_ROW --> ENFORCE{"Enforcement enabled?<br/>HOD_CAP_ENFORCEMENT_ENABLED"}
        ENFORCE -->|"Yes"| LIVE["Live mode:<br/>409 on overrun"]
        ENFORCE -->|"No"| SHADOW["Shadow mode:<br/>logs but never blocks"]
    end

    subgraph "Consumption Tracking"
        APPROVE["HOD approves request"] --> CHARGE["check_and_reserve_cap()"]
        CHARGE --> LEDGER["hod_allocation_ledger<br/>consumed_after_usd = MAX(...)"]
        LEDGER --> STATUS["GET /budget/hod/cap-status<br/>consumed = MAX(consumed_after_usd)"]
    end

    subgraph "Reset"
        MONTHLY_RESET["Monthly Reset"] -->|"Truncate ledger"| LEDGER
        MONTHLY_RESET -->|"Cap row persists"| CAP_ROW
    end
```

### Cap Enforcement (`services/hod_budget_governor.py`)

- **`check_and_reserve_cap(cur, hod_email, amount)`** — Called within the approval transaction. Locks the HOD's cap row (`FOR UPDATE`), computes projected consumption, and raises `HTTPException(409)` if the charge would exceed the cap and enforcement is enabled. In shadow mode, it logs but never raises.

- **`get_cap_status(hod_email)`** — Side-effect-free read for the UI banner. Returns `CapStatus` with `cap_usd`, `consumed_usd`, `remaining_usd`, `period_yyyymm`, and `resets_on`. Falls back to `HOD_DEFAULT_MONTHLY_CAP_USD` when no cap row exists.

- **`compute_allocate_delta(old, new)`** — Charge model: increases are charged, decreases and no-changes are zero (no refunds).

---

## Monthly Reset Cycle

The monthly reset is the mechanism that prevents budget accumulation across periods. It is triggered both automatically (cron thread) and manually (admin endpoint).

```mermaid
flowchart TD
    subgraph "Warning Phase (last day of month)"
        WARN_CRON["_budget_reset_warning_cron_thread<br/>fires on last day of month"]
        WARN_CRON --> WARN_SVC["send_pre_reset_warnings_for_all()"]
        WARN_SVC --> WARN_EMAIL["Pre-reset warning email<br/>to every budgeted user"]
    end

    subgraph "Reset Phase (1st of month)"
        RESET_CRON["_budget_reset_cron_thread<br/>fires on 1st at 03:15 UTC"]
        RESET_CRON --> RESET_SVC["snapshot_and_reset_all_budgeted_users()"]
        RESET_SVC --> SNAP["1. Snapshot closing state<br/>→ budget_period_audits"]
        RESET_SVC --> RESET_CFG["2. Reset budget_configs<br/>base → $50, extra → $0"]
        RESET_SVC --> RESET_USAGE["3. Zero usage counters<br/>(Redis + Postgres)"]
        RESET_SVC --> WIPE["4. Truncate hod_allocation_ledger"]
    end

    subgraph "Manual Trigger"
        ADMIN_EP["POST /budget/admin/run-monthly-reset"] --> RESET_SVC
        ADMIN_WARN["POST /budget/admin/send-reset-warning"] --> WARN_SVC
    end

    subgraph "Feature Gate"
        FLAG["BUDGET_MONTHLY_RESET_ENABLED"] -.->|"must be true"| RESET_CRON & ADMIN_EP & WARN_CRON & ADMIN_WARN
    end
```

### Idempotency

`snapshot_and_reset_all_budgeted_users()` is idempotent — re-running for the same period is a no-op because the snapshot `INSERT` uses `ON CONFLICT DO NOTHING`. If the audit row already exists, the reset step is skipped to avoid clobbering subsequent admin/HOD changes.

---

## Data Storage Architecture

Budget data is dual-written to Redis (fast-path) and PostgreSQL (source of truth) with a carefully designed fallback chain.

```mermaid
graph TB
    subgraph "Redis (Fast Path)"
        R_BUDGET["budget:{uid}<br/>HGETALL — budget config hash"]
        R_USAGE_T["usage:{uid}:total<br/>HGETALL — cumulative usage"]
        R_USAGE_D["usage:{uid}:{date}<br/>HGETALL — daily usage (8-day TTL)"]
        R_INDEX["budget:users:index<br/>SET — all budgeted user IDs"]
        R_ROSTER["budget:users:roster:v1:{actor}<br/>STRING — cached roster (45s TTL)"]
        R_PROD["usage:product:{pid}:{date}<br/>HGETALL — per-product daily cost"]
    end

    subgraph "PostgreSQL (Source of Truth)"
        P_BUDGET["budget_configs<br/>Per-user/band allocation"]
        P_USAGE["user_usage_totals<br/>Cumulative usage counters"]
        P_MODEL["model_usages<br/>Per-call usage log (partitioned)"]
        P_LEDGER["hod_allocation_ledger<br/>Append-only increase audit"]
        P_CAPS["hod_allocation_caps<br/>HOD monthly cap config"]
        P_AUDIT["budget_period_audits<br/>Monthly snapshot history"]
        P_HOD_MAP["department_hod_mapping<br/>Dept → HOD email mapping"]
    end

    R_BUDGET -.->|"fallback on miss"| P_BUDGET
    R_USAGE_T -.->|"fallback on miss"| P_USAGE
    R_USAGE_D -.->|"backfill from PG<br/>for dates > 8 days"| P_MODEL
```

### Read/Write Patterns

| Operation | Redis | PostgreSQL | Fallback |
|-----------|-------|------------|----------|
| `get_budget(uid)` | `HGETALL budget:{uid}` | `budget_configs` SELECT | Redis → PG → None |
| `set_budget(uid, ...)` | `HSET budget:{uid}` + `SADD budget:users:index` | `budget_configs` UPSERT | Both written |
| `get_usage_total(uid)` | `HGETALL usage:{uid}:total` | `user_usage_totals` SELECT | Redis → PG → zeros |
| `increment_usage(uid, ...)` | `HINCRBY` total + dated keys | `user_usage_totals` UPSERT | Both written |
| `get_usage_history(uid)` | Dated keys (≤8 days) | `model_usages` GROUP BY date | Redis → PG backfill |
| `check_budget(uid)` | Redis budget + usage | PG budget + usage | Fail-open if both down |

### Roster Caching Strategy

The `GET /budget/users` endpoint (admin/HOD roster) uses a per-actor Redis cache with a 45-second TTL:

1. **Cache key**: `budget:users:roster:v1:{actor_email}` — scoped so admin and each HOD have separate cached rosters.
2. **Cache invalidation**: `_invalidate_roster_cache()` deletes all `budget:users:roster:v1:*` keys after any mutating action (budget increase, winner allocation, usage reset).
3. **Cold-cache optimization**: Auto-seeding of default budgets is deferred to `GET /budget/me` — the roster returns synthetic defaults without persisting, avoiding a fan-out of Redis+PG writes for large orgs.
4. **Concurrent fetch**: Redis hashes for each user (budget, usage-total, usage-today) are fetched via a bounded thread pool (4–32 workers) to minimize wall-clock latency for large rosters.

---

## Utilization Breakdown

Cost breakdowns by channel or model are computed directly from the `model_usages` table using SQL-level canonicalization.

```mermaid
flowchart LR
    subgraph "Raw model_usages columns"
        RAW_CH["source_channel<br/>(inconsistent casing: 'cli', 'CLI', '')"]
        RAW_MODEL["model<br/>(various formats: raw id,<br/>friendly wrapper, local: prefix)"]
    end

    subgraph "SQL Canonicalization"
        CH_KEY["_CHANNEL_KEY_SQL<br/>UPPER(NULLIF(TRIM(source_channel), ''))<br/>→ 'CLI', 'WEB', 'UNKNOWN'"]
        MODEL_KEY["_MODEL_KEY_SQL<br/>Extract last (...) group,<br/>strip local: prefix,<br/>LOWER()"]
    end

    subgraph "Output"
        BREAKDOWN["[{key, cost_usd, requests, tokens}]<br/>ordered by cost DESC"]
    end

    RAW_CH --> CH_KEY --> BREAKDOWN
    RAW_MODEL --> MODEL_KEY --> BREAKDOWN
```

The `_breakdown_for_users()` function aggregates month-to-date costs for a set of user IDs, grouped by either `channel` or `model` dimension. It uses partition-pruning-friendly filters (`user_id::text = ANY(:uids)` and `created_at >= date_trunc('month', now())`).

---

## Chargeback / Per-Product Billing

The chargeback endpoints provide per-product cost attribution for the current calendar month, sourced directly from `model_usages` joined with `products`.

```mermaid
flowchart TD
    subgraph "Endpoints"
        CB_SUM["GET /budget/chargeback<br/>(admin/operator)"]
        CB_PROD["GET /budget/chargeback/{product_id}<br/>(admin or director-level)"]
        ASSIGN["POST /budget/users/{id}/product<br/>(admin or self)"]
    end

    subgraph "Data Sources"
        MU["model_usages<br/>(product_id, model, cost_usd, tokens)"]
        PROD["products<br/>(id, name, code)"]
        USERS["users<br/>(default_product_id)"]
        DPM["dept_product_mappings<br/>(product_id, department_name)"]
    end

    CB_SUM --> MU & PROD
    CB_PROD --> MU & USERS & PROD
    CB_PROD -.->|"non-admin dept check"| DPM
    ASSIGN --> USERS
```

### Authorization for Chargeback

- **Summary** (`GET /budget/chargeback`): Admin or operator role only (`_require_admin_or_operator`).
- **Per-product** (`GET /budget/chargeback/{product_id}`): Admin (unrestricted) or director-level (`ad_level ≤ 3`) with department-product mapping check. Non-admins can only view chargeback for products mapped to their department.

---

## Request/Response Models

### `BudgetIncreaseRequest`

```python
class BudgetIncreaseRequest(BaseModel):
    user_id: str
    requested_extra_cost_usd: float    # Must be > 0, ≤ $200.00
    justification: str                 # Mandatory, ≤ 1000 chars
```

Validators enforce finite numeric values, positive amounts, the `$200` per-request ceiling (`_MAX_REQUEST_EXTRA_USD`), and non-empty justification within length limits.

### `HodCapUpsert`

```python
class HodCapUpsert(BaseModel):
    monthly_cap_usd: float    # Must be > 0
    notes: Optional[str]      # ≤ 1000 chars
```

### `WinnerAllocationBatch`

```python
class WinnerAllocationBatch(BaseModel):
    user_ids: List[str]       # 1–500 entries, deduplicated
```

### `ProductBudgetAssign`

```python
class ProductBudgetAssign(BaseModel):
    product_id: str
```

---

## Business Rule Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `_MAX_TOKENS_PER_DAY` | 50,000,000 | Hard ceiling on daily tokens |
| `_MAX_REQUESTS_PER_DAY` | 100,000 | Hard ceiling on daily requests |
| `_MAX_COST_USD_PER_DAY` | $10,000 | Hard ceiling on daily cost |
| `_MAX_COST_USD_PER_MONTH` | $100,000 | Hard ceiling on monthly cost |
| `_MAX_REQUEST_EXTRA_USD` | $200 | Max extra budget per increase request |
| `_WINNER_BASE_COST_USD` | $1,000 | 10x winner base allocation |
| `_ROSTER_DEFAULT_BASE_COST_USD` | $50 | Default base for unseeded roster rows |
| `_ROSTER_CACHE_TTL_SECONDS` | 45 | Roster cache TTL |

---

## Audit & Observability

### HOD Action Audit

Every write action performed by an HOD is logged via `_audit_hod_action()`:

```python
logger.info(
    "hod_action actor_email=%s target_user_id=%s action=%s hod_departments=%s",
    ...
)
```

Tracked actions: `reset_usage`, `approve_request`, `reject_request`.

### HOD Allocation Audit Endpoint

`GET /budget/admin/hod-audit` reads the append-only `hod_allocation_ledger` table, providing:
- **Admins**: All HOD ledger rows (optional `?hod_email` filter).
- **HODs**: Forced to their own `hod_email` (cannot read another HOD's spend).
- **Rollup**: Per-HOD aggregate (`total_increased_usd`, `allocation_count`, `distinct_users`).

### My Budget Increases

`GET /budget/my-increases` returns the calling user's own approved budget-increase history — mirroring what their HOD/admin can see via the audit endpoint, but scoped to the requester's own records.

---

## Integration with BudgetMiddleware

The `BudgetMiddleware` (see [middleware](middleware.md) documentation) intercepts every LLM-generating request and calls `check_budget(user_id)` from `budget_store` before allowing the request through. If the user's cumulative spend has reached their `max_cost_usd_total`, the middleware returns a `429 BUDGET_EXCEEDED` response and publishes an inbox notification.

```mermaid
flowchart LR
    REQ["HTTP Request to LLM endpoint"] --> MW["BudgetMiddleware.dispatch()"]
    MW --> CHECK{"check_budget(uid)"}
    CHECK -->|"allowed=True"| NEXT["call_next(request)"]
    CHECK -->|"allowed=False"| BLOCK["429 BUDGET_EXCEEDED<br/>+ inbox notification"]
    NEXT --> INCR["increment_usage(uid, requests=1)"]
    NEXT --> RESP["Response"]
```

Local/in-house models bypass the budget check entirely (no external API cost).

---

## Frontend Integration

The Budget Manager UI (`ai-ui/src/components/BudgetManager.jsx`) is the primary consumer of these endpoints. It renders three views based on the user's role:

```mermaid
graph TB
    BM["BudgetManager.jsx"]

    subgraph "View Switcher"
        MINE["My Budget<br/>(all users)"]
        TEAM["Team<br/>(HOD / reporting manager)"]
        ADMIN["Admin<br/>(admin only)"]
    end

    MINE --> M_EP["GET /budget/me<br/>GET /budget/me/utilization<br/>POST /budget/request-increase<br/>GET /budget/my-increases"]
    TEAM --> T_EP["GET /budget/team<br/>GET /budget/team/utilization<br/>GET /budget/hod/cap-status<br/>GET /budget/admin/hod-audit<br/>GET /budget/requests?scope=hod<br/>POST /budget/requests/{id}/approve<br/>POST /budget/requests/{id}/reject"]
    ADMIN --> A_EP["GET /budget/users<br/>GET /budget/summary<br/>GET /budget/admin/hods<br/>PUT /budget/admin/hods/{email}/cap<br/>POST /budget/admin/users/{id}/winner-allocation<br/>POST /budget/admin/winner-allocation/batch<br/>GET /budget/requests"]

    BM --> MINE & TEAM & ADMIN
```

The `TeamBudgetPanel` and `UtilizationView` components provide the team roster table and cost-breakdown pie charts respectively, consuming the `/budget/team` and `/budget/*/utilization` endpoints.

---

## Dependencies

### Internal Modules

| Module | Usage |
|--------|-------|
| [`store/budget_store`](store_layer.md) | Redis + Postgres budget/usage CRUD, increase request lifecycle |
| [`services/hod_budget_governor`](services.md) | HOD monthly cap enforcement and status |
| [`services/budget_audit_service`](services.md) | Monthly snapshot, reset, and pre-reset warnings |
| [`services/hierarchy_service`](services.md) | Org-tree subtree resolution for reporting managers |
| [`store/inbox_store`](store_layer.md) | In-app notification publishing |
| [`services/smtp_service`](services.md) | HTML email delivery for HOD/winner notifications |
| [`auth/rbac`](authentication.md) | Role checks (`is_admin`, `is_hod`), scope filters |
| [`auth/dependencies`](authentication.md) | `get_current_user` dependency |
| [`core/rate_limiter`](core_infrastructure.md) | `enforce_rate_limit_with_behaviour`, `BUDGET_REQUEST`, `BUDGET_ADMIN` |
| [`core/security_validation`](core_infrastructure.md) | `validate_budget_request` input sanitization |
| [`core/logger`](core_infrastructure.md) | Structured logging |
| [`core/config`](core_infrastructure.md) | `postgres_dsn()` |
| [`db/database`](database.md) | `SessionLocal` for SQLAlchemy sessions |
| [`db/models`](database.md) | `User`, `BudgetConfig`, `ModelRateTable` ORM models |
| [`middleware/budget_middleware`](middleware.md) | Per-request budget enforcement (consumes `check_budget`) |
| [`workers/start_workers`](worker_orchestration.md) | Cron threads for monthly reset and warning emails |

### External Systems

| System | Usage |
|--------|-------|
| **Redis** | Fast-path cache for budget configs, usage totals, daily usage, roster cache |
| **PostgreSQL** | Source of truth for `budget_configs`, `user_usage_totals`, `model_usages`, `hod_allocation_ledger`, `hod_allocation_caps`, `budget_period_audits`, `department_hod_mapping` |
| **SMTP Relay** | Email delivery for HOD approval notifications, winner notifications, pre-reset warnings |

---

## Key Database Tables

```mermaid
erDiagram
    budget_configs {
        UUID id PK
        STRING user_id UK
        INTEGER band_level
        FLOAT monthly_limit_usd
        NUMERIC base_cost_usd
        NUMERIC extra_cost_usd
        JSONB model_allowlist
        STRING updated_by
        TIMESTAMP created_at
        TIMESTAMP updated_at
    }

    user_usage_totals {
        STRING user_id PK
        BIGINT tokens_used
        BIGINT requests_made
        FLOAT cost_usd_spent
        TIMESTAMP last_updated
    }

    model_usages {
        UUID id PK
        STRING user_id
        UUID product_id
        STRING model
        STRING source_channel
        BIGINT input_tokens
        BIGINT output_tokens
        BIGINT total_tokens
        FLOAT cost_usd
        TIMESTAMP created_at
    }

    hod_allocation_ledger {
        UUID id PK
        STRING hod_email
        STRING period_yyyymm
        UUID target_user_id
        STRING action
        NUMERIC amount_usd
        NUMERIC previous_limit_usd
        NUMERIC new_limit_usd
        STRING request_id
        NUMERIC cap_at_time_usd
        NUMERIC consumed_after_usd
        BOOLEAN shadow_mode
        TEXT justification
        STRING status
        NUMERIC requested_extra_cost_usd
        STRING approved_by
        STRING approved_by_name
        TIMESTAMP resolved_at
        TIMESTAMP created_at
    }

    hod_allocation_caps {
        STRING hod_email PK
        NUMERIC monthly_cap_usd
        BOOLEAN is_active
        TEXT notes
        TIMESTAMP created_at
        TIMESTAMP updated_at
        STRING updated_by
    }

    budget_period_audits {
        UUID id PK
        STRING user_id
        STRING period_yyyymm
        JSONB closing_state_json
        JSONB increase_history_json
        TIMESTAMP created_at
    }

    department_hod_mapping {
        UUID id PK
        STRING department_name
        STRING hod_email
        STRING hod_name
    }

    budget_configs ||--o{ model_usages : "user_id"
    budget_configs ||--o{ user_usage_totals : "user_id"
    hod_allocation_caps ||--o{ hod_allocation_ledger : "hod_email"
    department_hod_mapping ||--o{ hod_allocation_caps : "hod_email"
```

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUDGET_MONTHLY_RESET_ENABLED` | `false` | Master gate for monthly reset and warning features |
| `BUDGET_MONTHLY_RESET_CRON` | `15 3 1 * *` | Cron schedule for monthly reset (UTC) |
| `BUDGET_MONTHLY_RESET_WARNING_CRON` | `15 3 28-31 * *` | Cron schedule for pre-reset warnings (UTC) |
| `HOD_CAP_ENFORCEMENT_ENABLED` | `false` | Toggle between live (409 on overrun) and shadow mode |
| `HOD_DEFAULT_MONTHLY_CAP_USD` | — | Fallback cap when no `hod_allocation_caps` row exists |
| `BUDGET_RESET_EMAIL_TEST_OVERRIDE` | — | Route all reset-warning emails to a single inbox for testing |
