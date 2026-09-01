---
name: dslar-clauses-6-9-validation
description: Validate AiNxt DL-SAR Clauses 6 through 9 against extracted audit report content and previous workflow state.
---

# DSLAR Clauses 6-9 Validation

Use this skill for the `clauses-6-9-validator` agent in the DSLAR AiNxt Audit Validation workflow.

## Mandatory validation rule

Validate only these clauses:

- Clause 6: Transaction Processing
- Clause 7: Activities Related to Payment Processing
- Clause 8: Cross Border Transactions
- Clause 9: Database Storage and Maintenance

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
  "clause_id": "6",
  "clause_name": "Transaction Processing",
  "present": true,
  "inconclusive": false,
  "evidence_refs": ["Section: Processing Location", "Table 4 page 10"],
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

## Clause 6: Transaction Processing

### Requirement

The SAR must explicitly state which aspects of transaction processing are done in India and outside India. If processing occurs outside India, the SAR must clearly mention purging policy/process for processed payments data and state whether the policy/process aligns with RBI Data Localization compliance expectations and is followed satisfactorily.

### Checklist points

- Checks aspects of transaction processing in India vs outside India.
- Performs conclusive checks of purging policy for transaction data.
- Purging must align with RBI guideline: within 24 hours or end of business day, whichever is earlier.
- Related evidence is included in the report.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Processing location statements.
- India vs overseas processing split.
- Purging policy/process.
- Batch/process validation screenshots or evidence.
- 24-hour / close-of-business-day wording.
- Auditor conclusion.

## Clause 7: Activities Related to Payment Processing

### Requirement

The SAR must state whether any activities in end-to-end payment processing are done outside India. If yes, it must state time taken to get this data stored in India, purging policy for this data, and conclude that storage requirements and purging policy align with RBI Data Localization expectations.

### Checklist points

- Identifies post-payment activities such as settlements, customer support, dispute resolution, chargebacks, and data mining.
- Checks if these processes are carried out in India or outside India.
- Covers storage of data for all post-payment process activities.
- Performs conclusive checks of purging policy per RBI expectations.
- Includes relevant evidence.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Settlement/reconciliation, support, dispute, chargeback, analytics, or data mining sections.
- Location/jurisdiction for each activity.
- Storage and purging timelines.
- Evidence of purging/storage controls.
- Auditor conclusion.

## Clause 8: Cross Border Transactions

### Requirement

The SAR must clearly state whether cross-border transactions are supported by the application. If yes, it must bring out transaction flow and payment data elements stored outside India. If no, it must mention whether the current application version has capability to support such transactions even if not currently used.

### Checklist points

- Checks whether application conducts or supports cross-border transactions.
- If yes, storage of payment data elements for domestic and foreign components is covered.
- Evidence of payment data elements stored for domestic and foreign components is included.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Cross-border support yes/no statement.
- Application capability statement.
- Cross-border transaction flow.
- Domestic/foreign leg storage details.
- Data elements stored outside India, if any.
- Auditor conclusion.

## Clause 9: Database Storage and Maintenance

### Requirement

The SAR must bring out payments database storage in various jurisdictions and related maintenance activities, and state whether they were found satisfactory.

### Checklist points

- Verifies payments database storage in all applicable jurisdictions.
- Verifies database maintenance activities, frequency, and periodicity.
- Confirms database maintenance activities are satisfactory.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Database/storage inventory.
- Jurisdiction or data center/region for each payment database.
- Maintenance frequency/periodicity.
- Evidence of maintenance controls.
- Auditor conclusion.

## Final output schema

Return JSON-friendly state update:

```json
{
  "clause_results": [
    {"clause_id": "6", "clause_name": "Transaction Processing", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "7", "clause_name": "Activities Related to Payment Processing", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "8", "clause_name": "Cross Border Transactions", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "9", "clause_name": "Database Storage and Maintenance", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []}
  ],
  "points_not_concluded": []
}
```
