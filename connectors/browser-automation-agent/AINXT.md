# AINXT.md

This file provides guidance to AI coding agents working with code in this repository.

## What this project is

**AiNxt Browser Assistant** is a Chrome MV3 side-panel extension that drives browser automation via an OpenAI-compatible LLM. Users type natural-language instructions or attach JSON/YAML test files; the extension executes them against the active tab and returns a structured run report.

There is no build step, bundler, or package manager. All files are vanilla ES modules loaded directly by Chrome. To test changes, reload the extension at `chrome://extensions` → **Reload**.

## Architecture

```
background.js      Service worker: opens side panel per-tab, proxies screenshots/scripting,
                   runs the passive debug monitor (network error capture via webRequest API)

sidepanel.html/css/js   UI: chat thread, composer, settings panel. Imports runner and llm
                         via ES module imports. Talks to content.js via chrome.tabs.sendMessage.

content.js         Injected into every page. Executes DOM actions (click, type, select, assert,
                   wait, etc.) and the page snapshot. Also houses the recorder and QA debug
                   capture (fetch/console interceptors).

lib/llm.js         OpenAI-compatible /chat/completions client (chatCompletion supports an
                   optional `tools` array and returns parsed tool_calls). Builds prompts and
                   parses responses for: plan-once, agent-loop (planNextAction, tool-call mode
                   with text-JSON fallback), selector healing, natural-language find, root cause
                   analysis, and summarize. Contains SYSTEM_PROMPT, TOOL_REGISTRY (single source
                   of truth for tool schemas AND the prompt's action list), PERMISSION_TIERS
                   (shared with the runner's safety gate), and the compact snapshot formatter
                   (formatSnapshotForLLM).

lib/runner.js      Orchestrates a full run: resolves variables/secrets, dispatches steps to
                   content.js, handles retries, self-healing selectors (LLM-guided), agent loop
                   (perceive→act→re-perceive; prefers structured tool calls, several per turn,
                   with per-endpoint fallback to text-JSON cached in chrome.storage.local as
                   `toolsUnsupported:<baseUrl>`), approval gates, the observation channel
                   (read-type results ride back to the model via history entries), and builds
                   the output record.

lib/parser.js      Normalizes JSON/YAML test files into a canonical step array. Handles
                   single-test, array, and suite formats.

lib/yaml.js        Minimal custom YAML parser (not a full YAML 1.2 implementation — JSON is
                   always safer for complex values).

lib/markdown.js    Lightweight markdown renderer used to display LLM responses in the panel.
```

### Message flow

1. **sidepanel.js** calls `runAgent()` from `lib/runner.js`
2. **runner.js** sends each step to **content.js** via `chrome.tabs.sendMessage`
3. **content.js** executes the DOM action and replies with a result object
4. For LLM calls, **runner.js** (or **sidepanel.js** for `ask` mode) calls `lib/llm.js`
5. Screenshots go through **background.js** (runner sends a `captureScreenshot` message to the service worker, which calls `chrome.tabs.captureVisibleTab`)

### Operation modes

The UI dropdown exposes only `auto`, `test`, `ask`, and `debug`; the rest are internal modes `auto` routes to.

- `ask` — pure LLM chat, no browser interaction
- `test` — deterministic step execution from a JSON/YAML file, no LLM planning; the attach/record composer controls only appear in this mode
- `exploration` — internal: LLM plans and executes; with **Agent loop** on, runs perceive→act→re-perceive (one LLM call per action)
- `agentic` — internal: same as exploration with auto safety gates on risky actions (the planner classifies risky page work as agentic)
- `suite` — internal: chains multiple test files with shared variables (detected from the attached file)
- `debug` — passive network/console error monitor + exploration on Run
- `auto` — routes clear questions to ask, otherwise plans page work as exploration/agentic per the planner's risk classification

## Extending the extension

- **New action**: add it to the `switch` in `content.js` `execAction()` (DOM side) and, if it needs extension APIs (screenshots, tab control), handle the message in `background.js` and call from `runner.js`. Also add a `TOOL_REGISTRY` entry in `lib/llm.js` — that one entry defines both the tool schema (tool-call mode) and the ACTIONS line in the agent prompt — plus a line in `SYSTEM_PROMPT`'s action list if plan-once should use it
- **New wait condition**: extend `checkCondition()` in `content.js`
- **New assertion matcher**: extend `getActual()` and `compare()` in `content.js`
- **LLM prompt changes**: edit `SYSTEM_PROMPT` or the relevant prompt builder in `lib/llm.js`
- **Risk/safety changes**: edit `PERMISSION_TIERS` in `lib/llm.js` — the runner's gate (`RISKY_ACTIONS`, `RISKY_INTENT_RE`) and both prompts' tier sections derive from it

## Key constraints

- **Shadow DOM**: open shadow roots ARE pierced (`deepQuerySelectorAll()` in `content.js`); closed roots and CSS combinators crossing a shadow boundary are not
- **Cross-origin iframes**: supported via `switch_frame` with a URL substring or `frame=<index>` — the runner injects `content.js` into the frame (`frameId`-targeted) and routes messages there. SoM labels and `click_at` are disabled while inside a frame; a replaced iframe needs a fresh `switch_frame`
- **YAML parser is minimal**: use JSON for complex test files; the custom parser in `lib/yaml.js` doesn't cover the full spec
- **Agent loop cap**: configurable action budget (Settings → "Max steps per run", 5–100, default 20; `max_steps` on a test file/plan overrides). Exhausting it ends the run with status `max_steps_reached`, not a failure
- **Vision is tri-state**: `off` | `auto` | `on`. Auto attaches a screenshot only after a failed step, on model request (`request_screenshot` tool / `need_screenshot` flag), or when the snapshot is degenerate; each step record carries `vision_attached`
- **Observability**: console + network capture is armed for every agent-loop run (`read_console_messages` / `read_network_requests` read the buffers), not only in QA Debug Mode
- **Assistant tab group**: a run whose tab sits inside a tab group titled "Assistant" (case-insensitive) is scoped to that group — `list_tabs`/`switch_tab`/`read_tab` see only grouped tabs, and agent-opened or page-spawned tabs auto-join the group. No group → previous unscoped behavior. The group id is resolved once per run (`resolveAssistantGroup` in `lib/runner.js`); membership is queried live from `chrome.tabGroups` on every use, never cached. `read_tab` reads another grouped tab's text (or document via `readDocument`) without switching; cross-tab screenshots are not supported (`captureVisibleTab` needs the active tab)
- **MV3 service worker**: `background.js` is a service worker — no persistent state beyond `chrome.storage`; it can be terminated between events
