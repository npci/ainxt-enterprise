# shared_core_knowledge_base

The `shared_core_knowledge_base` module is the central ingestion, storage, and entity-resolution layer for the platform's Retrieval-Augmented Generation (RAG) knowledge base. It is responsible for turning raw uploaded files into searchable vector chunks, managing document lifecycle states, and maintaining a canonical entity registry so that cross-document references (e.g. "UPI Lite", "UPI-Lite") collapse to a single node.

## Purpose

- Provide a **document knowledge-base store** that parses, chunks, embeds, and indexes uploaded documents into `pgvector`.
- Enforce an **approval workflow** so unapproved content is never RAG-searchable.
- Support **structured, section-aware chunking** with parent/leaf relationships, page numbers, and atomic code/table blocks.
- Maintain a **canonical entity registry** with product-scoped and global tiers, alias normalization, and graph-edge linking.
- Integrate with compliance, OCR/Docling parsing, embedding service, replication, caching, and async entity-extraction workers.

## Architecture Overview

```mermaid
flowchart TB
    subgraph shared_core_knowledge_base
        DS[store/docs_store.py]
        ER[store/kb_entity_registry.py]
    end

    subgraph Inputs
        UI[Web UI / API upload]
        Router[docs_router / kb_router]
    end

    subgraph Core Services
        DP[core/document_parser<br/>core/docling_parser<br/>core/section_promoter]
        CE[agents/compliance_engine]
        ES[embedding_service]
        DB[(shared_core_database)]
        KV[(KV cache)]
        FS[KB_DOC_STORAGE_PATH]
        Repl[store/kb_replication]
        Cache[store/kb_doc_cache]
    end

    subgraph Async Workers
        KW[workers/kb_worker]
        KEW[workers/kb_entity_worker]
    end

    UI --> Router
    Router --> DS
    DS --> DP
    DS --> CE
    DS --> DB
    DS --> FS
    DS --> Repl
    DS --> Cache
    DS -.->|enqueue| KW
    KW -->|activate_doc| DS
    KW --> ES
    KW --> DB
    KEW --> ER
    ER --> DB
    ER -->|link_chunks| DB
```

### Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| Document Store | `store/docs_store.py` | Upload staging, parsing, chunking, approval workflow, activation/embedding, deprecation, deletion, and namespace management. |
| Entity Registry | `store/kb_entity_registry.py` | Canonical entity resolution, alias management, and generic knowledge-graph edge insertion. |

## Sub-modules

- **[shared_core_knowledge_base_entity_registry](shared_core_knowledge_base_entity_registry.md)** — canonical entity resolution and alias normalization.
- **[shared_core_knowledge_base_document_store](shared_core_knowledge_base_document_store.md)** — document ingestion, chunking, activation, and lifecycle management.

## Document Lifecycle Data Flow

```mermaid
sequenceDiagram
    participant U as User/API
    participant DS as docs_store.upload_doc
    participant DB as KnowledgeDocument (PGS01)
    participant FS as File System
    participant KW as kb_worker
    participant ES as embedding_service
    participant VDB as document_embeddings (PGS02)

    U->>DS: Upload file bytes + metadata
    DS->>DS: Parse (legacy) / dedup / section-promote / chunk
    DS->>DB: Insert PENDING_APPROVAL row
    DS->>FS: Save original binary
    DS->>DB: Notify approvers (inbox)

    alt Auto-approve
        DS->>KW: activate_doc
    else Manual approve
        U->>KW: Approve document
    end

    KW->>DS: activate_doc(doc_id)
    DS->>DS: Docling parse (post-approval)
    DS->>DS: Deferred compliance (scanned PDFs)
    DS->>DS: Re-chunk if Docling succeeded
    DS->>ES: Embed chunks (batches)
    DS->>VDB: Write DocumentEmbedding rows
    DS->>FS: Write canonical .md
    DS->>DB: Flip status to ACTIVE
    DS->>KEW: Enqueue entity extraction
```

## Entity Resolution Data Flow

```mermaid
sequenceDiagram
    participant EW as kb_entity_worker
    participant ER as kb_entity_registry
    participant DB as kb_entities / kb_edges

    EW->>ER: resolve_entity(surface_form, product_id, create_if_missing=True)
    ER->>DB: Lookup canonical_name / aliases (product scope)
    alt Not found
        ER->>DB: Lookup global tier
    else Create allowed
        ER->>DB: INSERT new product-scoped node
    end
    ER-->>EW: entity dict
    EW->>ER: link_chunks(edge_type, src, dst, entity ids, ...)
    ER->>DB: INSERT kb_edges row
```

## Integration with the Rest of the System

- **API layer**: `shared_api_routers.docs_router`, `shared_api_routers.kb_router`, and `abstudio_backend.app.api.kb` call `upload_doc`, `list_docs`, `delete_doc`, and entity APIs.
- **Frontend**: `ai_ui_frontend.knowledge_base` (`KnowledgeBase.jsx`) and `abstudio_frontend.common_components.KnowledgeUploadInline` drive uploads and namespace selection.
- **Database**: Uses `shared_core_database` (`KnowledgeDocument`, `DocumentEmbedding`, `kb_entities`, `kb_edges`) via `db.database.SessionLocal` / `VectorSessionLocal`.
- **Parsing**: Delegates to `shared_core_document_processing` (`core/document_parser`, `core/docling_parser`, `core/section_promoter`).
- **Compliance**: Scans content through `shared_core_agent_system.compliance_engine`.
- **Embedding**: Calls the standalone `embedding_service` over HTTP.
- **Workers**: `workers/kb_worker` triggers activation; `workers/kb_entity_worker` extracts entities and links chunks.
- **Storage/Cache**: Persists originals and markdown to `KB_DOC_STORAGE_PATH`, replicates via `store/kb_replication`, and invalidates `store/kb_doc_cache`.

## Key Design Decisions

1. **Approval-gated embedding**: Vectors are written only in `activate_doc`, so rejected or pending documents are never RAG-searchable.
2. **Post-approval Docling**: Expensive OCR/Docling parsing runs after approval to avoid wasted work on rejected docs.
3. **Structured chunking**: `docs_store` emits parent (whole-section) and leaf chunks with `section_path`, `section_name`, and `page_number` for better citation and retrieval.
4. **Two-tier entity registry**: Product-scoped entities prevent cross-product leakage; global entities are admin-promoted only.
5. **Alias normalization**: Lowercase, punctuation-stripped, whitespace-collapsed keys with hyphen/space/underscore convergence.
6. **Zero-vector guard**: Activation fails if >10% of embeddings are zero vectors, preventing silently broken RAG results.
