# Sandbox Document Execution

## Introduction

The **sandbox_document_execution** module provides a secure, isolated execution environment for rendering AI-authored document build code into downloadable office documents (DOCX, PPTX, XLSX, PDF). It is the document-specialised counterpart to the general-purpose code sandbox, running agent-generated build scripts inside a network-disabled, resource-capped Docker container (`ainxt-doc-sandbox`) and returning both the final document bytes and a rasterised page-image preview for in-app display.

The module lives within the broader [sandbox](#sandbox-subsystem) subsystem of the shared-core layer and is consumed by document-generation workers and API routes that serve office-document artifacts to end users.

---

## Architecture Overview

```mermaid
graph TB
    subgraph Callers
        DW["Document Workers<br/>(doc_worker, doc_worker_agent)"]
        API["API Routes<br/>(doc_download_router, api_documents)"]
    end

    subgraph sandbox_document_execution
        B["build() / build_docx()"]
        IG["_generate_doc_image()"]
        SN["_safe_image_name()"]
        SE["_summarize_error()"]
        DBR["DocBuildResult"]
    end

    subgraph "Docker Sandbox (ainxt-doc-sandbox)"
        NODE["Node.js<br/>(docx-js, pptxgenjs)"]
        PY["Python3<br/>(openpyxl)"]
        SOFFICE["LibreOffice (soffice)"]
        PDFTOPPM["pdftoppm"]
    end

    subgraph External
        LLMPROXY["LLM Proxy<br/>/llm/imagen"]
    end

    DW --> B
    API --> B
    B --> IG
    IG --> LLMPROXY
    IG --> SN
    B -->|"docker run --network none"| NODE
    B -->|"docker run --network none"| PY
    NODE --> SOFFICE
    PY --> SOFFICE
    SOFFICE --> PDFTOPPM
    B --> SE
    B --> DBR
    DBR --> DW
    DBR --> API
```

### Key Design Principles

| Principle | Implementation |
|---|---|
| **Network isolation** | Container launched with `--network none` — no outbound access during build |
| **Resource caps** | 1 GB memory, 1 CPU, 256 PID limit, 30-minute build timeout |
| **Read-only root FS** | `--read-only` with a single writable bind-mounted `/work` directory |
| **Non-root execution** | Container runs as `uid 10001` (USER sandbox) |
| **Image pre-generation** | AI images fetched *before* sandbox launch so the network-isolated container can embed them by filename |
| **Non-fatal image failures** | If an image generation fails, the document still builds without that image |
| **Byte-level return** | Returns artifact bytes (not container paths) so callers can persist/serve directly |

---

## Core Components

### `build(code, fmt, images)` — Primary Entry Point

The central function that orchestrates the entire document build lifecycle:

1. **Validates** the requested format against the supported set (`docx`, `pptx`, `xlsx`, `pdf`)
2. **Checks** Docker availability and sandbox image presence
3. **Pre-generates** any requested AI images via the LLM proxy's `/llm/imagen` endpoint
4. **Writes** the agent-authored build code to a temporary work directory
5. **Fixes permissions** on the work directory and files so the non-root sandbox user can access them
6. **Launches** the Docker container with the build script
7. **Collects** the deliverable file, an intermediate PDF, and page-image previews
8. **Cleans up** the temporary work directory

```mermaid
flowchart TD
    A["build(code, fmt, images)"] --> B{"Format supported?"}
    B -->|No| ERR1["DocBuildResult(ok=False)"]
    B -->|Yes| C{"Docker available?"}
    C -->|No| ERR2["DocBuildResult(ok=False)"]
    C -->|Yes| D{"Sandbox image present?"}
    D -->|No| ERR3["DocBuildResult(ok=False)"]
    D -->|Yes| E["Create temp workdir"]
    E --> F["Write build code to workdir"]
    F --> G["Pre-generate AI images<br/>via LLM proxy /llm/imagen"]
    G --> H["chmod workdir & files<br/>(world-readable for uid 10001)"]
    H --> I["docker run --network none<br/>--memory 1g --cpus 1<br/>--read-only --tmpfs /tmp"]
    I --> J{"Build succeeded?"}
    J -->|No| ERR4["DocBuildResult(ok=False)<br/>_summarize_error(logs)"]
    J -->|Yes| K["Read deliverable bytes"]
    K --> L["Read PDF bytes<br/>(if available)"]
    L --> M["Collect page-image previews<br/>(JPEG, up to MAX_PREVIEW_PAGES)"]
    M --> N["DocBuildResult(ok=True)"]
    N --> O["rmtree workdir"]
    ERR1 --> O
    ERR2 --> O
    ERR3 --> O
    ERR4 --> O
```

### `build_docx(code)` — Backward-Compatible Entry Point

A thin wrapper around `build(code, "docx")` retained for backward compatibility with the original docx-only implementation.

### `DocBuildResult` — Result Data Class

| Field | Type | Description |
|---|---|---|
| `ok` | `bool` | Whether the build succeeded |
| `error` | `str` | Human-readable error summary (empty on success) |
| `logs` | `str` | Truncated build logs (last 2000–4000 chars) |
| `doc_bytes` | `bytes` | The deliverable document file bytes |
| `ext` | `str` | File extension of the deliverable (`docx`, `pptx`, `xlsx`, `pdf`) |
| `pdf_bytes` | `bytes` | Intermediate PDF rendering (for preview/download) |
| `page_images` | `list[bytes]` | JPEG page-image previews in page order (up to 20 pages) |

### Helper Functions

| Function | Purpose |
|---|---|
| `_safe_image_name(name, idx)` | Sanitises agent-supplied image filenames — strips path traversal, enforces alphanumeric/`._-` characters, ensures image extension, caps at 64 chars |
| `_generate_doc_image(prompt, aspect_ratio, provider)` | Calls the LLM proxy's `/llm/imagen` endpoint to generate a single image; returns raw bytes |
| `_summarize_error(logs)` | Extracts the most useful error line from Node.js/Python/LibreOffice stderr for agent feedback |
| `docker_available()` | Checks if the Docker daemon is running |
| `image_present()` | Checks if the `ainxt-doc-sandbox` image is locally cached |
| `supported_formats()` | Returns the list of supported document formats |

---

## Format-Specific Build Recipes

Each format has a defined build recipe stored in the `_FORMATS` dictionary:

```mermaid
graph LR
    subgraph "Format Recipes"
        DOCX["docx<br/>interp: node<br/>src: build.js<br/>built: output.docx<br/>deliver: docx<br/>convert: No"]
        PPTX["pptx<br/>interp: node<br/>src: build.js<br/>built: output.pptx<br/>deliver: pptx<br/>convert: No"]
        XLSX["xlsx<br/>interp: python3<br/>src: build.py<br/>built: output.xlsx<br/>deliver: xlsx<br/>convert: No"]
        PDF["pdf<br/>interp: node<br/>src: build.js<br/>built: output.docx<br/>deliver: pdf<br/>convert: Yes"]
    end
```

| Format | Interpreter | Build Source | Built File | Deliverable | Conversion |
|---|---|---|---|---|---|
| `docx` | Node.js | `build.js` | `output.docx` | `output.docx` | None |
| `pptx` | Node.js | `build.js` | `output.pptx` | `output.pptx` | None |
| `xlsx` | Python 3 | `build.py` | `output.xlsx` | `output.xlsx` | None |
| `pdf` | Node.js | `build.js` | `output.docx` | `output.pdf` | soffice docx→pdf |

The **PDF** format is notable: the agent authors a DOCX build script (using docx-js), and the sandbox then converts the resulting DOCX to PDF via LibreOffice headless mode. This leverages the rich DOCX authoring capabilities while delivering a PDF to the user.

---

## Preview Generation Pipeline

Page-image previews are generated inside the sandbox so users can see the rendered document in-app without downloading it:

```mermaid
flowchart LR
    subgraph "PDF deliverable"
        A1["output.pdf"] -->|"pdftoppm -jpeg -r 150"| A2["page-01.jpg<br/>page-02.jpg<br/>..."]
    end
    subgraph "Non-PDF deliverable (docx/pptx/xlsx)"
        B1["output.docx/pptx/xlsx"] -->|"soffice --convert-to pdf"| B2["output.pdf"]
        B2 -->|"pdftoppm -jpeg -r 150"| B3["page-01.jpg<br/>page-02.jpg<br/>..."]
    end
```

- **Preview DPI**: 150 (configurable via `AINXT_DOC_PREVIEW_DPI`)
- **Max preview pages**: 20 (configurable via `AINXT_DOC_PREVIEW_PAGES`)
- Preview generation failures are non-fatal (`|| true`) — the document is still returned even if previews fail

---

## AI Image Embedding

The module supports embedding AI-generated images into documents. Because the sandbox is network-isolated, images must be generated *before* the container starts:

```mermaid
sequenceDiagram
    participant Caller
    participant Build as build()
    participant Proxy as LLM Proxy /llm/imagen
    participant Sandbox as Docker Container

    Caller->>Build: build(code, fmt, images=[{name, prompt, aspect_ratio, provider}])
    loop For each image (max 8)
        Build->>Build: _safe_image_name(name, idx)
        Build->>Proxy: POST /llm/imagen {provider, prompt, aspect_ratio}
        Proxy-->>Build: Raw image bytes (PNG/JPEG)
        Build->>Build: Write image to workdir/{safe_name}
    end
    Build->>Sandbox: docker run (workdir bind-mounted to /work)
    Note over Sandbox: Build code embeds images<br/>by filename (e.g. docx ImageRun,<br/>pptxgenjs addImage, openpyxl add_image)
    Sandbox-->>Build: output.{ext} + page-*.jpg previews
    Build-->>Caller: DocBuildResult
```

**Image generation constraints:**
- Maximum 8 images per document (`AINXT_DOC_MAX_IMAGES`)
- 120-second timeout per image (`AINXT_DOC_IMAGE_TIMEOUT_S`)
- Approved providers only: `gemini` or `openai` (Imagen / DALL-E)
- Default provider: `openai` (configurable via `AINXT_DOC_IMAGE_PROVIDER`)
- Image generation failures are non-fatal — the document builds without the failed image

---

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `AINXT_DOC_SANDBOX_IMAGE` | `ainxt-doc-sandbox:latest` | Docker image name for the document sandbox |
| `AINXT_DOC_BUILD_TIMEOUT_S` | `1800` (30 min) | Maximum build execution time |
| `AINXT_DOC_PREVIEW_DPI` | `150` | DPI for page-image preview rasterisation |
| `AINXT_DOC_PREVIEW_PAGES` | `20` | Maximum number of preview page images |
| `LLM_PROXY_URL` | `http://localhost:8003` | LLM proxy base URL for image generation |
| `AINXT_DOC_IMAGE_TIMEOUT_S` | `120` | Timeout for individual image generation |
| `AINXT_DOC_MAX_IMAGES` | `8` | Maximum AI-generated images per document |
| `AINXT_DOC_IMAGE_PROVIDER` | `openai` | Default image generation provider (`gemini` or `openai`) |

---

## Sandbox Container Security Model

```mermaid
graph TB
    subgraph "Docker Run Flags"
        NET["--network none<br/>No outbound access"]
        MEM["--memory 1g<br/>1 GB RAM cap"]
        CPU["--cpus 1<br/>1 CPU core"]
        PID["--pids-limit 256<br/>Process count cap"]
        RO["--read-only<br/>Immutable root filesystem"]
        TMPFS["--tmpfs /tmp<br/>Ephemeral temp storage"]
        VOL["-v workdir:/work<br/>Single writable bind mount"]
        USER["USER sandbox (uid 10001)<br/>Non-root execution"]
    end
```

The container script (`run_script`) executes in this order:
1. `set -e` — fail on any error
2. `cd /work` — switch to the writable bind mount
3. `export HOME=/tmp` — use tmpfs for HOME/profile
4. Run the interpreter (`node build.js` or `python3 build.py`)
5. Verify the built file exists (`test -f output.{ext}`)
6. If `convert=True`: run soffice to convert (e.g., docx→pdf)
7. Generate page-image previews (soffice→pdf→pdftoppm or direct pdftoppm)

---

## Dependencies & Related Modules

```mermaid
graph LR
    subgraph "Sandbox Subsystem"
        SDE["sandbox_document_execution<br/>(this module)"]
        SDE2["sandbox_docker_execution<br/>DockerExecutor / SubprocessExecutor"]
        SIB["sandbox_image_building<br/>SandboxImageBuilder"]
        SHE["sandbox_self_healing<br/>SelfHealingEngine"]
    end
    subgraph "Document Generation"
        DG["doc_generator tool<br/>markdown_to_docx / slides_to_pptx"]
    end
    subgraph "LLM Infrastructure"
        LLMPROXY["llm_proxy<br/>/llm/imagen endpoint"]
    end
    subgraph "Core"
        LOG["core.logger"]
    end

    SDE -->|"image generation"| LLMPROXY
    SDE -->|"logging"| LOG
    SDE -.->|"complementary<br/>(general code sandbox)"| SDE2
    SDE -.->|"complementary<br/>(repo image builder)"| SIB
    SDE -.->|"complementary<br/>(auto-heal code)"| SHE
    DG -.->|"simpler alternative<br/>(template-based)"| SDE
```

### Relationship to Other Sandbox Modules

| Module | Relationship |
|---|---|
| [sandbox_docker_execution](sandbox_docker_execution.md) | General-purpose code execution sandbox (`DockerExecutor`). The document executor is a **specialised** variant — it uses its own dedicated image (`ainxt-doc-sandbox`) with pre-installed Node.js, Python, LibreOffice, and pdftoppm, rather than the generic language images. The document executor calls `docker run` directly via subprocess rather than using the Docker SDK. |
| [sandbox_image_building](sandbox_image_building.md) | Builds per-repo Docker images for SDLC code execution. Not directly used by the document executor, but shares the same Docker-based isolation philosophy. |
| [sandbox_self_healing](sandbox_self_healing.md) | Automatically heals failing code via LLM re-generation. The document executor does **not** use self-healing — build failures are returned to the agent/caller for manual correction. |

### Relationship to Document Generation Tools

The [doc_generator](doc_generator.md) tool (`tools/doc_generator.py`) provides a simpler, template-based document generation path (`markdown_to_docx`, `slides_to_pptx`) that runs in-process using the `python-docx` and `python-pptx` libraries. The sandbox document executor is the more powerful alternative: it runs **agent-authored** build code that can produce arbitrarily complex documents with full formatting control, embedded AI-generated images, and multi-format output — at the cost of requiring Docker and the sandbox image.

---

## Data Flow: End-to-End Document Build

```mermaid
sequenceDiagram
    participant Agent as Office Agent
    participant Worker as Document Worker
    participant Exec as doc_executor.build()
    participant Docker as Docker Container
    participant Proxy as LLM Proxy

    Agent->>Worker: Authored build code + format + image specs
    Worker->>Exec: build(code, fmt, images)

    alt Images requested
        loop Each image
            Exec->>Proxy: POST /llm/imagen
            Proxy-->>Exec: Image bytes
            Exec->>Exec: Write to workdir
        end
    end

    Exec->>Exec: Write build code to workdir
    Exec->>Exec: chmod files (uid 10001 access)
    Exec->>Docker: docker run --network none --rm ...

    Docker->>Docker: node build.js / python3 build.py
    Docker->>Docker: (soffice convert if needed)
    Docker->>Docker: soffice → pdf → pdftoppm (previews)
    Docker-->>Exec: Exit code + output files in /work

    Exec->>Exec: Read deliverable bytes
    Exec->>Exec: Read PDF bytes
    Exec->>Exec: Collect page-image JPEGs
    Exec->>Exec: rmtree workdir
    Exec-->>Worker: DocBuildResult

    alt Build succeeded
        Worker->>Worker: Persist doc_bytes, pdf_bytes, page_images
        Worker-->>Agent: Success + preview images
    else Build failed
        Worker-->>Agent: Error summary from _summarize_error()
        Note over Agent: Agent can revise build code and retry
    end
```

---

## Error Handling

The module provides graceful degradation at multiple levels:

| Failure Scenario | Behaviour |
|---|---|
| Unsupported format | Returns `DocBuildResult(ok=False)` with descriptive error |
| Docker not running | Returns `DocBuildResult(ok=False)` with "Docker is not running" |
| Sandbox image missing | Returns `DocBuildResult(ok=False)` with build instructions |
| Build timeout (30 min) | Returns `DocBuildResult(ok=False)` with timeout message |
| Build script failure | Returns `DocBuildResult(ok=False)` with `_summarize_error()` output — extracts the most relevant error line from logs |
| Image generation failure | Non-fatal — logged as warning, document builds without that image |
| Preview generation failure | Non-fatal — `|| true` in container script, document still returned |
| Permission issues | `chmod` applied proactively; failures logged as warnings but build continues |

The `_summarize_error()` function scans build logs in reverse for lines containing error keywords (`Error`, `error`, `Cannot`, `Traceback`, `Exception`, `throw`) and returns the first match (truncated to 300 chars), falling back to the last log line.

---

## Permission Handling

A critical operational detail: the host worker often runs under a restrictive umask (e.g., `0o077`), causing written files to be mode `0o600` (owner-only). The sandbox container runs as `uid 10001` (USER sandbox), which would get `EACCES` when trying to open these files. The module proactively fixes this:

```python
os.chmod(workdir, 0o777)       # Build can write output.* back
os.chmod(src_path, 0o644)      # Sandbox can read the build script
for p in image_paths:
    os.chmod(p, 0o644)          # Sandbox can read pre-generated images
```

Permission failures are logged as warnings but do not abort the build — the container may still succeed if the Docker daemon applies different default permissions.
