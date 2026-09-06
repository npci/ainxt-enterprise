# Atlassian Connector Adapters (Confluence & Jira)

> **Module ID:** `shared_integrations_connector_adapters_atlassian`
> **Source files:** `connectors/adapters/confluence.py`, `connectors/adapters/jira.py`
> **Parent module:** [shared_integrations_connector_adapters](shared_integrations_connector_adapters.md)

## Overview

This module provides two custom connector adapters that integrate **Atlassian Cloud** services — **Jira** and **Confluence** — into the platform's unified connector execution pipeline. Both adapters subclass `AdapterBase` and are discovered by `ConnectorEngine` as module-level singletons (`confluence_adapter`, `jira_adapter`).

| Adapter | File | Atlassian API | HTTP strategy |
|---|---|---|---|
| `ConfluenceAdapter` | `connectors/adapters/confluence.py` | Confluence Cloud REST API v1 | Direct `httpx` calls |
| `JiraAdapter` | `connectors/adapters/jira.py` | Jira Cloud REST API v3 | Delegates to `tools/jira_tools.py` (shared SDLC client) |

The two adapters follow the same contract but differ in implementation philosophy:

- **ConfluenceAdapter** implements HTTP calls, CQL query building, pagination, and response normalization directly — it is self-contained.
- **JiraAdapter** is a thin dispatch layer that injects per-user credentials and delegates all HTTP work to `tools/jira_tools.py`. This ensures the connector path and the SDLC pipeline share identical code for proxy relay, circuit breaking, retry, and tracing.

## Architecture

```mermaid
graph TB
    subgraph "Caller Layer"
        CE["ConnectorEngine.execute()"]
    end

    subgraph "Atlassian Adapter Module"
        CA["ConfluenceAdapter"]
        JA["JiraAdapter"]
    end

    subgraph "Shared Infrastructure"
        AB["AdapterBase / AdapterPage"]
        CC["ConnectorContext / ConnectorTool"]
        PC["extract_atlassian_creds()"]
    end

    subgraph "Tool Clients"
        JT["tools/jira_tools.py"]
        CT["tools/confluence_tools.py<br/>(SDLC path — not used by adapter)"]
    end

    subgraph "External"
        AC["Atlassian Cloud<br/>(Jira + Confluence REST)"]
        LP["LLM Proxy<br/>/atlassian/proxy"]
    end

    CE -->|"selects adapter by<br/>connector_name"| CA
    CE --> JA
    CA -.->|"extends"| AB
    JA -.->|"extends"| AB
    CA -->|"httpx GET/POST"| AC
    JA -->|"set_credentials()"| JT
    JA --> PC
    JT -->|"POST /atlassian/proxy"| LP
    LP --> AC
    CT -.->|"parallel SDLC path"| AC

    style CA fill:#4a90d9,color:#fff
    style JA fill:#d94a4a,color:#fff
    style CE fill:#50c878,color:#fff
```

### Class relationships

```mermaid
classDiagram
    class AdapterBase {
        <<abstract>>
        +TIMEOUT
        +execute(tool, params, context, cursor) AdapterPage
        +build_headers(context) dict
        +_resolve_path(path, params) tuple
    }

    class ConfluenceAdapter {
        +TIMEOUT = 25
        +execute(tool, params, context, cursor) AdapterPage
        -_base_url(context) str
        -_fetch_cursor(base, cursor, tool, context) AdapterPage
        -_build_query_params(tool, remaining) dict
        -_build_write_body(tool, remaining) dict
        -_extract_items(data, tool) list
        -_normalize_page(item, include_body, created) dict
    }

    class JiraAdapter {
        -_TOOL_MAP dict
        -_ALLOWED_PARAMS dict
        +execute(tool, params, context, cursor) AdapterPage
        -_dispatch(tool_name, params, cursor) AdapterPage
        -_normalise_params(tool_name, params) dict
    }

    class AdapterPage {
        +items: list~dict~
        +next_cursor: Optional~str~
        +meta: dict
    }

    AdapterBase <|-- ConfluenceAdapter
    AdapterBase <|-- JiraAdapter
    AdapterBase ..> AdapterPage : returns
    ConfluenceAdapter ..> AdapterPage : returns
    JiraAdapter ..> AdapterPage : returns
```

## Core Components

### ConfluenceAdapter

**File:** `connectors/adapters/confluence.py`
**Singleton:** `confluence_adapter = ConfluenceAdapter()`

A self-contained adapter for the Confluence Cloud REST API. It handles Confluence-specific quirks directly:

- **CQL (Confluence Query Language)** search via `/content/search`
- **Cursor pagination** using the `_links.next` relative-URL convention
- **HTML storage body** extraction for page content (`body.storage`)
- **Create-page** request shaping with optional ancestor (parent page) support

#### Supported tools

| Tool name | Method | Description |
|---|---|---|
| `confluence_search_pages` | GET | Search pages via CQL; auto-builds CQL from `query`/`space_key` if raw `cql` not supplied |
| `confluence_get_page` | GET | Retrieve a single page by ID with full `body.storage` content |
| `confluence_create_page` | POST | Create a page in a space with title, body, and optional `parent_id` |

#### Key behaviours

- **Base URL resolution:** `_base_url()` reads `context.metadata["base_url"]`, falling back to the `CONFLUENCE_BASE` constant.
- **Pagination:** For GET requests with a cursor, `_fetch_cursor()` follows the `_links.next` relative path (or absolute URL). `_next_cursor()` extracts the next token from the response.
- **Query building:** `_build_query_params()` constructs CQL from simple params (`query`, `space_key`) when raw CQL is not provided, and sets `expand` fields appropriate to each tool.
- **Write body:** `_build_write_body()` shapes `confluence_create_page` params into the Confluence create-page JSON structure (`type`, `title`, `space.key`, `body.storage`, optional `ancestors`).
- **Normalization:** `_normalize_page()` flattens Confluence's nested content object into a flat dict with `id`, `title`, `space_key`, `version`, `url`, and optionally `body`/`body_format`.
- **Error handling:** HTTP 401 raises `ConnectorReauthRequired` (engine deactivates token and prompts reconnect). HTTP 429/5xx are re-raised so the engine's retry/backoff handles them. HTTP 400/403/404 are re-raised as fatal. Response bodies are never logged to avoid token leakage through proxy echoes.

### JiraAdapter

**File:** `connectors/adapters/jira.py`
**Singleton:** `jira_adapter = JiraAdapter()`

A thin dispatch layer over `tools/jira_tools.py`. It does **not** make HTTP calls directly. Instead, it:

1. Extracts the per-user Atlassian credential (`email:api_token`) from `context.access_token` via `extract_atlassian_creds()`.
2. Injects credentials into `jira_tools`'s thread-local via `set_credentials()`.
3. Maps the connector tool name to the matching `jira_tools` function via `_TOOL_MAP`.
4. Normalizes the return value into an `AdapterPage`.
5. **Always** clears credentials in a `finally` block to prevent cross-request leakage in thread-pooled workers.

#### Why delegate instead of calling HTTP directly

`jira_tools._request()` carries four production-critical behaviours that a raw `httpx` call cannot replicate:

| Behaviour | Detail |
|---|---|
| **Proxy relay** | Routes through `POST {LLM_PROXY_URL}/atlassian/proxy` — Atlassian Cloud is reachable only from `web02` in production; direct calls from `app02` fail |
| **Circuit breaker** | Uses `get_breaker("jira")` to short-circuit during outages |
| **Retry with backoff** | `retry_llm()` handles 429/5xx with exponential backoff |
| **Correlation** | `request_id` / `chat_id` tracing links Jira calls back to the originating `/ask` or SDLC run |

#### Tool dispatch map (`_TOOL_MAP`)

| Connector tool | `jira_tools` function | Read/Write | Returns list |
|---|---|---|---|
| `jira_get_current_user` | `jira_get_current_user` | Read | No |
| `jira_search_issues` | `jira_search_issues` | Read | Yes |
| `jira_get_issue` | `jira_get_issue_dict` | Read | No |
| `jira_list_projects` | `jira_list_projects` | Read | Yes |
| `jira_get_project` | `jira_get_project` | Read | Yes |
| `jira_get_transitions` | `jira_get_transitions` | Read | Yes |
| `jira_list_comments` | `jira_list_comments` | Read | Yes |
| `jira_count_issues` | `jira_count_issues` | Read | No |
| `jira_create_issue` | `jira_create_issue` | Write | No |
| `jira_add_comment` | `jira_add_comment` | Write | No |
| `jira_update_issue` | `jira_update_issue` | Write | No |
| `jira_transition_issue` | `jira_transition_issue` | Write | No |
| `jira_assign_issue` | `jira_assign_issue` | Write | No |

> **Note:** `_TOOL_MAP` must stay in sync with the tool list in `connectors/seed.py` and the DB row in `ainxt.connector_definitions`. A tool listed in seed but unmapped here raises `"unknown tool"`; a mapped tool missing from seed is unreachable.

#### Parameter normalization

`_normalise_params()` bridges the connector schema (LLM-facing param names) and `jira_tools` function signatures:

- `project_key` → `project` (for `jira_create_issue`)
- `assignee_account_id` → `account_id` (for `jira_assign_issue`)
- Issue keys are uppercased (Jira is case-sensitive on some endpoints)
- `description` defaults to `""` for `jira_create_issue` (required by the function, optional in the tool schema)
- Unsupported params are dropped via `_ALLOWED_PARAMS` to prevent `TypeError` from hallucinated extra arguments
- `user_id` / `user_email` are **never** accepted from caller params — credentials arrive exclusively through the thread-local

#### Pagination

Only `jira_search_issues` supports cursor pagination. The `jira_tools` function returns `{"issues": [...], "next_cursor": str|None}` using Jira's `nextPageToken` convention. The adapter passes the engine-supplied `cursor` through as `call_params["cursor"]`.

## Data Flow

### Jira tool execution flow

```mermaid
sequenceDiagram
    participant Caller as Agent / SDLC / MCP
    participant CE as ConnectorEngine
    participant JA as JiraAdapter
    participant JT as jira_tools
    participant LP as LLM Proxy<br/>/atlassian/proxy
    participant AC as Atlassian Cloud

    Caller->>CE: execute("jira", "jira_search_issues", params, user_id)
    CE->>CE: Load definition from DB
    CE->>CE: Validate params against schema
    CE->>CE: Fetch + decrypt token (PAT)
    CE->>CE: Build ConnectorContext
    CE->>JA: adapter.execute(tool, params, context, cursor)
    JA->>JA: extract_atlassian_creds(context.access_token)
    JA->>JT: set_credentials(email, token)
    JA->>JT: jira_search_issues(jql, fields, limit, cursor)
    JT->>LP: POST /atlassian/proxy (with circuit breaker + retry)
    LP->>AC: Jira REST /rest/api/3/search/jql
    AC-->>LP: {issues: [...], nextPageToken: "..."}
    LP-->>JT: JSON response
    JT-->>JA: {issues: [...], next_cursor: "..."}
    JA-->>JA: Wrap into AdapterPage
    JA->>JT: clear_credentials() [finally]
    JA-->>CE: AdapterPage
    CE->>CE: Compliance check (PCI/PII)
    CE->>CE: Data minimization
    CE->>CE: Cache result (if not partial)
    CE-->>Caller: ConnectorResponse
```

### Confluence tool execution flow

```mermaid
sequenceDiagram
    participant Caller as Agent / MCP
    participant CE as ConnectorEngine
    participant CA as ConfluenceAdapter
    participant AC as Atlassian Cloud

    Caller->>CE: execute("confluence", "confluence_search_pages", params, user_id)
    CE->>CE: Load definition from DB
    CE->>CE: Validate params + fetch token
    CE->>CA: adapter.execute(tool, params, context, cursor)
    CA->>CA: _base_url(context) → resolve site URL
    CA->>CA: build_headers(context) → auth headers
    CA->>CA: _build_query_params() → CQL + expand + limit
    CA->>AC: httpx GET /wiki/rest/api/content/search
    AC-->>CA: {results: [...], _links: {next: "..."}}
    CA->>CA: _extract_items() → _normalize_page()
    CA-->>CE: AdapterPage(items, next_cursor, meta)
    CE->>CE: Compliance + minimization + cache
    CE-->>Caller: ConnectorResponse
```

## Authentication & Security

### Jira (PAT — email + API token)

Jira Cloud authenticates with `Authorization: Basic base64(email:api_token)`. The credential lifecycle:

```mermaid
flowchart LR
    subgraph "Storage"
        DB[("user_oauth_tokens<br/>encrypted 'email:api_token'")]
        PV[("user_tokens (profile vault)<br/>token_type='atlassian'")]
    end

    subgraph "Engine"
        CE["ConnectorEngine._get_token_row()"]
        AC2["_try_auto_connect_pat()"]
        CV["decrypt_value()"]
    end

    subgraph "Adapter"
        EA["extract_atlassian_creds()"]
        SC["set_credentials()"]
        CC2["clear_credentials()"]
    end

    DB -->|encrypted access_token| CV
    PV -->|fallback if no row| AC2
    AC2 -->|write row| DB
    CV -->|plaintext email:token| EA
    EA -->|email, token| SC
    SC -->|thread-local| JT["jira_tools._request()"]
    JT --> CC2
```

Key security properties:

- **Thread-local isolation:** Credentials are set per-thread and always cleared in a `finally` block — no leakage across concurrent requests in thread-pooled workers.
- **No caller-supplied identity:** `_ALLOWED_PARAMS` explicitly excludes `user_id` / `user_email` — credentials arrive only through the thread-local, never from LLM-generated params.
- **Auto-connect from profile vault:** If no `user_oauth_tokens` row exists, the engine attempts to read the user's stored Atlassian token from the profile vault (`user_tokens` table) and auto-create the connection. See [shared_integrations_connector_infrastructure](shared_integrations_connector_infrastructure.md).
- **401/403 handling:** Both adapters translate authentication failures into `ConnectorReauthRequired`, which causes the engine to deactivate the stored token and surface a reconnect prompt.

### Confluence (OAuth2 / token-based)

`ConfluenceAdapter` relies on `AdapterBase.build_headers(context)` to construct auth headers. The base class supports OAuth2 Bearer (default) and PAT variants (`Basic`, raw token). The adapter itself does not handle credential extraction — it trusts the engine to deliver a valid `context.access_token`.

## Error Handling Strategy

| Scenario | ConfluenceAdapter | JiraAdapter | Engine response |
|---|---|---|---|
| HTTP 401 | Raises `ConnectorReauthRequired` | Detects `"401"`/`"403"` in exception → raises `ConnectorReauthRequired` | Deactivates token; returns `REAUTH_REQUIRED` |
| HTTP 403 | Re-raised (fatal) | Raises `ConnectorReauthRequired` | Deactivates token; returns `REAUTH_REQUIRED` |
| HTTP 429 / 5xx | Re-raised | Handled inside `jira_tools` (retry + circuit breaker) | Engine retries with backoff |
| HTTP 400 / 404 | Re-raised (fatal) | Re-raised | Returns error to caller |
| Missing token | N/A (engine checks first) | Raises `ConnectorReauthRequired` | Returns `REAUTH_REQUIRED` |
| Unknown tool | N/A | Raises `ValueError` | Returns error to caller |

## Dependencies

```mermaid
graph LR
    subgraph "This module"
        CA[ConfluenceAdapter]
        JA[JiraAdapter]
    end

    subgraph "Connector infrastructure"
        AB[AdapterBase / AdapterPage]
        CB[ConnectorContext / ConnectorTool / ConnectorReauthRequired]
        CE[ConnectorEngine]
    end

    subgraph "Shared core"
        CL["core.logger"]
        PC["core.platform_credentials<br/>extract_atlassian_creds()"]
    end

    subgraph "Tool clients"
        JT["tools/jira_tools.py"]
    end

    subgraph "External"
        HTTPX["httpx"]
        LP["LLM Proxy<br/>atlassian_proxy"]
    end

    CA --> AB
    CA --> CB
    CA --> CL
    CA --> HTTPX
    JA --> AB
    JA --> CB
    JA --> CL
    JA --> PC
    JA --> JT
    JT --> LP
    CE --> CA
    CE --> JA
```

### Internal dependencies

| Dependency | Purpose |
|---|---|
| [`connectors/adapters/base.py`](shared_integrations_connector_infrastructure.md) — `AdapterBase`, `AdapterPage` | Abstract base class and page result container |
| [`connectors/base.py`](shared_integrations_connector_infrastructure.md) — `ConnectorContext`, `ConnectorTool`, `ConnectorReauthRequired` | Runtime context, tool definition, reauth exception |
| [`connectors/engine.py`](shared_integrations_connector_infrastructure.md) — `ConnectorEngine` | Loads adapters via `_load_custom_adapter()`, handles retry/pagination/compliance |
| `tools/jira_tools.py` — `set_credentials`, `clear_credentials`, 13 tool functions | Shared Jira HTTP client with proxy, circuit breaker, retry |
| `core/platform_credentials.py` — `extract_atlassian_creds()` | Splits stored `email:api_token` credential |
| `core/logger.py` — `logger` | Structured logging (no token/secret logging) |

### External dependencies

| Dependency | Purpose |
|---|---|
| `httpx` | HTTP client for Confluence direct calls |
| [LLM Proxy `atlassian_proxy`](../llm/llm_proxy_main.md) | Production egress relay for Jira calls (reachable only from `web02`) |

## System Integration

### Where these adapters fit

```mermaid
graph TB
    subgraph "Entry points"
        CR["Connectors Router<br/>/api/connectors/*"]
        MCP["MCP Servers<br/>JiraMCPServer / ConfluenceMCPServer"]
        SDLC["SDLC Pipeline<br/>(uses jira_tools directly)"]
        AGENT["Agent / Cowork runtime"]
    end

    subgraph "Connector layer"
        CE["ConnectorEngine"]
        JA["JiraAdapter"]
        CA["ConfluenceAdapter"]
    end

    subgraph "Tool layer"
        JT["tools/jira_tools.py"]
        CT["tools/confluence_tools.py"]
    end

    CR --> CE
    MCP --> CE
    AGENT --> CE
    CE --> JA
    CE --> CA
    JA --> JT
    SDLC --> JT
    SDLC --> CT

    style JA fill:#d94a4a,color:#fff
    style CA fill:#4a90d9,color:#fff
```

- **Connectors Router** (`routers/connectors_router.py`) — manages connector definitions, OAuth/PAT connections, permissions, and execution. See shared_api_routers_connectors_router.
- **MCP Servers** — `JiraMCPServer` and `ConfluenceMCPServer` expose Jira/Confluence tools to LLM agents via the MCP protocol. See [mcp_servers](../mcp/mcp_servers.md).
- **SDLC Pipeline** — uses `tools/jira_tools.py` directly (not through the adapter) for governance workflows, PR reviews, and issue linking. The Jira adapter's delegation design ensures both paths share the same HTTP client.
- **ConnectorEngine** — the central orchestrator that loads definitions, validates params, manages tokens, enforces scopes/rate limits, handles retry/pagination, and runs compliance checks. See [shared_integrations_connector_infrastructure](shared_integrations_connector_infrastructure.md).

### Adapter discovery

`ConnectorEngine._load_custom_adapter()` maps connector names to adapter modules:

| Connector name(s) | Module path | Singleton |
|---|---|---|
| `jira`, `jira_connector` | `connectors.adapters.jira` | `jira_adapter` |
| `confluence` | `connectors.adapters.confluence` | `confluence_adapter` |

The engine uses the convention `connector_name.replace("-", "_") + "_adapter"` to find the module-level singleton, falling back to scanning for any `AdapterBase` instance.

## Related Documentation

- [shared_integrations_connector_adapters](shared_integrations_connector_adapters.md) — parent module covering all connector adapters
- [shared_integrations_connector_infrastructure](shared_integrations_connector_infrastructure.md) — `ConnectorEngine`, `ConnectorRegistry`, `OAuth2Handler`, `ConnectorMetrics`
- shared_integrations_jira_tools — shared Jira HTTP client used by both the adapter and SDLC pipeline
- shared_integrations_confluence_tools — standalone Confluence tool functions (SDLC path)
- [llm_proxy_main](../llm/llm_proxy_main.md) — LLM proxy service including the `atlassian_proxy` endpoint
- shared_api_routers_connectors_router — REST API for connector management
- [mcp_servers](../mcp/mcp_servers.md) — MCP server implementations for Jira and Confluence
