# CLI execution mode

Runs every ABStudio agent turn inside a spawned headless `ainxt` process instead
of the in-process LLM loop. Tools are served back to that process over MCP by
this same FastAPI application, so responses come from the CLI while credentials,
sandboxing and auditing stay exactly where they are.

> **What `ainxt` is:** the `ainxt` binary invoked here is built from the separate
> `ainxt-cli` repository — an NPCI fork of xAI/SpaceXAI's **Grok Build** CLI,
> licensed Apache-2.0 with a full fork-attribution `NOTICE`. It is not bundled in
> this repository; install it from `ainxt-cli` and point `ABSTUDIO_CLI_PATH` at
> the resulting binary. See `THIRD-PARTY-NOTICES.md` §3.1 in this repository for
> the attribution summary, and `ainxt-cli`'s own `LICENSE`/`NOTICE` for the full
> record. The CLI's flag surface (`--permission-mode`, `--output-format
> streaming-json`, folder-trust, MCP) is inherited from upstream Grok Build, not
> added by this integration.

Controlled by one flag:

```bash
ABSTUDIO_CLI_MODE=true
```

Off (the default) the codebase behaves exactly as before — the branches added to
`AgentRunner.run` and `NativeEngine._run_agent` are skipped and no subprocess,
workspace or MCP session is ever created.

---

## How a turn runs

```
POST /run-stream                    POST /agent-runner/chat-stream
       │                                       │
NativeEngine._traverse                    AgentRunner.run
(conditions, loops, gates —               (chat path)
 still native)                                 │
       │  agent node                           │
       └───────────────┬───────────────────────┘
                       ▼
              cli_runtime.bridge          ← one shared entry point
                       │
              ┌────────┴────────┐
              │  runner.py      │  semaphore → workspace → spawn
              └────────┬────────┘
                       │  stdout: streaming-json (text / thought / end / error)
   spawned ainxt ──────┤
        │              │
        │  tools/call  │        ┌─────────────────────────────┐
        └──────────────┼───────▶│ mcp_router → mcp_server     │
          HTTP + token │        │   → ToolDispatcher          │
                       │        │   → user's own credentials  │
                       │◀───────│   → publishes tool events   │
                       ▼        └─────────────────────────────┘
                 event_mapper → existing SSE vocabulary
```

Two design decisions carry the whole thing.

**The graph walker stays native.** `NativeEngine._traverse` owns conditions,
loops, evaluation gates, fan-out/fan-in and P5 memory nodes — none of which is
LLM work. Only the inner LLM-and-tool loop of an agent node is replaced, so every
loop, condition, gate and sub-agent event keeps working untouched.

**The MCP server is the tool-event source.** `--output-format streaming-json`
emits only `text`, `thought`, `end` and `error` — there are *no* tool events on
stdout. Because every tool call is served in-process, the MCP layer is what
publishes `tool_call_start` / `tool_call_result` to the UI. This is also why the
server is HTTP-in-process rather than a stdio sidecar: it reuses the live
Postgres pool, the warmed catalog and the same `ToolDispatcher`, so per-user
vault credentials, the `python -I` sandbox, retries and the audit trail are the
ones already in production.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| **`ABSTUDIO_CLI_MODE`** | `false` | **The only behavioural switch.** |
| `ABSTUDIO_CLI_PATH` | `ainxt` | Binary path or name on `PATH`. |
| `ABSTUDIO_CLI_API_KEY` | *(falls back to `AINXT_API_KEY`)* | Exported to the child as `AINXT_API_KEY` for gateway auth. Refuses to spawn if empty. |
| `ABSTUDIO_MCP_BASE_URL` | `http://127.0.0.1:8000` | Where the child calls back. Must be reachable from it. |
| `ABSTUDIO_MCP_SERVER_NAME` | `abstudio` | MCP server name; becomes the `server__tool` prefix. |
| `ABSTUDIO_CLI_MAX_CONCURRENCY` | `5` | Concurrent CLI processes **per worker process**. |
| `ABSTUDIO_CLI_RUN_TIMEOUT_S` | `900` | Wall-clock cap per turn. |
| `ABSTUDIO_CLI_MAX_TURNS` | `20` | Agent turn cap; mirrors `AGENT_MAX_ITER_DEFAULT`. |
| `ABSTUDIO_CLI_WORKSPACE_ROOT` | `<RUNTIME_ARTIFACTS_DIR>/cli_runs` | Per-run workspace root. |
| `ABSTUDIO_CLI_WORKSPACE_TTL_SECONDS` | `86400` | Age at which a workspace is swept. |
| `ABSTUDIO_CLI_EXPOSE_DRAFT_TOOLS` | `false` | Also expose `draft: True` tools (`confluence_*`, `zoho_*`, …). |
| `ABSTUDIO_CLI_EMERGENCY_FALLBACK` | `false` | Break-glass: fall back to native on CLI failure. Logged at WARNING. |
| `ABSTUDIO_CLI_PROMPT_FILE_THRESHOLD` | `0` (always use a file) | Prompt size above which `--prompt-file` is used. |

Minimum production configuration:

```bash
ABSTUDIO_CLI_MODE=true
ABSTUDIO_CLI_PATH=/opt/ainxt/bin/ainxt-linux-x64
ABSTUDIO_CLI_API_KEY=<platform API key>
ABSTUDIO_MCP_BASE_URL=http://127.0.0.1:8000     # must match the bound port
```

Concurrency is **per process**: with *N* uvicorn/gunicorn workers the host-level
cap is *N* × `ABSTUDIO_CLI_MAX_CONCURRENCY`. Size it against available RAM and CPU
accordingly, or wire `core.distributed_semaphore.DistributedSemaphore` for a true
global cap.

---

## Verifying a deployment

The startup log states readiness explicitly:

```
[AGENT] CLI execution mode is ON — every agent turn will run in a spawned ainxt process
```

or, if something is missing:

```
[AGENT] CLI execution mode is ON but NOT READY — agent runs will fail until these are fixed
        problems=['no ABSTUDIO_CLI_API_KEY (or AINXT_API_KEY) — ...']
```

`GET /health` reports the same, plus live load:

```json
{ "execution_backend": "cli", "cli_ready": true, "cli_active_runs": 2 }
```

Run the checks (no pytest required):

```bash
cd ABStudio/backend
python tests/cli_runtime/run_checks.py            # 154 checks, ~45s
ABSTUDIO_CLI_SMOKE_MODEL=1 python tests/cli_runtime/run_checks.py TestModelRoundTrip
```

`tests/cli_runtime/fake_cli.py` stands in for the binary and honours the same argv
and NDJSON contract, so timeout, crash, malformed-output and cancellation paths
are all deterministic tests. `test_real_cli.py` runs against the actual binary and
is skipped when it is absent.

---

## Things that will bite you

These are all verified behaviours of `ainxt 0.2.101`, and three of them are why
the two previous attempts at this feature failed.

**Folder trust is mandatory.** A repo-local (project-scope) MCP server is
*silently* skipped in an untrusted folder. There is no error — the agent simply
runs with zero ABStudio tools. `runner.build_env` sets `AINXT_FOLDER_TRUST=0` in
the child env for exactly this reason. To confirm by hand:

```bash
cd <a run workspace> && AINXT_FOLDER_TRUST=0 ainxt mcp doctor
#   ✓ handshake OK (protocol 2024-11-05)
#   ✓ N tools discovered
```

**Do not reuse `agents/sdlc_cli_engine.py`.** It targets a different CLI
generation. `--yes`, `--no-review`, `--output-schema`, `--allowed-tools`,
`--add-dir` and `--mcp-config` are all rejected by this build with
`error: unexpected argument`, so every spawn would fail on usage. `test_real_cli.py`
asserts both the flags we need and the absence of those.

**MCP permission rules need the `MCPTool(server__tool)` form.** A rule written
`mcp__server__tool` never matches.

**Tool names must not contain `__`.** The CLI splits `server__tool` on `__` and
*silently drops* any tool whose own name adds a second one — for example
`microsoft_365__outlook_send_mail`. `mcp_server.sanitize_tool_name` collapses
these on the way out and restores them before dispatch.

**Use a hostname, not an IP, if `ABSTUDIO_MCP_BASE_URL` is not loopback.** The CLI
uses rustls for MCP connections, which rejects IP-based URLs with
`NotValidForName`. Preflight warns about this.

**Windows needs the threaded subprocess backend.** `app/main.py` installs
`WindowsSelectorEventLoopPolicy`, and a Windows `SelectorEventLoop` raises
`NotImplementedError` for any asyncio subprocess. `process.py` detects this and
falls back to `subprocess.Popen` on reader threads, the same approach
`ToolDispatcher._run_in_sandbox` already uses. Production is Linux and uses the
native asyncio path.

---

## Security model

A spawned CLI is anonymous — it holds no user JWT. Identity comes from a per-run
bearer token minted at spawn time, written into the run's private
`.ainxt/config.toml` (mode `0600`), and revoked in the runner's `finally`.

The token is deliberately narrower than a user credential:

- **Per-run.** It authorises one run, not a user or a session, and dies with the
  process.
- **Scoped.** `allowed_tools` and `allowed_skills` are fixed at spawn time from
  the agent definition, so a prompt-injected CLI cannot widen its own surface.
  `read_skill_file` is additionally fail-closed: if the scope guard cannot be
  loaded, the call is refused.
- **Local.** The endpoint is loopback and the token never leaves the host.
- **Opaque on failure.** Bad token and unknown run return the same message, so a
  prober cannot enumerate live runs.

Tool execution itself is unchanged: `ToolDispatcher.dispatch()` resolves the
user's own GitLab PAT and Atlassian credentials from the vault, runs tool code in
the `python -I` sandbox with a sanitised environment, and writes the audit trail.
There is no service-account fallback — an unconfigured token surfaces as
"add one under Profile → GitLab Token" rather than borrowing wider access.

---

## Known gaps

**HITL agents stay native.** `ask_human` and the `before_tool` / `after_response`
gates need the run to suspend, which means killing the child and resuming its
session. The MCP server returns a sentinel for `ask_human` and `spawn_swarm`
(which needs a live in-process swarm runtime) and the engine runs the native path.
The downgrade is logged, never silent.

**Resume is plumbed but not wired.** `CliTurnRequest.resume_session_id` maps onto
`--resume` and the CLI session id is captured on every run; the HITL state machine
does not use it yet.

**No silent fallback, by design.** With the flag on, a CLI failure produces a
normal `error` SSE frame and stops. The earlier attempt defaulted to falling back
to native, so it appeared healthy while never once using the CLI — and needed two
rounds of diagnostic logging to discover that. `ABSTUDIO_CLI_EMERGENCY_FALLBACK`
exists for emergencies only and logs loudly whenever it fires.
