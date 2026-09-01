# connectors_integrations Module Overview

## Purpose

The `connectors_integrations` module is the platform's unified integration layer for external services. It provides a secure, observable, and compliant runtime for connecting agents and applications to third-party APIs — including SaaS tools (Microsoft 365, Google Workspace, Slack, Jira, GitLab, Confluence, Zoom, DocuSign), email/calendar services, and India-specific DPI consent-based data sources (Account Aggregator, DigiLocker).

The module separates **provider-specific adapter logic** from **shared connector infrastructure**, ensuring every external tool call follows the same guardrails: schema validation, OAuth/PAT/consent token management, rate limiting, caching, pagination, compliance scanning, and MCP tool registration.

## Architecture

### High-Level Component Diagram

```mermaid
flowchart TB
    subgraph Callers["Agent / Chat / Cowork / API"]
        A[LLM Agent]
        C[Cowork Desktop MCP Client]
        R[connectors_router]
    end

    subgraph Connectors["connectors_integrations"]
        subgraph Infra["connector_infrastructure"]
            REG[ConnectorRegistry]
            ENG[ConnectorEngine]
            OAUTH[OAuth2Handler]
            MET[ConnectorMetrics]
            MCP[MCP Bridge]
        end

        subgraph Adapters["connector_adapters"]
            ADP1[Microsoft365Adapter]
            ADP2[GitLabAdapter]
            ADP3[SlackAdapter]
            ADP4[GmailAdapter]
            ADP5[JiraAdapter]
            ADP6[GoogleDriveAdapter]
            ADP7[ConfluenceAdapter]
            ADP8[ZoomAdapter]
            ADP9[DocuSignAdapter]
            ADP10[DpiAccountAggregatorAdapter]
            ADP11[DpiDigilockerAdapter]
        end

        DPI[ConsentHandler<br/>DPI Consent]
    end

    subgraph External["External Services"]
        MS[Microsoft Graph]
        GL[GitLab API]
        SL[Slack API]
        GD[Google APIs]
        DPI_API[DPI AA / DigiLocker]
    end

    subgraph Stores["Data Stores"]
        DB[(Postgres)]
        KV[(Redis / KV)]
        VLT[(Credential Vault)]
    end

    A -->|tool_use| REG
    C -->|JSON-RPC| MCP
    R -->|REST| REG
    REG -->|execute| ENG
    MCP -->|execute| ENG
    ENG -->|load token / definition| DB
    ENG -->|cache / rate limit| KV
    ENG -->|decrypt / encrypt| VLT
    ENG -->|refresh token| OAUTH
    ENG -->|verify consent| DPI
    ENG -->|adapter call| ADP1
    ENG -->|adapter call| ADP2
    ENG -->|adapter call| ADP3
    ADP1 -->|HTTP| MS
    ADP2 -->|HTTP| GL
    ADP3 -->|HTTP| SL
    ADP10 -->|HTTP / sandbox| DPI_API
    ENG -->|record| MET
```

### Execution Pipeline

```mermaid
sequenceDiagram
    participant Caller as Agent / Cowork
    participant Reg as ConnectorRegistry
    participant Eng as ConnectorEngine
    participant Auth as OAuth2Handler / ConsentHandler
    participant Adp as Adapter
    participant API as External API

    Caller->>Reg: execute(connector, tool, params)
    Reg->>Eng: execute(...)
    Eng->>Eng: load connector definition
    Eng->>Auth: load / refresh token or verify consent
    Eng->>Eng: validate params & enforce scopes
    Eng->>Eng: check rate limit & cache
    Eng->>Adp: execute with retry + pagination
    Adp->>API: HTTP request
    API-->>Adp: response
    Adp-->>Eng: normalized page
    Eng->>Eng: compliance scan & data minimization
    Eng-->>Caller: ConnectorResponse
```

## Core Components

| Sub-module | Key Components | Documentation |
|---|---|---|
| **connector_infrastructure** | `ConnectorEngine`, `ConnectorRegistry`, `OAuth2Handler`, `ConnectorMetrics`, `MCPBridge` | [connector_infrastructure_engine](connector_infrastructure_engine.md), [connector_infrastructure_registry](connector_infrastructure_registry.md), [connector_infrastructure_oauth2](connector_infrastructure_oauth2.md), [connector_infrastructure_metrics](connector_infrastructure_metrics.md), [connector_infrastructure_mcp_bridge](connector_infrastructure_mcp_bridge.md) |
| **connector_adapters** | `Microsoft365Adapter`, `GitLabAdapter`, `JiraAdapter`, `SlackAdapter`, `GmailAdapter`, `GoogleDriveAdapter`, `ConfluenceAdapter`, `ZoomAdapter`, `DocuSignAdapter`, `DpiAccountAggregatorAdapter`, `DpiDigilockerAdapter` | [connector_adapters](connector_adapters.md) |
| **dpi_consent** | `ConsentHandler` | Documented within [connector_infrastructure.md](connector_infrastructure.md) and the DPI Consent module docs |

## Integration Points

- **MCP System**: Connector tools are registered as `{connector}__{tool}` and exposed to agents via the MCP bridge.
- **Authentication**: Tokens are encrypted in the credential vault; OAuth flows use `OAuth2Handler`.
- **Compliance**: Connector outputs are scanned before being injected into LLM context.
- **Cowork Desktop**: The MCP bridge exposes user-scoped connector tools over SSE JSON-RPC.
- **Knowledge Base**: The bridge can query the platform knowledge base via the retrieval tool.