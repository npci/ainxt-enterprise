# CIL Intent — Conversation Intelligence Layer: Understanding Stage

## Brief Introduction

The `cil_intent` module is the **understanding stage** of the Conversation Intelligence Layer (CIL). It transforms a raw user turn into a structured `UnifiedIntent` object using a single fast LLM call — **no regex, no keyword heuristics**. This is what elevates the platform from a simple prompt→LLM→UI chatbot into an *intelligence* layer: the model genuinely *understands* whether a turn is trivial vs. deep, a follow-up vs. new, whether it needs tools/retrieval/freshness, and whether it should route to a skill or autonomous agent.

The module is designed with a **fail-safe philosophy**: `classify()` returns `None` on any failure (model outage, bad JSON, timeout), allowing the caller to degrade to safe static defaults (`medium`/`general`/`chat`) — never to regex, and never a crash.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "Conversation Intelligence Layer (CIL)"
        subgraph "cil_intent — Understanding Stage"
            classify["classify()<br/>Model-only intent classifier"]
            UnifiedIntent["UnifiedIntent<br/>Structured intent dataclass"]
            to_state["to_conversation_state()<br/>Maps intent → ConversationState"]
            parse_json["_parse_json()<br/>Robust JSON recovery"]
            cache["Classification Cache<br/>SHA-256 keyed, TTL-based"]
        end

        cil_lexical["cil_lexical<br/>Cheap regex prefilter hints"]
        cil_policy["cil_policy<br/>Domain policy & risk derivation"]
        cil_state["cil/state.py<br/>ConversationState, Score"]
    end

    subgraph "External Dependencies"
        model_router["model_routing<br/>ModelRouter.generate()"]
        doc_intent["models/doc_intent.py<br/>Artifact signal & JSON parser"]
        kv_store["kv_store<br/>Redis KV cache"]
        core_config["core_config<br/>CIL_INTENT_MODEL, RDB_CACHE"]
        core_logger["core_infrastructure<br/>Logger"]
    end

    classify -->|"uses"| model_router
    classify -->|"caches via"| kv_store
    classify -->|"reads config"| core_config
    classify -->|"logs"| core_logger
    classify -->|"parses JSON via"| parse_json
    parse_json -->|"delegates to"| doc_intent
    classify -->|"returns"| UnifiedIntent
    to_state -->|"consumes"| UnifiedIntent
    to_state -->|"produces"| cil_state
    cil_lexical -.->|"provides hints to<br/>upstream caller"| classify
    cil_policy -.->|"derives policy from<br/>ConversationState"| to_state

    style classify fill:#4a90d9,color:#fff
    style UnifiedIntent fill:#50b87e,color:#fff
    style to_state fill:#e8a838,color:#fff
    style cache fill:#9b6dc4,color:#fff
```

### Design Principles

| Principle | Implementation |
|-----------|---------------|
| **Model-only, zero regex** | All intent detection is performed by a single LLM call. No keyword/regex heuristics anywhere in this module. |
| **Fail-safe** | `classify()` returns `None` on any failure; callers degrade to safe static defaults. |
| **Single source of truth for doc intent** | Document-generation intent is merged into `UnifiedIntent` but `models/doc_intent.py` remains the sole doc authority for artifact-signal detection. |
| **Performance caching** | Results are cached by a SHA-256 hash of all classification inputs, so identical turns skip the LLM round-trip (~2.3s in production). |
| **Enum validation & clamping** | All model-returned fields are validated against enum sets and clamped to valid ranges before use. |

---

## Core Components

### 1. `UnifiedIntent` Dataclass

The central structured output of the understanding stage. Encapsulates every signal the model extracts from a single user turn.

```mermaid
classDiagram
    class UnifiedIntent {
        +String task_complexity
        +String domain
        +bool is_continuation
        +String output_format
        +float tool_need
        +float retrieval_need
        +String freshness_need
        +String route
        +String skill_hint
        +String agent_hint
        +bool clarification_needed
        +String tone
        +float formality
        +String language
        +String sentiment
        +bool wants_brief
        +float confidence
        +String reason
        +dict raw
        +String doc_intent
        +String doc_format
        +String doc_source_scope
        +String doc_target_artifact_id
        +bool doc_needs_topic
        +String doc_topic
        +float doc_confidence
        +String doc_reason
    }
```

**Field Groups:**

| Group | Fields | Purpose |
|-------|--------|---------|
| **Understanding** | `task_complexity`, `domain`, `is_continuation`, `output_format` | Core comprehension of what the user is asking |
| **Needs** | `tool_need`, `retrieval_need`, `freshness_need` | What external resources the turn requires |
| **Routing** | `route`, `skill_hint`, `agent_hint`, `clarification_needed` | Where the turn should be dispatched |
| **Style/Persona** | `tone`, `formality`, `language`, `sentiment`, `wants_brief` | How the response should be shaped |
| **Document Intent** | `doc_intent`, `doc_format`, `doc_source_scope`, `doc_target_artifact_id`, `doc_needs_topic`, `doc_topic`, `doc_confidence` | Whether the user wants a downloadable file artifact |
| **Provenance** | `confidence`, `reason`, `raw` | Trust and debuggability metadata |

**Valid Enum Values:**

| Field | Valid Values |
|-------|-------------|
| `task_complexity` | `simple`, `medium`, `complex`, `deep`, `solution` |
| `route` | `chat`, `skill`, `agent`, `analyse` |
| `output_format` | `prose`, `code`, `table`, `document`, `data` |
| `freshness_need` | `none`, `low`, `high` |
| `tone` | `formal`, `neutral`, `casual`, `frustrated`, `excited` |
| `sentiment` | `neg`, `neutral`, `pos` |
| `doc_intent` | `generate`, `summarize`, `convert`, `extract`, `compare`, `revise`, `none` |
| `doc_format` | `pdf`, `docx`, `pptx`, `xlsx`, `csv`, `md`, `txt` |
| `doc_source_scope` | `uploaded`, `chat`, `artifact`, `none` |

---

### 2. `classify()` — The Primary Entry Point

```mermaid
flowchart TD
    Start["classify(text, rag_mode, history_summary,<br/>has_attachments, doc_memory_summary,<br/>has_chat_context, recent_turns, include_doc_intent)"]
    
    Start --> EmptyCheck{"text empty?"}
    EmptyCheck -->|"Yes"| ReturnNone["return None"]
    EmptyCheck -->|"No"| CacheCheck{"Cache enabled?"}
    
    CacheCheck -->|"Yes"| BuildKey["Build cache key<br/>(SHA-256 of all inputs)"]
    CacheCheck -->|"No"| BuildPrompt
    
    BuildKey --> CacheLookup["Lookup in KV store"]
    CacheLookup --> CacheHit{"Cache HIT?"}
    CacheHit -->|"Yes"| Deserialize["Deserialize UnifiedIntent<br/>from cached JSON"]
    CacheHit -->|"No"| BuildPrompt
    Deserialize --> ReturnResult["return UnifiedIntent"]
    
    BuildPrompt["Build system prompt<br/>(_build_sys_prompt)"]
    BuildPrompt --> BuildContext["Assemble context:<br/>history, rag_mode, attachments,<br/>doc_memory, recent turns"]
    BuildContext --> LLMCall["model_router.generate()<br/>with model_hint=_INTENT_MODEL"]
    
    LLMCall --> ErrorCheck{"Empty or<br/>'Error:' sentinel?"}
    ErrorCheck -->|"Yes"| FailSafe["Log warning<br/>return None"]
    ErrorCheck -->|"No"| ParseJSON["_parse_json(raw)"]
    
    ParseJSON --> ValidateJSON{"Valid dict?"}
    ValidateJSON -->|"No"| FailSafe
    ValidateJSON -->|"Yes"| Normalize["Normalize & validate:<br/>enum checks, clamp 0-1,<br/>route sanity (skill/agent<br/>without name → chat)"]
    
    Normalize --> BuildResult["Construct UnifiedIntent"]
    BuildResult --> CacheWrite{"Cache key<br/>available?"}
    CacheWrite -->|"Yes"| WriteCache["setex to KV store<br/>(best-effort)"]
    CacheWrite -->|"No"| ReturnResult
    WriteCache --> ReturnResult
    
    style Start fill:#4a90d9,color:#fff
    style FailSafe fill:#d94a4a,color:#fff
    style ReturnResult fill:#50b87e,color:#fff
    style CacheHit fill:#9b6dc4,color:#fff
```

**Key Behaviors:**

- **Model selection**: Uses `CIL_INTENT_MODEL` from `core_config` (defaults to `"local_mini"` tier, configured via `OPENAI_OSS_MODEL`). The `model_router` cascades from local→cloud on outage.
- **Cache key**: A SHA-256 hash of `text`, `rag_mode`, `history_summary`, `has_attachments`, `doc_memory_summary`, `has_chat_context`, `recent_turns[-5:]`, and `include_doc_intent`. The cache is a pure function of inputs — no `chat_id`/`user_id`/time dependence — so a HIT is exactly the answer the model would give.
- **Cache TTL**: Default 3600s (1 hour), configurable via `CIL_INTENT_CACHE_TTL`.
- **Cache toggle**: `CIL_INTENT_CACHE_ENABLED=false` disables caching without a deploy.
- **Route sanitization**: If the model returns `route=skill` or `route=agent` but no `skill_hint`/`agent_hint`, the route is clamped to `chat`. The authoritative existence check against the DB is done by the gateway downstream.
- **Doc-intent gating**: When `include_doc_intent=False`, all doc-intent fields are forced to defaults (`none`/`None`/`0.0`), and the system prompt omits the doc-intent schema entirely, producing a shorter prompt.

---

### 3. `to_conversation_state()` — Intent → State Mapping

Maps a `UnifiedIntent` into a `ConversationState` (defined in `cil/state.py`), ensuring every downstream reader operates on the same state shape regardless of whether the state was produced by the model classifier or by safe static defaults.

```mermaid
flowchart LR
    subgraph "UnifiedIntent"
        ui_fields["task_complexity, domain,<br/>is_continuation, output_format,<br/>freshness_need, route,<br/>skill_hint, agent_hint,<br/>tool_need, retrieval_need,<br/>tone, formality, language,<br/>sentiment, wants_brief,<br/>doc_intent, doc_format, ..."]
    end

    subgraph "to_conversation_state()"
        mapping["Field-by-field mapping<br/>+ Score wrapping<br/>+ tone gating"]
    end

    subgraph "ConversationState"
        cs_fields["Same shape as analyze() returns<br/>+ intent_source='model'<br/>+ signal_sources=['model']"]
    end

    ui_fields --> mapping
    mapping --> cs_fields

    style mapping fill:#e8a838,color:#fff
```

**Notable mapping rules:**

| UnifiedIntent Field | ConversationState Field | Transformation |
|---------------------|------------------------|----------------|
| `tool_need` (float) | `tool_need` (Score) | Wrapped as `Score(score=ui.tool_need, tags=[domain])` |
| `retrieval_need` (float) | `retrieval_need` (Score) | Only set when `rag_mode != "off"` |
| `route` | `intent` | Direct copy |
| `confidence` | `intent_conf`, `classifier_conf` | Set to same value |
| `tone`, `formality`, `language`, `sentiment`, `wants_brief` | Same names | Only surfaced when `CIL_TONE_DETECT=true` (default); otherwise neutral defaults are preserved |
| `doc_*` fields | `doc_*` fields | Direct copy |
| — | `intent_source` | Hardcoded to `"model"` |
| — | `signal_sources` | Hardcoded to `["model"]` |

---

### 4. `_parse_json()` — Robust JSON Recovery

A defensive JSON extractor that handles common LLM output quirks:

1. **Delegates** to `models/doc_intent.py::_parse_json` (which itself delegates to `agents/doc_generator_agent._parse_llm_json`) when available.
2. **Fallback**: Strips markdown code fences (` ``` `), finds the first `{` and last `}`, and attempts `json.loads` on the slice.
3. **Never raises** — returns whatever `json.loads` produces or propagates the exception to the caller's `try/except`.

> See [cil_policy](cil_policy.md) and [cil_lexical](cil_lexical.md) for how lexical hints and domain policies complement the model-based classification.

---

## System Prompt Construction

The system prompt is dynamically assembled by `_build_sys_prompt()`:

```mermaid
flowchart TD
    Base["_BASE_SYS<br/>Core JSON schema + role description"]
    Guidance["_GUIDANCE<br/>Classification rules for each field"]
    DocSchema["_DOC_INTENT_SYS<br/>Doc-intent JSON keys"]
    DocGuidance["_DOC_INTENT_GUIDANCE<br/>Doc-intent classification rules"]
    
    Base --> Decision{"include_doc_intent?"}
    Guidance --> Decision
    
    Decision -->|"True"| Merge["Insert doc-intent keys<br/>before confidence key<br/>in base schema"]
    DocSchema --> Merge
    DocGuidance --> Merge
    Merge --> FullPrompt["Full system prompt<br/>(base + doc schema + guidance + doc guidance)"]
    
    Decision -->|"False"| ShortPrompt["Short system prompt<br/>(base + guidance only)"]
    
    style Merge fill:#4a90d9,color:#fff
    style FullPrompt fill:#50b87e,color:#fff
    style ShortPrompt fill:#e8a838,color:#fff
```

The `include_doc_intent` flag is set by the upstream caller (gateway) based on `models/doc_intent.py::_has_artifact_signal()` — a cheap lexical check that detects whether the prompt references a downloadable file/artifact. When no artifact signal is present, the prompt is shorter and the model is not asked to produce doc-intent fields, saving tokens and reducing classification noise.

---

## Data Flow: End-to-End Turn Processing

```mermaid
sequenceDiagram
    participant GW as Gateway
    participant DI as models/doc_intent.py
    participant CIL as cil/intent.py
    participant MR as model_routing (ModelRouter)
    participant KV as kv_store (Cache)
    participant State as cil/state.py
    participant Policy as cil_policy

    GW->>DI: _has_artifact_signal(text)
    DI-->>GW: bool (include_doc_intent)

    GW->>CIL: classify(text, rag_mode, history_summary,<br/>has_attachments, doc_memory_summary,<br/>has_chat_context, recent_turns, include_doc_intent)

    alt Cache enabled
        CIL->>KV: GET(cache_key)
        alt Cache HIT
            KV-->>CIL: cached JSON
            CIL-->>GW: UnifiedIntent (from cache)
        else Cache MISS
            KV-->>CIL: nil
    end
    end

    alt Cache MISS or disabled
        CIL->>CIL: _build_sys_prompt(include_doc_intent)
        CIL->>MR: generate(prompt, model_hint=local_mini)
        MR-->>CIL: raw JSON string

        CIL->>CIL: _parse_json(raw)
        CIL->>CIL: Validate enums, clamp values,<br/>sanitize routes
        CIL-->>CIL: UnifiedIntent

        alt Cache enabled
            CIL->>KV: SETEX(cache_key, TTL, JSON)
        end

        CIL-->>GW: UnifiedIntent
    end

    alt classify returned None (failure)
        GW->>GW: Use safe static defaults<br/>(medium/general/chat)
    else classify succeeded
        GW->>CIL: to_conversation_state(ui, rag_mode)
        CIL->>State: Construct ConversationState
        State-->>CIL: ConversationState
        CIL-->>GW: ConversationState

        GW->>Policy: derive_policy(state, profile)
        Policy-->>GW: Policy decisions<br/>(risk, clarification, tools_allowed)
    end

    GW->>GW: Route to chat / skill / agent / analyse<br/>based on ConversationState.intent
```

---

## Dependency Graph

```mermaid
graph LR
    subgraph "cil_intent module"
        intent["cil/intent.py"]
    end

    subgraph "CIL siblings"
        state_mod["cil/state.py<br/>(ConversationState, Score)"]
        lexical["cil/lexical.py<br/>(lexical hints)"]
        policy["cil/policy.py<br/>(DomainProfile, derive_policy)"]
    end

    subgraph "Model layer"
        model_router["models/model_router.py<br/>(ModelRouter.generate)"]
        doc_intent_mod["models/doc_intent.py<br/>(_has_artifact_signal, _parse_json)"]
    end

    subgraph "Infrastructure"
        config["core/config.py<br/>(CIL_INTENT_MODEL, RDB_CACHE)"]
        kv["core/kv<br/>(get_kv, Redis)"]
        logger["core/logger.py<br/>(logger)"]
    end

    intent -->|"to_conversation_state()"| state_mod
    intent -->|"classify() LLM call"| model_router
    intent -->|"_parse_json() delegates"| doc_intent_mod
    intent -->|"cache read/write"| kv
    intent -->|"reads CIL_INTENT_MODEL,<br/>RDB_CACHE"| config
    intent -->|"logging"| logger

    lexical -.->|"upstream caller uses<br/>for cheap hints"| intent
    policy -.->|"downstream: derives policy<br/>from ConversationState"| state_mod

    style intent fill:#4a90d9,color:#fff,stroke-width:3px
```

### Dependency Summary

| Dependency | Type | Purpose |
|------------|------|---------|
| [`model_routing`](../models/model_routing.md) | **Hard** | `ModelRouter.generate()` — the single LLM call for classification. Cascades local→cloud on outage. |
| `cil/state.py` | **Hard** | `ConversationState` and `Score` — the output shape consumed by all downstream readers. |
| [`models/doc_intent.py`](../models_doc_intent.md) | **Hard** | `_parse_json()` for robust JSON recovery; `_has_artifact_signal()` used by upstream caller to gate `include_doc_intent`. |
| [`core_config`](../infrastructure/core_config.md) | **Hard** | `CIL_INTENT_MODEL` (model tier), `RDB_CACHE` (cache KV namespace). |
| [`kv_store`](../storage/kv_store.md) | **Hard** | `get_kv()` for cache read/write via Redis. |
| [`core_infrastructure`](../infrastructure/core_infrastructure.md) | **Hard** | `logger` for structured logging. |
| [`cil_lexical`](cil_lexical.md) | **Indirect** | Upstream caller may use lexical hints as a cheap prefilter; `cil_intent` itself is regex-free. |
| [`cil_policy`](cil_policy.md) | **Downstream** | Consumes the `ConversationState` produced by `to_conversation_state()` to derive risk, clarification, and tool-allowance policy. |

---

## Configuration

All configuration is environment-variable driven, requiring no code changes or deploys:

| Variable | Default | Description |
|----------|---------|-------------|
| `CIL_INTENT_MODEL` | `local_mini` | Model tier hint passed to `ModelRouter`. Configured via `OPENAI_OSS_MODEL` in `core/model_registry.py`. |
| `CIL_INTENT_CACHE_ENABLED` | `true` | Master toggle for classification caching. |
| `CIL_INTENT_CACHE_TTL` | `3600` (1 hour) | TTL in seconds for cached classification results. |
| `CIL_TONE_DETECT` | `true` | When `false`, `to_conversation_state()` preserves neutral `ConversationState` defaults for tone/formality/language/sentiment/wants_brief instead of surfacing model-detected values. |

---

## Fail-Safe & Degradation Strategy

```mermaid
flowchart TD
    Turn["User turn arrives"]
    Turn --> Classify["classify()"]
    
    Classify --> Outcome{"Outcome"}
    
    Outcome -->|"Success"| UI["UnifiedIntent<br/>(model-classified)"]
    Outcome -->|"Model outage"| None1["return None"]
    Outcome -->|"Bad JSON"| None2["return None"]
    Outcome -->|"Empty text"| None3["return None"]
    Outcome -->|"Any exception"| None4["return None"]
    
    None1 & None2 & None3 & None4 --> Caller["Caller (cil/analyze.py)"]
    Caller --> Defaults["Safe static defaults:<br/>task_complexity=medium<br/>domain=general<br/>route=chat<br/>intent_source='default'"]
    
    UI --> Caller2["Caller (cil/analyze.py)"]
    Caller2 --> ModelState["ConversationState<br/>intent_source='model'<br/>signal_sources=['model']"]
    
    Defaults --> Downstream["Downstream:<br/>gateway routing, policy derivation,<br/>response shaping"]
    ModelState --> Downstream
    
    style None1 fill:#d94a4a,color:#fff
    style None2 fill:#d94a4a,color:#fff
    style None3 fill:#d94a4a,color:#fff
    style None4 fill:#d94a4a,color:#fff
    style UI fill:#50b87e,color:#fff
    style Defaults fill:#e8a838,color:#fff
    style ModelState fill:#50b87e,color:#fff
```

The fail-safe contract is absolute:
- `classify()` **never raises** — every code path either returns a valid `UnifiedIntent` or `None`.
- `to_conversation_state()` is only called when `classify()` succeeds; the caller handles the `None` case by constructing a `ConversationState` with safe defaults.
- The `ConversationState` itself has safe defaults on every field, so even a partial failure degrades gracefully.

---

## Relationship to Sibling CIL Modules

The CIL is organized into three sibling modules under `cil/`:

```mermaid
graph TB
    subgraph "Conversation Intelligence Layer"
        intent_mod["cil_intent<br/>(this module)<br/>Model-only understanding"]
        lexical_mod["cil_lexical<br/>Cheap regex prefilter<br/>(output format, continuation,<br/>freshness hints)"]
        policy_mod["cil_policy<br/>Domain-aware policy derivation<br/>(risk, clarification, tools)"]
        state_mod["cil/state.py<br/>ConversationState dataclass<br/>(shared output shape)"]
    end

    UserTurn["User Turn"] --> intent_mod
    UserTurn -.->|"upstream caller may<br/>use as prefilter"| lexical_mod
    
    intent_mod -->|"produces"| state_mod
    lexical_mod -.->|"hints merged by<br/>upstream caller"| state_mod
    state_mod -->|"consumed by"| policy_mod
    policy_mod -->|"policy decisions"| Gateway["Gateway routing &<br/>response shaping"]

    style intent_mod fill:#4a90d9,color:#fff,stroke-width:3px
    style state_mod fill:#9b6dc4,color:#fff
```

- **[cil_lexical](cil_lexical.md)**: Provides cheap regex-based hints (`detect_output_format`, `detect_continuation`, `detect_freshness`) that the upstream caller may use as a prefilter. These hints are **not** used inside `cil_intent` — the intent module is strictly model-only.
- **[cil_policy](cil_policy.md)**: Consumes the `ConversationState` produced by `to_conversation_state()` and derives domain-aware policy decisions (risk level, clarification needs, tool allowances, sensitivity) using `DomainProfile` configurations.
- **`cil/state.py`**: The shared `ConversationState` dataclass that serves as the canonical output shape for all CIL components, ensuring downstream readers are agnostic to whether the state came from the model classifier or static defaults.

---

## Performance Characteristics

| Metric | Value | Notes |
|--------|-------|-------|
| LLM round-trip (cache MISS) | ~2.3s | Measured in production with `local_mini` tier |
| Cache lookup (cache HIT) | <5ms | Redis GET + JSON deserialize |
| Cache key computation | <1ms | SHA-256 of input string |
| Prompt size (with doc intent) | ~1.5KB | System prompt + context + user turn |
| Prompt size (without doc intent) | ~1.1KB | Shorter schema, no doc-intent guidance |

The cache is a **pure function** of its inputs (no `chat_id`/`user_id`/time dependence), meaning a cache HIT is provably the same answer the model would give — not a staleness trade-off. This makes it safe for double-submits, retried requests, and cross-chat identical questions.
