# Decision Engines — Compliance Engine

## 1. Introduction

The **Compliance Engine** (`agents/compliance_engine.py::ComplianceEngine`) is the central PCI/PII/secret-detection and redaction subsystem for the entire platform. It sits in the request path of every LLM-bound message — chat, agent, SDLC pipeline, CLI/Cowork, and OpenAI-compatible endpoints — ensuring that cardholder data, personally identifiable information, cryptographic secrets, and key material are either **redacted** before reaching the model or **blocked** entirely based on a per-type, config-driven policy.

The engine is designed around three principles:

| Principle | Implementation |
|---|---|
| **Config-driven** | Each compliance type (PAN, CVV, AADHAAR, SECRET, …) is independently toggleable as `redact`, `block`, or `off` via a JSON config file, env var, or admin API. |
| **Defence in depth** | A fast regex layer (PII/secret/key-leak detectors) is augmented by an ML privacy-filter service that catches natural-language PII the regexes miss. |
| **Fail-safe, never fail-closed** | Output is *never* blocked (only redacted). Input is blocked only for explicitly `block`-configured types. ML service failures are non-fatal — the regex layer still runs. |

---

## 2. Module Position in the System

The Compliance Engine is one of three sibling modules under the **Decision Engines** group within the shared-core agent system:

```
shared_core → agent_system → decision_engines
                                ├── decision_engines_core       (DecisionEngine — LLM-based tool selection)
                                ├── decision_engines_compliance (ComplianceEngine — PCI/PII/secret redaction & blocking)  ← this module
                                └── decision_engines_hardblock  (HardBlockEngine — AI-safety keyword blocking)
```

- **DecisionEngine** ([decision_engines_core](decision_engines_core.md)) decides *which tools* an agent should invoke.
- **ComplianceEngine** (this module) decides *what sensitive data must be redacted or blocked* before content reaches an LLM or is returned to the user.
- **HardBlockEngine** ([decision_engines_hardblock](decision_engines_hardblock.md)) decides *whether a prompt is an AI-safety violation* (jailbreak, harmful intent) using a weighted confidence-score gate.

Together they form the pre-LLM gate: HardBlock checks safety, Compliance checks data-leak, and only clean content proceeds.

---

## 3. Architecture

### 3.1 High-Level Architecture

```mermaid
graph TB
    subgraph Callers
        GW["Gateway /ask, /v1/chat"]
        MSG["messages_compat_router<br/>_compliance_check"]
        GR["guardrails<br/>check_input"]
        CLI["SDLC AgentLoop / CLI"]
        BATCH["compliance_router<br/>batch_check"]
    end

    subgraph ComplianceEngine["ComplianceEngine (singleton)"]
        ANALYZE["analyze()"]
        VAL_IN["validate_input()"]
        VAL_OUT["validate_output()"]
        REDACT["redact_text()"]
        CONFIG["Config Manager"]
    end

    subgraph Regex Layer
        PII["pii_detector<br/>detect_pii()"]
        SEC["secret_detector<br/>detect_secrets()"]
        KEY["key_leak_detector<br/>detect_key_leaks()"]
        RED["redactor<br/>redact_all()"]
    end

    subgraph ML Layer
        PSVC["privacy_svc /filter<br/>(ONNX NER model)"]
        CACHE["ML Result Cache<br/>(sha256 → findings, LRU)"]
    end

    subgraph Admin
        ADMIN["admin_router<br/>get/patch/reload config"]
    end

    GW --> VAL_IN
    MSG --> VAL_IN
    GR --> VAL_IN
    CLI --> VAL_IN
    BATCH --> VAL_IN

    VAL_IN --> ANALYZE
    VAL_IN --> RED
    ANALYZE --> PII
    ANALYZE --> SEC
    ANALYZE --> KEY
    ANALYZE --> PSVC
    PSVC --> CACHE
    VAL_OUT --> RED
    REDACT --> RED
    CONFIG --> VAL_IN
    CONFIG --> VAL_OUT
    ADMIN --> CONFIG
```

### 3.2 Component Relationships

```mermaid
graph LR
    subgraph "agents/compliance_engine.py"
        CE["ComplianceEngine"]
        SING["compliance_engine (singleton)"]
        SENT["COMPLIANCE_BLOCK_SENTINEL<br/>is_compliance_block()"]
    end

    subgraph "Detection Dependencies"
        PII["agents/pii_detector.py<br/>detect_pii()"]
        SEC["agents/secret_detector.py<br/>detect_secrets()"]
        KEY["agents/key_leak_detector.py<br/>detect_key_leaks()"]
        RED["agents/redactor.py<br/>redact_all()"]
    end

    subgraph "External Services"
        PSVC["privacy_svc<br/>(ML NER filter)"]
        LOG["core/logger.py"]
        TEL["core/telemetry.py<br/>inc_compliance_blocks()"]
    end

    subgraph "Config Sources"
        FILE["config/compliance_config.json"]
        ENV["COMPLIANCE_CONFIG env var"]
        API["admin_router API"]
    end

    CE --> PII
    CE --> SEC
    CE --> KEY
    CE --> RED
    CE --> PSVC
    CE --> LOG
    CE --> TEL
    CE --> FILE
    CE --> ENV
    API --> CE
    SING --> CE
```

---

## 4. Core Component: `ComplianceEngine`

### 4.1 Class Overview

```mermaid
classDiagram
    class ComplianceEngine {
        -Dict _config
        +__init__()
        +analyze(text) List~Dict~
        +validate_input(text, keep_types?) Dict
        +validate_output(text) Dict
        +redact_text(text, keep_types?) tuple
        +should_block(findings) bool
        +get_config() Dict
        +reload_config() Dict
        +update_type(type_name, enabled?, action?) Dict
        +update_config(patch) Dict
        -_load_config() Dict
        -_redact_types() Set~str~
        -_block_types() Set~str~
        -_off_types() Set~str~
        -_call_privacy_svc(text) List~Dict~
        -_should_call_privacy_svc(text) bool
        -_normalize(raw, category) List~Dict~
        -_severity(type) str
    }
```

### 4.2 Key Methods

#### `analyze(text) → List[Dict]`

The full detection pipeline. Runs all three regex detectors, then optionally calls the ML privacy service. Returns a deduplicated list of finding dicts:

```python
{
    "type": "PAN",           # compliance type
    "value": "4111********1111",  # masked value
    "category": "PII",       # PII | SECRET | KEY | ML
    "severity": "CRITICAL",  # CRITICAL | HIGH | MEDIUM | LOW
    "blocked": False,        # whether this type is block-configured
}
```

**Short-circuit optimisation**: if the regex layer already found a `block`-configured type, the ML HTTP call is skipped — the request will be rejected regardless.

#### `validate_input(text, keep_types=None) → Dict`

The primary entry point for all inbound content. Executes a four-step pipeline:

```mermaid
flowchart TD
    START["validate_input(text, keep_types)"] --> A["Step 1: analyze(text)<br/>regex + ML detection"]
    A --> B["Step 2: regex redaction<br/>redact_all(text, redact_types - keep_types)"]
    B --> C["Step 3: ML-gap redaction<br/>mask ML-detected values regex missed"]
    C --> D["Step 4: block check<br/>any finding type in block_types?"]
    D --> E{"blocked?"}
    E -->|Yes| F["allowed=False<br/>blocked_types=[...]"]
    E -->|No| G["allowed=True"]
    F --> H["Write audit log (if enabled)"]
    G --> H
    H --> I["Return result dict"]
```

The `keep_types` parameter allows callers (e.g., the Cowork/CLI tool-driven path) to preserve certain identifiers like `EMAIL`, `MOBILE`, or `UPI` that connector tool calls need to function. Card and secret types are **never** eligible for `keep_types`.

**Result dict fields:**

| Field | Type | Description |
|---|---|---|
| `allowed` | `bool` | `False` if any block-configured type was found |
| `blocked` | `bool` | Mirror of `not allowed` |
| `blocked_types` | `List[str]` | Types that triggered the block |
| `findings` | `List[Dict]` | All detection findings (regex + ML) |
| `redacted_text` | `str` | Text with sensitive values masked — use this for the LLM call |
| `was_redacted` | `bool` | Whether any redaction occurred |
| `redacted_types` | `List[str]` | Types that were redacted |
| `total_latency_ms` | `float` | End-to-end wall-clock latency |
| `privacy_svc_latency_ms` | `float` | ML service round-trip time (0.0 on cache hit) |
| `ml_called` | `bool` | Whether the ML service was actually invoked |

#### `validate_output(text) → Dict`

Redacts all configured types (both `redact` and `block` actions) from LLM output. **Output is never blocked** — `blocked` is always `False`. This ensures generated content is sanitised before reaching the user without interrupting the response.

#### `redact_text(text, keep_types=None) → (str, List[str])`

A lightweight redaction-only helper that skips detection and block logic. Used by paths that need sanitisation without the full analysis overhead (e.g., connector tool-result passthrough).

#### `should_block(findings) → bool`

Backward-compatible method that checks whether any finding's type is in the current block set.

---

## 5. Detection Layers

### 5.1 Regex Layer (Synchronous, In-Process)

Three specialised detectors run in sequence. Each returns normalised finding dicts that the engine enriches with severity and block status.

| Detector | File | Types Detected |
|---|---|---|
| `detect_pii()` | `agents/pii_detector.py` | PAN (Luhn-validated), CVV, EXPIRY (context-gated), PIN_BLOCK, INDIA_PAN, AADHAAR (Verhoeff-validated), ACCOUNT_NUMBER, ACCOUNT_NAME_COMBO, IFSC_CODE, UPI, EMAIL, MOBILE, IP_ADDRESS |
| `detect_secrets()` | `agents/secret_detector.py` | AWS_KEY, JWT_TOKEN, API_KEY, BEARER_TOKEN, STRIPE_KEY, PRIVATE_KEY |
| `detect_key_leaks()` | `agents/key_leak_detector.py` | PRIVATE_KEY_LEAK, CERTIFICATE_LEAK, SSH_KEY_LEAK, KEY_ASSIGNMENT_LEAK, PAYMENT_KEY_LEAK |

**Validation algorithms**: PAN detection uses Luhn checksum validation; Aadhaar uses Verhoeff checksum validation. Both strip inter-digit separators (`-`, `.`, `_`, spaces) before validation to catch obfuscated formats.

**Context gating**: Card expiry detection skips plain calendar dates unless a card-related keyword appears nearby. IP address detection skips RFC-1918 private ranges. Account-number detection requires a keyword anchor (`account`, `acct`, `a/c`, `acc`).

### 5.2 ML Privacy-Filter Layer (Asynchronous HTTP)

When `PRIVACY_SVC_URL` is set and the text is natural language (not pure code), the engine calls the [privacy_service](privacy_service.md) `/filter` endpoint. This service runs an ONNX NER model that detects entity groups the regexes cannot — e.g., person names in "Send money to Ramesh".

**Label mapping** (`_ML_LABEL_MAP`):

| privacy_svc `entity_group` | Compliance type | Action |
|---|---|---|
| `account_number` | `ACCOUNT_NUMBER` | Redact/block (with keyword-anchor guard) |
| `private_email` | `EMAIL` | Redact/block |
| `private_phone` | `MOBILE` | Redact/block |
| `secret` | `SECRET` | Redact/block |
| `private_person` | `ML_PRIVATE_PERSON` | Informational only (logged, not redacted) |
| `private_address` | `ML_PRIVATE_ADDRESS` | Informational only |
| `private_date` | `ML_PRIVATE_DATE` | Informational only |
| `private_url` | `ML_PRIVATE_URL` | Informational only |

**False-positive guards for ML `ACCOUNT_NUMBER`**: The NER model misclassifies short digit strings (order IDs, IFSC codes, partial Aadhaar) as account numbers. The engine requires either:
- An account keyword anchor (`account`, `acct`, `a/c`, `acc`) in the original text, **or**
- The finding is filtered out entirely in `analyze()`.

**ML result cache**: The privacy-svc call is the single most expensive step. Results are cached by `sha256(text)` in a bounded LRU `OrderedDict` (default 2048 entries, configurable via `COMPLIANCE_ML_CACHE_SIZE`). This eliminates redundant re-scans of byte-identical content — critical for the CLI agent path where the full tool-result history is re-sent every turn.

### 5.3 Layer Interaction

```mermaid
flowchart TD
    INPUT["Input text"] --> REGEX["Regex Layer<br/>detect_pii + detect_secrets + detect_key_leaks"]
    REGEX --> REG_FIND["Regex findings"]
    REG_FIND --> CHECK_BLOCK{"Any block-type<br/>in regex findings?"}
    CHECK_BLOCK -->|Yes| SKIP_ML["Skip ML call<br/>(short-circuit)"]
    CHECK_BLOCK -->|No| IS_CODE{"Pure code block?"}
    IS_CODE -->|Yes| SKIP_ML2["Skip ML call"]
    IS_CODE -->|No| ML_CALL["Call privacy_svc /filter"]
    ML_CALL --> CACHE_CHECK{"Cache hit?"}
    CACHE_CHECK -->|Yes| CACHE_HIT["Return cached findings<br/>latency=0, ml_called=False"]
    CACHE_CHECK -->|No| HTTP["HTTP POST to privacy_svc"]
    HTTP --> CACHE_PUT["Cache result (200 only)"]
    CACHE_PUT --> ML_FIND["ML findings"]
    SKIP_ML --> MERGE["Merge + deduplicate<br/>by (type, value)"]
    SKIP_ML2 --> MERGE
    CACHE_HIT --> MERGE
    ML_FIND --> MERGE
    MERGE --> FINAL["Final findings list"]
```

---

## 6. Configuration System

### 6.1 Config Hierarchy

```mermaid
flowchart TD
    ENV["COMPLIANCE_CONFIG env var<br/>(JSON string — highest priority)"]
    FILE["config/compliance_config.json<br/>(on-disk file)"]
    DEFAULT["_DEFAULT_CONFIG<br/>(hardcoded fallback)"]

    ENV -->|valid JSON| LOAD["Loaded config"]
    ENV -->|invalid| FILE
    FILE -->|exists & readable| LOAD
    FILE -->|missing/error| DEFAULT
    DEFAULT --> LOAD
    LOAD --> BACKFILL["_backfill_new_types()<br/>adds any missing _DEFAULT_TYPES"]
    BACKFILL --> PERSIST["Persist to disk<br/>(if new types added)"]
    PERSIST --> ACTIVE["Active config"]
```

### 6.2 Config Structure

```json
{
  "types": {
    "PAN":              { "enabled": true, "action": "redact" },
    "CVV":              { "enabled": true, "action": "redact" },
    "AADHAAR":          { "enabled": true, "action": "redact" },
    "SECRET":           { "enabled": true, "action": "redact" },
    "API_KEY":          { "enabled": true, "action": "redact" },
    "PRIVATE_KEY_LEAK": { "enabled": true, "action": "redact" }
    // ... 20+ types total
  }
}
```

Each type supports three actions:

| Action | Input behaviour | Output behaviour |
|---|---|---|
| `redact` | Value is masked; request proceeds | Value is masked; response proceeds |
| `block` | Request is rejected with `COMPLIANCE_BLOCK_SENTINEL` | Value is masked (output never blocks) |
| `off` | Type is ignored entirely | Type is ignored entirely |

### 6.3 Default Compliance Types

The engine ships with 20+ types covering PCI DSS cardholder data, Indian financial PII, secrets, and cryptographic key material:

| Category | Types |
|---|---|
| **Card data** | `PAN`, `CVV`, `EXPIRY`, `PIN_BLOCK` |
| **Indian identity** | `INDIA_PAN`, `AADHAAR` |
| **Banking** | `ACCOUNT_NUMBER`, `IFSC_CODE`, `ACCOUNT_NAME_COMBO`, `UPI` |
| **Contact** | `EMAIL`, `MOBILE`, `IP_ADDRESS` |
| **Secrets** | `SECRET`, `API_KEY`, `ACCESS_TOKEN` |
| **Key material** | `PRIVATE_KEY_LEAK`, `CERTIFICATE_LEAK`, `SSH_KEY_LEAK`, `KEY_ASSIGNMENT_LEAK`, `PAYMENT_KEY_LEAK` |

### 6.4 Severity Classification

| Severity | Types |
|---|---|
| `CRITICAL` | PAN, CVV, PIN_BLOCK, EXPIRY, INDIA_PAN, AADHAAR, ACCOUNT_NAME_COMBO, PRIVATE_KEY_LEAK, PAYMENT_KEY_LEAK |
| `HIGH` | ACCOUNT_NUMBER, IFSC_CODE, EMAIL, MOBILE, SECRET, API_KEY, ACCESS_TOKEN, CERTIFICATE_LEAK, SSH_KEY_LEAK, KEY_ASSIGNMENT_LEAK |
| `MEDIUM` | UPI |
| `LOW` | IP_ADDRESS, ML informational types |

### 6.5 Admin API

Configuration is managed at runtime through the [admin_router](admin_router.md) endpoints (requires admin flag):

| Endpoint | Method | Description |
|---|---|---|
| `/admin/compliance/config` | GET | Returns full config + summary (`redact`/`block`/`off` lists) |
| `/admin/compliance/config` | PATCH | Merges a partial config patch and persists to disk |
| `/admin/compliance/config/reload` | POST | Reloads config from disk/env (discards in-memory changes) |
| `/admin/compliance/config/reset` | POST | Resets to default config |

---

## 7. Data Flow

### 7.1 Input Validation Flow (Pre-LLM)

```mermaid
sequenceDiagram
    participant Caller as Gateway / Router
    participant CE as ComplianceEngine
    participant Regex as Regex Detectors
    participant ML as privacy_svc
    participant Red as Redactor
    participant LLM as LLM Provider

    Caller->>CE: validate_input(text, keep_types?)
    CE->>Regex: detect_pii(text)
    CE->>Regex: detect_secrets(text)
    CE->>Regex: detect_key_leaks(text)
    Regex-->>CE: findings (regex)

    alt No block-type in regex findings & text is natural language
        CE->>ML: POST /filter {texts: [text]}
        ML-->>CE: entities (NER labels + scores)
        CE->>CE: Map labels → compliance types
        CE->>CE: Filter ML ACCOUNT_NUMBER (keyword guard)
    end

    CE->>CE: Deduplicate findings by (type, value)
    CE->>Red: redact_all(text, redact_types - keep_types)
    Red-->>CE: redacted_text, redacted_types
    CE->>CE: Mask ML-detected values regex missed

    alt Any finding type in block_types
        CE-->>Caller: {allowed: false, blocked: true, blocked_types: [...]}
        Caller-->>LLM: (nothing — request rejected)
    else
        CE-->>Caller: {allowed: true, redacted_text: "...", findings: [...]}
        Caller->>LLM: POST with redacted_text
    end
```

### 7.2 Output Validation Flow (Post-LLM)

```mermaid
sequenceDiagram
    participant LLM as LLM Provider
    participant Caller as Gateway / Router
    participant CE as ComplianceEngine
    participant Red as Redactor

    LLM-->>Caller: Generated response text
    Caller->>CE: validate_output(text)
    CE->>Red: redact_all(text, redact_types ∪ block_types)
    Red-->>CE: redacted_text, redacted_types
    CE-->>Caller: {allowed: true, blocked: false, redacted_text: "..."}
    Caller-->>Caller: Return redacted text to user
```

### 7.3 Batch Compliance Check Flow

The [compliance_router](compliance_router.md) exposes a batch endpoint for bulk PII/PCI regression testing (up to 1000 texts, no LLM call):

```mermaid
flowchart LR
    REQ["POST /compliance/batch-check<br/>{texts: [...]}"] --> AUTH["Authenticate user"]
    AUTH --> LOOP["For each text (max 1000)"]
    LOOP --> VI["validate_input(text)"]
    VI --> COLLECT["Collect findings, latency, ml_called"]
    COLLECT --> LOOP
    LOOP --> STATS["Compute p50/p95/p99 latency<br/>throughput, blocked/redacted counts"]
    STATS --> RESP["Return BatchCheckResponse"]
```

---

## 8. Integration Points

### 8.1 Gateway (`gateway.py`)

The gateway's `/ask` and `/v1/chat/completions` endpoints call `validate_input()` on user prompts before forwarding to the LLM provider. When a block is triggered, the gateway returns the `COMPLIANCE_BLOCK_SENTINEL` string. Programmatic callers (e.g., SDLC file-read paths) use `is_compliance_block()` to detect this sentinel and drop the content rather than feeding it forward as model output.

### 8.2 Messages Compat Router (`messages_compat_router.py`)

The `_compliance_check()` function is the CLI/agent-path compliance gate. It implements critical performance optimisations:

- **Windowed scanning**: Only scans messages *after* the last assistant turn — prior messages have already passed compliance on a previous request. This reduces 169-message turns from ~107s to tens of milliseconds.
- **Hash-cache**: Each unique message body is scanned at most once per process.
- **HardBlock integration**: Runs [HardBlockEngine](decision_engines_hardblock.md) on each message before compliance, returning immediately if a safety block is triggered.
- **Findings forwarding**: Non-blocking findings are collected and forwarded to the provider gateway so it can redact without re-running `validate_input` (avoids double-validation false positives).

### 8.3 Guardrails (`guardrails/runtime_guardrails.py`)

The `check_input()` function provides NeMo Guardrails integration for AI-safety blocking. It operates as a separate layer from compliance — guardrails handle *intent* (jailbreak, harmful content), while compliance handles *data* (PCI, PII, secrets). See [guardrails](guardrails.md) for details.

### 8.4 SDLC Agent Loop (`agents/sdlc_agent_loop.py`)

The `ComplianceBlocked` exception is raised when the OpenAI-compatible `/v1/messages` endpoint returns a 400 due to PCI/PII/secret content in a tool result. The SDLC agent loop catches this and surfaces it to the pipeline rather than crashing.

### 8.5 Admin Router (`routers/admin_router.py`)

Provides authenticated REST endpoints for reading, patching, reloading, and resetting the compliance configuration at runtime. See [Section 6.5](#65-admin-api).

### 8.6 Telemetry (`core/telemetry.py`)

The `inc_compliance_blocks()` metric is incremented whenever a compliance block is triggered, exposing block counts through the Prometheus metrics endpoint.

---

## 9. Audit Logging

When the `COMPLIANCE_AUDIT_LOG` environment variable is set to a file path, every `validate_input()` call appends a JSONL record. **No raw PCI/PII is ever persisted** — the audit log stores only:

- Redacted text (not the original)
- Finding types with masked values (first 2 + last 2 characters, e.g., `41********1111`)
- Timestamp, redaction status, and block status

```json
{
  "ts": "2025-01-15T10:30:00Z",
  "input": "My card is XXXX-XXXX-XXXX-1111",
  "findings": [{"type": "PAN", "value_masked": "41********1111"}],
  "redacted": "My card is XXXX-XXXX-XXXX-1111",
  "was_redacted": true,
  "blocked": false
}
```

---

## 10. Performance Characteristics

| Aspect | Detail |
|---|---|
| **Regex layer** | Sub-millisecond for typical messages; pure Python regex with Luhn/Verhoeff validation |
| **ML layer** | 2s read timeout, 0.5s connect timeout; connection-pooled HTTP client (10 keepalive, 20 max) |
| **ML cache** | Bounded LRU (2048 entries default); cache hit = 0ms latency, `ml_called=False` |
| **Short-circuit** | If regex finds a block-type, ML call is skipped entirely |
| **Code detection** | Pure code blocks (≥2 code signals + avg token length >5) skip ML entirely |
| **Windowed scan** | CLI path scans only messages after the last assistant turn, avoiding O(N²) re-scans |

---

## 11. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `COMPLIANCE_CONFIG` | — | JSON string overriding the config file (highest priority) |
| `COMPLIANCE_AUDIT_LOG` | — | File path for JSONL audit log; unset = no audit logging |
| `PRIVACY_SVC_URL` | — | Base URL of the privacy service; unset = ML layer disabled |
| `COMPLIANCE_ML_CACHE` | `true` | Enable/disable the ML result cache |
| `COMPLIANCE_ML_CACHE_SIZE` | `2048` | Maximum number of entries in the ML cache LRU |

---

## 12. Singleton & Module-Level Exports

```python
# Singleton instance — import this, not the class, in application code
from agents.compliance_engine import compliance_engine

# Block sentinel — for programmatic callers to detect blocked content
from agents.compliance_engine import COMPLIANCE_BLOCK_SENTINEL, is_compliance_block

# Backward-compat: current block-type set (evaluated at import time)
from agents.compliance_engine import BLOCKING_TYPES
```

> **Note**: `BLOCKING_TYPES` is evaluated at import time. If the config is changed at runtime via the admin API, use `compliance_engine._block_types()` for the current set.

---

## 13. Related Documentation

| Module | Relationship |
|---|---|
| [decision_engines_core](decision_engines_core.md) | Sibling — LLM-based tool selection (`DecisionEngine`) |
| [decision_engines_hardblock](decision_engines_hardblock.md) | Sibling — AI-safety keyword blocking (`HardBlockEngine`) |
| [privacy_service](privacy_service.md) | ML NER service called by the ML layer |
| [admin_router](admin_router.md) | Runtime config management API |
| [compliance_router](compliance_router.md) | Batch compliance check endpoint |
| [guardrails](guardrails.md) | NeMo Guardrails AI-safety layer (complementary) |
| [core_infrastructure](core_infrastructure.md) | Logger, telemetry, config infrastructure |
| [sdlc_pipeline_agents](sdlc_pipeline_agents.md) | `ComplianceBlocked` exception consumer |
