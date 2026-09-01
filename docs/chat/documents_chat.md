# documents_chat

## Brief Introduction

`documents_chat` is a small, focused frontend module in the `ai-ui` application. Its single responsibility is to render an inline **document disambiguation picker** inside the Knowledge Base (KB) chat stream. When the backend's `/ask` endpoint detects that a user query could match multiple KB documents, it returns a special `__clarify__` Server-Sent Events (SSE) frame instead of a normal answer. `documents_chat` displays that frame as an interactive card, lets the user select one or more documents, and automatically re-sends the original question scoped to the selected documents.

The module lives at:

```
ai-ui/src/components/DocPickerCard.jsx
```

and is consumed by the KB chat component at:

```
ai-ui/src/components/KbChat.jsx
```

---

## Core Component

### `DocPickerCard`

`DocPickerCard` is a React functional component that renders a scrollable, selectable list of candidate documents.

**Props**

| Prop | Type | Description |
|------|------|-------------|
| `message` | `string` | Human-readable prompt from the backend (e.g. "I found 5 related documents..."). |
| `candidates` | `{ doc_id, doc_name, score }[]` | All matching documents, sorted by relevance descending. |
| `multiSelect` | `boolean` | If `true`, renders checkboxes; otherwise radio buttons. |
| `onConfirm` | `(selectedDocIds: string[]) => void` | Callback invoked when the user confirms a selection. |

**Key behaviors**

- **No upper cap on candidates** — every matching document is shown. The list is scrollable (`max-h-72`) so the card never overflow the chat window.
- **Relative relevance badges** — each document shows a 0–100% badge normalized against the top score, color-coded green/amber/gray.
- **Select all / deselect all** — shown only in multi-select mode when more than one document is present.
- **Two action buttons** — "Search in selected" (enabled only when at least one doc is selected) and "Search in all" (always available).
- **Re-sends the original question** scoped to the chosen `doc_id`s via the parent callback.

---

## Where It Fits in the System

`documents_chat` sits at the intersection of three larger subsystems:

1. **KB Chat UI** (`KbChat.jsx`) — renders messages, handles streaming, and decides when to mount a `DocPickerCard`.
2. **Gateway `/ask` fast-path** (`gateway.py`) — performs KB retrieval and emits the `__clarify__` SSE frame when disambiguation is needed.
3. **KB retrieval stack** (`models/hybrid_search.py`, `models/hybrid_retriever.py`, `store/kb_doc_cache.py`) — provides the ranked document candidates and later serves full-document coverage for the re-query.

```mermaid
flowchart TB
    subgraph Frontend["ai-ui Frontend"]
        KbChat["KbChat.jsx<br/>KB chat message list"]
        DocPickerCard["DocPickerCard.jsx<br/>documents_chat module"]
    end

    subgraph Gateway["Gateway (gateway.py)"]
        AskEndpoint["POST /ask"]
        DisambigGate["KB Disambiguation Gate"]
        Coverage["KB Coverage / full_file retrieval"]
    end

    subgraph Retrieval["Retrieval Stack"]
        HybridSearch["models/hybrid_search.py"]
        HybridRetriever["models/hybrid_retriever.py"]
        KbCache["store/kb_doc_cache.py"]
    end

    KbChat -->|"1. sends question"| AskEndpoint
    AskEndpoint -->|"2. probes docs_kb via"| HybridSearch
    HybridSearch -->|"3. ranked candidates"| DisambigGate
    DisambigGate -->|"4. __clarify__ SSE frame"| KbChat
    KbChat -->|"5. renders picker"| DocPickerCard
    DocPickerCard -->|"6. user selects doc_ids"| KbChat
    KbChat -->|"7. re-POST /ask with kb_doc_ids"| AskEndpoint
    AskEndpoint -->|"8. full_file coverage on selected docs"| Coverage
    Coverage -->|"9. reads cached docs"| KbCache
```

---

## Architecture

### Component Hierarchy

```mermaid
flowchart LR
    subgraph ai-ui/src/components
        KbChat["KbChat.jsx"]
        DocPickerCard["DocPickerCard.jsx"]
        DocsPanel["DocsPanel.jsx"]
        DocPreviewCard["DocPreviewCard.jsx"]
        DocWorkflowCard["DocWorkflowCard.jsx"]
    end

    KbChat -->|"renders role=doc_picker_card messages"| DocPickerCard
    KbChat -.->|"sibling document components"| DocsPanel
    KbChat -.->|"sibling document components"| DocPreviewCard
    KbChat -.->|"sibling document components"| DocWorkflowCard
```

`DocPickerCard` is intentionally a **leaf component**: it owns no state beyond the current selection and delegates all side effects (re-query, message list updates) to `KbChat` through the `onConfirm` callback.

### Message Role Contract

`KbChat` stores the picker as a message object with a special role:

```javascript
{
  id: crypto.randomUUID(),
  role: "doc_picker_card",
  question: clarify.question,
  message: clarify.message,
  candidates: clarify.candidates || [],
  multiSelect: clarify.multi_select !== false,
  streaming: false,
}
```

When iterating over `messages`, `KbChat` branches on `msg.role === "doc_picker_card"` and renders `DocPickerCard` instead of the standard user/assistant bubbles.

---

## Data Flow

### Triggering Disambiguation

The backend `/ask` fast-path runs a KB probe whenever `rag_mode` is `auto`/`on` or the request comes from voice mode. After retrieving and reranking chunks, it counts distinct matching documents.

```mermaid
sequenceDiagram
    actor User
    participant KbChat as KbChat.jsx
    participant Ask as POST /ask
    participant Probe as KB Probe
    participant Picker as DocPickerCard

    User->>KbChat: asks a KB-scoped question
    KbChat->>Ask: POST /ask {question, chat_id, scope...}
    Ask->>Probe: pgvector + BM25 across docs_kb namespaces
    Probe-->>Ask: ranked chunks + doc metadata
    Ask->>Ask: count distinct doc_ids
    alt distinct docs >= threshold
        Ask-->>KbChat: SSE: {__clarify__: {question, message, candidates, multi_select}}
        KbChat->>Picker: render DocPickerCard
        User->>Picker: selects documents
        Picker->>KbChat: onConfirm(selectedDocIds)
        KbChat->>Ask: POST /ask {question, kb_doc_ids: [...]}
        Ask->>Ask: skip disambig gate (kb_doc_ids set)
        Ask-->>KbChat: SSE: normal answer from selected docs
    else below threshold
        Ask-->>KbChat: SSE: direct answer
    end
```

### Threshold Logic

The disambiguation threshold is adaptive:

- **At scope level** (domain or product selected in the KB scope picker): threshold is `1`. The picker fires even for a single matching document so the user always confirms the exact document before the LLM answers.
- **General chat / no scope**: threshold defaults to `4` and is tunable via `KB_DISAMBIG_MIN_DOCS`.

The gate is skipped when:

- The user already selected a specific `kb_doc_id` in the scope picker.
- The request includes `kb_doc_ids` (a re-query from the picker).

### Re-Query Path

When the user confirms a selection, `KbChat` calls `sendDisambigMessage(question, kbDocIds)`:

1. Removes the picker card from the message list.
2. Appends a fresh assistant placeholder message.
3. POSTs to `/ask` with `kb_doc_ids` set.
4. The gateway bypasses the disambiguation gate and runs `full_file` coverage on exactly those documents.
5. The streaming answer is rendered normally.

---

## Dependencies

### Direct Consumers

- **[KbChat.jsx](../knowledge/kb_chat.md)** — mounts `DocPickerCard` and handles `onConfirm` by re-sending the scoped query.

### Backend Collaborators

- **[gateway.py](../models/gateway.md) `POST /ask`** — emits the `__clarify__` SSE frame and honors `kb_doc_ids` on re-query.
- **[models/hybrid_search.py](../hybrid_search.md)** — provides `pgvector_search` and `keyword_search` used by the KB probe.
- **[models/hybrid_retriever.py](../hybrid_retriever.md)** — supplies the BGE reranker (`_rerank_via_svc`) used to score candidates before disambiguation.
- **[store/kb_doc_cache.py](../kb_doc_cache.md)** — serves cached full-document text for the `full_file` coverage path after the user selects documents.

### Sibling Document Components

- **[DocsPanel.jsx](../documents/documents_guide.md)** — platform documentation browser.
- **[DocPreviewCard.jsx](../documents/documents_preview.md)** — expandable document summary card.
- **[DocWorkflowCard.jsx](../documents/documents_generation.md)** — PPTX theme picker for generated presentations.

---

## Process Flow: Rendering a Picker

```mermaid
flowchart TD
    A[User sends KB-scoped question] --> B[KbChat streams /ask response]
    B --> C{SSE event type?}
    C -->|t / __meta__| D[Render normal assistant message]
    C -->|__clarify__| E[Inject doc_picker_card message]
    E --> F[Message loop renders DocPickerCard]
    F --> G{User action}
    G -->|Select docs + Search in selected| H[KbChat calls sendDisambigMessage]
    H --> I[Remove picker card, append assistant placeholder]
    I --> J[POST /ask with kb_doc_ids]
    J --> K[Gateway runs full_file coverage]
    K --> L[Stream final answer]
    G -->|Search in all| M[onConfirm with all candidate ids]
    M --> J
```

---

## Design Notes

- **State isolation** — `DocPickerCard` keeps only the local `selected` set. It does not talk to the backend directly; this keeps the component testable and avoids duplicating chat state logic.
- **Accessibility** — inputs use native checkboxes/radios with labels, and the list is keyboard-focusable.
- **Performance** — the candidate list is virtualized by CSS scroll, not by a virtual-list library, because the design requirement is to show *all* matches without an arbitrary cap.
- **No PII** — the picker only displays document names and relevance scores; no document content is rendered in the card.

---

## Related Documentation

- [kb_chat.md](../knowledge/kb_chat.md) — the parent KB chat component that hosts the picker.
- [gateway.md](../models/gateway.md) — the `/ask` endpoint and disambiguation gate.
- [hybrid_search.md](../hybrid_search.md) — semantic and keyword retrieval.
- [hybrid_retriever.md](../hybrid_retriever.md) — reranking and coverage dispatch.
- [documents.md](../documents/documents.md) — overview of the `documents` feature family.
