sequenceDiagram
    participant User as User (Voice / Chat / IDE)
    participant Voice as Voice Interface (STT→TTS)
    participant UI as React SPA (30+ components)
    participant API as Backend API (FastAPI)
    participant Guard as Guardrails / PCI-DSS Compliance
    participant Cache as Answer Cache (Redis db=0)
    participant Orch as Agent Orchestrator
    participant WF as Workflow Engine (DAG)
    participant Agent as Agent Runtime
    participant SDLC as SDLC Pipeline
    participant Sandbox as Docker Sandbox
    participant Mem as Memory Layer
    participant RAG as RAG Retrieval
    participant KB as Knowledge Base
    participant Tool as Tool Registry
    participant MCP as MCP Connectors
    participant Embed as Embed Microservice (:8001)
    participant Queue as Async Workers (RQ)
    participant Obs as Observability
    participant Sem as Distributed Semaphore
    participant Gateway as LLM Gateway
    participant Router as Model Router
    participant Local as In-House Models (Ollama / GPU)
    participant Cloud as Cloud Models (Claude / GPT / Gemini)
    participant Gov as RBAC + Governance

    %% ── Voice path ────────────────────────────────────────────────────────
    User->>Voice: Speak question
    Voice->>Voice: STT (Web Speech API) + STT correction dictionary
    Voice->>API: POST /ask  { voice_platform: true }
    API->>Guard: Validate input (PAN / PII / secrets / API keys)
    Guard->>Cache: Check answer cache (Redis TTL 24h)
    Cache-->>Guard: Cache miss
    Guard->>RAG: Fast-path: query docs_kb:platform + KB namespaces
    RAG->>Embed: POST /embed  nomic-embed-text 768-dim
    Embed-->>RAG: Vectors
    RAG-->>Guard: Top-6 chunks (pgvector HNSW + BM25 + reranker)
    Guard->>Gateway: Platform spokesperson prompt + context
    Gateway->>Router: Determine best model (voice → complex tier)
    Router->>Cloud: Claude Sonnet 4.6
    Cloud-->>Gateway: Streaming tokens
    Gateway-->>API: SSE token stream
    API-->>Voice: Streamed tokens (onToken callback)
    Voice->>Voice: Sentence detection → ttsApi() pre-fetch (parallel)
    Voice->>API: POST /voice/tts  (nova, tts-1-hd, speed 0.92)
    API-->>Voice: Audio blob per sentence
    Voice-->>User: Play sentence 1 while sentences 2-N pre-fetch

    %% ── Standard chat path ────────────────────────────────────────────────
    User->>UI: Type / submit prompt
    UI->>API: POST /ask
    API->>Guard: Validate input (20+ PCI/PII/secret types)
    Guard->>Cache: Check answer cache
    Cache-->>Guard: Cache miss
    Guard->>Orch: Safe request → Agent Orchestrator

    Orch->>Mem: Retrieve conversation + task memory
    Mem-->>Orch: Redis (transient) + Postgres (persistent) context

    Orch->>RAG: Hybrid retrieval
    RAG->>Embed: Batch embed query
    Embed-->>RAG: 768-dim vectors (cached in Redis db=7)
    RAG-->>Orch: pgvector HNSW + BM25 tsvector + TinyBERT rerank → top-6

    Orch->>Sem: Acquire distributed LLM semaphore (cross-process rate limit)
    Orch->>Gateway: Prompt + retrieved context
    Gateway->>Router: Route by hint → vision → complexity
    Router->>Local: simple tier → Ollama llama3.1 (in-house GPU)
    Router->>Cloud: medium → GPT-5.2 / complex → Claude Sonnet 4.6 / vision → Gemini 2.0 Flash
    Local-->>Gateway: Streaming tokens
    Cloud-->>Gateway: Streaming tokens
    Gateway->>Guard: Output compliance check (redact before streaming)
    Gateway-->>Orch: Clean LLM output

    Orch->>Tool: Tool / skill call decision
    Tool->>RAG: Knowledge retrieval
    Tool->>MCP: External system call (GitHub / Jira / Confluence / N8N / Zoho)
    MCP-->>Tool: API results
    Tool-->>Orch: Tool output

    Orch->>Obs: Log metrics / traces / LLM-as-Judge eval
    Orch->>Cache: Write answer to cache (TTL 24h)
    Orch-->>API: Final result (SSE stream)
    API-->>UI: Streamed response

    %% ── SDLC pipeline path ────────────────────────────────────────────────
    User->>UI: SDLC request (code gen / review / test / bug fix)
    UI->>API: POST /sdlc/webhook
    API->>Queue: Enqueue sdlc_queue (RQ worker)
    Queue->>SDLC: Execute SDLC pipeline
    SDLC->>RAG: RAG context per file (existing code + docs)
    SDLC->>Gateway: Claude Sonnet 4.6 (primary) / GPT-5.2 (fallback)
    Gateway-->>SDLC: Generated / reviewed / fixed code
    SDLC->>Guard: PCI compliance check on all generated output
    SDLC->>Sandbox: Docker-isolated execution (network-off, 512MB cap)
    Sandbox-->>SDLC: Test results / self-healing loop
    SDLC-->>API: Completed artefacts
    API-->>UI: SDLC result

    %% ── Workflow engine path ──────────────────────────────────────────────
    User->>UI: Trigger named workflow
    UI->>API: POST /workflows/{id}/run
    API->>WF: DAG execution (Kahn topological sort)
    WF->>Agent: Execute each step (parallel where possible)
    Agent->>Gateway: Per-step LLM call with {step_id} chaining
    Agent->>Guard: Compliance check on each step output
    Gateway-->>Agent: Step result
    Agent-->>WF: Step complete
    WF-->>API: Workflow completed
    API-->>UI: Final output

    %% ── Knowledge base ingestion ──────────────────────────────────────────
    User->>UI: Upload PDF / Word / URL
    UI->>API: POST /kb/upload
    API->>Queue: Enqueue kb_queue
    Queue->>KB: Parse → chunk → embed
    KB->>Embed: POST /embed (batch, 64 texts, 50ms accumulator)
    Embed-->>KB: Vectors
    KB->>RAG: Upsert into document_embeddings (pgvector)
    KB-->>API: Indexed

    %% ── Governance path ───────────────────────────────────────────────────
    User->>Gov: Submit agent / skill / MCP for approval
    Gov->>Gov: DRAFT → PENDING_APPROVAL → APPROVED → PRODUCTION → DEPRECATED
    Gov->>API: RBAC gate (viewer / developer / operator / security / admin)
    Gov-->>UI: Governance status
