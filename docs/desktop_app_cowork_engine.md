# Desktop App Cowork Engine

## Introduction

The **Desktop App Cowork Engine** is the core subsystem within the Electron desktop application that manages the lifecycle of local AI coding-agent sessions. It bridges the desktop UI (renderer process) to the `ainxt` CLI binary, handling authentication, session creation, streaming protocol communication, tool-permission gating, git repository cloning, and session history persistence.

The engine supports two distinct CLI wire protocols — a legacy **stream-json** protocol (production default) and a newer **Agent-Client-Protocol (ACP)** JSON-RPC 2.0 protocol (SIT/testing) — selected at runtime via an environment variable. This dual-protocol design allows the desktop app to work with whichever CLI binary is installed without code changes.

### Key Responsibilities

| Responsibility | Component |
|---|---|
| Gateway token validation & re-login orchestration | `auth.js` |
| CLI process spawning, protocol negotiation, streaming I/O | `cliManager.js` |
| Protocol selection (stream-json vs ACP) | `protocol.js` |
| Authenticated git clone with PAT scrubbing | `clone.js` |
| On-disk session history reading for sidebar/resume | `sessions.js` |

---

## Architecture Overview

```mermaid
graph TB
    subgraph "Desktop App (Electron)"
        UI["Renderer Process<br/>CoworkDesktop / CoworkCanvas"]
        Main["Main Process<br/>(desktop_app_main_process)"]
        Preload["Preload Bridge<br/>(desktop_app_preload_bridge)"]
    end

    subgraph "Cowork Engine"
        Auth["auth.js<br/>Token validation, login, secrets"]
        CliMgr["cliManager.js<br/>SessionManager → CliSession"]
        Protocol["protocol.js<br/>Protocol resolver"]
        Clone["clone.js<br/>Git clone with PAT"]
        Sessions["sessions.js<br/>History reader"]
    end

    subgraph "External"
        CLI["ainxt CLI Binary<br/>(stream-json or ACP)"]
        Gateway["Gateway API<br/>/auth/me, /ainxt/v1/api"]
        GitLab["GitLab<br/>(git clone)"]
        FS["Filesystem<br/>~/.ainxt/"]
    end

    UI -->|IPC| Preload
    Preload -->|IPC| Main
    Main --> Auth
    Main --> CliMgr
    Main --> Clone
    Main --> Sessions
    CliMgr --> Protocol
    CliMgr -->|spawn + stdin/stdout| CLI
    Auth -->|HTTP GET /auth/me| Gateway
    Auth -->|read/write config.json| FS
    Clone -->|git clone| GitLab
    Sessions -->|read JSON| FS
    CLI -->|Bearer token| Gateway
```

---

## Component Documentation

### 1. `protocol.js` — Protocol Resolver

**Purpose:** Single source of truth for which CLI wire protocol the desktop app drives.

The desktop app ships with two different `ainxt` CLI binaries:

| Protocol | Binary | Wire Format | Status |
|---|---|---|---|
| `streamjson` | `ainxt-windows-x64_claude.exe` (v1.0.2-beta) | Newline-delimited stream-json, single-shot `--json` per turn | **Production default** |
| `acp` | `ainxt-windows-x64.exe` (v0.2.101) | JSON-RPC 2.0 over persistent `agent stdio` process | SIT / testing only |

**Selection mechanism:** The `AINXT_CLI_PROTOCOL` environment variable (set in the launcher script). If unset or unrecognised, defaults to `streamjson` for production safety.

```mermaid
flowchart LR
    Start["AINXT_CLI_PROTOCOL env var"] --> Check{"Set?"}
    Check -->|No| Default["streamjson (default)"]
    Check -->|Yes| Aliases{"Match aliases?"}
    Aliases -->|acp, new, groq, json-rpc| ACP["acp"]
    Aliases -->|streamjson, old, claude, cloud, legacy| SJ["streamjson"]
    Aliases -->|unknown| Default
```

**Exported functions:**
- `resolveProtocol()` → `"acp" | "streamjson"`
- `isAcp()` → `boolean`

---

### 2. `auth.js` — Authentication & Token Management

**Purpose:** Manages the CLI's gateway authentication lifecycle. The CLI owns the real auth lifecycle (JWT in `~/.ainxt/config.json` with auto-refresh); this module validates tokens, drives `ainxt login`, and manages encrypted secrets.

#### Authentication Model

```mermaid
flowchart TB
    subgraph "Token Sources (priority order)"
        T1["1. Long-lived API Key<br/>(electron-store, OS-encrypted)"]
        T2["2. CLI config.json token<br/>(~/.ainxt/config.json)"]
        T3["3. Renderer web-login JWT<br/>(electron-store lastToken)"]
    end

    T1 --> Validate["validateToken()<br/>GET /auth/me"]
    T2 --> Validate
    T3 --> Validate

    Validate -->|HTTP 200| Valid["{ token, gatewayUrl }"]
    Validate -->|non-200 / error / timeout| Invalid["{ token: '', gatewayUrl }"]
    Invalid --> Relogin["Prompt re-login<br/>runLogin()"]
```

#### Key Functions

| Function | Description |
|---|---|
| `readAuthState()` | Reads `~/.ainxt/config.json` to check if a token is present. Returns `{ authenticated, gatewayUrl, error }`. |
| `readToken()` | Extracts the CLI's gateway token (JWT/API key/OAuth) from config.json. |
| `writeToken(token, gatewayUrl, extraFields)` | Persists a validated token to config.json, self-healing the gateway URL and ensuring `auth_method` is set. |
| `validateToken(token, gatewayUrl)` | Hits the gateway's `/auth/me` endpoint (4s timeout). Resolves `true` only on HTTP 200. |
| `resolveValidToken(fallbackToken, gatewayUrl, apiKey)` | Tries token candidates in priority order; returns the first that validates. |
| `runLogin(onOutput)` | Spawns `ainxt login` as a subprocess, streaming stdout/stderr to the renderer. Has a configurable timeout (`COWORK_LOGIN_TIMEOUT_MS`, default 120s) and supports cancellation. |
| `cancelLogin()` | Kills the active login subprocess (process-group kill on non-Windows). |

#### Secret Management

Secrets are stored in `electron-store` using `safeStorage` (OS-backed encryption: DPAPI on Windows, Keychain on macOS). Two secrets are managed:

| Secret Key | Purpose |
|---|---|
| `coworkApiKeyEnc` | Long-lived CLI API key (no expiry, bypasses session-registry 401) |
| `ssoRefreshEnc` | Entra (SSO) refresh token |

The `_makeSecret(storeKey)` factory returns `{ read, write, clear }` with a plaintext fallback when OS encryption is unavailable.

#### TLS Handling

When `AINXT_TLS_INSECURE=1` is set (for self-signed gateway certs), the module creates a reusable `https.Agent` with `rejectUnauthorized: false`. This env var name **must match** the one the Rust CLI reads — a mismatch silently disables the desktop's TLS bypass.

#### Diagnostics

All `validateToken` outcomes (HTTP status, TLS errors, network errors, timeouts) are appended to `~/.ainxt/desktop-auth.log` for post-mortem diagnosis without DevTools.

> **Note:** The CLI's gateway token is separate from the web UI's `httpOnly` session cookie. A user may be signed into the web app but still need `ainxt login` once to enable local-agent mode.

---

### 3. `cliManager.js` — CLI Session Manager

**Purpose:** Spawns and drives the `ainxt` CLI agent headless, managing the full session lifecycle from creation through streaming output to disposal.

#### Class: `SessionManager`

A registry of active `CliSession` instances. Delegates all operations to the underlying session:

```mermaid
flowchart LR
    subgraph SessionManager
        Create["create(cwd, resumeId)"]
        Run["run(id, payload)"]
        Confirm["respondConfirm(id, confirmId, answer)"]
        Interrupt["interrupt(id)"]
        Model["setModel(id, model)"]
        Close["close(id)"]
        DisposeAll["disposeAll()"]
    end

    Create -->|"new CliSession"| Session["CliSession instance"]
    Run --> Session
    Confirm --> Session
    Interrupt --> Session
    Model --> Session
    Close --> Session
    DisposeAll --> Session
```

#### Class: `CliSession`

The core session driver. Its behavior bifurcates based on the resolved protocol:

##### Stream-JSON Protocol (Legacy / Production)

```mermaid
sequenceDiagram
    participant UI as Renderer
    participant SM as SessionManager
    participant CS as CliSession
    participant CLI as ainxt CLI (--json)

    UI->>SM: create(cwd)
    SM->>CS: start()
    Note over CS: ready = true (no persistent process)
    SM-->>UI: { id, ready: true }

    UI->>SM: run(id, { task, model })
    SM->>CS: run({ task, model })
    CS->>CLI: spawn ainxt --json --model X --add-dir cwd "task"
    CLI-->>CS: stdout (single JSON object)
    CS->>UI: emit token (response text)
    CS->>UI: emit result (status, response, model, usage)
    CLI-->>CS: process exit
```

Each turn spawns a fresh `--json` process that outputs a single JSON object and exits. No persistent process, no handshake, no per-tool confirmations.

##### ACP Protocol (New / SIT)

```mermaid
sequenceDiagram
    participant UI as Renderer
    participant CS as CliSession
    participant CLI as ainxt agent stdio

    UI->>CS: start()
    CS->>CLI: spawn ainxt --cwd X --model Y agent stdio
    CLI-->>CS: process spawned

    Note over CS,CLI: ACP Handshake
    CS->>CLI: initialize (protocolVersion: 1)
    CLI-->>CS: init result (modelState, availableCommands)
    CS->>CLI: authenticate (methodId: ainxt.api_key)
    CLI-->>CS: auth result
    CS->>CLI: session/new (cwd, mcpServers: [])
    CLI-->>CS: sessionId
    CS->>UI: emit session:init, session:id

    Note over CS,CLI: Turn Execution
    UI->>CS: run({ task })
    CS->>CLI: session/prompt (sessionId, prompt)
    loop Streaming
        CLI-->>CS: session/update (agent_message_chunk)
        CS->>UI: emit token (text)
    end
    CLI-->>CS: session/update (tool_call)
    CS->>UI: emit tool:start
    CLI-->>CS: session/request_permission
    CS->>UI: emit confirm
    UI->>CS: respondConfirm(reqId, "yes")
    CS->>CLI: RPC response { outcome: "approve" }
    CLI-->>CS: session/update (tool_call_update: completed)
    CS->>UI: emit tool:done
    CLI-->>CS: session/update (turn_complete)
    CS->>UI: emit result (status, response, cost, usage)
    CLI-->>CS: JSON-RPC result (id match)
```

#### Event Types Emitted

The `CliSession` emits the following event types to the renderer via the `emit` callback:

| Event Type | Trigger | Key Fields |
|---|---|---|
| `session:init` | Handshake complete (ACP) or available_commands_update | `model`, `slashCommands`, `tools`, `permissionMode` |
| `session:id` | ACP session/new returns sessionId | `sessionId` |
| `session:exit` | CLI process closes | `code` |
| `token` | Streaming text delta | `text` |
| `tool:start` | Tool execution begins | `name`, `detail`, `diff` |
| `tool:done` | Tool completes successfully | `name` |
| `tool:fail` | Tool fails | `name` |
| `confirm` | Permission request from CLI | `id`, `tool`, `detail`, `label` |
| `result` | Turn completes | `status`, `response`, `model`, `costUsd`, `usage`, `numTurns` |
| `context` | Context window usage update | `pct`, `tokens`, `max` |
| `error` | Fatal error | `msg` |
| `notice` | CLI stderr (info level) | `msg`, `level` |

#### Tool Name Resolution

The ACP CLI wraps every MCP tool call behind a generic `use_tool`/`search_tool` meta-tool. The `_realToolName(name, rawInput)` function unwraps these to surface the actual tool name (e.g., an MCP server's real tool name) in the UI.

#### Diff Building

The `buildDiff(name, input)` function constructs renderable diffs from mutating tool inputs:

| Tool | Diff Source |
|---|---|
| `Edit` | `old_string` → `new_string` |
| `MultiEdit` | Array of `{ old_string, new_string }` edits |
| `Write` | Full `content` (shown as all-added) |

Diffs are capped at `MAX_DIFF_LINES` (240) to prevent UI overload.

#### Bundled Skills

Creative-code skills (canvas-design, frontend-design, algorithmic-art) ship with the desktop app and are exposed to the Code agent via `--add-dir`. The `bundledSkillsDir()` function locates the skills directory in both packaged (`process.resourcesPath/code-skills`) and dev (`desktop/resources/code-skills`) modes. Code-tab only — never wired into the Cowork office agent.

#### Models Cache Version Sync

On module load, `_syncModelsCacheVersion()` patches `~/.ainxt/models_cache.json` to set `ainxt_version` to `"3.0.0-beta"` so the new CLI uses cached models instead of fetching from the gateway (which fails due to TLS cert issues).

#### CLI Protocol Tracer

When `AINXT_CLI_TRACE=1` is set, all raw stdin/stdout lines and process lifecycle events are written to `~/.ainxt/cli-trace.log` for debugging protocol-level issues.

#### Auto-Answering CLI Questions

The ACP CLI may send `_ainxt_dev/ask_user_question` (interactive questions) and `_ainxt_dev/exit_plan_mode` (plan approval) as JSON-RPC requests. Since the desktop has no interactive question UI:

- **Questions:** Auto-answered with the first (recommended) option; the user is notified via a token in the chat showing what was selected.
- **Plan approval:** Auto-approved; the plan content is shown to the user via a token.

---

### 4. `clone.js` — Git Repository Cloning

**Purpose:** Clones a GitLab repository to the user's local machine using their stored PAT, then scrubs the token from the persisted git config.

```mermaid
flowchart TB
    Start["cloneRepo({ url, branch, dest, token })"] --> Validate{"URL is https://?"}
    Validate -->|No| ErrSSH["Error: only https:// supported"]
    Validate -->|Yes| HasToken{"Token present?"}
    HasToken -->|No| ErrToken["Error: no GitLab token in profile"]
    HasToken -->|Yes| ExtractPAT["Extract PAT from 'username:PAT' format"]
    ExtractPAT --> CheckDest{"Target dir empty?"}
    CheckDest -->|No| ErrExists["Error: directory not empty"]
    CheckDest -->|Yes| Clone["git clone --progress<br/>https://oauth2:PAT@..."]
    Clone -->|success| Scrub["git remote set-url origin<br/>(remove token)"]
    Clone -->|failure| SafeErr["Scrub PAT from error text"]
    Scrub --> Done["{ ok: true, path, name }"]
    SafeErr --> ErrResult["{ ok: false, error }"]
```

**Security measures:**
- The PAT is embedded in the clone URL only transiently during the clone operation.
- After a successful clone, the remote URL is rewritten to a token-less URL via `git remote set-url`.
- The PAT is never logged; error messages are scrubbed by replacing the token with `***`.
- `GIT_TERMINAL_PROMPT=0` ensures a bad token fails fast instead of hanging on a credential prompt.
- Only `https://` URLs are supported (no SSH).

**Token format:** The profile stores GitLab tokens as `"username:PAT"`. The module splits on the first `:` to extract just the PAT, mirroring `tools/gitlab_tools.py:set_token()` in the [shared_integrations](shared_integrations.md) module.

---

### 5. `sessions.js` — Session History Reader

**Purpose:** Reads the CLI's persisted session data for the history sidebar and session resume functionality.

#### On-Disk Layout

```
~/.ainxt/sessions/
├── index.json          # { version, sessions: [{ id, cwd, updatedAt, name, model, turnCount, source }] }
├── {sessionId}.json    # { id, cwd, turns: [{ role, content, ... }] }
└── ...
```

#### Functions

| Function | Description |
|---|---|
| `listSessions(cwd)` | Returns sessions for a given working directory (filtered), sorted newest-first. Each entry: `{ id, title, cwd, turnCount, mtime }`. |
| `readHistory(id)` | Reconstructs `[{ role, content }]` from a session's turns for transcript display. Only includes `user` and `assistant` roles with string content. |
| `sessionsDir()` | Returns the sessions directory path (`~/.ainxt/sessions/`). |

---

## Data Flow: Full Session Lifecycle

```mermaid
sequenceDiagram
    participant User
    participant Renderer as Renderer (UI)
    participant Main as Main Process
    participant Auth as auth.js
    participant SM as SessionManager
    participant Session as CliSession
    participant CLI as ainxt CLI
    participant GW as Gateway

    Note over User,GW: 1. Authentication
    User->>Renderer: Opens Cowork/Code tab
    Renderer->>Main: Get auth state
    Main->>Auth: readAuthState()
    Auth->>Auth: Read ~/.ainxt/config.json
    Auth-->>Main: { authenticated, gatewayUrl }
    Main->>Auth: resolveValidToken(fallback, gw, apiKey)
    Auth->>GW: GET /auth/me (Bearer token)
    GW-->>Auth: 200 OK
    Auth-->>Main: { token, gatewayUrl }

    Note over User,GW: 2. Session Creation
    User->>Renderer: New session (selects workspace)
    Renderer->>Main: create session(cwd)
    Main->>SM: create(cwd, resumeId)
    SM->>Session: new CliSession(id, cwd, emit, resumeId)
    Session->>Session: resolveProtocol()
    alt streamjson (legacy)
        Session->>Session: ready = true (no process)
    else acp (new)
        Session->>CLI: spawn agent stdio
        Session->>CLI: initialize → authenticate → session/new
        CLI-->>Session: sessionId
    end
    SM-->>Main: { id, ready, cwd }
    Main-->>Renderer: Session ready

    Note over User,GW: 3. Turn Execution
    User->>Renderer: Types prompt
    Renderer->>Main: run(id, { task, model })
    Main->>SM: run(id, payload)
    SM->>Session: run({ task, model })
    alt streamjson
        Session->>CLI: spawn ainxt --json "task"
        CLI-->>Session: single JSON object
    else acp
        Session->>CLI: session/prompt (streaming)
        loop Streaming tokens
            CLI-->>Session: agent_message_chunk
            Session-->>Renderer: emit token
        end
        CLI-->>Session: tool_call + request_permission
        Session-->>Renderer: emit confirm
        User->>Renderer: Allow / Deny
        Renderer->>Main: respondConfirm
        Main->>SM: respondConfirm(id, reqId, answer)
        SM->>Session: respondConfirm(reqId, answer)
        Session->>CLI: RPC response
        CLI-->>Session: turn_complete
    end
    Session-->>Renderer: emit result

    Note over User,GW: 4. Session Resume
    User->>Renderer: Clicks history item
    Renderer->>Main: readHistory(sessionId)
    Main->>Sessions: readHistory(id)
    Sessions->>Sessions: Read ~/.ainxt/sessions/{id}.json
    Sessions-->>Main: [{ role, content }]
    Main-->>Renderer: Transcript loaded
    Renderer->>Main: create(cwd, resumeId)
    Note over Session: Resumes with --resume flag
```

---

## Filesystem Layout

All cowork engine state is stored under `~/.ainxt/`:

```
~/.ainxt/
├── config.json              # CLI config: jwt, gateway_url, auth_method, email
├── desktop-auth.log         # Auth validation diagnostics (append-only)
├── cli-trace.log            # CLI protocol trace (when AINXT_CLI_TRACE=1)
├── models_cache.json        # Cached LLM models (version-patched on startup)
├── sessions/
│   ├── index.json           # Session index (id, cwd, name, updatedAt, turnCount)
│   └── {id}.json            # Per-session transcript turns
└── (electron-store data)    # OS-encrypted secrets (coworkApiKeyEnc, ssoRefreshEnc)
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AINXT_CLI_PROTOCOL` | `streamjson` | Selects CLI wire protocol: `streamjson` (legacy) or `acp` (new) |
| `AINXT_TLS_INSECURE` | unset | When `1`, skips TLS verification for gateway requests (self-signed certs) |
| `AINXT_API_PREFIX` | `/ainxt/v1/api` | Gateway API path prefix |
| `AINXT_CLI_TRACE` | unset | When `1`, enables CLI protocol tracing to `~/.ainxt/cli-trace.log` |
| `COWORK_LOGIN_TIMEOUT_MS` | `120000` | Timeout for `ainxt login` subprocess (min 30s) |
| `AINXT_IS_COWORK` | unset | Set to `1` by cliManager when spawning CLI (marks cowork context) |
| `FORCE_COLOR` | unset | Set to `0` to disable color output in CLI subprocesses |

---

## Dependencies & Cross-Module References

```mermaid
graph LR
    subgraph "desktop_app_cowork_engine"
        auth["auth.js"]
        cliMgr["cliManager.js"]
        protocol["protocol.js"]
        clone["clone.js"]
        sessions["sessions.js"]
    end

    subgraph "Desktop App Siblings"
        main["desktop_app_main_process"]
        preload["desktop_app_preload_bridge"]
        browser["desktop_app_browser_automation"]
        computer["desktop_app_computer_use"]
    end

    subgraph "Backend"
        gateway["gateway"]
        abstudio["abstudio_backend"]
    end

    subgraph "Shared"
        integrations["shared_integrations"]
    end

    auth -->|"resolveCliBinary()"| binary["./binary.js"]
    cliMgr -->|"resolveCliBinary()"| binary
    cliMgr --> protocol
    auth -->|"GET /auth/me"| gateway
    auth -->|"reads config.json"| fs["~/.ainxt/config.json"]
    clone -->|"mirrors token split"| integrations
    main --> auth
    main --> cliMgr
    main --> clone
    main --> sessions
```

### Related Module Documentation

- **[desktop_app_main_process](desktop_app_main_process.md)** — The Electron main process that orchestrates the cowork engine, manages windows, tray, shortcuts, and SSO flows.
- **[desktop_app_preload_bridge](desktop_app_preload_bridge.md)** — The preload script that exposes IPC bridges between the renderer and main process.
- **[desktop_app_browser_automation](desktop_app_browser_automation.md)** — Playwright-based browser automation manager for the desktop app.
- **[desktop_app_computer_use](desktop_app_computer_use.md)** — Computer-use manager for desktop automation.
- **[gateway](gateway.md)** — The backend gateway API that validates tokens (`/auth/me`) and serves agent/workflow endpoints.
- **[abstudio_backend](abstudio_backend.md)** — The ABStudio backend with agent factory, workflow engine, and CLI runtime.
- **[shared_integrations](shared_integrations.md)** — Contains `gitlab_tools.py` whose token-splitting logic is mirrored in `clone.js`.
- **[ai_ui_frontend](ai_ui_frontend.md)** — The web UI frontend; the `useDesktop.js` hook provides desktop-specific cowork APIs (`coworkListSessions`, `coworkSessionHistory`, etc.) that consume session data managed by this engine.

---

## Security Considerations

1. **Token isolation:** The CLI's gateway token (in `~/.ainxt/config.json`) is separate from the web UI's `httpOnly` session cookie. Both may coexist; the engine prefers the CLI token for local-agent mode.

2. **Secret encryption:** API keys and SSO refresh tokens are encrypted at rest using OS-backed `safeStorage` (DPAPI/Keychain). Plaintext fallback only when OS encryption is unavailable.

3. **PAT scrubbing:** Git clone embeds the PAT transiently in the clone URL, then rewrites the remote to a token-less URL. Error messages are scrubbed before surfacing to the user.

4. **TLS bypass:** The `AINXT_TLS_INSECURE` flag must match between the desktop's Node.js process and the Rust CLI. A mismatch silently disables the desktop's TLS bypass while the CLI's remains active (or vice versa).

5. **Login process isolation:** The `ainxt login` subprocess runs in its own process group (on non-Windows) so it can be killed as a unit on timeout or cancellation, preventing leaked concurrent logins.

6. **Tool permission gating:** Both protocols support per-tool confirmation. The ACP protocol uses `session/request_permission` with `approve`/`reject` outcomes; the stream-json protocol uses `control_request`/`can_use_tool` with `allow`/`deny`. The renderer surfaces these as Allow/Don't Allow dialogs.
