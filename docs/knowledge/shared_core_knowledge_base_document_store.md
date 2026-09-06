# Knowledge Base Document Store

## Introduction

The **Knowledge Base Document Store** (`store/docs_store.py`) is the central persistence and lifecycle management layer for enterprise documents in the RAG (Retrieval-Augmented Generation) pipeline. It governs the complete document journey — from upload through parsing, structure-aware chunking, approval-gated embedding, and final activation into the pgvector search index — while enforcing deduplication, compliance redaction, version deprecation, and multi-tenant access control.

The module sits within the broader [shared_core_knowledge_base](shared_core_knowledge_base.md) subsystem and is the **single authoritative entry point** for writing document content and embeddings. No other component writes to the `document_embeddings` pgvector table except `activate_doc()` in this module.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "API Layer"
        DR["routers/docs_router.py<br/>upload_doc · approve_doc · delete_doc"]
        KR["routers/kb_router.py<br/>list_entities · get_lineage"]
    end

    subgraph "Document Store (this module)"
        UD["upload_doc<br/>Parse → Chunk → Stage"]
        AD["activate_doc<br/>Docling → Re-chunk → Embed → pgvector"]
        CD["_chunk_document_structured<br/>Section-aware chunker"]
        LD["list_docs · delete_doc<br/>list_namespaces · get_original_path"]
    end

    subgraph "Parsing & Chunking"
        DP["core/docling_parser.py<br/>Docling + PaddleOCR"]
        LCP["core/document_parser.py<br/>Legacy parsers"]
        SP["core/section_promoter.py<br/>Bold→Heading promotion"]
        SS["core/structure_scorer.py<br/>Chunk quality scoring"]
    end

    subgraph "Persistence"
        PGS1["PGS01 — knowledge_docs<br/>(PostgreSQL main DB)"]
        PGS2["PGS02 — document_embeddings<br/>(pgvector)"]
        FS["Filesystem<br/>KB_DOC_STORAGE_PATH/<id>.md<br/>KB_DOC_STORAGE_PATH/<id>.<ext>"]
        KV["Redis KV<br/>docs:namespaces"]
    end

    subgraph "External Services"
        ES["Embedding Service<br/>/embed endpoint"]
        CE["ComplianceEngine<br/>PII/PCI scan + redact"]
    end

    subgraph "Workers"
        KW["workers/kb_worker.py<br/>run_activate_doc (RQ)"]
        KEW["workers/kb_entity_worker.py<br/>Entity extraction"]
    end

    DR -->|"upload"| UD
    DR -->|"approve → enqueue"| KW
    DR -->|"delete"| LD
    KW -->|"calls"| AD

    UD -->|"skip_docling=True"| LCP
    UD -->|"pre_parsed_text"| CD
    UD --> SP
    UD --> SS
    UD -->|"stage chunks"| PGS1
    UD -->|"save original binary"| FS
    UD -->|"notify approvers"| IS["store/inbox_store.py"]

    AD -->|"re-parse post-approval"| DP
    AD -->|"deferred compliance"| CE
    AD -->|"re-chunk Docling output"| SP
    AD --> CD
    AD -->|"batch embed"| ES
    AD -->|"write vectors"| PGS2
    AD -->|"write .md"| FS
    AD -->|"deprecate prior versions"| PGS1
    AD -->|"enqueue entity extraction"| KEW

    LD -->|"delete vectors"| PGS2
    LD -->|"delete record"| PGS1
    LD -->|"delete files"| FS
```

---

## Document Lifecycle State Machine

The module enforces a strict approval-gated lifecycle. Vectors are **never** written to pgvector until a document is approved — unapproved content is never RAG-searchable.

```mermaid
stateDiagram-v2
    [*] --> PENDING_APPROVAL : upload_doc()
    [*] --> AUTO_APPROVED : upload_doc(auto_approve=True)

    PENDING_APPROVAL --> INDEXING : approve_doc() (approver action)
    AUTO_APPROVED --> ACTIVE : activate_doc() (inline)

    INDEXING --> ACTIVE : kb_worker success
    INDEXING --> PENDING_APPROVAL : kb_worker failure (rollback)
    INDEXING --> REJECTED : compliance block after OCR
    INDEXING --> DELETING : delete_doc() during activation

    ACTIVE --> DEPRECATED : newer version activates (deprecate_prior=True)
    ACTIVE --> DELETING : delete_doc()
    PENDING_APPROVAL --> DELETING : delete_doc()
    REJECTED --> DELETING : delete_doc()
    DEPRECATED --> DELETING : delete_doc()

    DELETING --> [*] : cleanup complete

    note right of INDEXING
        Docling parse + embed + pgvector write
        Cancellation checks at every stage
    end note

    note right of ACTIVE
        Vectors in pgvector (status='ACTIVE')
        .md file on filesystem
        RAG-searchable
    end note
```

### Status Values

| Status | Meaning | Vectors in pgvector? |
|---|---|---|
| `PENDING_APPROVAL` | Uploaded, awaiting approver action | No |
| `AUTO_APPROVED` | Admin/approver uploaded — embed immediately | No (transient) |
| `INDEXING` | Approved, background worker parsing/embedding | No (in progress) |
| `ACTIVE` | Fully indexed and RAG-searchable | Yes (`status='ACTIVE'`) |
| `REJECTED` | Compliance block or parse failure | No |
| `DEPRECATED` | Superseded by newer version | Yes (`status='DEPRECATED'`, filtered out) |
| `DELETING` | Deletion in progress — activation workers must stop | Being removed |

---

## Core Components

### 1. `upload_doc()` — Upload, Parse, Chunk, and Stage

The primary entry point for document ingestion. Performs a **lightweight** parse (legacy parsers only — Docling is intentionally deferred to post-approval), structure-aware chunking, deduplication, and staging into the `knowledge_docs` table.

```mermaid
flowchart TD
    A["upload_doc() called"] --> B{"pre_parsed_text<br/>provided?"}
    B -->|"Yes (router pre-parsed)"| C["Use pre-parsed text"]
    B -->|"No"| D["Legacy parse<br/>parse_file_structured(skip_docling=True)"]

    C --> E{"Text empty?<br/>is_scanned_pdf?"}
    D --> E
    E -->|"Empty & not scanned"| F["Return error:<br/>No text extracted"]
    E -->|"Empty & scanned"| G["Proceed with empty text<br/>(OCR deferred to activate_doc)"]
    E -->|"Has text"| H["Compute SHA-256 content hash"]

    G --> H
    H --> I{"Duplicate check<br/>against PENDING/INDEXING/<br/>ACTIVE/DEPRECATED"}
    I -->|"Duplicate found"| J["Return error:<br/>identical content exists"]
    I -->|"No duplicate"| K["Section promoter<br/>promote_sections(doc_kind=source_type)"]

    K --> L["Structured chunking<br/>_chunk_document_structured()"]
    L --> M["Structure quality scoring<br/>score_chunk_set()"]
    M --> N["Generate UUID4 doc_id"]
    N --> O["Persist KnowledgeDocument row<br/>(PGS01) with chunks as JSONB"]

    O --> P{"Save original binary<br/>to KB_DOC_STORAGE_PATH"}
    P -->|"Success"| Q["Replicate to replica nodes"]
    P -->|"Failure"| R["Rollback doc row<br/>Return error"]
    Q --> S

    S{"auto_approve?"}
    S -->|"No"| T["Status = PENDING_APPROVAL<br/>Notify approvers via Inbox"]
    S -->|"Yes"| U["Status = AUTO_APPROVED<br/>Call activate_doc() inline"]

    T --> V["Register namespace in Redis KV"]
    U --> V
    V --> W["Return success + doc_id"]
```

**Key design decisions:**

- **Docling deferred**: The expensive Docling/PaddleOCR parse runs only in `activate_doc()` post-approval, avoiding wasted parse calls for documents deleted before approval.
- **Deduplication on content hash**: SHA-256 of cleaned text (or raw bytes for scanned PDFs) is checked against all non-terminal document statuses.
- **Original binary retained**: The raw file (PDF/DOCX/etc.) is saved to `KB_DOC_STORAGE_PATH/<doc_id>.<ext>` at upload time so `activate_doc()` can pass it to Docling without needing the raw bytes again.
- **Scanned PDF handling**: Image-only PDFs with zero extractable text are allowed through (flagged `is_scanned_pdf=True`); OCR + compliance run during activation.
- **Mixed PDF detection**: PDFs with some scanned pages and some digital pages are flagged (`has_mixed_scanned_pages=True`); PaddleOCR runs on scanned pages at activation and results are merged.

---

### 2. `_chunk_document_structured()` — Section-Aware Chunker

The chunking engine produces **parent + leaf** chunk pairs with section-path breadcrumbs, enabling hierarchical retrieval and precise citation rendering.

```mermaid
flowchart LR
    subgraph "Input"
        RT["Raw markdown text<br/>with <!-- page:N --> markers"]
    end

    subgraph "Pre-processing"
        CT["_clean_text()<br/>Strip page numbers, separators,<br/>conversion-error placeholders"]
        AB["_extract_atomic_blocks()<br/>Fenced code, JSON, XML,<br/>code-like blocks → placeholders"]
    end

    subgraph "Section Splitting"
        HS["Split at ATX headings<br/>(# ## ###)"
        SS2["Build heading stack<br/>→ section_path breadcrumbs"]
    end

    subgraph "Per-Section Processing"
        SP2["_section_to_pieces()<br/>Tables = atomic units<br/>Paragraphs → sentence split"]
        MP["_merge_pieces()<br/>Greedy merge to target=800 chars"]
        FL["Filter noise leaves<br/>(heading-only, <50 chars)"]
        OL["Overlap stitching<br/>(skip if prev ends with table)"]
    end

    subgraph "Output"
        PR["PARENT rows<br/>(whole section, ≤6000 chars)"]
        LR["LEAF rows<br/>(fine-grained, parent_idx link)"]
        AR["ATOMIC rows<br/>(code/JSON/XML, content_type+language)"]
    end

    RT --> CT --> AB --> HS --> SS2 --> SP2 --> MP --> FL --> OL
    OL --> PR
    OL --> LR
    OL --> AR
```

**Chunk metadata emitted per row:**

| Field | Description |
|---|---|
| `text` | Chunk content |
| `section_path` | Breadcrumb path, e.g. `"1. Intro > 1.2 Scope"` |
| `section_name` | Last segment of section_path (for UI badges) |
| `page_number` | Page from `<!-- page:N -->` markers (PDF only) |
| `is_parent` | `True` for whole-section rows |
| `parent_idx` | Index of parent row in the output list (for leaves) |
| `atomic` | `True` for code/JSON/XML blocks |
| `content_type` | `code`, `json`, `xml`, `code_like` |
| `language` | Inferred language: `python`, `java`, `sql`, `json`, etc. |

**Table handling**: Consecutive Markdown table lines (`|…|`) are kept as atomic units — never split mid-table. Overlap stitching is skipped when the previous leaf ends with a table row to prevent corrupting table fragments.

**Atomic block extraction**: Fenced code blocks, JSON objects/arrays, XML blocks, and code-like text regions are extracted as placeholders before chunking, then re-injected as standalone atomic chunks with inferred language metadata. This prevents code from being fragmented across chunk boundaries.

---

### 3. `activate_doc()` — Post-Approval Activation

The **only path** that makes a document RAG-searchable. Called either inline (for auto-approved uploads) or via the `kb_worker` background queue (for approver-approved documents).

```mermaid
flowchart TD
    START["activate_doc() called"] --> LOAD["Load chunks from<br/>knowledge_docs (PGS01)"]

    LOAD --> DOCLING{"Original ext in<br/>Docling formats?<br/>(pdf/docx/html/htm/pptx)"}

    DOCLING -->|"Yes"| DP["Docling parse<br/>_try_docling(original_file)"]
    DOCLING -->|"No"| LC["Use legacy content<br/>from upload time"]

    DP --> DPS{"Docling success?"}
    DPS -->|"No / error"| FAIL1["Return failure<br/>(no fallback — approver retries)"]
    DPS -->|"Yes"| CERR{"Conversion-error<br/>placeholders in output?"}
    CERR -->|"Yes"| FAIL2["Return failure with<br/>failed page ranges"]
    CERR -->|"No"| DOCOK["Docling text ready"]

    LC --> DOCOK

    DOCOK --> COMP{"Scanned/mixed PDF<br/>+ compliance enabled?"}
    COMP -->|"Yes"| COMPL["Deferred compliance:<br/>ComplianceEngine.validate_input()"]
    COMP -->|"No"| RECHUNK

    COMPL --> CB{"Blocked?"}
    CB -->|"Yes"| REJECT["Status = REJECTED<br/>Return compliance error"]
    CB -->|"No"| REDACT["Apply redacted text"]
    REDACT --> RECHUNK

    RECHUNK["Re-chunk with Docling text<br/>section_promoter + _chunk_document_structured"]
    RECHUNK --> CANC1{"Doc deleted<br/>during activation?"}
    CANC1 -->|"Yes"| CANCEL["Cleanup + return cancelled"]
    CANC1 -->|"No"| EMBED

    EMBED["Batch embed<br/>POST /embed (64 chunks/batch)"]
    EMBED --> CANC2{"Doc deleted?"}
    CANC2 -->|"Yes"| CANCEL
    CANC2 -->|"No"| ZVC

    ZVC["Zero-vector check<br/>(>10% zero → fail)"]
    ZVC --> CANC3{"Doc deleted?"}
    CANC3 -->|"Yes"| CANCEL
    CANC3 -->|"No"| PGV

    PGV["Write to pgvector (PGS02)<br/>parent-first, leaves with parent_chunk_id"]
    PGV --> CANC4{"Doc deleted?"}
    CANC4 -->|"Yes"| CANCEL
    CANC4 -->|"No"| CLR

    CLR["Clear staged chunks<br/>from knowledge_docs"]
    CLR --> MDW["Write .md file<br/>(atomic tmp+rename, 30s timeout)"]
    MDW --> CANC5{"Doc deleted?"}
    CANC5 -->|"Yes"| CANCEL
    CANC5 -->|"No"| ACTIVE

    ACTIVE["Flip status → ACTIVE"]
    ACTIVE --> CACHE["Invalidate kb_doc_cache"]
    ACTIVE --> VF["Set valid_from timestamp"]
    ACTIVE --> DEP{"deprecate_prior<br/>+ product_id?"}

    DEP -->|"Yes"| DEPRECATE["Deprecate prior versions:<br/>knowledge_docs.status=DEPRECATED<br/>document_embeddings.status=DEPRECATED<br/>Invalidate caches"]
    DEP -->|"No"| ENT

    DEPRECATE --> ENT{"product_id<br/>set?"}
    ENT -->|"Yes"| ENTE["Enqueue entity extraction<br/>kb_entity_worker"]
    ENT -->|"No"| DONE
    ENTE --> DONE["Return success"]
```

**Cancellation safety**: At every major stage (after Docling parse, before/after embedding, before/after pgvector write, before MD write, before status flip), the module checks whether the document has been marked `DELETING` or `DELETED`. If so, it cleans up any partial outputs (pgvector rows, .md files) and returns a cancelled result.

**Zero-vector guard**: The Ollama embedder silently returns zero vectors for failed individual texts. If more than 10% of chunks are zero vectors, activation fails — preventing documents with silently broken embeddings from becoming ACTIVE.

**Version deprecation**: When `deprecate_prior=True` and `product_id` is set, all prior versions of the same product+domain are atomically deprecated — both in `knowledge_docs` (status → `DEPRECATED`, `valid_to` set) and in `document_embeddings` (chunk-level `status` → `DEPRECATED`). The hybrid search query appends `AND status='ACTIVE'`, so deprecation is instant with no re-indexing.

---

### 4. `delete_doc()` — Document Deletion

Removes a document from all storage layers with cache-first invalidation:

1. **Invalidate cache** — Drop `kb_doc_cache` entry before touching SQL (prevents race where another request warms cache from the row being deleted)
2. **Mark DELETING** — Signal to any in-flight `activate_doc()` workers to stop
3. **Delete filesystem files** — Remove `.md` and original binary (`.<ext>`) from `KB_DOC_STORAGE_PATH`, plus replicas
4. **Delete pgvector rows** — `DELETE FROM document_embeddings WHERE metadata->>'doc_id' = :doc_id`
5. **Delete DB record** — Remove `KnowledgeDocument` row from PGS01
6. **Cleanup namespace** — Remove namespace from Redis KV if no chunks remain

---

### 5. Supporting Functions

| Function | Purpose |
|---|---|
| `list_docs()` | List documents with optional filters (namespace, status, product_id, domain, spec_version) |
| `list_namespaces()` | Return registered namespaces from Redis KV, with DB and pgvector fallbacks |
| `get_original_path()` | Return absolute path to retained original binary for citation "Open original" links |
| `_notify_approvers_kb()` | Fire-and-forget inbox notification to all active approvers (ad_level ≤ 3 or admin) |

---

## Data Model

### PGS01: `knowledge_docs` (KnowledgeDocument)

Stores document metadata, lifecycle status, and staged chunks (pre-embedding).

```mermaid
erDiagram
    knowledge_docs {
        UUID id PK
        STRING name
        STRING filename
        STRING namespace
        TEXT content "Full parsed text (Docling or legacy)"
        STRING content_hash "SHA-256 for dedup"
        JSONB chunks "Staged chunks (cleared after activation)"
        INTEGER chunk_count
        STRING status "PENDING_APPROVAL → INDEXING → ACTIVE"
        STRING visibility "PUBLIC | PRIVATE"
        JSONB department_ids
        UUID product_id FK
        STRING domain
        STRING spec_version
        DATETIME version_date
        BOOLEAN deprecate_prior
        UUID parent_doc_id "Lineage pointer"
        DATETIME valid_from
        DATETIME valid_to
        STRING source_type "BRD|FSD|ARCHITECTURE|..."
        STRING original_ext
        BOOLEAN is_scanned_pdf
        BOOLEAN has_mixed_scanned_pages
        STRING approved_by
        TEXT rejection_reason
        TEXT parse_error
        STRING uploaded_by
        STRING uploaded_by_dept
    }

    document_embeddings {
        UUID id PK
        STRING repo "docs_kb:{namespace}"
        TEXT file_path
        INTEGER chunk_index
        TEXT content
        VECTOR embedding "768-dim"
        JSONB metadata "doc_id, namespace, visibility, ..."
        STRING content_hash
        STRING classification
        UUID product_id FK
        STRING domain
        STRING spec_version
        UUID parent_chunk_id "Leaf → parent section"
        TEXT section_path
        BOOLEAN is_section_parent
        STRING source_type
        TEXT section_name
        INTEGER page_number
        TEXT doc_name
        STRING status "ACTIVE | DEPRECATED"
        STRING department
    }

    knowledge_docs ||--o{ document_embeddings : "doc_id in metadata"
```

### PGS02: `document_embeddings` (DocumentEmbedding)

The pgvector table holding embedded chunks. Written **only** by `activate_doc()`. Each chunk row carries denormalized metadata (doc_name, source_type, section_path, page_number) to enable citation rendering and source-type filtering without cross-DB joins.

---

## Dependency Map

```mermaid
graph LR
    subgraph "This Module"
        DS["store/docs_store.py"]
    end

    subgraph "Parsing"
        DP["core/docling_parser.py<br/>Docling + PaddleOCR"]
        DOP["core/document_parser.py<br/>Legacy file parsers"]
        SP["core/section_promoter.py<br/>Bold→Heading promotion"]
        SS["core/structure_scorer.py<br/>Chunk quality scoring"]
    end

    subgraph "Persistence"
        DB["db/database.py<br/>SessionLocal · VectorSessionLocal"]
        DM["db/models.py<br/>KnowledgeDocument · DocumentEmbedding"]
        KBR["store/kb_replication.py<br/>File replication"]
        KBC["store/kb_doc_cache.py<br/>Document content cache"]
        IS["store/inbox_store.py<br/>Approval notifications"]
    end

    subgraph "Compliance"
        CE["agents/compliance_engine.py<br/>PII/PCI scan + redact"]
    end

    subgraph "Infrastructure"
        CFG["core/config.py<br/>EMBED_SVC_URL · KB_DOC_STORAGE_PATH"]
        LOG["core/logger.py"]
        KV["core/kv<br/>Redis namespace registry"]
    end

    subgraph "Workers"
        KW["workers/kb_worker.py<br/>Background activation"]
        KEW["workers/kb_entity_worker.py<br/>Entity extraction"]
    end

    subgraph "API"
        DR["routers/docs_router.py"]
    end

    subgraph "Retrieval (consumers)"
        HS["models/hybrid_search.py<br/>semantic_search"]
        ACL["core/rag_acl.py<br/>filter_chunks_by_acl"]
        KGE["models/kb_graph_expand.py<br/>neighbors_for_chunks"]
    end

    DR -->|"upload/approve/delete"| DS
    KW -->|"calls activate_doc"| DS
    DS --> DP
    DS --> DOP
    DS --> SP
    DS --> SS
    DS --> DB
    DS --> DM
    DS --> KBR
    DS --> KBC
    DS --> IS
    DS --> CE
    DS --> CFG
    DS --> LOG
    DS --> KV
    DS -->|"enqueue"| KEW

    HS -->|"reads document_embeddings"| DM
    ACL -->|"filters chunks"| HS
    KGE -->|"reads parent_chunk_id"| DM
```

---

## Integration Points

### Upstream Callers

| Caller | Function Called | Context |
|---|---|---|
| `routers/docs_router.py::upload_doc` | `upload_doc()` | HTTP multipart upload with validation, compliance scan, scanned-PDF detection |
| `routers/docs_router.py::approve_doc` | Enqueues `kb_worker.run_activate_doc` | Sets status to `INDEXING`, enqueues RQ job |
| `workers/kb_worker.py::run_activate_doc` | `activate_doc()` | Background worker — handles rollback, original file cleanup, uploader notification |
| `routers/docs_router.py::delete_doc` | `delete_doc()` | HTTP delete with cache invalidation |

### Downstream Consumers

| Consumer | What It Reads | Module Reference |
|---|---|---|
| `models/hybrid_search.py` | `document_embeddings` table — semantic + keyword search with `status='ACTIVE'` filter | [model_routing](../llm/model_routing.md) |
| `core/rag_acl.py` | Chunk metadata (`classification`, `department`, `product_id`) for access control | [core_infrastructure](../core/core_infrastructure.md) |
| `models/kb_graph_expand.py` | `parent_chunk_id`, `section_path` for graph-based context expansion | [model_routing](../llm/model_routing.md) |
| `store/kb_doc_cache.py` | `.md` file on filesystem for full-document content caching | [shared_core_knowledge_base_entity_registry](shared_core_knowledge_base_entity_registry.md) |
| `workers/kb_entity_worker.py` | `knowledge_docs.content` for entity extraction and knowledge graph building | [document_knowledge_workers](../workers/document_knowledge_workers.md) |

---

## End-to-End Document Flow

```mermaid
sequenceDiagram
    participant U as User
    participant API as docs_router
    participant DS as docs_store
    participant DB as PGS01 (knowledge_docs)
    participant FS as Filesystem
    participant KV as Redis KV
    participant INB as Inbox Store
    participant KW as kb_worker
    participant DP as Docling Parser
    participant ES as Embed Service
    participant PGV as PGS02 (pgvector)
    participant CE as ComplianceEngine

    U->>API: POST /api/docs/upload (file + metadata)
    API->>API: Validate file type/size
    API->>API: Legacy parse (skip_docling=True)
    API->>API: Compliance scan (if enabled)
    API->>DS: upload_doc(pre_parsed_text, is_scanned_pdf, ...)

    DS->>DS: _clean_text → content hash
    DS->>DB: Dedup check (content_hash)
    DS->>DS: promote_sections → _chunk_document_structured
    DS->>DS: score_chunk_set (quality scoring)
    DS->>DB: INSERT KnowledgeDocument (status=PENDING_APPROVAL)
    DS->>FS: Save original binary (<id>.<ext>)
    DS->>INB: Notify approvers (kb_approval inbox)
    DS->>KV: SADD docs:namespaces
    DS-->>API: {success, doc_id, chunk_count}
    API-->>U: Upload successful — pending approval

    Note over U,CE: ... Approver reviews and approves ...

    U->>API: POST /api/docs/{doc_id}/approve
    API->>DB: SET status=INDEXING
    API->>KW: Enqueue run_activate_doc(doc_id)

    KW->>DS: activate_doc(doc_id)
    DS->>DB: Load chunks + metadata
    DS->>FS: Read original binary
    DS->>DP: _try_docling(original_file)

    alt Docling success
        DP-->>DS: Markdown text
        DS->>DS: Re-chunk (section_promoter + structured)
        opt Scanned/mixed PDF + compliance ON
            DS->>CE: validate_input(ocr_text)
            CE-->>DS: {blocked?, redacted_text}
        end
    else Docling failure
        DP-->>DS: Error
        DS-->>KW: Return failure
        KW->>DB: Rollback to PENDING_APPROVAL
    end

    DS->>ES: POST /embed (batched, 64/batch)
    ES-->>DS: Embeddings[]
    DS->>DS: Zero-vector check
    DS->>PGV: DELETE stale + INSERT DocumentEmbedding rows
    DS->>DB: Clear staged chunks
    DS->>FS: Write <id>.md (atomic tmp+rename)
    DS->>DB: SET status=ACTIVE
    DS->>KV: Invalidate kb_doc_cache

    opt deprecate_prior=True
        DS->>DB: Deprecate prior versions (status=DEPRECATED)
        DS->>PGV: Flip chunk status=DEPRECATED
    end

    opt product_id set
        DS->>KW: Enqueue entity extraction
    end

    DS-->>KW: {success, chunk_count}
    KW->>FS: Delete original binary
    KW->>INB: Notify uploader (activation complete)
```

---

## Compliance Integration

The module integrates with the [ComplianceEngine](../sdlc/shared_core_sdlc_pipeline.md) at two points, gated by the `COMPLIANCE_SCAN_KB_UPLOAD` environment flag (default OFF):

1. **Upload time** (in `docs_router.py`): For non-scanned documents with extractable text, PII/PCI scan + redaction runs before staging. Blocking types (e.g., card numbers, private keys) reject the upload entirely.

2. **Activation time** (in `activate_doc()`): For scanned/mixed PDFs where compliance was deferred (no OCR text existed at upload), the compliance scan runs after Docling/PaddleOCR extracts text. Blocking types set the document to `REJECTED` with the compliance reasons stored in `rejection_reason`.

When compliance is OFF, raw text is stored and indexed as-is; redaction happens at retrieval time for cloud models only.

---

## Filesystem Layout

```
KB_DOC_STORAGE_PATH/
├── <doc_id>.md              # Canonical markdown body (Docling-processed)
├── <doc_id>.pdf             # Original binary (deleted by kb_worker post-activation)
├── <doc_id>.docx            # Original binary (deleted by kb_worker post-activation)
└── ...
```

- The `.md` file is the single source of truth for full-document content (Coverage tier in RAG).
- The original binary is retained only until activation completes; `kb_worker` deletes it after successful embedding.
- For pending/rejected documents that were never activated, the original binary persists until `delete_doc()` removes it.
- File writes use atomic tmp+rename to be safe for concurrent cache readers.

---

## Key Design Principles

1. **Approval-gated embedding**: Vectors are never written to pgvector until a document is approved. Unapproved content is invisible to RAG search.

2. **Deferred expensive parsing**: Docling/PaddleOCR runs only post-approval in `activate_doc()`, not at upload time. This avoids wasted parse calls for documents deleted before approval.

3. **Structure-aware chunking**: The chunker respects Markdown headings, tables (atomic), code blocks (atomic with language inference), and sentence boundaries — producing parent+leaf pairs with section-path breadcrumbs for hierarchical retrieval.

4. **Cancellation safety**: Every stage of `activate_doc()` checks for document deletion, cleaning up partial outputs if the document was deleted mid-activation.

5. **User-facing error sanitization**: Internal exceptions (DB errors, service URLs, model names) are logged in full but never surfaced to users. Error messages are scrubbed via `sanitize_user_error()` before returning.

6. **Version deprecation**: When a new spec version activates with `deprecate_prior=True`, prior versions are atomically deprecated at both the document and chunk level — no re-indexing required.

7. **Multi-DB architecture**: Document metadata lives in PGS01 (PostgreSQL main DB); vector embeddings live in PGS02 (pgvector). Chunk metadata is denormalized onto embedding rows to avoid cross-DB joins during retrieval.
