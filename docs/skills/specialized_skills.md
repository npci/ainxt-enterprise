# specialized_skills

The `specialized_skills` module contains small, standalone scripts that implement domain-specific logic for individual platform skills. Each script is intentionally narrow in scope, dependency-light, and safe to invoke either as a CLI utility or as a helper from inside a skill's `run()` implementation. They encapsulate deterministic, auditable operations that agents should not perform by "eyeballing" â€” such as regulated-data detection.

## Purpose

- Provide **reusable, testable helpers** for skills that need exact, repeatable math or pattern matching.
- Keep skill logic **out of prompt engineering** so that scoring caps and detection patterns are versioned as code.
- Remain **free of platform imports** where possible, so scripts can run inside sandboxed skill environments without pulling in the full backend stack.

## Architecture Overview

```mermaid
flowchart TB
    subgraph specialized_skills["specialized_skills module"]
        direction TB
        dpdp["example-skill-dpdp-change-review/scripts/scan_personal_data.py"]
    end

    skill_dpdp["DPDP change-review skill"]

    skill_dpdp -->|invokes| dpdp

    dpdp -->|returns| findings["personal_data_detected, count, findings[]"]
```

The module is a leaf under `shared_skills` and has no internal dependencies. Each script is self-contained:

| Script | Responsibility | Invocation Style |
|--------|----------------|------------------|
| `scan_personal_data.py` | Scan changed files for field-name patterns that indicate personal data, returning evidence for a DPDP review. | CLI `--diff <files...>` |

## Sub-modules

- **[specialized_skills_dpdp_onboarding](specialized_skills_dpdp_onboarding.md)** - Personal-data field detection for DPDP change reviews (`scan_personal_data.py`).

## How It Fits into the System

`specialized_skills` sits at the edge of the skill ecosystem. The scripts are not part of the runtime engine, API layer, or agent orchestration; instead, they are **called by skill definitions** (typically via `subprocess` or direct function import from a skill's `run()` method). Their outputs are consumed as structured JSON that downstream prompt steps or governance checks can cite as evidence.

```mermaid
flowchart LR
    Agent["Agent / Skill runtime"] -->|calls| SkillScript["specialized_skills script"]
    SkillScript -->|structured JSON| Agent
    Agent -->|renders / decides| User["User or reviewer"]
```

Because the scripts avoid platform imports, they can also be exercised locally, unit-tested in isolation, and audited without standing up the full backend.

## Dependencies

- The script uses only the **Python standard library** (`json`, `sys`, `argparse`, `re`).
- No imports from `abstudio_backend`, `shared_core`, or other platform modules.
