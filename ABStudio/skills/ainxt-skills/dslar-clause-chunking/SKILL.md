---
name: dslar-clause-chunking
description: Page-chunk extracted AiNxt DL-SAR audit content and reduce per-chunk clause verdicts with a present-if-any rule, so large PDFs are validated over their entire content instead of only the first ~50,000 characters.
---

# DSLAR Clause Chunking

Use this skill from the DSLAR clause validator agents (`clause1-data-elements-validator`, `clauses-2-5-validator`, `clauses-6-9-validator`, `clauses-10-13-validator`) whenever validating a DL-SAR audit report.

## Why

A single read of `enriched.json` truncates evidence (`full_text[:50000]`, `sections[:50]`, `tables[:20]`). For large reports (100+ pages) that drops everything after roughly the first ~15-25 pages, so clauses whose evidence appears later are wrongly marked not-present or inconclusive. This skill splits the document into page windows, lets you evaluate **each chunk** under the same caps, then reduces the per-chunk verdicts with **present-if-any**.

Bundled scripts:

```text
scripts/chunk_dslar_pages.py        # split / read-batch / reduce (per-branch map-reduce)
scripts/aggregate_dslar_clauses.py  # deterministic single-writer aggregator (see below)
```

## Mandatory pattern

Run the bundled script with `code_executor` using Python `runpy`, not shell/bash. Use the absolute script path from the skill manifest.

### Step 1 — split (one code_executor call)

```python
import runpy, sys, json
from pathlib import Path

script_path = r"<absolute_path_from_skill_manifest>/scripts/chunk_dslar_pages.py"
work_dir = Path(WORKFLOW_ARTIFACT_DIR)

sys.argv = ["chunk_dslar_pages.py", "--mode", "split", "--work-dir", str(work_dir), "--chunk-pages", "15"]
runpy.run_path(script_path, run_name="__main__")
# prints: {"chunk_count": N, "chunk_files": [...], "chunk_pages": 15, "total_pages": ...}
```

`--chunk-pages` defaults to 15. A document with `total_pages <= chunk_pages` yields exactly one chunk, so small reports behave identically to the old single-read path.

### Step 2 — evaluate chunks in batches

**Do not read one chunk per `code_executor` call.** Reading each chunk in its own tool call spends one agent iteration per chunk, so a 160-page report (~11 chunks) blows past the node's iteration budget mid-loop, the branch is truncated, and **no `clause_results` are emitted** (the decision-maker then fails with "clause_results is empty"). Read in batches instead:

```python
import json
from pathlib import Path

partials_path = Path(work_dir) / "partials.json"   # canonical recovery file the aggregator reads
batch_start = 0
all_partials = []
while batch_start is not None:
    sys.argv = ["chunk_dslar_pages.py", "--mode", "read-batch", "--work-dir", str(work_dir),
                "--batch-start", str(batch_start), "--batch-size", "4"]
    runpy.run_path(script_path, run_name="__main__")
    # prints: {total_chunks, batch_start, batch_end, next_batch_start, chunks:[{chunk_index, page_start, page_end, full_text, sections, tables, images}, ...]}
    # Reason over EVERY chunk in chunks[] and append its per-chunk partials to all_partials.
    # CHECKPOINT NOW, before the next batch — this write is what the aggregator recovers:
    partials_path.write_text(json.dumps(all_partials, ensure_ascii=False), encoding="utf-8")
    # Then set batch_start = next_batch_start (it is null after the final batch).
```

`--batch-size` defaults to 4, so the whole document is swept in `ceil(total_chunks / 4)` reads regardless of length. Reason over **every** chunk in the returned `chunks[]` and record one **compact partial** per clause (or per Clause-1 data element) for each chunk. Keep partials small — booleans and short `evidence_refs` only, never full chunk text. `evidence_refs` should cite the page so late-page evidence is traceable (e.g. `"Table 2 page 97"`).

(A single-chunk read via `--mode read --chunk-index i` is still available for debugging, but batched reads are mandatory in the validator loop.)

Per-chunk partial shapes:

```json
// clauses 2-13
{"clause_id": "6", "clause_name": "Transaction Processing", "chunk_index": 2,
 "present": true, "inconclusive": false, "satisfactory": true, "evidence_refs": ["... page 97"]}

// clause 1 data elements
{"serial": 3, "scope": "payments", "category": "Customer Data", "label": "VPA", "chunk_index": 2,
 "present": true, "inconclusive": false, "satisfactory": true,
 "rest_or_processing": "...", "jurisdiction": "...", "brought_back_status": "...",
 "evidence_refs": ["... page 97"]}
```

A chunk that has no evidence for a clause/element should set `present=false` (clearly absent in that window) — only the reduce step decides the final not-present.

**Checkpoint after every batch — this is the single most important step.** As soon as you finish reasoning over a batch, overwrite `BRANCH_DIR/partials.json` with the **full accumulated** `all_partials` list *before* requesting the next batch. If the node then runs out of iteration budget mid-loop, the checkpointed `partials.json` still holds every chunk evaluated so far and the deterministic aggregator can reduce it — the branch is never silently lost. A branch that reads every chunk but never writes `partials.json` produces a blank "not concluded" verdict for all of its clauses, because the chat fan-in does not carry your reasoning to the aggregator — **only the files in `BRANCH_DIR` do.**

**Do not write scratch files.** The only files this branch may leave in `BRANCH_DIR` are the script's own `chunk_*.json`, your `partials.json`, and (at the end) `result.json`. Do NOT dump batch responses or chunk evidence into other filenames such as `all_chunks_data.json` or `*_full.json`: it consumes iteration budget, produces nothing the aggregator reads, and is the most common cause of a blank branch.

### Step 3 — reduce (present-if-any)

The full per-chunk partials list has already been checkpointed to `BRANCH_DIR/partials.json` after each batch (Step 2). Ensure the final complete list is written there, then reduce:

```python
partials_path = work_dir / "partials.json"   # canonical name; aggregator also reads legacy clause_partials.json
partials_path.write_text(json.dumps(all_partials, ensure_ascii=False), encoding="utf-8")

sys.argv = ["chunk_dslar_pages.py", "--mode", "reduce", "--work-dir", str(work_dir),
            "--partials-json", str(partials_path), "--reduce-kind", "clause"]  # or "data_element"
runpy.run_path(script_path, run_name="__main__")
```

`--reduce-kind clause` groups by `clause_id` → `{"clause_results": [...]}`. `--reduce-kind data_element` groups by `serial` → `{"data_element_results": [...]}`.

## Reduce semantics (present-if-any)

For each clause (or data element serial), across all chunk partials:

- `present` = `true` if **any** chunk found it; `false` only if **every** chunk clearly marked it absent; otherwise `null`.
- `inconclusive` = `true` exactly when `present` is `null`.
- `satisfactory` = `false` if any contributing partial is `false`; `true` if all contributing partials are `true`; else `null`.
- `rest_or_processing` / `jurisdiction` / `brought_back_status` = taken from the first chunk that found the element present.
- `evidence_refs` = de-duplicated union across all chunks.

The reduced objects match the clause validators' existing output schema exactly. Do **not** write `enriched.json` from a clause validator — return the reduced branch update only; the aggregator is the single writer.

## Aggregating branches (`aggregate_dslar_clauses.py`)

The `clause-results-aggregator` node runs this deterministic script instead of merging clause results in the prompt. It is the **single writer** of `clause_results` into `enriched.json` and **always produces exactly 13 ordered clauses** (Clause 1 carrying all 68 data-element rows), so the rendered PDF can never collapse to a metadata-only page even when a clause branch ran out of budget.

```python
import runpy, sys
script_path = r"<absolute_path_from_skill_manifest>/scripts/aggregate_dslar_clauses.py"
sys.argv = ["aggregate_dslar_clauses.py",
            "--work-dir", WORKFLOW_ARTIFACT_DIR,
            "--enriched-json", WORKFLOW_ARTIFACT_DIR + "/enriched.json"]
runpy.run_path(script_path, run_name="__main__")
# prints: {artifact_dir, enriched_json, clause_count, clause1_data_elements, recovery, validation_type, status}
```

CLI: `--work-dir <dir>` (default `$WORKFLOW_ARTIFACT_DIR`), `--enriched-json <path>` (default `<work-dir>/enriched.json`), `--output-json <path>` (optional; default writes back in place — used for safe testing).

Per-branch recovery precedence (for each of `_chunk_clause1`, `_chunk_clauses_2_5`, `_chunk_clauses_6_9`, `_chunk_clauses_10_13`):

1. `BRANCH_DIR/result.json` — the branch's finalized output, used directly.
2. else `BRANCH_DIR/partials.json` (or the legacy `clause_partials.json`) — reduced via `reduce_all` (present-if-any). Clause 1 reduces to 68 `data_element_results` and is rolled up into one parent Clause-1 result.
3. else a **deterministic chunk scan** of `BRANCH_DIR/chunk_*.json` — when a branch split and read its chunks but ran out of iteration budget before checkpointing `partials.json`, the capped chunk evidence is still on disk. Each owned clause (or Clause-1 data element) is keyword-matched against every chunk's `full_text`; a match yields `present=true` with page-cited `evidence_refs` (e.g. `"pages 75-99: '...snippet...'"`), but always `inconclusive=true` / `satisfactory=null` because a keyword scan cannot reach an auditor conclusion. This tier only counts as a recovery if at least one owned clause finds evidence; an all-empty scan falls through to the skeleton.
4. else a **not-concluded skeleton** (`present=null`, `inconclusive=true`); Clause 1 synthesizes all 68 rows from the embedded canonical table.

It embeds the canonical 13-clause id→name map and the 68-row Clause-1 table (no config file exists), recomputes `points_not_concluded` (preserving the metadata-validator's existing entries and adding `"Clause <id> (<name>): could not be concluded"` for each inconclusive clause), writes `clause_results` + `points_not_concluded` at the **top level** of `enriched.json`, and normalizes `validation_type` to the lowercase `dlsar` token. It never raises on an incomplete branch; it only hard-fails if `enriched.json` itself is unreadable.

## Backward compatibility

With the default 15-page window, a 5-page report produces one chunk whose caps equal the original full-document caps, and present-if-any over a single partial is the identity. Behavior for small PDFs is unchanged.
