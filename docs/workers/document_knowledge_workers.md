# Document & Knowledge Workers

## Overview

The `document_knowledge_workers` module is a collection of background RQ workers that power document generation, knowledge-base ingestion, and knowledge-graph construction for the ABStudio / ai-nxt platform. These workers run outside the request path (in the `workers` process pool started via `workers/start_workers.py`) so that long-running, CPU/IO-heavy, or LLM-dependent operations do not block HTTP requests.

The module sits at the intersection of three product capabilities:

1. **Document Generation** — produce branded PDF, DOCX, PPTX, XLSX, TXT, and Markdown artifacts from a user question or pre-structured sections.
2. **Knowledge Base (KB) Ingestion** — parse, chunk, embed, and activate uploaded documents so they become searchable for RAG.
3. **Knowledge Graph Construction** — extract entities and relations from KB docs and code repos, build cross-links, and cluster domains.

These workers are consumers; they are enqueued by API routes in `shared_api_routers` (e.g. `doc_download_router`, `docs_router`, `knowledge_graph_router`), by chat flows in `gateway`, and by other workers such as `index_worker`.

## Architecture

```mermaid
flowchart TB
    subgraph Producers
        A[doc_download_router]
        B[docs_router]
        C[knowledge_graph_router]
        D[chat_worker / gateway]
        E[index_worker]
    end

    subgraph Queues
        Q_DOC[Q_DOC]
        Q_KB[Q_KB]
        index_queue[index_queue]
    end

    subgraph document_knowledge_workers
        subgraph Document Generation
            DW[doc_worker]
            DWA[doc_worker_agent]
            DSW[doc_skill_worker]
            PW[presenton_worker]
        end
        subgraph KB Ingestion
            KW[kb_worker]
            KCW[kb_cleanup_worker]
        end
        subgraph Knowledge Graph
            KGW[knowledge_graph_worker]
            KEW[kb_entity_worker]
        end
        subgraph Code Chunking
            TSC[tree_sitter_chunker]
        end
    end

    subgraph Storage
        R[(Redis result/progress)]
        PG[(Postgres audit)]
        PGV[(pgvector embeddings)]
        FS[File storage]
    end

    A -->|enqueue| Q_DOC
    D -->|enqueue| Q_DOC
    B -->|enqueue| Q_KB
    E -->|enqueue| index_queue
    C -->|enqueue| Q_KB

    Q_DOC --> DW & DWA & DSW & PW
    Q_KB --> KW & KCW & KGW & KEW
    index_queue --> TSC

    DW & DWA & DSW & PW --> R
    DW & DWA & DSW & PW --> PG
    DW & DWA & DSW & PW --> FS
    KW --> PG
    KW --> PGV
    KGW & KEW --> PG
    TSC -->|chunks| PGV
```

### Worker Process Model

All workers are RQ jobs. They are started by `workers/start_workers.py` with flags such as `--doc` and `--kb`. Each worker function receives a `payload` dict, performs the work, and writes results to Redis (`doc:result:{job_id}`, `ppt:result:{job_id}`) and/or Postgres. Progress is published to Redis (`doc:progress:{job_id}`) so the UI can show real-time status.

### Key Design Principles

- **Fail-open compliance gates**: document workers run input through `agents.compliance_engine` but continue if the engine itself errors.
- **Idempotency**: KB activation and graph jobs use content-hash guards and `ON CONFLICT` upserts so re-running is safe.
- **Cancellation awareness**: generation workers check `core.generation_registry.is_stopped_redis(job_id)` at safe points.
- **Budget accounting**: token/cost metadata is written to chat messages and `store.budget_store.increment_usage`.
- **RBAC scoping**: graph and KB operations mirror `classification`, `department`, and `min_band_level` from source records.

## Sub-modules

| Sub-module | Purpose | Files | Documentation |
|---|---|---|---|
| Document Generation Workers | Generate PDF/DOCX/PPTX/XLSX/TXT/MD artifacts and presentations | `doc_worker.py`, `doc_worker_agent.py`, `doc_skill_worker.py`, `presenton_worker.py` | document_knowledge_workers_document_generation.md |
| Knowledge Base Ingestion Workers | Parse, chunk, embed, and activate uploaded KB documents | `kb_worker.py`, `kb_cleanup_worker.py` | document_knowledge_workers_kb_ingestion.md |
| Knowledge Graph Workers | Extract entities/relations, build graphs, cross-link code and docs, cluster domains | `knowledge_graph_worker.py`, `kb_entity_worker.py` | document_knowledge_workers_knowledge_graph.md |
| Code Chunking Worker | AST-aware code chunking for repository indexing | `tree_sitter_chunker.py` | document_knowledge_workers_code_chunking.md |

## Data Flow

### Document Generation Flow

```mermaid
sequenceDiagram
    participant UI as ai-ui / ABStudio frontend
    participant API as doc_download_router
    participant Q as Q_DOC
    participant W as doc_worker_agent
    participant DW as doc_worker
    participant R as Redis
    participant PG as Postgres

    UI->>API: POST /docs/generate
    API->>Q: enqueue generate_doc_from_question
    Q->>W: pick job
    W->>W: redirect md → generate_md_job
    alt format == md
        W->>DW: generate_md_doc / edit_md_doc
        DW-->>W: sections, content, meta
        W->>W: optional format regen (docx/pdf/xlsx)
    else other format
        W->>DW: generate_doc_from_question / generate_doc_job
        DW-->>W: binary + metadata
    end
    W->>R: SET doc:result:{job_id}
    W->>PG: INSERT GeneratedDocument
    W->>PG: increment_usage
    UI->>API: GET /docs/job/{job_id}/status
    API->>R: GET doc:result:{job_id}
    API-->>UI: status / file_id
    UI->>API: GET /docs/download/{file_id}
    API->>PG: resolve file_path
    API-->>UI: artifact bytes
```

### KB Activation Flow

```mermaid
sequenceDiagram
    participant UI as ai-ui
    participant API as docs_router
    participant Q as Q_KB
    participant KW as kb_worker
    participant DS as docs_store
    participant PG as Postgres
    participant PGV as pgvector

    UI->>API: POST /docs/{id}/approve
    API->>PG: status = INDEXING
    API->>Q: enqueue run_activate_doc
    Q->>KW: pick job
    KW->>DS: activate_doc
    DS->>DS: Docling parse → chunk → embed
    DS->>PGV: INSERT embeddings
    DS-->>KW: {success, chunk_count}
    KW->>PG: status = ACTIVE
    KW->>PG: delete original binary
    KW->>PG: Inbox notification
    KW-->>API: return result
```

### Knowledge Graph Flow

```mermaid
sequenceDiagram
    participant API as knowledge_graph_router
    participant Q as Q_KB
    participant KGW as knowledge_graph_worker
    participant KEW as kb_entity_worker
    participant PG as Postgres

    API->>Q: enqueue build_graph_job
    Q->>KGW: pick job
    alt graph_id starts with kb:
        KGW->>Q: enqueue extract_doc_entities_job per doc
        Q->>KEW: pick job
        KEW->>PG: resolve entities + write edges
    else graph_id starts with repo:
        KGW->>PG: mark status done
    end
    opt trigger_domain
        KGW->>Q: enqueue cluster_domains_job
        Q->>KGW: cluster + pagerank
    end
    opt trigger_cross
        KGW->>Q: enqueue build_cross_links_job
        Q->>KGW: cross-link code <-> docs
    end
```

## Integration with Other Modules

- **API layer**: `doc_download_router`, `docs_router`, `knowledge_graph_router`, and `presenton_router` enqueue jobs and poll results. See their respective documentation for request/response schemas.
- **Agent layer**: `agents.compliance_engine` gates content; `agents.doc_generator_agent` handles Markdown generation/editing; `sandbox.doc_executor` runs agent-authored document builds. See [shared_core.md](../core/shared_core.md) and its sub-modules.
- **Storage layer**: `store.docs_store`, `store.kb_entity_registry`, and `store.budget_store` provide persistence and accounting. See [shared_core.md](../core/shared_core.md).
- **Model routing**: `models.model_router` is used by graph workers for LLM extraction. See [shared_core.md](../core/shared_core.md).
- **Frontend**: `ai-ui` components such as `DocLivePreview`, `DocWorkflowCard`, `KnowledgeBase`, and `KbChat` consume these workers. See ai_ui_frontend.md.

## Operational Notes

- **Queues**: document jobs use `Q_DOC`; KB/graph jobs use `Q_KB`. Both are defined in `core.job_queue`.
- **Scaling**: run more `--doc` and `--kb` worker processes when queue depth grows. Presenton calls are blocking up to 240s, so keep enough workers.
- **Timeouts**: KB activation has no hard RQ cap but stage-level HTTP timeouts apply; graph entity extraction uses 600s; Presenton uses 240s.
- **Monitoring**: each worker wraps itself in `core.log_job_context` and emits structured logs with `job_id`, `user_id`, `chat_id`, and `agent_id`.
- **Cleanup**: `kb_cleanup_worker.recover_stale_indexing_docs` runs every 10 minutes to reset documents stuck in `INDEXING` longer than 35 minutes.
