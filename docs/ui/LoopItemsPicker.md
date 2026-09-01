# LoopItemsPicker

## Overview

`LoopItemsPicker` is a React component in the ABStudio frontend workflow editor that provides a **connection-aware list picker** for the Loop node's "for each" iteration mode. Instead of forcing users to type a raw dotted-path expression (e.g. `input.results.docs`), it inspects the upstream node's last-run output, automatically detects any lists within it, and presents them as plain-language, click-to-pick options (e.g. "Docs (5 items)"). When exactly one list is found, it is selected silently with no user interaction required.

The component is part of the [workflow editor](#workflow-editor-context) sub-module tree and is rendered inside the [ConfigPanel](../infrastructure/ConfigPanel.md) when a Loop node is selected and its mode is set to `for_each`.

---

## Architecture

### Component Location

```
ABStudio/frontend/src/features/workflows/editor/LoopItemsPicker.jsx
```

### High-Level Architecture

```mermaid
graph TB
    subgraph "Workflow Editor"
        ConfigPanel["ConfigPanel<br/>(Loop config section)"]
        LoopItemsPicker["LoopItemsPicker"]
        LoopNode["LoopNode<br/>(canvas node)"]
    end

    subgraph "Helpers"
        LoopPickerHelpers["helpers/loopPicker.js<br/>parseUpstreamOutput<br/>findListsInOutput<br/>getUpstreamNodeId"]
    end

    subgraph "State"
        WorkflowStore["workflowStore.js<br/>(Zustand)"]
    end

    subgraph "API Layer"
        ApiFetch["apiFetch<br/>(config/api.js)"]
    end

    subgraph "Backend"
        ChatAPI["chat.py<br/>GET /node-last-output"]
        NativeEngine["NativeEngine<br/>.get_node_last_output()"]
        CheckpointStore["CheckpointStore<br/>.load_node_output()"]
    end

    ConfigPanel -->|"renders when mode=for_each"| LoopItemsPicker
    ConfigPanel -->|"derives upstreamNodeId"| LoopPickerHelpers
    LoopItemsPicker -->|"fetches upstream output"| ApiFetch
    ApiFetch -->|"HTTP GET"| ChatAPI
    ChatAPI -->|"delegates"| NativeEngine
    NativeEngine -->|"loads persisted record"| CheckpointStore
    LoopItemsPicker -->|"parse + detect lists"| LoopPickerHelpers
    LoopItemsPicker -->|"onChange(itemsExpression)"| ConfigPanel
    ConfigPanel -->|"updateNodeData"| WorkflowStore
    WorkflowStore -->|"getWorkflowForExecution<br/>forwards itemsExpression"| NativeEngine
    LoopNode -->|"reads data.itemsExpression<br/>for canvas label"| WorkflowStore
```

### Module Dependencies

```mermaid
graph LR
    LoopItemsPicker -->|"import"| ApiFetchMod["config/api.js::apiFetch"]
    LoopItemsPicker -->|"import"| LoopPickerMod["helpers/loopPicker.js"]
    LoopPickerMod -->|"exports"| ParseUpstream["parseUpstreamOutput"]
    LoopPickerMod -->|"exports"| FindLists["findListsInOutput"]
    LoopPickerMod -->|"exports"| GetUpstream["getUpstreamNodeId"]
    ConfigPanelMod["ConfigPanel.jsx"] -->|"import"| LoopItemsPicker
    ConfigPanelMod -->|"import"| GetUpstream
```

---

## Core Component: `LoopItemsPicker`

### Props

| Prop | Type | Description |
|------|------|-------------|
| `value` | `string` | The current dotted-path items expression (e.g. `input.items`, `input.results.docs`). Defaults to `'input.items'` when unset. |
| `onChange` | `(path: string) => void` | Callback invoked when the user (or auto-detect) selects a different list path. The ConfigPanel wires this to `updateNodeData(nodeId, { itemsExpression: next })`. |
| `upstreamNodeId` | `string \| null` | The node ID whose output feeds the Loop node's top input. Derived by `getUpstreamNodeId(edges, loopNodeId)` in the ConfigPanel. `null` when nothing is connected. |
| `upstreamNodeName` | `string` | Human-friendly name of the upstream node (from `node.data.name` or `node.type`), used in hint messages. |
| `threadId` | `string` | The active chat thread ID. Required to fetch the upstream node's last-run output. Empty/absent before the workflow has been run at least once. |

### Internal State

| State | Type | Description |
|-------|------|-------------|
| `output` | `any \| null` | The raw upstream node output fetched from the backend. |
| `status` | `'idle' \| 'loading' \| 'loaded' \| 'error'` | Current fetch lifecycle state. |
| `error` | `string` | Error message when `status === 'error'`. |
| `autoAssignedRef` | `Ref<string \| null>` | Tracks the last path that was auto-assigned so a subsequent manual override is never clobbered by a later auto-detect cycle. |

### Render Branches

The component renders different UI based on the connection and fetch state:

```mermaid
flowchart TD
    Start["LoopItemsPicker render"] --> Q1{"upstreamNodeId<br/>connected?"}
    Q1 -->|"No"| NoConn["Muted card:<br/>'Connect a node into the top<br/>of this loop...'"]
    Q1 -->|"Yes"| Q2{"threadId<br/>available?"}
    Q2 -->|"No"| NoThread["Muted card:<br/>'Run the workflow once...'<br/>(safe default retained)"]
    Q2 -->|"Yes"| Q3{"Fetch status"}
    Q3 -->|"loading"| Loading["Muted card:<br/>'Reading upstream output...'"]
    Q3 -->|"error"| ErrorCard["Warn card:<br/>error message"]
    Q3 -->|"loaded"| Q4{"detectedLists.length"}
    Q4 -->|"0"| NoLists["Warn card:<br/>'did not produce a list...'<br/>instructs user to adjust"]
    Q4 -->|"1"| SingleList["OK card:<br/>'Looping over X from Y<br/>(N items)'"]
    Q4 -->|">1"| MultiList["Dropdown select:<br/>friendly labels + item counts<br/>+ hint text"]
```

### Auto-Assignment Logic

When exactly one list is detected in the upstream output, the component silently locks the items expression to that list's path — but only if the user hasn't already manually chosen a different path:

```mermaid
flowchart TD
    A["detectedLists.length === 1?"] -->|"No"| End["No auto-assign"]
    A -->|"Yes"| B["only = detectedLists[0].path"]
    B --> C{"value === only?"}
    C -->|"Yes"| End
    C -->|"No"| D{"value is empty or<br/>DEFAULT_PATH ('input.items')?"}
    D -->|"Yes"| Assign["Auto-assign: onChange(only)<br/>autoAssignedRef.current = only"]
    D -->|"No"| E{"value === autoAssignedRef.current?<br/>(user picked the auto value)"}
    E -->|"Yes"| Assign
    E -->|"No"| End
```

This ensures that:
1. A user who manually selects a different list from a multi-list dropdown is never overridden.
2. A user who accepts the auto-assigned single list and later re-opens the config sees the same value (no flicker).
3. The safe default (`input.items`) is always present so the canvas can be saved even before the workflow has been run.

---

## Helper Module: `helpers/loopPicker.js`

Three pure functions support the picker. They are also imported directly by the [ConfigPanel](../infrastructure/ConfigPanel.md) for upstream-node derivation.

### `parseUpstreamOutput(raw)`

Parses the cached upstream output string into a JS value.

- If `raw` is `null` → returns `null`.
- If `raw` is not a string → returns it as-is.
- If the trimmed string starts with `{` or `[` → attempts `JSON.parse`; on failure, falls through.
- Otherwise → returns the raw string (prose output).

### `findListsInOutput(value)`

Walks a parsed JS value (object tree) and collects every array, paired with its dotted path.

**Returns:** `Array<{ path: string, length: number, samplePreview: string }>`

**Safety limits:**

| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_DEPTH` | 4 | Prevents deep recursion into nested objects. |
| `MAX_LISTS` | 12 | Caps the number of detected lists (dropdown stays usable). |
| `MAX_NODES_VISITED` | 5000 | Guards against pathological structures. |
| `SAMPLE_PREVIEW` | 80 | Truncates the first item's preview string. |

Arrays are detected but their items are **not** recursed into (avoids noise). The root path is always `'input'`, matching the engine's `_run_loop` pathing convention.

### `getUpstreamNodeId(edges, loopNodeId)`

Finds the source node ID of the edge connected to the Loop node's top input handle (`'target'`). Accepts edges with an empty `targetHandle` for backward compatibility with older edges created before the handle ID was added.

**Returns:** `string | null`

---

## Data Flow

### End-to-End Flow: From Canvas Wiring to Loop Execution

```mermaid
sequenceDiagram
    participant User
    participant Canvas as Canvas / ConfigPanel
    participant Picker as LoopItemsPicker
    participant API as Backend API
    participant Engine as NativeEngine
    participant Store as CheckpointStore

    User->>Canvas: Wire UpstreamAgent → Loop (top handle)
    Canvas->>Canvas: getUpstreamNodeId(edges, loopId) → upstreamId
    User->>Canvas: Select Loop node, set mode = for_each
    Canvas->>Picker: Render with upstreamNodeId, threadId

    Note over Picker: If threadId is empty → show "run once" hint

    User->>Canvas: Run workflow (chat panel)
    Canvas->>API: POST /run-stream
    API->>Engine: execute(chain, input, ctx)
    Engine->>Store: Persist node outputs per thread
    Engine-->>API: SSE events (agent_complete, etc.)
    API-->>Canvas: SSE stream → threadId assigned

    Note over Picker: threadId now available → triggers fetch

    Picker->>API: GET /node-last-output/{threadId}/{upstreamNodeId}
    API->>Engine: get_node_last_output(threadId, nodeId)
    Engine->>Store: load_node_output(threadId, nodeId)
    Store-->>Engine: { agent, output, updated_at }
    Engine-->>API: record
    API-->>Picker: { output: "..." }

    Picker->>Picker: parseUpstreamOutput(output)
    Picker->>Picker: findListsInOutput(parsed)
    Note over Picker: If 1 list → auto-assign<br/>If >1 lists → dropdown<br/>If 0 lists → warn card

    Picker->>Canvas: onChange(itemsExpression)
    Canvas->>Canvas: updateNodeData(loopId, { itemsExpression })

    User->>Canvas: Run workflow again
    Canvas->>Canvas: getWorkflowForExecution()
    Note over Canvas: Serializes itemsExpression into<br/>loop node payload
    Canvas->>API: POST /run-stream
    API->>Engine: _run_loop reads itemsExpression<br/>resolves list from upstream output
```

### Items Expression Path Convention

The dotted-path value maintained by the picker mirrors the backend engine's `_run_loop` resolution:

| Path | Meaning |
|------|---------|
| `input` | The whole upstream value (treated as the list itself if it's an array). |
| `input.items` | Default safe path — expects an `items` array in the upstream output. |
| `input.results.docs` | Walks `upstream_output["results"]["docs"]` to find the list. |

Inside the loop body, agents reference the current item as `{{loop.item}}` and the iteration number as `{{loop.index}}`.

---

## Integration Points

### ConfigPanel Integration

The [ConfigPanel](../infrastructure/ConfigPanel.md) renders `LoopItemsPicker` only when the selected node is a Loop node and its mode is `for_each`:

```jsx
{loopMode === 'for_each' && (
    <div className="form-group">
        <label className="form-label">List to iterate</label>
        <LoopItemsPicker
            value={loopData.itemsExpression || 'input.items'}
            onChange={(next) => handleChange('itemsExpression', next)}
            upstreamNodeId={loopUpstreamId}
            upstreamNodeName={loopUpstreamNode?.data?.name || loopUpstreamNode?.type || ''}
            threadId={activeThreadId}
        />
    </div>
)}
```

The `loopUpstreamId` is derived reactively from the store's edges via `getUpstreamNodeId`, so the picker re-renders only when the edge wiring into the selected Loop node actually changes — not on every unrelated edge edit.

### Workflow Store Integration

The `itemsExpression` value flows through the [workflow store](../storage/store.md) (`workflowStore.js`):

1. **Default:** `getDefaultNodeData('loop')` sets `itemsExpression: 'input.items'`.
2. **Update:** `updateNodeData(nodeId, { itemsExpression })` persists the picker's selection into `node.data`.
3. **Serialization:** `getWorkflowForExecution()` forwards `itemsExpression` in the loop node payload sent to the backend.
4. **Validation:** `loopBodyClosesOnNode()` validates that the loop body subgraph closes back on the loop node (body/exit handles wired correctly).

### Backend API Endpoint

The picker fetches upstream output via:

```
GET /node-last-output/{thread_id}/{node_id}
```

Defined in `ABStudio/backend/app/api/chat.py::get_node_last_output`, this endpoint delegates to `NativeEngine.get_node_last_output(thread_id, node_id)`, which loads the persisted record from the [CheckpointStore](../reference/checkpoint.md) via `load_node_output(thread_id, node_id)`.

**Response shape:**

```json
{
    "thread_id": "...",
    "node_id": "...",
    "output": "...",
    "agent": "...",
    "updated_at": "..."
}
```

Returns `{"output": null}` when the node hasn't run in the given thread yet.

### LoopNode Canvas Display

The [LoopNode](../workflows/workflow_editor_nodes.md) component reads `data.itemsExpression` to render a human-friendly label on the canvas (e.g. "For each doc" instead of the raw path). This label is purely cosmetic — the dotted path remains the source of truth in `node.data`.

---

## Related Modules

| Module | Relationship |
|--------|-------------|
| [ConfigPanel](../infrastructure/ConfigPanel.md) | Parent component that renders `LoopItemsPicker` inside the Loop configuration form. |
| [LoopNode](../workflows/workflow_editor_nodes.md) | Canvas node component that displays the loop mode and reads `itemsExpression` for its label. |
| [workflowStore](../storage/store.md) | Zustand store holding nodes, edges, and the `getWorkflowForExecution` serializer that forwards `itemsExpression` to the backend. |
| [api_execution](../api/api_execution.md) | Backend execution endpoints (`/run-stream`) that drive the NativeEngine, which persists per-node outputs consumed by the picker. |
| [api_chat](../api/api_chat.md) | Backend chat endpoints including `GET /node-last-output` that the picker calls to fetch upstream output. |
| [engine_native_engine](../reference/engine_native_engine.md) | The `NativeEngine` class that executes workflows, runs `_run_loop`, and provides `get_node_last_output()`. |
| [checkpoint](../reference/checkpoint.md) | Checkpoint store layer that persists per-node outputs (`load_node_output`) queried by the picker. |
| [LoopWhileEditor](../workflows/workflow_editor_conditions.md) | Sibling component rendered in the ConfigPanel for the Loop node's `while` mode (condition-driven iteration). |

---

## Design Principles

1. **No raw paths visible to users.** The dotted-path value (`input.results.docs`) is maintained in the store for the backend engine but is never surfaced as an editable string in the UI. Users see humanised labels ("Docs").

2. **Progressive disclosure.** The component adapts its UI to the available context:
   - No upstream connection → instruct user to wire one.
   - No thread (workflow not run) → instruct user to run once; safe default retained.
   - Loading → transient status message.
   - Error → warn card with the error.
   - No lists found → actionable guidance to adjust upstream instructions.
   - Exactly one list → silent auto-selection with confirmation card.
   - Multiple lists → structured dropdown with item counts.

3. **Non-clobbering auto-assign.** The `autoAssignedRef` ensures that once a user manually picks a list from a multi-list dropdown, a subsequent re-detect (e.g. after re-running the workflow) never overrides their choice — even if the re-detect finds only one list.

4. **Safe defaults.** The `DEFAULT_PATH` (`input.items`) is always present so the canvas can be saved and the workflow can execute (with a potentially empty list) even before the upstream node has produced output.
