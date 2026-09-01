# AiNxt Browser Assistant (Chrome MV3)

> LLM-driven browser automation, agentic web tasks, and UI testing — lives in a
> Chrome side panel, driven by any OpenAI-compatible model.

**AiNxt Browser Assistant** is a Chrome side-panel extension that executes
natural-language instructions or attached test files (JSON / YAML) against the
**currently active tab** and returns a structured run report. The UI is
chat-style: the instruction box is pinned to the bottom of the panel and results
appear above it, so you can keep reading the previous outcome while you type the
next instruction.

The panel **follows the active tab** like modern AI browser agents —
switch tabs and the next instruction targets whatever page is now in front. Runs
also keep going **in the background** when you switch away, and you can start a
**second run on a different tab while the first is still running**. See
[Multi-tab & concurrent runs](#multi-tab--concurrent-runs).

---

## Operation Modes

The **Mode** dropdown in the panel controls how the agent interprets your input.
`auto` is the default and the right choice for most sessions.

### `auto` (default)

Picks the right mode automatically:

- A test file is attached → runs in **test** mode.
- No file attached → runs in **exploration** mode.

### `ask`

A pure LLM conversation — no browser interaction at all. Type any question and
the assistant answers directly from the model. Conversation history is preserved
within a session (last 20 messages), so you can ask follow-up questions. Use
this for quick lookups, explanations, or drafting test steps before you run
them.

### `test`

Executes a structured JSON or YAML test file step-by-step. Each step is
individually timed and recorded as `pass`, `fail`, or `skipped`. The LLM is
**not** involved in planning — steps run exactly as written.

Use `test` mode when you:

- Have a repeatable script you want to run on demand or in CI.
- Need a structured pass/fail report (downloadable as JSON or HTML).
- Want deterministic, auditable execution with no LLM guessing.

A `critical: true` flag on any step causes the run to stop immediately on
failure; remaining steps are recorded as `skipped`.

### `suite`

A test file with a `suite_name` and a `tests` array chains multiple individual
test files in sequence. Variables set in earlier tests carry forward into later
ones, so you can share login state, extracted values, etc. across a whole suite.

### `exploration`

For natural-language instructions when no test file is attached. With the
**Agent loop** setting on (default — see [Agent](#agent)), the agent runs a
continuous perceive→reason→act→re-perceive loop, the same behaviour as
modern AI browser agents:

1. Takes a **snapshot** of the current page (interactive elements, headings,
   visible text, open dialogs) — and, if **Vision** is on, a screenshot.
2. Asks the LLM for the **single next action** plus a one-line narration of why.
3. **Highlights** the target on the page, executes that one action, then
   re-snapshots and repeats — up to 20 steps — until the goal is reached.
4. Returns a full trace of what happened, including any extracted values.

Each action's reasoning streams into the Activity log as an italic narration
line, so you can follow the agent's thinking in real time. Risky steps
(submit / pay / delete / login, etc.) pause for your approval automatically —
see [Auto safety gates](#agent).

> With **Agent loop** off, exploration falls back to the legacy **plan-once**
> behaviour: the LLM plans the entire step list from a single snapshot, then the
> runner executes it deterministically (with self-healing selector retries).

Conversation history is preserved within a session (last 20 messages), so
follow-up instructions build on what was already done.

Use `exploration` mode for ad-hoc tasks ("apply for casual leave on Zoho"),
one-off workflows, or when you want the agent to figure out the exact steps
itself. The agent can self-correct if a step fails by re-planning from the
updated snapshot.

### `agentic`

Like `exploration`, but for flows with high-stakes side effects (form
submissions, bookings, payments). It runs the same agent loop and additionally
pauses on **auto safety gates**: before executing any risky or irreversible
action — submit, pay, checkout, delete, login, send, navigate, `exec_script`,
etc. — the run halts on an approval modal and waits for your confirmation.

> Auto-detection works in plain `exploration` too (see [Agent](#agent)); the
> `agentic` label is just an explicit signal that risky steps are expected. The
> LLM may also insert its own `request_human` gates on top of the automatic ones.

### `debug`

Activates **passive network and console monitoring** on the active tab — no instruction or test file required. As soon as you select this mode:

- The extension installs a `fetch` interceptor and console override on the page.
- Every 3 seconds it polls for new **network errors** (4xx / 5xx responses, fetch exceptions) and **console `error`** entries.
- Each detected failure appears as a **red Debug Capture card** in the panel thread, showing the URL, HTTP status, response body snippet (up to 300 chars), and an **LLM root cause analysis block** (same category / summary / evidence / fix format as QA Debug Mode).

Use `debug` mode when you:

- Are browsing or manually operating a page and want automatic alerts for backend errors or JS exceptions.
- Want real-time RCA without running any automation — just open the panel, select `debug`, and use the page normally.
- Are investigating a flaky integration and want to catch the failing network call as it happens.

If you also click **Run** while in `debug` mode, it executes like `exploration` mode with `QA Debug Mode` forced on, so step failures also include root cause analysis.

> Selecting any other mode stops the passive monitor cleanly.

### Record mode

Click the **●** button in the composer to start recording browser interactions.
The extension watches your clicks, form fills, and navigation and auto-generates
a YAML test file. Stop recording to get a ready-to-run test you can attach and
execute in `test` mode.

---

## What it can do

- **Selectors** supported in `target`: `role=ROLE[name="..."]`,
  `[data-testid=...]`, `#id`, `[name=...]`, `text="..."`, `xpath=...`,
  `ref=N` (an index from the current agent-loop snapshot), and any CSS
  selector.
- **Actions**: `navigate` (optional custom `headers`), `back`, `forward`,
  `reload`, `click`, `dblclick`, `hover`, `type`, `clear`, `select`, `check`,
  `uncheck`, `press_key`, `scroll`, `scroll_to`, `wait`, `assert`, `extract`,
  `summarize`, `screenshot`, `zoom`, `switch_tab`, `list_tabs`, `read_page`,
  `find`, `get_page_text`, `read_console_messages`, `read_network_requests`,
  `request_human`, `exec_script`, `upload_file`, `drop_file`, `drag`,
  `switch_frame`, `open_tab`, `read_download`, `resize_window`, `autofill`,
  `click_at`, `hover_at`, `screenshot_baseline`, `assert_screenshot`,
  `accessibility_audit`, `assert_performance`, `mock_network`,
  `clear_network_mocks`, `datepick`, `if`.
- **Agent-loop-only tool calls** (not available in `test`/`suite` step files):
  `list_tabs`, `read_tab`, `read_page`, `find`, `get_page_text`,
  `read_document`, `read_console_messages`, `read_network_requests`, `zoom`,
  `scroll_to`, `click_at`, `hover_at`, `resize_window`, `open_tab`,
  `read_download`, `autofill`, plus the two loop-control signals `done` and
  `request_screenshot`. See
  [Agent-only perception & control tools](#agent-only-perception--control-tools).
- **Reads documents** — PDFs, Office Online (Word/Excel/PowerPoint), and Google
  Docs/Sheets/Slides, parsed off-DOM with structured fidelity (tables, headings,
  cell references). See [Reading documents](#reading-documents).
- **Wait conditions**: `visible`, `attached`, `detached`, `enabled`,
  `text:<substring>`, `url_matches:<regex>`, `js:<expression>`,
  `network_idle` (best-effort).
- **Assertions**: `equals`, `not_equals`, `contains`, `not_contains`,
  `matches`, `visible`, `hidden`, `present`, `absent`, `enabled`, `disabled`,
  `count`, `attribute:<name>`.
- **Variables and secrets** — reference as `${var}` and `${secrets.KEY}`.
  Secrets are redacted in the output and on screen.
- **`critical: true` on any step** — hard stop on failure; remaining steps
  recorded as `skipped`.
- **`repeat` field** — repeat any step N times: `{ action: click, target: "...", repeat: 3 }`.
- **`if`/`else` branching** — conditional steps based on a `js:` expression or
  wait condition.
- **Suite runner** — a test file with a `suite_name` and `tests` array chains
  multiple test files in sequence with shared variables.
- **`exec_script` action** — run arbitrary JavaScript on the page and capture
  the return value into a variable.
- **`upload_file` action** — inject a file into a `<input type="file">` element.
- **`drag` action** — synthetic drag-and-drop between two elements.
- **`switch_frame` action** — switch the DOM context to a same-origin `<iframe>`,
  or back to `top`.
- **`screenshot_baseline` / `assert_screenshot`** — save a named screenshot
  baseline and compare future screenshots against it with a configurable diff
  threshold.
- **`accessibility_audit` action** — scan for ARIA violations (missing alt text,
  unlabelled inputs, nameless buttons, duplicate IDs).
- **`assert_performance` action** — assert that a Web Vitals metric (LCP, FCP,
  TTFB, Load) is within a threshold.
- **`mock_network` / `clear_network_mocks`** — intercept `fetch` calls matching
  a URL pattern and return a stub response.
- **Auto-screenshot on failure** — whenever a step fails, the runner
  automatically captures a screenshot and attaches it to the step record.
- **Self-healing selectors** — when a selector-based step fails, the runner
  asks the LLM to suggest a corrected selector and retries automatically (up to
  2 retries). The healed selector is appended to the step's target ladder so
  subsequent runs benefit from it.
- **Download JSON** — a "download JSON" button appears in every run result.
  "Download report" (HTML) is available for `test` and `suite` modes.

---

## React / SPA form handling

Modern internal portals and tools (Zoho, custom React apps, etc.) use controlled
inputs and custom dropdown components. The agent handles these explicitly:

### Controlled inputs (`type` action)

The native value setter is used to bypass React's change-detection guard, then
an `InputEvent` with `inputType: "insertText"` is fired. This is the signal
React 17+'s synthetic event system needs to update component state. A `blur`
event fires afterwards to trigger any validation that runs on focus loss.

### Custom dropdowns (`select` action)

Many React apps render dropdowns as `<div>` / `<ul>` trees with ARIA roles
rather than native `<select>` elements (React Select, Ant Design Select, Zoho
pickers, etc.). When the target is not a native `<select>`:

1. The trigger element is clicked (and `mousedown` is dispatched) to open the
   dropdown.
2. The agent polls up to 2 seconds for option elements to appear in the DOM
   (`role="option"`, `role="menuitem"`, common class patterns).
3. The option whose text matches `value` (exact → case-insensitive → contains)
   is clicked.

### Dynamic modals

When a dropdown selection or button click opens a modal, the subsequent steps
work naturally:

- Each action calls `waitFor("visible", target, 15000)` before acting, so modal
  animations (fade-in, slide-in) are waited out automatically.
- React portals render into `document.body`, so all selectors and the page
  snapshot see modal content without any special handling.
- The snapshot includes an `open_dialogs` field listing any visible
  `[role="dialog"]` / `.modal` elements and their titles, so the LLM in
  `exploration` mode knows a modal is active and targets elements inside it.

> If a modal is inside a same-origin `<iframe>` rather than a React portal,
> add a `switch_frame` step targeting the iframe first.

---

## Reading documents

The agent can read documents rendered in a tab even when they expose no page
DOM — Chrome's built-in PDF viewer, Office Online (Word/Excel/PowerPoint on
SharePoint/OneDrive), and Google Docs/Sheets/Slides. Instead of scraping the
viewer, `read_document` **fetches the underlying file bytes or export with your
own cookies and parses them off-DOM** using in-house, license-clean parsers
(`lib/pdf.js` PDF extractor, `lib/zip.js` OOXML unzip via native
`DecompressionStream`) — no vendored libraries.

- **PDF** — reads the real text (not a screenshot); chunk large files with
  `pages="6-10"` (default first 5, reports the total). Image-only/scanned PDFs
  are detected and steer to a visual (screenshot) read.
- **Spreadsheets** (Excel / Google Sheets) — returned as an **addressed grid**
  (column letters + real row numbers) so the model can cite exact cells. Reads
  across **multiple sheets/tabs** (`sheet="Q3"`), and formula cells return their
  **computed value**. `find="text"` searches the whole workbook and reports each
  match with its real cell reference.
- **Word & PDF tables** — reconstructed into the same addressed grid rather than
  flattened into the text flow, and Word **headings** (`#`/`##`) and **bullet
  lists** (`-`) are preserved so document structure survives extraction.
- **Chunking** — `pages` / `range` / `sheet` windows plus a `max_chars` cap keep
  large documents within budget; the result header always reports totals and how
  to page forward.

`read_tab` reads another open tab's document/text **without switching to it**
(by `tab_id` from `list_tabs`), so "compare these three tabs" is a few reads in
one turn. When you share tabs via an **Assistant tab group** (a tab group titled
"Assistant"), reads and tab tools are scoped to just those tabs.

> **Limits.** Embedded images are skipped (no placeholder); password-protected
> Office files are detected and reported; encrypted PDFs currently report "no
> extractable text" and fall back to a visual read.

---

## Project layout

```
browser-automation-agent/
├── manifest.json            MV3 manifest
├── background.js            service worker (tab/scripting proxies, notifications, network-error capture)
├── sidepanel.html/css/js    side panel UI
├── content.js               in-page DOM executor
├── icons/
│   ├── ainxt-icon-16.png    toolbar icon
│   ├── ainxt-icon-48.png    extensions page card
│   ├── ainxt-icon-128.png   Web Store / install dialog
│   └── README.md            how to swap the logo
├── lib/
│   ├── llm.js               OpenAI-compatible chat completions client, TOOL_REGISTRY, prompts
│   ├── parser.js            JSON/YAML test-file normalizer
│   ├── runner.js            orchestrator (variable resolve, retries, agent loop, output)
│   ├── yaml.js              minimal YAML parser
│   ├── markdown.js          lightweight markdown renderer for LLM responses
│   ├── memory.js            per-origin site memory (persistent selector heals + notes)
│   ├── pii.js               conservative PII pattern detection (SSN/card/IBAN shape)
│   ├── gif.js               dependency-free GIF89a encoder for run-recording export
│   ├── report.js            run-record sanitizing + run-history writer (shared by panel and bridge)
│   ├── swbus.js             service-worker command hop (lets the runner work in either context)
│   ├── bridge.js            local command bridge: outbound WebSocket + token handshake
│   └── bridge-run.js        headless host for runAgent — turns a delegated task into a run
├── tools/
│   └── ainxt-bridge/        the local helper process + `ainxt` CLI (Node, no dependencies)
├── examples/
│   ├── login.yaml
│   ├── search.json
│   └── regression-reference.md   full action & selector reference
└── README.md
```

---

## Installation

1. **Download / copy this folder** to a convenient location on disk
   (e.g. `~/Code/browser-automation-agent/`).
2. Open Chrome and go to `chrome://extensions`.
3. Toggle **Developer mode** on (top-right).
4. Click **Load unpacked** and choose the `browser-automation-agent` folder.
5. The extension appears in the list. Pin it to the toolbar via the puzzle
   icon for easy access.
6. Open any tab. Click the extension icon — the **AiNxt Browser Assistant**
   side panel appears on the right.

> **Branding.** Icons live in `icons/` as `ainxt-icon-16.png`,
> `ainxt-icon-48.png`, and `ainxt-icon-128.png`, wired into `manifest.json`.
> To swap the logo, drop replacement PNGs into that folder using the same
> filenames and reload the extension. See `icons/README.md` for details.

---

## Configuring the LLM

The agent talks to any **OpenAI-compatible** `/chat/completions` endpoint.
Setup is a four-step flow:

1. Click the ⚙ button in the side panel header.
2. Enter **Base URL** and **API Key**:

   | Field    | Example values |
   | -------- | -------------- |
   | Base URL | `https://api.openai.com/v1` (OpenAI), `http://localhost:11434/v1` (Ollama), `http://localhost:1234/v1` (LM Studio), `https://openrouter.ai/api/v1`, your Azure deployment URL |
   | API Key  | `sk-...` (OpenAI), any non-empty string for local servers, your Azure key, etc. |

3. Click **Load models**. The extension calls `GET {base_url}/models` and
   populates the dropdown with what your endpoint advertises.
4. **Pick a model** in the dropdown (or type a custom id in the override
   field if your provider doesn't expose `/models`), then click **Save
   settings**.

The chosen model and the model list are cached in `chrome.storage.local`
(per-profile, local to your machine) so you don't have to reload them every
time. Re-open Settings and click **Load models** again to refresh.

### Agent

Below the Secrets field, the **Agent** section controls how `exploration` /
`agentic` runs behave:

- **Agent loop** (default **on**) — run the perceive→act→re-perceive loop:
  one LLM call per action against a fresh page snapshot, with live narration and
  an on-page highlight of each target. Turn it off to use the legacy plan-once
  behaviour. `test` and `suite` modes are always deterministic and never use this.
- **Vision** (default **off**) — `off` / `auto` / `on`. `auto` attaches a
  screenshot only after a failed step, on model request (`request_screenshot`
  tool call), or when the snapshot is degenerate (a canvas-heavy page with
  almost no interactive elements); `on` attaches one every turn. **Requires a
  multimodal model** (any vision-capable model); leave it off for
  text-only endpoints such as most local Ollama / LM Studio models.
- **Max steps per run** (default **20**, range 5–100) — the agent-loop action
  budget. A test file/plan's own `max_steps` overrides this. Exhausting the
  budget ends the run with status `max_steps_reached`, not a failure.
- **Stream narration** (default **off**) — type each action's narration out
  token-by-token as the model generates it (SSE streaming), instead of showing
  it all at once. Purely cosmetic — the action itself still runs only after the
  full response arrives. Leave off for OpenAI-compatible servers that don't
  support streaming; all other LLM calls remain non-streaming regardless.
- **Auto-approve** (default **off**) — skip the approval prompt and let the
  agent run risky steps (navigation, submits, uploads, …) without asking. Use
  with caution — this disables the auto safety gates below.
- **Ask before acting** (default **off**) — for `exploration` / `agentic` runs,
  AiNxt drafts a plan from the current page first and shows it in an approval
  modal. The plan is editable in place: change any step's url/selector/value,
  reorder (↑ ↓) or delete (✕) steps — or type feedback ("also verify the cart
  total") and click **Update plan** to have AiNxt redraft it; clicking with an
  empty field just asks you to describe the change first. Then **Approve** to
  run exactly the reviewed list, **Dry run** to highlight each step's target on
  the page without executing anything, or **Cancel** to abort. The per-step
  auto safety gates still apply during execution. Test/suite files are
  deterministic and skip this. (This mirrors the "ask before
  acting" pattern — review the plan once, then let it run.)
- **Step-by-step** (default **off**) — pause before *every* action and wait
  for **Continue**, showing what the previous step did. Full manual oversight;
  Auto-approve does not skip these pauses.
- **Record GIF** (default **off**) — capture a screenshot per action (capped
  at 60 frames) and offer a **Download GIF** button of the annotated run on the
  result card once it finishes. Encoded entirely client-side by the
  dependency-free encoder in `lib/gif.js` — no external service involved.

Auto safety gates are always active regardless of these toggles: whenever the
agent's next action is risky or irreversible (submit, pay, checkout, delete,
login, send, …) the run pauses on the approval-gate modal before executing —
even in plain `exploration` mode — unless **Auto-approve** is on. Approve to
continue or Cancel to abort.

### Site permissions

Below the Agent section, **Site permissions** restricts which hosts the agent
may act on:

- **All sites** (default) — no restriction.
- **Allowlist** — runs only on the listed hosts (and their subdomains).
- **Blocklist** — runs everywhere *except* the listed hosts.

Enter one host per line (e.g. `github.com`, `mail.google.com`). A blocked run is
stopped **before it starts**, and any mid-run `navigate` to a disallowed host is
refused with a clear error. This is a safety guardrail for agentic runs on
sensitive sites.

### Site memory

Below Site permissions, **Site memory** lets AiNxt learn per-origin over time:

- **Remember fixes for this site** (default **off**, per-origin) — when on,
  selector fixes the self-healing LLM finds during a run are stored keyed by
  origin and retried automatically on future visits **with zero LLM calls**,
  as an extra rung on that step's selector ladder. Off by default; opt in per
  site from Settings while that site's tab is active.
- **Notes** — free-text notes for the current origin (e.g. "the login button
  is in the top-right dropdown"), injected into the planner prompt whenever
  the agent works on that site.
- The remembered-sites list shows every origin with stored heals/notes, with a
  remove control per entry.

This is separate from [Site navigation hints](#site-navigation-hints) below,
which are built-in and need no configuration.

### Site navigation hints

For popular sites (GitHub, Gmail, Google Calendar, Docs/Sheets/Slides, Drive,
Slack), AiNxt injects a short block of built-in navigation know-how into the
planner prompt so the agent needs less trial-and-error to find the compose
button, the search bar, the right tab, etc. No configuration — it activates
automatically when the active page matches.

### Debug

Below the Agent section, the **Debug** section contains:

- **QA Debug Mode** — enables LLM root cause analysis on step failures. See [QA Debug Mode](#qa-debug-mode--llm-root-cause-analysis) above for details.

### Secrets

In the same settings panel, paste a JSON object such as:

```json
{ "password": "hunter2", "api_token": "ghp_xxx" }
```

Steps reference secrets as `${secrets.password}`. The runner substitutes them
before sending to the page, redacts them as `***` in the run output, and never
echoes them back.

---

## UI Features

### Multi-tab & concurrent runs

The side panel is **global** to the browser window — it stays open as you switch
tabs and always targets whatever tab is active when you hit **Run**:

- **Follows the active tab.** Each new instruction binds to the tab focused at
  submit time. Switch to a different tab, type a new instruction, and it runs
  there. The header shows the host of the current tab, and each request bubble is
  tagged with the tab it ran against.
- **Concurrent runs.** A task started on Tab A keeps running while you switch to
  Tab B and start another — both execute at once, each with its own Activity log
  and step timeline. The only restriction is **one run per tab**: starting a
  second run on a tab that's already busy is blocked with a toast (two runs would
  fight over the same page).
- **Per-run Stop.** Every running bubble has its own **Stop** button that aborts
  only that run. The **Stop** button in the composer is a **Stop-all** — it
  aborts every in-flight run at once.
- **Agent-driven tab switches.** The `switch_tab` action retargets the rest of
  the run at the tab it activates, so subsequent steps act on the new page.

### Background runs & notifications

Because runs continue in the background, you can switch tabs, focus another
window, or minimize Chrome while a long task finishes. When the panel **isn't
focused**, the extension raises a **Chrome notification** for:

- a completed run (with its pass/fail summary),
- a failed run,
- an approval gate that needs your input,
- a finished (or failed) chat answer in `ask` mode.

Clicking the notification re-focuses the window the run started in. Notifications
are suppressed while the panel is in the foreground, so runs you're actively
watching stay quiet. (Requires the `notifications` permission, declared in the
manifest.)

To be notified **even while the panel is focused**, click **🔔 Notify me** on the
running bubble (next to **Stop**). The toggle covers all three notification kinds
above for that run, and your last choice is remembered as the default for the
next run.

### Shortcuts (saved prompts)

Reuse prompts you run often. The composer foot shows a **☆ Save** button
(appears once you've typed something) — click it to store the current prompt.

**Type `/` to use them.** Start the composer with `/` and a menu of saved
prompts pops up above it; keep typing to filter by title or body, navigate with
the **↑/↓** arrows and press **Enter** (or click) to insert the full prompt.
Manage and delete saved prompts in **Settings → Shortcuts** (up to 30 are kept,
newest first).

### Live agent narration

In `exploration` / `agentic` runs with **Agent loop** on, each action is preceded by a one-line **narration** of what the agent is about to do and why, shown in the Activity log as an italic, accent-bordered line. You can follow the agent's reasoning in real time, the same way modern AI browser agents talk through their steps. Enable **Stream narration** in Settings → Agent to have each narration type out token-by-token as it's generated.

### On-page action highlight

Just before the agent acts on an element, a purple outline (with a small action label) is drawn around that element **on the page itself**, so you can see exactly what it's about to click, type into, or select. The highlight clears automatically after the action.

### Live step pill timeline

While a run is in progress, each step appears as a numbered pill above the Activity log. Pills start purple with a pulse animation and flip to **green** on success or **red** on failure — so you can see at a glance how far the run has progressed without reading the raw log.

### Inline error explainer

When a step fails, the result row shows a collapsible **"Why did this fail?"** card instead of a plain error message. Expand it to see:

- The exact **selector** that was used.
- A **screenshot thumbnail** taken at the moment of failure (click to zoom to full size).
- An **automated suggestion** — e.g. "ID selectors break when IDs are dynamic. Try a `role=` or `text=` selector instead."

### QA Debug Mode — LLM root cause analysis

There are two ways to enable debug capture:

- **Settings checkbox** (`Settings → Debug → QA Debug Mode`) — applies to all automation runs. Capture is active during every step; analysis fires on failures.
- **`debug` mode in the mode dropdown** — additionally starts a **passive monitor** that works even when no run is in progress. See [`debug` mode](#debug) above.

When active, the extension silently captures the same signals a QA engineer would check in DevTools:

- **Console** — `log`, `warn`, `error`, `info`, and `debug` entries (up to 50 per step, 500 chars each).
- **Network** — all `fetch` / `XMLHttpRequest` calls: URL, method, status, duration. Response body is captured (up to 2 KB) only for error responses (4xx / 5xx). Request body is captured (up to 1 KB) only for mutations (POST / PUT / PATCH / DELETE).

On failure these are sent to the LLM, which returns a structured diagnosis shown directly in the error card:

| Field | What it means |
| --- | --- |
| **Category badge** | One of `AUTH FAILURE`, `SERVER ERROR`, `CORS ERROR`, `NETWORK ERROR`, `ASSERTION MISMATCH`, `ELEMENT NOT FOUND`, `JS EXCEPTION`, `TEST CONFIG`, `RACE CONDITION`, or `UNKNOWN`. |
| **Summary** | 1–2 sentence plain-English explanation of what went wrong. |
| **Evidence** | The specific log line or HTTP call that triggered the diagnosis. |
| **Fix** | One actionable suggestion the engineer should try first. |

No additional Chrome permissions are required — capture is done via pure JS interception and is off by default. Enable it once in Settings → **Debug** → **QA Debug Mode**, then click **Save settings**.

### Run history panel

A clock icon (🕐) in the panel header opens the **Run History** drawer. The last 50 runs are persisted in `chrome.storage.local` and displayed with:

- Goal / test name
- Mode (test / exploration / agentic / debug)
- Step pass/fail counts
- Elapsed time and relative timestamp ("3m ago")

Click **Clear all** to reset the run history. **Clear debug history** removes
only the passive debug capture cards from the thread and storage.

### Approval gate modal (agentic mode)

When a `request_human` step fires during an agentic run, the panel shows a **prominent modal** instead of silently stopping:

- The **reason** the agent needs human input.
- A preview of the **next step** that will run after approval.
- An **Approve & continue** button — the run resumes where it left off.
- A **Cancel run** button — the run aborts cleanly.

The composer **Stop** (Stop-all) button also dismisses an open gate and cancels
the run. If the panel isn't focused when a gate fires, a Chrome notification
alerts you and clicking it re-focuses the run's window.

---

## Usage

### A. Run a natural-language instruction

1. Open the page you want to operate on. **Reload it once** if it was open
   before you installed the extension (so the content script is injected).
2. Open the side panel.
3. Type something like:
   - `Test that the search box returns at least one result for "playwright"`
   - `Click the "Sign up" button and fill the email field with test@example.com`
   - `Find the price of the first product on the page and extract it as price`
   - `Apply for casual leave on Zoho for tomorrow`
4. Pick a **Mode** (`auto` is fine — see [Operation Modes](#operation-modes)).
5. Hit **Run** (or press **Enter** — Shift+Enter inserts a newline). Live
   progress streams into the **Activity** card and a readable summary appears
   in the **Result** card above it. Use **show raw JSON** / **copy JSON** for
   the full structured output. **Run stays enabled while a run is in progress**,
   so you can switch tabs and start another task; each running bubble has its
   own **Stop**, and the composer **Stop** aborts every active run. Click **+**
   in the header to start a new session (disabled while any run is active).

### B. Run an attached test file

1. Open **Attach test file** and either pick a `.json`/`.yaml` file or paste
   the contents into the text area. (Try `examples/login.yaml`.)
2. Optionally check **Dry run** to highlight each step's target on the page
   and confirm every selector resolves, without executing anything — a quick
   sanity check before a real run.
3. Mode `auto` switches to `test`. Click **Run** — before executing, the panel
   probes the file's selectors against the current page (best-effort; skipped
   for suites and for the run's first step navigating to a different origin)
   and shows a non-blocking toast if some don't resolve, so you know healing
   may be needed.

> You can also paste test YAML/JSON directly into the text area without using
> the file picker.

### Sample test file (YAML)

```yaml
test_name: Login flow
base_url: https://the-internet.herokuapp.com
timeout_ms: 15000
variables:
  username: tomsmith
steps:
  - { action: navigate, url: "${base_url}/login" }
  - { action: type, target: "#username", value: "${username}" }
  - { action: type, target: "#password", value: "SuperSecretPassword!" }
  - { action: click, target: "role=button[name='Login']" }
  - { action: wait, condition: "url_matches:/secure" }
  - { action: assert, target: "#flash", matcher: contains, expected: "logged into a secure area" }
```

### Sample run output (excerpt)

```json
{
  "mode": "test",
  "goal": "Login flow",
  "started_at": "2026-04-30T12:00:00.000Z",
  "finished_at": "2026-04-30T12:00:08.412Z",
  "steps": [
    { "index": 1, "action": "navigate", "value": "https://the-internet.herokuapp.com/login", "status": "success", "duration_ms": 940 },
    { "index": 2, "action": "type", "target": "#username", "value": "tomsmith", "status": "success", "duration_ms": 36 },
    { "index": 3, "action": "type", "target": "#password", "value": "SuperSecretPassword!", "status": "success", "duration_ms": 28 },
    { "index": 4, "action": "click", "target": "role=button[name='Login']", "status": "success", "duration_ms": 41 },
    { "index": 5, "action": "wait", "condition": "url_matches:/secure", "status": "success", "duration_ms": 6800 },
    { "index": 6, "action": "assert", "target": "#flash", "matcher": "contains", "expected": "logged into a secure area", "actual": "You logged into a secure area!", "status": "success", "duration_ms": 5 }
  ],
  "variables": { "base_url": "https://the-internet.herokuapp.com", "username": "tomsmith" },
  "artifacts": { "screenshots": [], "downloads": [], "trace": null },
  "result": { "status": "pass", "passed_steps": 6, "failed_steps": 0, "skipped_steps": 0 },
  "summary": "Run passed in test mode. 6 step(s) executed cleanly."
}
```

---

## Action reference

| Action | Required fields | Notes |
| --- | --- | --- |
| `navigate` | `url` (or `value`), optional `headers` | Waits for the tab to reach `complete`. `headers` (e.g. `{"X-Test":"value"}`, values may reference `${secrets.*}`) attaches custom request headers for that navigation only, via a scoped `declarativeNetRequest` session rule that's redacted in logs and auto-cleaned up afterward. |
| `back` / `forward` / `reload` | — | History navigation. |
| `click` / `dblclick` / `hover` | `target` | Waits for visible before acting. CSS-hidden radio/checkbox inputs are clicked via their `<label>`. |
| `type` | `target`, `value` | Sets the input value via the native setter and fires `InputEvent(inputType: "insertText")` for React controlled inputs, then dispatches `blur` to trigger form validation. Works on `<input>`, `<textarea>`, and `contentEditable` elements. |
| `clear` | `target` | Empties an input (same mechanism as `type` with an empty string). |
| `select` | `target`, `value` | **Native `<select>`**: sets `el.value` and dispatches change. **Custom dropdowns** (React Select, Ant Design, Zoho pickers, etc.): clicks the trigger to open, waits up to 2 s for options to appear, then clicks the option whose text matches `value`. |
| `check` / `uncheck` | `target` | Clicks the checkbox/radio (via label if available) if its state needs to change. |
| `press_key` | `value` (e.g. `Enter`, `Tab`, `Cmd+K`) | Optionally `target`; defaults to focused element. |
| `scroll` | `value` (`into_view` / `bottom` / pixel count) | `value` is a pixel offset (e.g. `"500"`), `bottom` (end of page/element), or `into_view` (scroll element into viewport). Omit `target` to scroll the window. |
| `wait` | `condition` | See conditions list. Optional `target`, `timeout_ms`. |
| `extract` | `target`, `value` (variable name) | `attr` optionally pulls an attribute instead of text/value. |
| `summarize` | `value` (variable name), optional `target` | Sends the element's (or full page's) `innerText` to the LLM and stores the AI summary in the named variable. |
| `screenshot` | optional `target` | Captures the visible viewport. |
| `assert` | `target`, `matcher`, `expected` | Captures `actual` in the run output. Add `critical: true` to abort the run on failure. |
| `switch_tab` | `value` (title or URL substring) | Activates the first open tab matching the value, then **retargets the rest of the run at that tab** so subsequent steps act on the new page. |
| `request_human` | `value` (reason) | Halts the run with `result.status = "needs_human"`. Used as an approval gate in `agentic` mode. |
| `exec_script` | `value` (JS expression) | Evaluates the expression in the page context. Use `variable` to capture the return value. |
| `upload_file` | `target`, `value` (text or `data:` URL) | Injects a file into a `<input type="file">`. Optional: `filename`, `mime_type`. |
| `drag` | `target`, `destination` | Fires `dragstart` → `dragenter` → `dragover` → `drop` → `dragend` between two elements. |
| `switch_frame` | `target` (iframe CSS selector, or `"top"`) | Switches the DOM context to a same-origin iframe. Use `"top"` to return to the main document. All subsequent actions run inside the frame until you switch back. |
| `screenshot_baseline` | `value` (baseline name) | Captures the viewport and stores it as a named baseline in `chrome.storage.local`. |
| `assert_screenshot` | `baseline` (name), optional `threshold` | Compares the current viewport against the stored baseline. Default threshold: 1% pixel diff. |
| `accessibility_audit` | optional `target`, optional `variable` | Checks for missing alt text, unlabelled inputs, nameless buttons, duplicate IDs. Stores violation count. |
| `assert_performance` | `metric`, `max_ms` | Asserts that a Web Vitals metric (LCP, FCP, FP, TTFB, DOMContentLoaded, Load) is ≤ `max_ms`. |
| `mock_network` | `url` (substring), `response`, optional `method`, `status` | Intercepts matching `fetch` calls and returns a stub response. |
| `clear_network_mocks` | — | Removes all active network mocks and restores the original `fetch`. |
| `datepick` | `target` (calendar trigger or container), `value` (date string, e.g. `2026-05-15`), optional `inputTarget` | Opens a date-picker component (jQuery UI, Flatpickr, React DatePicker, etc.), navigates to the correct month, and clicks the matching day cell. The recorder emits `datepick` steps automatically when it detects a calendar interaction. |
| `if` | `condition`, `then` (steps array), optional `else` | Evaluates a `js:` expression or wait condition; runs `then` or `else` sub-steps. |
| `scroll_to` | `target` (or `ref=N`) | Scrolls an element into the viewport center and returns its settled bounding rect. Agent-loop only. |
| `zoom` | `target`/`ref` (+ optional `padding`), or literal `x0,y0,x1,y1`; optional `upscale` | Captures a cropped, upscaled screenshot of one region for close inspection of a small icon or dense table cell. Agent-loop only. |
| `list_tabs` | optional `variable` | Lists open tabs in the current window as `{ index, id, title, url, active }`. Agent-loop only. |
| `read_page` | optional `filter` (`interactive`\|`all`), `ref`, `max_depth`, `max_chars` | Returns the page as a text accessibility tree; `ref` reads only a sub-tree, for exploring large pages incrementally. Agent-loop only. |
| `find` | `query` (natural-language description), optional `limit` | Finds elements by plain-language description and returns ranked matches with `ref` ids. Agent-loop only. |
| `get_page_text` | optional `target`, `max_chars`, `variable` | Returns the page's raw visible text with no LLM involvement — the zero-cost alternative to `summarize`. Agent-loop only. |
| `read_console_messages` | optional `filter`, `level`, `limit`, `clear` | Reads console messages captured during the run (observability is armed for every agent-loop run, not only QA Debug Mode). Agent-loop only. |
| `read_network_requests` | optional `filter`, `status` (`error`\|`4xx`\|`5xx`\|exact code), `limit`, `clear` | Reads captured network requests — use after a mutation to confirm it actually succeeded. Agent-loop only. |
| `open_tab` | `url` | Opens a URL in a **new** tab and continues there; `switch_tab` returns to a previous one. Agent-loop only. |
| `read_download` | `value` (filename substring), `variable` | Reads a recently downloaded **text** file's content by re-fetching its source URL; directly-linked files only (MV3 blocks reading file bytes from disk). Agent-loop only. |
| `read_document` | optional `url` (defaults to current tab), `pages`, `sheet`, `range`, `find`, `max_chars`, `variable` | Reads a PDF, Office Online (Word/Excel/PowerPoint), or Google Doc/Sheet/Slides — parsed off-DOM. Spreadsheets/tables come back as an addressed grid with real cell refs; `find="text"` searches the whole document. Chunk large files with `pages`/`range`/`sheet`. Agent-loop only. See [Reading documents](#reading-documents). |
| `read_tab` | `tab_id` (from `list_tabs`), optional `max_chars`, `variable` | Reads another open tab's text or document **without switching** to it — for cross-tab compare/summarize. Agent-loop only. |
| `resize_window` | `width`, `height`, or `restore: true` | Resizes the whole browser window (affects every tab in it) — useful for responsive breakpoints. Auto-restores to the pre-run size at the end of an agent-loop run. Agent-loop only. |
| `drop_file` | `value` (text, `data:` URL, or `"screenshot:last"`), `target` or `x`/`y`, optional `filename`, `mime_type` | Drag-and-drops a file onto a drop zone that has no `<input type="file">`. Agent-loop only. |
| `autofill` | `value` (JSON object of field data) | Fills the visible form from structured data in one LLM call that maps keys onto visible fields (PII-masked in the prompt; real values substituted locally). Agent-loop only. |
| `click_at` | `x`, `y` (viewport CSS pixels) | Last-resort click by coordinate when no selector (including `ref=N`) can address the element. Agent-loop only. |
| `hover_at` | `x`, `y` (viewport CSS pixels) | Hover by coordinate (`pointermove`/`mouseover`/`mousemove`) — for canvas apps or tooltip triggers with no addressable element. Agent-loop only. |

> **`critical: true`** can be added to any step. When a critical step fails for
> any reason (element not found, navigation error, assertion failure), the runner
> skips all remaining steps and records them as `"skipped"`.
>
> Example: `{ action: click, target: "#confirm", critical: true }`

### Agent-only perception & control tools

The rows above marked "Agent-loop only" are exposed to the LLM as **tool
calls** during `exploration`/`agentic` runs (perceive→act→re-perceive) — they
are not valid step actions in a `test`/`suite` JSON/YAML file, since those run
deterministically with no LLM in the loop. On endpoints that don't support
OpenAI-style function calling, the same vocabulary is offered via a text-JSON
fallback (cached per-endpoint as `toolsUnsupported:<baseUrl>` after the first
failure, so later runs skip straight to text mode).

Two additional **loop-control** signals exist only as tool calls and are never
dispatched to the page:

- **`done`** — the model declares the entire goal satisfied (or impossible)
  and gives its final answer in `summary`. Completing one action is not the
  same as completing the goal.
- **`request_screenshot`** — asks for a screenshot to be attached to the next
  turn, when the text snapshot alone is insufficient (used automatically when
  Vision is set to `auto`).

---

## Local command bridge (CLI / desktop trigger)

*1.16.0.* Delegate a task to the browser from a CLI, a script, or a desktop app,
and get the run report back — with live progress and a meaningful exit code.

```sh
export AINXT_TOKEN=<token from Settings>
node tools/ainxt-bridge/server.js --token $AINXT_TOKEN     # the helper

node tools/ainxt-bridge/cli.js run "what's the top story" --url https://news.ycombinator.com
node tools/ainxt-bridge/cli.js run --file smoke.json --mode test --json > report.json
```

**The extension never listens.** MV3 can't open a socket, and that absence is a
security property worth keeping — AiNxt declares no `externally_connectable`, so
nothing outside the browser can send it anything. The bridge inverts the direction:
a helper *you* run listens on `127.0.0.1`, and the service worker dials out to it,
only while you've enabled the bridge in **Settings → Local command bridge** (off by
default). Both sides then prove they hold the same token by HMAC-ing the other's
nonce; the token itself never crosses the wire.

Runs execute **headlessly in the service worker** — no side panel needed. Pass
`--attach panel` to run in an open panel instead (needed for vision-heavy work,
since screenshots require a focused tab).

**Approvals move to your terminal, they don't disappear:**

```
⏸ CRITICAL approval needed
   Vault secret ${secrets.GH_TOKEN} is about to be typed into github.com
   approve? [y/N]
```

- `--yes` pre-approves ordinary risk gates. It does **not** cover critical ones —
  `exec_script`, a vault secret's first use against a host, or a `js:` condition.
  Those are the gates nothing is allowed to auto-approve, and a CLI flag is not a
  human.
- `--deny-gates`, and any non-TTY invocation (so CI by default), refuses every gate;
  the run ends `needs_human` naming what blocked it.
- `exec_script` additionally needs **Allow script execution** ticked on that machine.
  That setting cannot be changed over the bridge, by design — nor can the bridge be
  enabled by an imported backup or a synced device.

Exit codes: `0` pass · `1` fail/partial · `2` needs_human/max_steps_reached ·
`3` transport error. Every bridge run lands in run history tagged `source: "bridge"`
and raises a desktop notification, so unattended work is still attributable.

Full protocol, threat model, and setup: [`tools/ainxt-bridge/README.md`](tools/ainxt-bridge/README.md).

---

## Limitations

- **Shadow DOM** — open shadow roots ARE traversed (`deepQuerySelectorAll` in
  `content.js` descends into every `shadowRoot`), so web-component content is
  reachable by selectors and the snapshot. Two residual gaps: closed shadow
  roots are inaccessible by design, and CSS combinators that cross a shadow
  boundary won't match (per shadow-DOM scoping rules).
- **Cross-origin iframes** — supported since 1.6.0: `switch_frame` with a URL
  substring or `frame=<index>` injects `content.js` into the target frame and
  routes actions there. Residual limits: Set-of-Marks labels and `click_at`
  are disabled while inside a frame (the screenshot is tab-global but rects are
  frame-local), and a removed/re-added iframe gets a new frame id — re-run
  `switch_frame` if the run reports the frame was replaced.
- **Vision** requires a multimodal model (any vision-capable endpoint). Text-only
  endpoints (most local Ollama / LM Studio models) ignore the toggle — keep it off.
- **Agent loop cost** — one LLM call per action means more requests and higher
  latency than plan-once. The action budget is configurable (Settings → "Max
  steps per run", 5–100, default 20; a test file's `max_steps` overrides it)
  but still finite — exhausting it ends the run with status
  `max_steps_reached`, not a failure.
- **Network idle** is approximated by `document.readyState === "complete"`.
  For SPAs, prefer `wait url_matches:` or `wait visible`.
- **YAML parsing** covers the spec's example shape but is not a full YAML 1.2
  implementation. JSON is always safe.
- **`read_download`** (1.6.0) reads a recently downloaded *text* file by
  re-fetching its source URL — MV3 blocks reading file bytes from disk, so
  POST-generated exports and expired signed links return metadata only.
  Triggering downloads still requires explicit user action.
- **Remote (non-loopback) triggers** — the local command bridge (below) binds
  `127.0.0.1` only. Driving the browser from another machine is out of scope.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Could not establish connection" | Page loaded before the extension was installed; content script not present. | Reload the tab, or press **Run** — the runner re-injects the content script automatically. |
| LLM error 401 / 403 | Bad API key or wrong base URL. | Re-check Settings. For Ollama/LM Studio, the key can be any non-empty string. |
| LLM error 404 on `/chat/completions` | Base URL missing the API version (e.g. missing `/v1`). | Add the version segment. |
| `Selector resolved 0 visible elements` | Element not rendered yet, or selector too strict. | Add a `wait` step, or switch to a `role=` / `text=` selector. |
| `type` action fills the field but React resets it | The component re-renders and overwrites the value before blur. | Add `{ action: wait, condition: "js: document.querySelector('...').value === 'expected'" }` after the `type` step to confirm state settled. |
| `select` option not found in custom dropdown | Option text in the DOM differs from what you passed as `value`. | Use the browser console to inspect the option element's `textContent` and match it exactly. |
| Modal fields not found after dropdown selection | Modal has an open animation; element not visible yet. | Add `{ action: wait, condition: "visible", target: "[role='dialog']" }` before the first field step inside the modal. |
| `chrome://` URLs don't work | Chrome blocks extensions on internal URLs. | Test on regular http(s) pages. |
| Side panel says "Run failed" with no details | Open `chrome://extensions` → **Inspect views: side panel** and check the Console. | |

---

## Extending

- **New action**: implement it in `content.js` `execAction` switch (DOM side)
  and, if needed, in `runner.js` `executeStep` (extension-API side).
- **New wait condition**: extend `checkCondition` in `content.js`.
- **New assertion matcher**: extend `getActual` and `compare` in `content.js`.
- **LLM prompt**: edit `SYSTEM_PROMPT` in `lib/llm.js`.

---

## Security notes

- Secrets (the vault and any configured LLM API key) are stored **unencrypted**
  in `chrome.storage.local` and are resolved client-side immediately before
  each step is sent to the page. Anyone with local profile/OS access to this
  Chrome profile can read them; full at-rest encryption is intentionally out
  of scope for now — everything else in the profile is equally exposed to
  that same access level.
- Secrets are **never mirrored to `chrome.storage.sync`**. Settings sync
  across devices signed into the same Chrome profile (base URL, model name,
  site policy, etc.), but the secrets vault (`secretsJson`) is excluded
  outright, and the LLM config's `apiKey` (and any fallback endpoint's
  `apiKey`) is stripped before the rest of that config syncs.
- A full plaintext backup **including** secrets is available via
  Settings → Export backup — that's a manual, local-file action, distinct
  from the automatic sync mirror above.
- The redactor replaces literal secret values with `***` in the run output.
  If a secret value is a very short string (e.g. `"a"`), the redactor may be
  too aggressive — keep secrets meaningful.
- The agent never echoes the API key.
- The **local command bridge** is off by default and adds no inbound surface:
  the extension dials out to a loopback helper, never listens. Its token is a
  credential — excluded from the sync mirror and from Export/Import, like the
  secrets vault — and the enable toggle is per-machine, so neither a synced
  device nor an imported backup can turn it on. The helper refuses any HTTP
  request carrying an `Origin` header, so a web page in your own browser can't
  drive it.

---

## Roadmap / Remaining Work

Nothing currently blocked. The **local command bridge** (1.16.0) closed the last
standing item, external triggering — see below.

> **Shipped:** the agent loop, vision, live narration, on-page highlights,
> automatic side-effect detection (`agentic` gates now fire without the LLM
> having to plan them — see [Agent](#agent)), **ask-before-acting plan
> approval**, **site permissions (allow/blocklist)**, **built-in site
> navigation hints**, and **saved-prompt shortcuts**.
>
> **Shipped in 1.3.0 (perception):** **Set-of-Marks vision** — with Vision on,
> every snapshot element gets a numbered on-page label during the screenshot,
> and the model can target elements as `ref=N` (the number it sees); a
> **`click_at` coordinate action**; **ranked self-healing** (the heal call now
> returns 3–5 candidate selectors tried in order); and a **visual fallback** —
> when text healing runs out and Vision is on, the model locates the element on
> a labeled screenshot by number or coordinates.
>
> **Shipped in 1.4.0 (site memory):** **persistent self-healing** — on sites
> with *Remember fixes* enabled (Settings → Site memory), healed selectors are
> stored per-origin and retried automatically on future runs with zero LLM
> calls; **site notes** injected into planner prompts; and a **pre-run selector
> scan** that probes an attached test file's selectors against the current page
> and warns about likely failures before the run starts.
>
> **Shipped in 1.5.0 (plan UX + safety):** the ask-before-acting plan is now
> **editable** (edit targets/values inline, reorder, delete) and runs exactly as
> approved — no second planning call; **dry run** (from the plan modal or the
> attached-file panel) highlights every target and checks selectors without
> executing; **step-by-step mode** pauses for Continue before every action
> (Auto-approve never skips it); **LLM risk scoring** — the planner rates each
> step 1–5 and 4+ pauses for approval alongside the keyword gate; the gate shows
> a **pre-submit form diff** ("About to submit: Email = …", passwords masked);
> **PII detection** warns before typing SSN/card-shaped values and masks them in
> run logs; and a **persistent annotation trail** on the page — past actions
> stay as greyed ✓ badges, the next action pulses, extracted values float next
> to their source elements.
>
> **Shipped in 1.6.0 (platform):** **`open_tab`** + a multi-tab research
> pattern (open sources in tabs, extract from each, switch back, synthesize);
> **`read_download`** — read a just-downloaded text file (CSV/JSON/…) into a
> variable and act on it; **cross-origin iframe support** — `switch_frame` by
> URL substring or `frame=<index>` injects the content script into the frame
> (SSO dialogs, payment widgets, embedded editors); and **`autofill`** — paste
> a JSON object and one LLM call maps its keys onto the visible form fields
> (PII-masked in the prompt, real values substituted locally).
>
> **Shipped in 1.7.x (tool-call architecture + perception primitives):** the
> agent loop moved from text-JSON parsing to real OpenAI-style **function
> calling** (`TOOL_REGISTRY` in `lib/llm.js` defines every tool once, shared
> with the prompt's action list; endpoints without tool-call support are
> auto-detected and fall back to text-JSON, cached per-endpoint so later runs
> skip the probe) — 1.7.2 further threads tool-call turns as a real growing
> assistant/tool message conversation instead of replaying summaries. New
> agent-only perception tools: **`list_tabs`**, **`read_page`** (richer
> accessibility tree, readable incrementally via `ref`), **`find`**
> (natural-language element search returning ranked `ref` matches),
> **`get_page_text`** (zero-LLM raw text read), **`zoom`** (cropped/upscaled
> region screenshot via `OffscreenCanvas`), **`scroll_to`** (ref/selector-based,
> returns the settled rect), and **`hover_at`** (coordinate hover, mirrors
> `click_at`) — plus a fix for pages whose extracted text came back empty
> despite visible content. Also new: **`resize_window`** (whole-window resize
> for responsive breakpoints, auto-restored at run end), **`drop_file`** +
> `"screenshot:last"` artifact references for `upload_file`/`drop_file`,
> **custom request headers on `navigate`** (a scoped `declarativeNetRequest`
> session rule, redacted in logs, auto-cleaned up), and **GIF export**
> (`lib/gif.js`, the **Record GIF** setting, and a **Download GIF** button on
> run results).
>
> **Shipped in 1.8.x (polish):** the attach/record composer controls (Record
> button, file upload) now only appear in `test` mode; composer/narration UI
> refinements; and an `isVisible()` fix so `<body>`/`<html>` visibility checks
> work correctly inside iframes too.
>
> **Shipped in 1.9.x (performance + branding):** a **shadow-root cache** in
> `content.js` — shadow roots are collected once per cache generation instead
> of a fresh `querySelectorAll("*")` per lookup, cutting a snapshot from ~5
> full-DOM wildcard scans to 1 on shadow-DOM-heavy pages; and updated branding
> assets.
>
> **Shipped in 1.10–1.12 (documents + tab groups):** **`read_document`** — read
> PDFs (via Chrome's opaque PDF viewer), Office Online Word/Excel/PowerPoint,
> and Google Docs/Sheets/Slides by fetching the underlying bytes/export and
> parsing them off-DOM (`lib/documents.js`); spreadsheets return an addressed
> grid with real cell refs and multi-sheet/`find` search. **`read_tab`** reads
> another open tab's text/document without switching. **Assistant tab-group
> scoping** — sharing tabs via a tab group titled "Assistant" scopes
> `list_tabs`/`switch_tab`/`read_tab` and auto-joins agent-opened tabs (see
> `docs/requirements/REQUIREMENTS-READ_DOCUMENTS.md`, `-TAB_GROUPING.md`).
>
> **Shipped in 1.13–1.14 (in-house PDF engine + fidelity):** the vendored
> `pdf.js` was replaced with a **license-clean in-house PDF text extractor**
> (`lib/pdf.js`: classic/stream xref, object streams, FlateDecode via native
> `DecompressionStream`, ToUnicode/WinAnsi decoding, inline-image skip — see
> `-PDF_ENGINE.md`); and **structured-content fidelity** — PDF and Word tables
> are reconstructed into an addressed grid (shared `lib/grid.js`) and Word
> heading levels (`#`/`##`) and bullet lists (`-`) are preserved instead of
> flattened (see `-STRUCTURED_FIDELITY.md`).
