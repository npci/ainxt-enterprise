# security_scan_tools

The `security_scan_tools` module provides a unified, normalised interface for static application security testing (SAST) and code-quality scanning. It orchestrates both **deep-tier** scanners (SonarQube, Checkmarx CxSAST, PMD/CPD) that run against a full repository clone, and **fast-tier** scanners (Semgrep, Bandit, hardcoded-secret detection) that run on individual files or snippets at generation time. All findings are mapped to a common CVSS-based schema so downstream components can apply a single risk gate and produce consistent reports.

---

## Core Responsibilities

1. **Scanner abstraction** – Wrap SonarQube, Checkmarx, PMD, CPD, Semgrep, Bandit, and the platform compliance engine behind simple Python functions.
2. **CVSS normalisation** – Convert each scanner's native severity/priority into a comparable CVSS base score.
3. **Risk gate** – Aggregate findings and decide whether a PR or generated file should be blocked based on `SECURITY_CVSS_BLOCK_THRESHOLD` (default 7.0).
4. **Reporting** – Generate a markdown report suitable for GitLab MR comments or CLI output.
5. **Fail-open safety** – Every scanner is optional; missing configuration or a missing binary logs a warning and returns empty findings rather than crashing the pipeline.

---

## Architecture

```mermaid
flowchart TB
    subgraph Inputs
        A[PR / Repository clone]
        B[Generated code snippet]
        C[Raw text / file content]
    end

    subgraph security_scan_tools
        D[Deep-tier scanners]
        E[Fast-tier scanners]
        F[CVSS normalisation]
        G[compute_risk_gate]
        H[format_scan_report]
    end

    subgraph Downstream
        I[GitLab MR comment]
        J[Commit status]
        K[SDLC run record]
        L[Secure code gate]
    end

    A --> D
    B --> E
    C --> E

    D --> F
    E --> F
    F --> G
    G --> H
    G --> J
    H --> I
    G --> K
    E --> L
```

### Component Breakdown

| Function | Tier | Purpose |
|----------|------|---------|
| `_http` | Shared | Small JSON HTTP helper used by SonarQube and Checkmarx REST calls. |
| `sonar_ensure_project`, `sonar_trigger_scan`, `sonar_wait_for_scan`, `sonar_get_vulnerabilities` | Deep | Create project, run `sonar-scanner`, poll CE queue, fetch VULNERABILITY/BUG issues. |
| `_cx_get_token`, `checkmarx_create_scan`, `checkmarx_poll_scan`, `checkmarx_get_findings` | Deep | OAuth2 token management, zip upload, SAST scan lifecycle, result statistics. |
| `pmd_scan`, `cpd_scan` | Deep | Run PMD rulesets and Copy-Paste Detector on a cloned working directory. |
| `semgrep_scan`, `bandit_scan`, `secrets_scan` | Fast | Multi-language / Python SAST and hardcoded-secret detection on files or strings. |
| `compute_risk_gate` | Aggregate | Calculate `max_cvss`, critical/high counts, per-tool summary, and `blocked` flag. |
| `format_scan_report` | Report | Build a markdown report with status, summary table, top findings, and blocking verdict. |

---

## Data Flow

### Deep-tier PR Scan

```mermaid
sequenceDiagram
    participant Worker as security_scan_worker
    participant SST as security_scan_tools
    participant Sonar as SonarQube
    participant CX as Checkmarx
    participant PMD as PMD/CPD
    participant GitLab as GitLab

    Worker->>SST: clone PR branch
    Worker->>SST: sonar_ensure_project + sonar_trigger_scan
    SST->>Sonar: POST /api/projects/create
    SST->>Sonar: sonar-scanner CLI
    Sonar-->>SST: analysis queued
    SST->>Sonar: GET /api/ce/activity
    Sonar-->>SST: queue empty
    SST->>Sonar: GET /api/issues/search
    Sonar-->>SST: vulnerability list

    Worker->>SST: zip source + checkmarx_create_scan
    SST->>CX: upload zip + create scan
    CX-->>SST: scan_id
    SST->>CX: poll scan status
    CX-->>SST: Finished
    SST->>CX: GET resultsStatistics
    CX-->>SST: severity counts

    Worker->>SST: pmd_scan / cpd_scan
    SST->>PMD: CLI JSON/XML output
    PMD-->>SST: violations / duplications

    Worker->>SST: compute_risk_gate(all_findings)
    SST-->>Worker: gate dict
    Worker->>SST: format_scan_report(gate, findings, repo, pr)
    SST-->>Worker: markdown
    Worker->>GitLab: post MR comment + commit status
```

### Fast-tier Secure Code Gate

```mermaid
sequenceDiagram
    participant Router as secure_code_gate_router
    participant Worker as secure_code_gate_worker
    participant SST as security_scan_tools
    participant LLM as LLM fix loop

    Router->>Worker: run_secure_code_gate(payload)
    loop each file
        Worker->>SST: semgrep_scan(path, content, language)
        Worker->>SST: bandit_scan(path, content)
        Worker->>SST: secrets_scan(content, path)
        SST-->>Worker: normalised findings
        alt blocking findings and do_fix
            Worker->>LLM: request fix
            LLM-->>Worker: fixed content
            Worker->>SST: re-scan fixed content
        end
    end
    Worker->>SST: compute_risk_gate
    SST-->>Worker: gate
    Worker->>SST: format_scan_report
    SST-->>Worker: report markdown
    Worker-->>Router: {blocked, findings, fixed_files, gate, report}
```

---

## CVSS Normalisation

Each scanner uses its own severity vocabulary. The module maps them to CVSS base scores so a single threshold can gate all tools.

```mermaid
flowchart LR
    Sonar[BLOCKER/CRITICAL/MAJOR/MINOR/INFO] -->|_SONAR_CVSS| CVSS[9.0 / 7.5 / 5.0 / 2.5 / 0.5]
    CX[Critical/High/Medium/Low/Info] -->|_CX_CVSS| CVSS
    PMD[P1/P2/P3/P4/P5] -->|_PMD_CVSS| CVSS
    Semgrep[ERROR/WARNING/INFO] -->|_SEMGREP_CVSS| CVSS
    Bandit[HIGH/MEDIUM/LOW] -->|_BANDIT_CVSS| CVSS
    Secrets[hardcoded secret] -->|9.0| CVSS
    CPD[duplicate code] -->|3.0| CVSS
```

A finding is considered **blocking** when its `cvss_score` is greater than or equal to `SECURITY_CVSS_BLOCK_THRESHOLD` (default 7.0). This means SonarQube CRITICAL, Checkmarx High/Critical, PMD P1/P2, Semgrep ERROR, Bandit HIGH, and any hardcoded secret will block by default.

---

## Configuration

All scanners are controlled through environment variables. If a required variable is absent, that scanner is skipped with a log message.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SONAR_HOST_URL` | `""` | SonarQube base URL. |
| `SONAR_TOKEN` | `""` | SonarQube authentication token. |
| `SONAR_SCANNER_PATH` | `sonar-scanner` | Path to the `sonar-scanner` binary. |
| `CHECKMARX_HOST_URL` | `""` | Checkmarx server base URL. |
| `CHECKMARX_CLIENT_ID` | `""` | Checkmarx OAuth client ID. |
| `CHECKMARX_CLIENT_SECRET` | `""` | Checkmarx OAuth client secret. |
| `PMD_PATH` | `pmd` | Path to the PMD binary (also used for CPD). |
| `SEMGREP_PATH` | `/appdata/fastapi/venv/bin/semgrep` | Path to the Semgrep binary. |
| `SEMGREP_CONFIG` | `p/ci,p/owasp-top-ten,p/secrets` | Semgrep rule packs or local ruleset paths. |
| `BANDIT_PATH` | `bandit` | Path to the Bandit binary. |
| `SECURITY_CVSS_BLOCK_THRESHOLD` | `7.0` | CVSS score at which a scan is considered blocking. |
| `SECURITY_SCAN_ENABLED` | `true` | Master switch for the deep-tier PR scan worker. |

---

## Integration with the System

The module is consumed by two worker paths and surfaced through several routers:

```mermaid
flowchart LR
    subgraph Consumers
        W1[security_scan_worker]
        W2[secure_code_gate_worker]
    end

    subgraph security_scan_tools
        SST[security_scan_tools]
    end

    subgraph API Routers
        R1[secure_code_gate_router]
        R2[compliance_scan_router]
    end

    subgraph External Systems
        E1[SonarQube]
        E2[Checkmarx]
        E3[PMD/CPD]
        E4[GitLab]
        E5[Semgrep/Bandit]
    end

    W1 -->|deep-tier scans| SST
    W2 -->|fast-tier scans| SST
    SST --> E1
    SST --> E2
    SST --> E3
    SST --> E5
    W1 --> E4
    R1 --> W2
    R2 -->|secrets / PII detection| compliance_engine
```

- **[security_scan_worker](../security_scan_worker.md)** clones a PR, runs the deep-tier scanners, persists results, posts a GitLab MR comment, and sets the commit status.
- **[secure_code_gate_worker](../secure_code_gate_worker.md)** runs the fast-tier scanners on generated or edited code, optionally invokes an LLM fix loop, and returns per-file findings.
- **[secure_code_gate_router](../api/secure_code_gate_router.md)** exposes the secure code gate as a synchronous HTTP endpoint.
- **[compliance_scan_router](../api/compliance_scan_router.md)** uses the same compliance engine for read-path PII/secret redaction on desktop screenshots and extracted text.
- **[compliance_engine](../compliance_engine.md)** is imported by `secrets_scan` to detect hardcoded secrets without triggering PAN/PII false positives.

---

## Security and Safety Notes

- **Read-only static analysis**: Fast-tier scanners only read files; they never execute the scanned code.
- **Fail-open**: Missing binaries or missing credentials log a warning and return empty findings so the rest of the pipeline can continue.
- **No raw secret logging**: Findings include type and location, not the secret value itself.
- **Auto-fix validation**: When the secure code gate uses an LLM to fix blocking issues, the fixed code is re-scanned with `secrets_scan`; if the fix introduced a hardcoded secret, the fix is discarded.
- **Image scanning limitation**: The compliance scan router currently cannot scan screenshots for PII via OCR in all deployments; it fails closed and warns the caller not to feed the image into context.

---

## References

- [security_scan_worker](../security_scan_worker.md) – RQ worker that orchestrates full-repository PR scans.
- [secure_code_gate_worker](../secure_code_gate_worker.md) – Worker that runs fast-tier scans and optional LLM auto-fix.
- [secure_code_gate_router](../api/secure_code_gate_router.md) – HTTP router for the secure code gate.
- [compliance_scan_router](../api/compliance_scan_router.md) – HTTP router for PII/secret redaction on read paths.
- [compliance_engine](../compliance_engine.md) – Shared compliance engine used for secret detection.
- [core/logger](../core_logger.md) – Logging utility used throughout the module.
