# Ollama Gateway

## Brief Introduction

The `ollama_gateway` module is a **deprecated compatibility shim** that preserves legacy import paths for code previously relying on Ollama for LLM inference. In the current architecture, all LLM inference is routed through the in-house [local_llm_gateway](local_llm_gateway.md) proxy, and Ollama is used **only** by the [embedding_service](../knowledge/embedding_service.md) for generating `nomic-embed-text` embeddings.

The module exposes two functions:

- `generate(...)` — redirects text generation requests to `LocalLLMGateway`.
- `count_tokens(text)` — returns a rough character-based token estimate.

> **Deprecation Notice:** New code should call `gateway_local_llm.get_local_gateway()` directly instead of importing `gateway_ollama`. This shim exists solely to avoid breaking older call sites.

---

## Module Purpose and Core Functionality

### Purpose

1. **Backward Compatibility:** Keep legacy `from gateway_ollama import generate` imports working without runtime errors.
2. **Inference Redirection:** Transparently forward any generation call to the canonical in-house local LLM proxy.
3. **Token Estimation Fallback:** Provide a simple, dependency-free token count heuristic for contexts that do not need exact tokenizer counts.

### Core Functions

| Function | Purpose | Behavior |
|----------|---------|----------|
| `generate(prompt, system_prompt="", max_tokens=4096, temperature=0.2)` | Text generation | Prepends `system_prompt` to `prompt` and streams the response from `LocalLLMGateway.generate(..., tier="simple")`. Returns the concatenated string. |
| `count_tokens(text)` | Token counting | Returns `max(1, len(text) // 4)` as a coarse 4-characters-per-token estimate. |

---

## Architecture and Component Relationships

### High-Level Position

```mermaid
flowchart LR
    subgraph LegacyCode["Legacy Call Sites"]
        A[import gateway_ollama]
    end

    subgraph OllamaGateway["ollama_gateway<br/>gateway_ollama.py"]
        B["generate()"]
        C["count_tokens()"]
    end

    subgraph LocalLLM["local_llm_gateway<br/>gateway_local_llm.py"]
        D["LocalLLMGateway"]
        E["_ModelCatalog"]
    end

    subgraph EmbedService["embedding_service<br/>services/embed_svc/"]
        F["OllamaEmbedder"]
        G["/api/embed"]
    end

    A --> B
    B -->|forwards| D
    D -->|discovers| E
    C -->|heuristic| H[(Token estimate)]
    F -->|uses| G

    style OllamaGateway fill:#fff4e6,stroke:#ff9900
    style EmbedService fill:#e6f7ff,stroke:#1890ff
```

### Gateway Ecosystem Context

```mermaid
flowchart TB
    subgraph Providers["Cloud / Local LLM Providers"]
        OAI[OpenAI]
        CLA[Anthropic Claude]
        GEM[Google Gemini]
        LOC[In-house Local LLM Proxy]
    end

    subgraph Gateways["Provider Gateways"]
        OG[openai_gateway]
        CG[claude_gateway]
        GG[gemini_gateway]
        LLG[local_llm_gateway]
        OLG[ollama_gateway<br/>deprecated shim]
    end

    subgraph Consumers["Consumers"]
        GWP[gateway.py]
        MR[model_router]
        RF[ReactOrchestrator]
    end

    OAI --> OG
    CLA --> CG
    GEM --> GG
    LOC --> LLG
    OLG -.->|redirects| LLG

    OG --> GWP
    CG --> GWP
    GG --> GWP
    LLG --> GWP
    OLG -.-> GWP

    MR -->|selects provider| Gateways
    RF -->|tool-use loops| OG & CG & GG & LLG

    style OLG fill:#fff4e6,stroke:#ff9900
```

### Relationship with Embedding Service

Although `ollama_gateway` no longer performs LLM inference, Ollama itself remains in use inside the platform through the dedicated embedding service:

```mermaid
flowchart LR
    A[Embedding Request] --> B[embedding_service]
    B --> C{Provider?}
    C -->|ollama / default| D[OllamaEmbedder]
    C -->|openai| E[OpenAIEmbedder]
    C -->|nomic| F[NomicEmbedder]
    D --> G[Ollama /api/embed]
    G --> H[nomic-embed-text]
    H --> I[768-dim vectors]
```

See [embedding_service](../knowledge/embedding_service.md) for details on batching, caching, multi-instance load balancing, and fallback behavior.

---

## Data Flow

### `generate()` Redirection Flow

```mermaid
sequenceDiagram
    autonumber
    participant Caller as Legacy Caller
    participant OG as gateway_ollama.generate
    participant LLG as LocalLLMGateway
    participant Proxy as In-house LLM Proxy

    Caller->>OG: prompt, system_prompt, max_tokens, temperature
    OG->>OG: prepend system_prompt to prompt
    OG->>LLG: get_local_gateway()
    OG->>LLG: generate(full_prompt, tier="simple")
    LLG->>LLG: pick model from _ModelCatalog
    LLG->>Proxy: POST /v1/chat/completions (stream)
    Proxy-->>LLG: SSE token chunks
    LLG-->>OG: yield tokens
    OG->>OG: "".join(tokens)
    OG-->>Caller: final string
```

### `count_tokens()` Flow

```mermaid
sequenceDiagram
    autonumber
    participant Caller as Caller
    participant CT as gateway_ollama.count_tokens
    Caller->>CT: text
    CT->>CT: max(1, len(text) // 4)
    CT-->>Caller: integer token estimate
```

---

## Component Interaction

### Internal Components

```mermaid
flowchart TB
    subgraph gateway_ollama["gateway_ollama.py"]
        GEN["generate()"]
        TOK["count_tokens()"]
    end

    GEN -->|imports| GET["gateway_local_llm.get_local_gateway()"]
    GET -->|returns| GW["LocalLLMGateway instance"]
    GEN -->|calls| GWGEN["LocalLLMGateway.generate()"]
    GWGEN -->|uses| CAT["_ModelCatalog"]
    GWGEN -->|HTTP/SSE| PROXY["In-house LLM proxy<br/>vLLM / TGI / Ollama"]

    TOK -->|heuristic| EST["len(text) // 4"]
```

### External Dependencies

| Dependency | Module | Role |
|------------|--------|------|
| `core.logger` | [shared_core](../reference/shared_core.md) | Structured logging and request-id propagation. |
| `gateway_local_llm` | [local_llm_gateway](local_llm_gateway.md) | Canonical provider for local LLM inference. |
| `services/embed_svc` | [embedding_service](../knowledge/embedding_service.md) | The only production consumer of Ollama (`nomic-embed-text`). |

---

## Process Flows

### Legacy Generation Request Handling

```mermaid
flowchart TD
    A[Legacy caller invokes gateway_ollama.generate] --> B{system_prompt provided?}
    B -->|yes| C[Concatenate system_prompt + prompt]
    B -->|no| D[Use prompt as-is]
    C --> E[Resolve LocalLLMGateway singleton]
    D --> E
    E --> F[Call LocalLLMGateway.generate with tier=simple]
    F --> G[Stream tokens from in-house proxy]
    G --> H[Join tokens into final string]
    H --> I[Return string to caller]
```

### Token Estimation

```mermaid
flowchart TD
    A[Caller invokes count_tokens] --> B{len(text) < 4?}
    B -->|yes| C[Return 1]
    B -->|no| D[Return len(text) // 4]
```

---

## How It Fits into the Overall System

1. **Gateway Layer:** `gateway_ollama` sits alongside [openai_gateway](openai_gateway.md), [claude_gateway](claude_gateway.md), [gemini_gateway](gemini_gateway.md), and [local_llm_gateway](local_llm_gateway.md) as a provider-facing module. Unlike the others, it is not a first-class provider; it is a redirector.
2. **Model Router:** The [model_router](../reference/shared_core.md#model_routing) selects providers based on policy, cost, availability, and compliance. It does not select `ollama_gateway`; it selects `local_llm_gateway` when a local model is appropriate.
3. **Embedding Service:** Ollama's production role moved to the [embedding_service](../knowledge/embedding_service.md), where `OllamaEmbedder` calls `/api/embed` on one or more Ollama instances for `nomic-embed-text` embeddings.
4. **Request Tracing:** The shim relies on `core.logger.get_request_id()` to propagate the request ID through `LocalLLMGateway` and onward as `X-Request-ID` to the in-house LLM proxy.

---

## Migration Guidance

Replace legacy imports:

```python
# Deprecated
from gateway_ollama import generate
result = generate(prompt, system_prompt="...")

# Recommended
from gateway_local_llm import get_local_gateway
gw = get_local_gateway()
result = "".join(gw.generate(prompt, tier="simple"))
```

For exact token counts, use the tokenizer exposed by the active provider or the platform's token utilities instead of `count_tokens()`.

---

## References

- [local_llm_gateway](local_llm_gateway.md) — Canonical local LLM inference gateway.
- [embedding_service](../knowledge/embedding_service.md) — Production Ollama consumer for `nomic-embed-text` embeddings.
- [openai_gateway](openai_gateway.md) — OpenAI provider gateway.
- [claude_gateway](claude_gateway.md) — Anthropic Claude provider gateway.
- [gemini_gateway](gemini_gateway.md) — Google Gemini provider gateway.
- [gateway](gateway.md) — Main API gateway that orchestrates provider selection.
- [shared_core](../reference/shared_core.md) — Core logging, compliance, and routing infrastructure.
