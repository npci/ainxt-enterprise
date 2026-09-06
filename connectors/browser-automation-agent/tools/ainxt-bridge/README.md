# ainxt-bridge

Delegate a browser task to the AiNxt extension from a CLI, a script, or a desktop app,
and get a structured run report back.

No dependencies. Node 18+.

## Why there's a helper process at all

A Chrome MV3 extension cannot open a listening socket, and that's the safer arrangement
anyway — AiNxt declares no `externally_connectable`, so nothing outside the browser can
send it anything. The bridge inverts the direction instead: **this helper listens on
loopback, and the extension dials out to it.** Your CLI talks to the helper.

```
CLI / desktop app ──HTTP──> ainxt-bridge (127.0.0.1:8787)
                                   ▲ ws:// — the extension connects outward
                            Chrome service worker
```

## Setup

1. In the side panel: **Settings → Local command bridge** → enable it, pick a port,
   and copy the generated token. It is off by default and the token is never synced
   to your Google account.
2. Start the helper. Every path in this file is relative to **this directory**
   (`tools/ainxt-bridge`), so run the commands from here:

   ```sh
   node server.js --token <token>
   ```

3. Check that the browser found it:

   ```sh
   AINXT_TOKEN=<token> node cli.js health
   # helper: up · extension: connected
   ```

## Use

```sh
export AINXT_TOKEN=<token>

# a natural-language task
node cli.js run "what's the top story" --url https://news.ycombinator.com

# a deterministic test file, machine-readable output
node cli.js run --file smoke.json --mode test --json > report.json

# in the open side panel instead of headlessly (needed for vision-heavy work)
node cli.js run "compare these two prices" --attach panel
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | `pass` |
| 1 | `fail` or `partial` |
| 2 | `needs_human` or `max_steps_reached` |
| 3 | transport or configuration error |

### Approvals

Risky steps pause for a human. Over the bridge that prompt appears **in your terminal**:

```
⏸ CRITICAL approval needed
   Vault secret ${secrets.GH_TOKEN} is about to be typed into github.com
   approve? [y/N]
```

- `--yes` pre-approves ordinary risk gates. It does **not** cover critical ones —
  `exec_script`, a vault secret's first use against a host, or a `js:` condition. Those
  are the gates the extension refuses to let *anything* auto-approve, and a CLI flag is
  not a human.
- `--deny-gates` (and any non-TTY invocation, so CI by default) refuses every gate; the
  run ends `needs_human` naming what blocked it.
- `exec_script` additionally requires **Allow script execution** to be ticked in the
  extension's settings on that machine. That setting cannot be changed over the bridge,
  by design.

## Security model

- **Loopback only.** The helper binds `127.0.0.1`; it is never reachable off-box.
- **Mutual token proof.** Both sides HMAC the *other* side's nonce, so a process squatting
  the port can neither impersonate the helper nor drive your browser, and a captured
  transcript replays into nothing. The token itself never crosses the wire.
- **Origin-bearing HTTP requests are rejected**, so a web page in your own browser can't
  drive the CLI surface.
- **No new extension privileges.** The bridge adds no manifest permission and no inbound
  message path; the service worker's `sender.id` check is untouched.
- **The token is local.** Excluded from `chrome.storage.sync` and from settings export,
  the same treatment as the secret vault.

Anything that can reach the port is already a local process running as you. The token is
what separates *your* CLI from some other program that guessed the port.

## HTTP surface

All routes except `/health` require `Authorization: Bearer <token>`.

| Route | Purpose |
|---|---|
| `GET /health` | helper up? extension connected? |
| `POST /run` | body is the task object; responds with an SSE event stream |
| `POST /runs/:id/cancel` | abort a run in flight |
| `POST /runs/:id/gate` | `{gateId, decision}` — answer an approval gate |

Task fields: `instruction`, `fileText`, `mode`, `startUrl`, `vision`, `maxSteps`,
`dryRun`, `variables`, `includeScreenshots`, `attach`, `approvals`.

Events: `queued`, `accepted`, `progress`, `narration`, `image`, `tab`, `gate`, `done`,
`error`. `done` carries the full run record — same shape the side panel downloads, with
screenshots stripped unless you asked for them.

## Tests

```sh
node test/protocol.test.js   # handshake, framing, auth, origin rejection
node test/cli.test.js        # exit codes and output against a scripted extension
```
