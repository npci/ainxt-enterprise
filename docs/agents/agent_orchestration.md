# Agent Orchestration Module

## Overview

The `agent_orchestration` module is the central coordination layer of the NPCI AiNxt agentic engineering platform. It is responsible for receiving a user request, deciding how to fulfill it, invoking the right retrieval and tool-execution paths, and producing a streamed or batched response. The module sits between the gateway/frontend entry points and the lower-level tool, retrieval, compliance, and model-routing subsystems.

At a high level, the module provides four complementary execution styles:

1. **Planner-based orchestration** (`OrchestratorAgent`) — decomposes a request into a short plan (retrieve, connector call, local MCP call, generate) and executes it iteratively.
2. **ReAct tool-use loop** (`ReactOrchestrator`) — lets a frontier model decide which tools to call, observes the results, and iterates until the goal is achieved or a round limit is reached.
3. **Domain-specialist multi-agent routing** (`MultiAgentRunner` + `RouterAgent`) — classifies the request by domain and dispatches it to a specialist agent with a tailored system prompt and tool subset.
4. **Structured handoff** (`HandoffContext`) — carries state between agents so delegated work does not have to re-retrieve context.

The module is intentionally layered: simple or general questions take a fast `generate` path, repo-scoped code questions use deterministic retrieval, office/Cowork questions prefer connector-backed tools, and action-oriented tasks fall through to the ReAct loop.

## Architecture

```mermaid
flowchart TB
    subgraph Entry["Request Entry"]
        Gateway["gateway.py / chat_worker.py"]
    end

    subgraph AgentOrchestration["Agent Orchestration"]
        direction TB
        Router["RouterAgent<br/>semantic agent router"]
        Orchestrator["OrchestratorAgent<br/>planner + iterative executor"]
        React["ReactOrchestrator<br/>frontier-model ReAct loop"]
        MultiAgent["MultiAgentRunner<br/>domain specialist dispatch"]
        Handoff["HandoffContext<br/>structured context carrier"]
    end

    subgraph Capabilities["Capabilities & Data Sources"]
        Retrieve["retrieve_tool / hybrid_retriever"]
        Connectors["connector_registry"]
        LocalMCP["desktop_router / local MCP"]
        GitLab["gitlab_tools"]
        Jira["jira_tools"]
        Compliance["compliance_engine"]
        ModelRouter["models.model_router"]
    end

    Gateway --> Router
    Router -->|matched agent| MultiAgent
    Router -->|no match| Orchestrator
    Orchestrator -->|task-mode or action request| React
    Orchestrator -->|simple / repo code / office| Capabilities
    React --> Capabilities
    MultiAgent --> React
    MultiAgent --> Capabilities
    Handoff -.->|context reuse| MultiAgent
    Handoff -.->|context reuse| React
```

### Component Interaction

```mermaid
sequenceDiagram
    participant U as User / Gateway
    participant R as RouterAgent
    participant O as OrchestratorAgent
    participant Ro as ReactOrchestrator
    participant M as MultiAgentRunner
    participant T as Tools / Connectors / Retrieval

    U->>R: question + user_ctx
    alt production agent match
        R->>M: route to specialist
        M->>M: classify domain
        M->>Ro: run with domain prompt + tool subset
        Ro->>T: tool-use loop
        T-->>Ro: observations
        Ro-->>M: answer
        M-->>U: answer
    else no agent match
        O->>O: plan(question)
        alt office mode
            O->>T: connector_call / retrieve / generate
        else local filesystem query
            O->>T: local_mcp_call
        else repo-scoped code
            O->>T: retrieve + generate
        else task/action request
            O->>Ro: ReAct loop
            Ro->>T: tool-use loop
            T-->>Ro: observations
            Ro-->>O: answer
        end
        O-->>U: streamed answer
    end
```

## Sub-modules

| Sub-module | File(s) | Responsibility | Documentation |
|------------|---------|----------------|---------------|
| Core Orchestration | `agents/orchestrator.py` | Planner, iterative executor, routing guard, compliance gating, streaming generation | agent_orchestration_core |
| ReAct Orchestration | `agents/react_orchestrator.py` | Frontier-model ReAct loop, tool schemas, verifier/critique/recovery, confidence scoring | agent_orchestration_react |
| Multi-Agent Routing | `agents/multi_agent_runner.py`, `agents/router_agent.py` | Domain classification, specialist dispatch, semantic agent routing | agent_orchestration_multi_agent |
| Agent Handoff | `agents/handoff.py` | Structured context carrier for agent-to-agent delegation | agent_orchestration_handoff |

## High-Level Data Flow

```mermaid
flowchart LR
    A[User request] --> B{RouterAgent?}
    B -->|production agent| C[MultiAgentRunner]
    B -->|no match| D[OrchestratorAgent]
    D --> E{Mode / domain}
    E -->|office| F[Connector-first planner]
    E -->|filesystem| G[local_mcp_call]
    E -->|repo code| H[retrieve + generate]
    E -->|task/action| I[ReactOrchestrator]
    C --> I
    I --> J[Tool executor]
    J --> K[RAG, GitLab, Jira, run_code, compliance]
    K --> J
    J --> I
    I --> L[Verifier + critique + recovery]
    L --> M[Final answer]
    F --> M
    G --> M
    H --> M
```

## Key Design Decisions

- **Connectors-first for office mode**: Work-system vocabulary ("my open MRs", "tickets assigned to me") is routed to connector-backed tools before any local filesystem or generic retrieval path. This prevents common verbs like `show` or `find` from being misclassified as file operations.
- **Deterministic fast paths**: Repo-scoped code questions and local filesystem questions skip LLM planning when the intent is unambiguous, reducing latency and cost.
- **Risk-aware depth**: The ReAct loop and verifier use a query-risk classifier (`HIGH`, `MEDIUM`, `LOW`) to decide how many verification/recovery iterations to run and what confidence threshold is required before answering.
- **Provider fallback**: The ReAct loop tries Claude, then OpenAI, then Gemini so that a single provider outage does not break action-mode requests.
- **Compliance at boundaries**: User input is scanned before planning, and generated output is monitored during streaming. Retrieved code chunks are not re-scanned to avoid false positives from legitimate source patterns.
- **Memory extraction**: Successful runs extract tool sequences and design patterns into semantic memory so that similar future queries can benefit from prior experience.

## Integration Points

| External module | How it is used |
|-----------------|----------------|
| [model_routing](../llm/model_routing.md) | `models.model_router` and `models.classifier` select the model tier and classify query complexity/domain. |
| [core_infrastructure](../core/core_infrastructure.md) | `core.logger`, `core.telemetry`, `core.config`, `core.evals`, and `core.context_compressor` provide logging, tracing, evaluation, and context compression. |
| [shared_integrations](../skills/shared_integrations.md) | `connectors.registry`, `connectors.engine`, and `tools.gitlab_tools` / `tools.jira_tools` execute connector and GitLab/Jira calls. |
| [memory_system](../storage/memory_system.md) | `memory.postgres_memory` stores tool sequences and retrieves hints for future runs. |
| [advanced_reasoning](../llm/advanced_reasoning.md) | `TreeOfThoughts`, `SelfConsistency`, and `ChainOfVerification` are invoked for high-risk or low-confidence runs. |
| recovery_engine | `agents.recovery_engine` records reversible write actions and loads ReAct checkpoints. |
| compliance_engine | `agents.compliance_engine` scans user input and tool results for PCI/PII violations. |

## Operational Notes

- The module is stateless across requests; per-run state is held in `AgentState` and `HandoffContext`.
- Streaming responses are produced by `OrchestratorAgent.run()` as a Python generator; the gateway consumes the generator and forwards SSE chunks to clients.
- ReAct runs are internally non-streaming because frontier-model tool-use APIs return complete tool-call blocks; the orchestrator yields the final answer in chunks for SSE compatibility.
- Environment flags that affect behavior:
  - `ADAPTIVE_LOOP_DEPTH` (default `true`) — derives the iteration ceiling from task complexity.
  - `ADVANCED_REASONING_ENABLED` (default `false`) — enables Tree-of-Thoughts / Self-Consistency / Chain-of-Verification paths.
