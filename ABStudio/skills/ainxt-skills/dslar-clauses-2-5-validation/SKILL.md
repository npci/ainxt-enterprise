---
name: dslar-clauses-2-5-validation
description: Validate AiNxt DL-SAR Clauses 2 through 5 against extracted audit report content and previous workflow state.
---

# DSLAR Clauses 2-5 Validation

Use this skill for the `clauses-2-5-validator` agent in the DSLAR AiNxt Audit Validation workflow.

## Mandatory validation rule

Validate only these clauses:

- Clause 2: Transaction/Data Flow
- Clause 3: Application Architecture
- Clause 4: Network Diagram/Architecture
- Clause 5: Data Storage

Read `WORKFLOW_ARTIFACT_DIR/enriched.json` as the source of truth using `code_executor`, especially `extracted`, `metadata_checks`, and existing `points_not_concluded`. Do not invent evidence. Consider text, sections, tables, and image descriptions/refs before deciding.

## Evidence excerpt construction (page-chunked map-reduce)

Large audit reports overflow a single 50,000-character read, which silently drops
evidence on later pages. Use the **dslar-clause-chunking** skill to validate the
whole document chunk by chunk, then reduce per clause with present-if-any.

1. **Split** the document into page-chunks (default 15 pages) with
   `chunk_dslar_pages.py --mode split`. Read `metadata_checks`,
   `validation_type`, and `points_not_concluded` from `enriched.json` once.
2. **Evaluate chunks in batches.** Do NOT read one chunk per `code_executor`
   call — one tool call per chunk exhausts the node's iteration budget on large
   reports and the branch is truncated before it emits any `clause_results`.
   Instead loop `--mode read-batch --batch-start <n> --batch-size 4`, which
   returns several chunks' capped evidence at once (per-chunk caps unchanged:
   `full_text[:50000]`, `sections[:50]`, `tables[:20]`, `rows[:50]`,
   `images[:100]`) plus a `next_batch_start` cursor; repeat with that cursor
   until it is null. Decide each clause against EVERY chunk in each batch and
   record a compact partial per clause per chunk (`clause_id`, `clause_name`,
   `chunk_index`, `present`, `inconclusive`, `satisfactory`, `evidence_refs`
   citing the page). Consider text, sections, tables, and image
   descriptions/refs.
3. **Reduce** with `--mode reduce --reduce-kind clause` (present-if-any): a clause
   is present if **any** chunk yields grounded evidence; not-present only if
   **every** chunk clearly marked it absent; otherwise inconclusive. See the
   dslar-clause-chunking skill for the exact split/read/reduce commands.

A report with `total_pages <= chunk_pages` produces a single chunk, so small
reports behave exactly as before.

## Shared output rules

For each clause, return:

```json
{
  "clause_id": "2",
  "clause_name": "Transaction/Data Flow",
  "present": true,
  "inconclusive": false,
  "evidence_refs": ["Section: Transaction Flow", "Image on page 7"],
  "satisfactory": true,
  "raw_agent_output": "short reasoning grounded in evidence",
  "data_element_results": []
}
```

Use confidence threshold `0.7`:

- confidence >= 0.7: `inconclusive=false`
- confidence < 0.7: `inconclusive=true`
- ambiguous, missing, or contradictory evidence: `present=null`, `satisfactory=null`, `inconclusive=true`
- clear absence: `present=false`, `inconclusive=false`

For every inconclusive clause, append exactly:

```text
Clause <id> (<name>): could not be concluded
```

## Clause 2: Transaction/Data Flow

### Requirement

The SAR must include detailed transaction/data flow with stepwise explanation of how transactions flow. It must cover application modules/components through which each data element passes or gets stored in India or outside India for processing. The flow should provide conclusive evidence that transaction logs are in India, or if processed outside India, evidence that data at rest remains only in India.

### Checklist points

- Detailed diagram of transaction and data flow is included.
- Diagram details steps through different application components.
- Required for all transactions including cross-border ones.
- Explains data elements stored in India and other jurisdictions if applicable.
- Explains data elements flowing/getting stored through modules, including inter-process processing if applicable.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Transaction flow diagram or section.
- Stepwise transaction journey.
- Component/module mapping.
- Data element movement or storage mapping.
- Domestic vs overseas processing/storage statements.
- Auditor conclusion and supporting evidence refs.

## Clause 3: Application Architecture

### Requirement

The SAR must include detailed application architecture clearly indicating where application modules/components are located geographically.

### Checklist points

- Detailed application architecture diagram showing components and modules.
- Location of every component verified.
- Relevant evidence included.
- Diagram includes description and functionality of each module.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Architecture diagram.
- Module/component inventory.
- Geographic location/jurisdiction for modules.
- Functionality descriptions.
- Evidence documents/screenshots/tables.
- Auditor conclusion.

## Clause 4: Network Diagram/Architecture

### Requirement

The SAR must include detailed network architecture for all Primary Recovery (PR) and Disaster Recovery (DR) sites, showing relevant equipment including CBS where applicable. PR and DR site locations must be provided. If no DR site exists but AiNxt has permitted go-live, the SAR must state that SAR will be resubmitted after DR setup.

### Checklist points

- Detailed network diagram for PR and DR sites.
- Relevant equipment included, including CBS where applicable.
- Details of all PR and DR site locations.
- If DR is unavailable but permitted, resubmission after DR setup is stated.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Network architecture diagram.
- PR/DR site table or section.
- Equipment list.
- CBS references where applicable.
- DR exception/go-live/resubmission statement.
- Auditor conclusion.

## Clause 5: Data Storage

### Requirement

The SAR must clearly bring out that defined payment data is stored only in India and that no copy or backup is maintained outside Indian jurisdiction in any form.

### Checklist points

- Conclusive check that defined payment data is only stored in India.
- Conclusive check that no copy/backup is maintained outside India.
- Evidence showing data/application repository and availability zones.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Payment data storage location statement.
- Repository/database/application storage evidence.
- Availability zone / region / data center location.
- No overseas copy/backup statement.
- Auditor conclusion.

## Final output schema

Return JSON-friendly state update:

```json
{
  "clause_results": [
    {"clause_id": "2", "clause_name": "Transaction/Data Flow", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "3", "clause_name": "Application Architecture", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "4", "clause_name": "Network Diagram/Architecture", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "5", "clause_name": "Data Storage", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []}
  ],
  "points_not_concluded": []
}
```
