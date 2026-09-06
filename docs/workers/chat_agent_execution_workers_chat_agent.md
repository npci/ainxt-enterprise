# Chat & Agent Execution Workers — Chat Agent

## Introduction

The `chat_agent_execution_workers_chat_agent` module is the **core asynchronous execution layer** for user-facing chat and agent interactions in the AiNxt platform. It contains two RQ (Redis Queue) job functions that are enqueued by the [gateway](#gateway) and consumed by background worker processes:

| Component | File | Role |
|---|---|---|
| `run_agent_job` | `workers/agent_worker.py` | Thin RQ wrapper that delegates to `AgentRunner.run()` for named-agent executions. |
| `run_chat_job` | `workers/chat_worker.py` | Full chat pipeline: compliance gating, PII masking, conversation history, RAG retrieval, LLM streaming, caching, memory persistence, and document-generation routing. |

Both functions are designed to run inside worker processes spawned by the [worker orchestration](#worker-orchestration) layer (`workers/start_workers.py`). They communicate results back to the gateway via **Redis Streams** (SSE token streaming) and **Kafka** (durable persistence).

---

## Architecture Overview

```mermaid
graph TB
    subgraph Gateway
        GW["gateway.py<br/>FastAPI app"]
    end

    subgraph "Job Queue (Redis / RustyCluster)"
        Q_CHAT["chat_queue"]
        Q_AGENT["agent_queue"]
        Q_DOC["doc_queue"]
    end

    subgraph "Worker Processes (start_workers.py)"
        W_CHAT["Chat Worker<br/>--chat"]
        W_AGENT["Agent Worker<br/>--agent"]
    end

    subgraph "This Module"
    run_chat_job["run_chat_job<br/>Full chat pipeline"]
    run_agent_job["run_agent_job<br/>Agent execution wrapper"]
    end

    subgraph "Downstream Services"
    AR["AgentRunner<br/>(agents/agent_builder.py)"]
    MR["ModelRouter<br/>(models/model_router.py)"]
    CE["ComplianceEngine<br/>(agents/compliance_engine.py)"]
    RM["RedisMemory<br/>(memory/redis_memory.py)"]
    HS["HybridSearch<br/>(models/hybrid_search.py)"]
    DW["doc_worker / doc_worker_agent<br/>(document generation)"]
    end

    subgraph "Output Sinks"
    RS["Redis Stream<br/>chat:stream:{job_id}"]
    KAFKA["Kafka<br/>TOPIC_CHAT_HISTORY<br/>TOPIC_METRICS"]
    end

    GW -->|"enqueue_job"| Q_CHAT
    GW -->|"enqueue_job"| Q_AGENT
    Q_CHAT --> W_CHAT
    Q_AGENT --> W_AGENT
    W_CHAT --> run_chat_job
    W_AGENT --> run_agent_job

    run_agent_job --> AR
    run_chat_job --> CE
    run_chat_job --> RM
    run_chat_job --> MR
    run_chat_job --> HS
    run_chat_job -->|"enqueue_job"| Q_DOC
    Q_DOC --> DW

    run_chat_job -->|"XADD tokens"| RS
    run_chat_job -->|"produce()"| KAFKA
    AR -->|"produce()"| KAFKA
    RS -->|"SSE poll"| GW
```

---

## Component Documentation

### 1. `run_agent_job` — Agent Execution Worker

**File:** `workers/agent_worker.py`

A lightweight RQ job function that serves as the async entry point for named-agent runs. It receives a payload dictionary and delegates to the singleton `AgentRunner` instance.

#### Payload Contract

| Key | Type | Description |
|---|---|---|
| `agent_name` | `str` | Registered agent name (must be `PRODUCTION` status) |
| `message` | `str` | User message to send to the agent |
| `session_id` | `str` (optional) | Conversation session for history continuity |

#### Execution Flow

```mermaid
sequenceDiagram
    participant W as Worker Process
    participant J as run_agent_job
    participant AR as AgentRunner
    participant MR as ModelRouter
    participant K as Kafka

    W->>J: payload {agent_name, message, session_id}
    J->>AR: agent_runner.run(agent_name, message, session_id)
    Note over AR: 1. Compliance check (input)
    Note over AR: 2. Load conversation history (PostgresMemory)
    Note over AR: 3. Execute context tools (MCP registry)
    Note over AR: 4. Build LLM prompt
    Note over AR: 5. Compliance check (prompt)
    Note over AR: 6. LLM generate (or tool-use loop)
    Note over AR: 7. Compliance check (answer)
    Note over AR: 8. Persist run to Redis
    AR->>K: produce("ainxt.agent_events")
    AR-->>J: AgentRunResult
    J-->>W: result.answer or error string
```

#### Key Behaviours

- **Timeout enforcement:** `AgentRunner` enforces a 120-second wall-clock timeout per run via a `ThreadPoolExecutor`.
- **Greeting short-circuit:** Bare greetings skip the full tool/LLM pipeline and return a lightweight response.
- **Tool-use loop:** Agents with action tools (Jira, GitLab, Confluence, etc.) use Claude's native tool-use API with up to 10 LLM↔tool iterations.
- **Error propagation:** On exception, the error is logged and re-raised so RQ's retry mechanism can handle it.

#### Dependencies

| Dependency | Module | Purpose |
|---|---|---|
| `AgentRunner` | [agent_system](#agent-system) (`agents/agent_builder.py`) | Core agent execution engine |
| `logger` | [core_infrastructure](#core-infrastructure) (`core/logger.py`) | Structured logging |

---

### 2. `run_chat_job` — Chat Pipeline Worker

**File:** `workers/chat_worker.py`

The most complex worker in the platform. It executes the full conversational AI pipeline for a single user question, streaming tokens to a Redis Stream for SSE consumption by the gateway.

#### Payload Contract

| Key | Type | Description |
|---|---|---|
| `job_id` | `str` | Unique job ID; used as Redis Stream key suffix |
| `question` | `str` | Raw user question |
| `session_id` | `str` | Conversation memory lookup key |
| `chat_id` | `str` | Chat ID for memory storage (defaults to `session_id`) |
| `repo_filter` | `str` (optional) | Repository scope for code RAG |
| `model` | `str` (optional) | Model hint for routing |
| `user_id` | `str` | User ID for budget tracking |
| `user_ctx` | `dict` (optional) | User context (department, admin flag, etc.) |
| `rag_mode` | `str` (optional) | Context isolation: `off` / `auto` / `on` |
| `attachment_ids` | `list` (optional) | Uploaded file attachment IDs |
| `enqueued_at` | `float` (optional) | Timestamp for latency anchoring |
| `trace_headers` | `dict` (optional) | W3C traceparent for OTel span linking |
| `request_id` | `str` (optional) | Request ID for log correlation |

#### Pipeline Architecture

```mermaid
flowchart TD
    START["run_chat_job(payload)"] --> EMPTY{"question empty?"}
    EMPTY -->|Yes| DONE_ERR["_publish_done(error)"]
    EMPTY -->|No| BUDGET["STEP 0: Budget gate<br/>(cloud models only)"]
    
    BUDGET --> COMPLIANCE["STEP 1: Input compliance gate<br/>compliance_engine.validate_input()"]
    COMPLIANCE -->|Blocked| DONE_BLK["Publish block message"]
    COMPLIANCE -->|OK| PII["STEP 2: PII mask<br/>mask_pii()"]
    
    PII --> ENG_CTX["STEP 2b: Engineer context injection<br/>get_context_for_session()"]
    ENG_CTX --> HISTORY["STEP 3: Conversation history<br/>RedisMemory.get_conversation()<br/>+ auto-summarisation if >20 msgs"]
    
    HISTORY --> CLASSIFY["STEP 4: Classify + domain detect<br/>classify_query_complexity()<br/>detect_query_domain()"]
    CLASSIFY --> CACHE_L1["STEP 5: L1 cache check<br/>Redis GET cache_key"]
    
    CACHE_L1 -->|Hit| PUBLISH_CACHE["Publish cached answer"]
    CACHE_L1 -->|Miss| CACHE_L2["STEP 5b: L2 semantic cache<br/>pgvector cosine similarity"]
    
    CACHE_L2 -->|Hit| PUBLISH_SEM["Publish semantic answer"]
    CACHE_L2 -->|Miss| REWRITE["STEP 6: Query rewrite<br/>(code domain only)"]
    
    REWRITE --> REPO["STEP 7: Repo detection"]
    REPO --> RAG["STEP 8: RAG retrieval"]
    
    RAG --> DOC_CHECK{"STEP 8b: Doc generation intent?"}
    DOC_CHECK -->|Yes| DOC_GEN["_handle_doc_generation()<br/>→ enqueue doc_worker job"]
    DOC_CHECK -->|No| DOC_EDIT{"STEP 8b-edit: Doc edit follow-up?"}
    DOC_EDIT -->|Yes| DOC_EDIT_FLOW["_route_doc_edit_followup()"]
    DOC_EDIT -->|No| MEM_L3["STEP 8c: L3 semantic memory injection"]
    
    MEM_L3 --> PROMPT["STEP 9: Build prompt<br/>KB_DOC_PROMPT / GROUNDED_PROMPT"]
    PROMPT --> STREAM["STEP 10: Stream tokens<br/>model_router.stream()<br/>+ DistributedSemaphore throttle"]
    
    STREAM --> CACHE_WRITE["STEP 11: Cache write<br/>L1 + L2 semantic"]
    CACHE_WRITE --> MEM_SAVE["STEP 12: Save to memory<br/>RedisMemory.save_message()"]
    MEM_SAVE --> CTX_UPDATE["STEP 12b: Update engineer context"]
    CTX_UPDATE --> MEM_L3_WRITE["STEP 12c: L3 semantic memory write"]
    MEM_L3_WRITE --> KAFKA_PUB["STEP 13: Kafka publish<br/>TOPIC_CHAT_HISTORY + TOPIC_METRICS"]
    
    KAFKA_PUB --> DONE["_publish_done(meta)"]
    PUBLISH_CACHE --> DONE
    PUBLISH_SEM --> DONE
    DOC_GEN --> DONE
    DOC_EDIT_FLOW --> DONE
    DONE_BLK --> DONE
    DONE_ERR --> DONE
```

#### Key Subsystems

##### 2a. Document Generation Routing

The chat worker intercepts document-generation intents **before** normal LLM generation. This is a critical routing layer that supports multiple document formats via slash commands:

| Command | Format | Handler |
|---|---|---|
| `/pdf` | PDF | `_handle_doc_generation()` → `doc_worker_agent.generate_doc_job` |
| `/docx`, `/doc`, `/word` | Word | `_handle_doc_generation()` → `doc_worker_agent.generate_doc_job` |
| `/xlsx`, `/excel` | Excel | `_handle_doc_generation()` → `doc_worker_agent.generate_doc_job` |
| `/csv` | CSV | `_handle_doc_generation()` → `doc_worker_agent.generate_doc_job` |
| `/txt`, `/text` | Plain text | `_handle_doc_generation()` → `doc_worker_agent.generate_doc_job` |
| `/md` | Markdown | `_handle_md_generation()` → `doc_worker_agent.generate_md_job` |
| `/ppt`, `/pptx` | PowerPoint | `_handle_pptx_generation()` → theme picker flow |
| `/convert <fmt>` | File conversion | `_handle_doc_conversion()` → `doc_worker.convert_doc_job` |

The routing also handles:
- **Follow-up confirmations** (`_is_doc_followup`): Short replies like "yes" or "go with option A" after a prior doc command.
- **Plain-language edit follow-ups** (`_route_doc_edit_followup`): Natural phrases like "update this document" or "fix the title" when an `md:session:{chat_id}` exists in Redis.
- **Preservation mode**: When a user uploads a file and asks to reproduce/convert it, the `_build_preservation_prompt` is used instead of free-form generation.

```mermaid
flowchart LR
    Q["User question"] --> CMD{"Slash command?"}
    CMD -->|"/convert"| CONV["_handle_doc_conversion<br/>→ doc_worker.convert_doc_job"]
    CMD -->|"/pdf /docx /xlsx /csv /txt"| GEN["_handle_doc_generation<br/>→ doc_worker_agent.generate_doc_job"]
    CMD -->|"/md"| MD["_handle_md_generation<br/>→ doc_worker_agent.generate_md_job"]
    CMD -->|"/ppt /pptx"| PPT["_handle_pptx_generation<br/>→ theme picker → doc_worker_agent"]
    CMD -->|No command| FOLLOW{"Doc follow-up?"}
    FOLLOW -->|Confirmation reply| GEN
    FOLLOW -->|Edit verb + doc noun| EDIT["_route_doc_edit_followup<br/>→ _handle_md_generation (edit mode)"]
    FOLLOW -->|No| CHAT["Normal chat pipeline"]
```

##### 2b. Multi-Layer Caching

The chat worker implements a three-tier caching strategy:

| Tier | Mechanism | Scope | TTL |
|---|---|---|---|
| **L1** | Exact-match Redis cache (`chat_cache_key`) | Per question + repo + rag_mode | 24 hours |
| **L2** | Semantic cache (pgvector cosine similarity) | First-turn only, non-code domain | Configurable |
| **L3** | Semantic memory (learned patterns) | User + team scoped | Configurable |

L2 and L3 are gated by feature flags (`SEMANTIC_CACHE_ENABLED`, `SEMANTIC_MEMORY_ENABLED`) and include defense-in-depth checks at both the worker and store layers.

##### 2c. RAG Retrieval

Retrieval is context-dependent:

- **Platform queries** (keywords: `ainxt`, `npci`, `upi`, `sdlc`, etc.) or **voice mode**: Searches `docs_kb` namespaces via `pgvector_search` + `keyword_search`, with BGE cross-encoder reranking and section-aware re-ranking.
- **Code repo queries** (`repo_filter` set): Uses `hybrid_retrieve_context` with BGE reranking.
- **General queries**: No retrieval; goes directly to LLM.

When retrieval returns zero chunks for a `docs_kb` query, the worker returns a **refusal message** rather than allowing the LLM to hallucinate from training data.

##### 2d. Streaming & Sentinel Protocol

Tokens are published to a Redis Stream (`chat:stream:{job_id}`) via `XADD`. The stream uses:
- **`maxlen=10,000`** with approximate trimming to prevent unbounded growth.
- **`__done__` sentinel** with metadata (model, tokens, cost, latency, confidence, chunk_count).
- **`__error__` sentinel** for failure cases.
- **TTL of 1 hour** set in a `finally` block to guarantee self-cleaning even on crash.

The `try/finally` pattern ensures the sentinel is **always** delivered, preventing hanging SSE connections on the gateway side.

##### 2e. Concurrency Throttle

LLM calls are gated by a `DistributedSemaphore` (`llm_global`, capacity=500) backed by a Redis Lua script for atomic acquire/release. This prevents thundering-herd LLM calls under high load. If the semaphore initialization fails, the worker proceeds without throttling (fail-open).

##### 2f. Compliance & PII

- **Input gate** (Step 1): `compliance_engine.validate_input()` blocks requests containing PAN, CVV, secrets, etc.
- **PII masking** (Step 2): `mask_pii()` redacts sensitive patterns before the question enters the pipeline.
- **Attachment redaction**: Uploaded file content is passed through the compliance redactor before injection into LLM prompts (gated by `COMPLIANCE_SCAN_TOOL_RESULTS`).

##### 2g. Budget Enforcement

A defense-in-depth budget gate runs at Step 0. Cloud models (GPT, Claude, Gemini) are checked against the user's budget allocation. In-house models (local GPU, LiteLLM-routed) are always exempt. If the budget is exhausted, a user-friendly message is published and the job exits early.

#### Dependencies

| Dependency | Module Reference | Purpose |
|---|---|---|
| `compliance_engine` | [agent_system](#agent-system) | Input/prompt/answer compliance gating |
| `model_router` | [model_routing](#model-routing) | LLM routing, streaming, fallback |
| `RedisMemory` | [memory_system](#memory-system) | Conversation history persistence |
| `hybrid_search` / `hybrid_retriever` | [model_routing](#model-routing) | RAG retrieval + BGE reranking |
| `budget_store` | [store_layer](#store-layer) | Budget checking and usage increment |
| `semantic_cache` | [store_layer](#store-layer) | L2/L3 semantic caching |
| `context_manager` | [core_infrastructure](#core-infrastructure) | Engineer cross-session context |
| `distributed_semaphore` | [core_infrastructure](#core-infrastructure) | LLM concurrency throttle |
| `kafka_producer` | [core_infrastructure](#core-infrastructure) | Async persistence to Postgres |
| `job_queue` | [core_infrastructure](#core-infrastructure) | Enqueue doc generation jobs |
| `doc_worker` / `doc_worker_agent` | [document_knowledge_workers](#document-knowledge-workers) | Document generation |
| `doc_generator` (tools) | [shared_integrations](#shared-integrations) | Filename/theme utilities |
| `classifier` | [model_routing](#model-routing) | Query complexity + domain classification |
| `query_rewriter` | [model_routing](#model-routing) | Code-domain query rewriting |
| `section_query_router` | [core_infrastructure](#core-infrastructure) | KB section-aware reranking |
| `chat_utils` | [core_infrastructure](#core-infrastructure) | PII masking, cache keys, repo detection |

---

## Data Flow: End-to-End Chat Request

```mermaid
sequenceDiagram
    participant U as User
    participant GW as Gateway
    participant RQ as Redis Queue
    participant CW as Chat Worker
    participant CE as Compliance Engine
    participant RM as Redis Memory
    participant MR as Model Router
    participant LLM as LLM Gateway
    participant K as Kafka
    participant KC as Kafka Consumer
    participant PG as Postgres

    U->>GW: POST /ask {question}
    GW->>GW: Compliance check (inline)
    GW->>RQ: enqueue_job("workers.chat_worker.run_chat_job", payload)
    GW-->>U: SSE stream open (polls chat:stream:{job_id})

    RQ->>CW: run_chat_job(payload)
    CW->>CE: validate_input(question)
    CE-->>CW: {blocked: false}
    CW->>RM: get_conversation(chat_id)
    RM-->>CW: history[]
    CW->>CW: Classify + cache check
    CW->>CW: RAG retrieval (if applicable)
    CW->>CW: Build prompt
    CW->>MR: stream(prompt, model_hint)
    MR->>LLM: API call (streaming)
    
    loop Token streaming
        LLM-->>MR: token
        MR-->>CW: token
        CW->>RQ: XADD chat:stream:{job_id} {type: chunk, data: token}
        RQ-->>GW: SSE poll
        GW-->>U: SSE data: token
    end
    
    MR-->>CW: __stream_meta__ {in_tok, out_tok, model}
    CW->>RM: save_message(user + assistant)
    CW->>K: produce(TOPIC_CHAT_HISTORY, {...})
    CW->>K: produce(TOPIC_METRICS, {...})
    CW->>RQ: XADD chat:stream:{job_id} {type: __done__, meta: {...}}
    RQ-->>GW: SSE poll (done)
    GW-->>U: SSE event: done

    par Async persistence
        K->>KC: consume(TOPIC_CHAT_HISTORY)
        KC->>PG: INSERT chat_messages
        KC->>PG: INSERT model_usage
    end
```

---

## Worker Orchestration

Worker processes are spawned by `workers/start_workers.py`. The chat and agent workers are started with specific CLI flags:

```bash
# Chat workers (consume chat_queue, high_queue, default_queue)
python -m workers.start_workers --chat --n 2

# Agent workers (consume agent_queue)
python -m workers.start_workers --agent

# All queues (default mode)
python -m workers.start_workers
```

Each worker process calls `_worker_process()` which creates an RQ Worker (or RustyCluster equivalent) via the KV factory. The backend is selected by `REDIS_CLIENT_CONFIG_DB5` environment variable. Workers run with `with_scheduler=True` for RQ's built-in job scheduling.

For more details on worker process management, see [worker_orchestration](#worker-orchestration).

---

## Redis Stream Protocol

The chat worker communicates with the gateway via a well-defined Redis Stream protocol:

| Event Type | Fields | Description |
|---|---|---|
| `chunk` | `type=chunk`, `data=<token>` | Individual LLM token or text chunk |
| `__done__` | `type=__done__`, `meta=<json>` | Successful completion with metadata |
| `__done__` | `type=__done__`, `error=<msg>` | Empty question or early exit |
| `__error__` | `type=__error__`, `msg=<msg>` | Unhandled exception |

**Metadata JSON** (in `__done__`):

```json
{
  "model": "GPT-5.4 (gpt-5.4)",
  "in_tok": 1523,
  "out_tok": 487,
  "cost": 0.0234,
  "latency": 3.421,
  "confidence": 0.82,
  "chunk_count": 12
}
```

---

## Error Handling & Resilience

| Scenario | Behaviour |
|---|---|
| Empty question | Publishes `__done__` with error; exits immediately |
| Compliance block | Publishes block message; publishes `__done__`; exits |
| Budget exhausted | Publishes budget message; publishes `__done__`; exits |
| LLM gateway failure | ModelRouter fallback chain (e.g., Claude → OpenAI → Local) |
| Semaphore timeout | Publishes "Server busy" message; publishes `__done__` |
| Redis Stream write failure | Logged as warning; job continues (best-effort) |
| Kafka publish failure | Logged as warning; job never fails due to Kafka |
| Memory persistence failure | Logged as warning; job continues |
| Unhandled exception | Publishes `__error__`; re-raises for RQ retry |
| Worker crash | `finally` block sets stream TTL; orphaned stream self-cleans in 1h |

---

## Relationship to Sibling Modules

This module is part of the `chat_agent_execution_workers` family:

```mermaid
graph LR
    subgraph "chat_agent_execution_workers"
        CHAT_AGENT["chat_agent<br/>(this module)<br/>run_chat_job + run_agent_job"]
        WORKFLOW["workflow<br/>execute_durable_workflow"]
        SANDBOX["sandbox<br/>run_code_job"]
        SECURITY["security<br/>run_secure_code_gate<br/>run_security_scan_job"]
        SKILL_LOOP["skill_loop<br/>detect_and_propose<br/>enqueue_detect"]
    end
    
    CHAT_AGENT -->|"enqueue doc jobs"| DOC_WORKERS["document_knowledge_workers"]
    CHAT_AGENT -->|"LLM calls"| MODEL_ROUTING["model_routing"]
    CHAT_AGENT -->|"memory"| MEMORY["memory_system"]
```

- **[chat_agent_execution_workers_workflow](chat_agent_execution_workers_workflow.md)**: Durable workflow execution (`execute_durable_workflow`) — handles multi-step workflow graphs.
- **chat_agent_execution_workers_sandbox**: Code execution sandbox (`run_code_job`) — isolated Python/Docker execution.
- **chat_agent_execution_workers_security**: Security scanning (`run_secure_code_gate`, `run_security_scan_job`).
- **chat_agent_execution_workers_skill_loop**: Self-improving skill detection (`detect_and_propose`, `enqueue_detect`).

---

## Configuration Reference

| Environment Variable | Default | Description |
|---|---|---|
| `RDB_STREAM` | `6` | Redis DB for chat streams |
| `RDB_CACHE` | `0` | Redis DB for answer cache |
| `RDB_QUEUE` | `5` | Redis DB for RQ job queue + slides cache |
| `COMPLIANCE_SCAN_TOOL_RESULTS` | `False` | Gate attachment content redaction |
| `RERANKER_MIN_SCORE` | `0.30` | BGE reranker relevance threshold |
| `SEMANTIC_CACHE_ENABLED` | varies | L2 semantic cache feature flag |
| `SEMANTIC_MEMORY_ENABLED` | varies | L3 semantic memory feature flag |
| `SEMANTIC_MEMORY_MIN_CONFIDENCE` | varies | Minimum confidence for L3 writes |
| `STREAM_TTL` | `3600` (1h) | Redis Stream TTL |
| `STREAM_MAXLEN` | `10,000` | Max stream entries (approximate trim) |
| `_SLIDES_CACHE_TTL` | `3600` (1h) | PPTX slides cache TTL |

---

## Cross-References

| Topic | Documentation |
|---|---|
| Agent execution engine | [agent_system](#agent-system) — `AgentRunner`, `AgentBuilder` |
| Model routing & fallback | [model_routing](#model-routing) — `ModelRouter`, gateway dispatch |
| Conversation memory | [memory_system](#memory-system) — `RedisMemory`, `PostgresMemory` |
| Compliance & governance | [agent_system](#agent-system) — `ComplianceEngine` |
| Job queue infrastructure | [core_infrastructure](#core-infrastructure) — `enqueue_job`, queue management |
| Worker process management | [worker_orchestration](#worker-orchestration) — `start_workers.py` |
| Document generation | [document_knowledge_workers](#document-knowledge-workers) — `doc_worker`, `doc_worker_agent` |
| Budget management | [store_layer](#store-layer) — `budget_store` |
| Semantic caching | [store_layer](#store-layer) — `semantic_cache` |
| Kafka event consumption | [kafka_event_consumer](#kafka-event-consumer) — `kafka_consumer.py` |
| Gateway API layer | [gateway](#gateway) — `gateway.py` |
