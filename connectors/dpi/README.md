# AiNxt DPI Agent Connectors

Let AI agents act on **India's Digital Public Infrastructure (DPI / India Stack)** —
**Account Aggregator** (consent-based financial data) and **DigiLocker** (verified
documents) today; UPI / BBPS / ONDC to follow.

This package is self-contained for clean extraction into a standalone open-source
repo (`ainxt-dpi-agent`). It ships the **framework + a synthetic sandbox only** —
never real endpoints, credentials, or data.

## Why it's different from an OAuth connector
DPI uses a signed **consent artifact** (RBI **Account Aggregator** / **DEPA**
model), not a bearer token. A user grants consent for a specific **purpose**,
**data range**, and **expiry**; the agent acts strictly within that mandate.
Auth flows through `connectors/dpi/consent.py::ConsentHandler` (parallel to
`connectors/oauth2.py`), routed by `auth_type="dpi_consent"` in the engine.

## Sandbox (default for open source — fully offline)
```
export DPI_SANDBOX=true
```
DPI connectors then return **synthetic fixtures** (`connectors/dpi/sandbox/*.json`)
— no real upstream, no licensing. The fixtures contain fake Aadhaar/account-shaped
values so the platform's compliance redactor is exercised end-to-end.

## Flow
1. `POST /connectors/dpi/consent/start/{connector}` → returns a (sandbox: pre-signed) consent artifact.
2. `POST /connectors/dpi/consent/store/{connector}` → verifies + persists it (encrypted, in `user_oauth_tokens`, stamped `auth_type=dpi_consent`).
3. The connector's tools auto-surface to the agent; calls run within the consent.

## Components
| File | Role |
|---|---|
| `consent.py` | `ConsentHandler` — create / verify consent artifacts (sandbox self-signed; production = issuer signature, plug point) |
| `seed_dpi.py` | Connector definitions (`dpi_account_aggregator`, `dpi_digilocker`) |
| `sandbox/` | Synthetic fixtures + loader (offline) |
| `../adapters/dpi_account_aggregator.py`, `../adapters/dpi_digilocker.py` | Sandbox-aware adapters |

## Production (AiNxt-governed, not open-sourced)
Real DPI access requires **RBI/UIDAI licensing + certification** per entity.
Production wires: issuer signature verification in `verify_artifact`, the real
FIP/AA + DigiLocker endpoints (env-injected `base_url` + certs), and the live
consent-screen redirect in `create_consent_request`. None of that ships in the
open-source repo — only the framework + sandbox.

## Reused (unchanged) from the AiNxt connector framework
Engine pipeline, compliance redaction, rate-limit, cache, MCP exposure
(`connectors/registry.py`, `connectors/mcp_bridge.py`), and `AdapterBase`.
