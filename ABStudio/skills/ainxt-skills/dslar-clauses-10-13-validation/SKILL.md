---
name: dslar-clauses-10-13-validation
description: Validate AiNxt DL-SAR Clauses 10 through 13 against extracted audit report content and previous workflow state.
---

# DSLAR Clauses 10-13 Validation

Use this skill for the `clauses-10-13-validator` agent in the DSLAR AiNxt Audit Validation workflow.

## Mandatory validation rule

Validate only these clauses:

- Clause 10: Data Backup & Restoration
- Clause 11: Data Security
- Clause 12: Access Management
- Clause 13: Data Sharing

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
  "clause_id": "10",
  "clause_name": "Data Backup & Restoration",
  "present": true,
  "inconclusive": false,
  "evidence_refs": ["Section: Backup Policy", "Table 8 page 15"],
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

## Clause 10: Data Backup & Restoration

### Requirement

The SAR must cover how payments data backups and restoration align with data localization expectations. Details of payment element data stored in the database must be explicitly called out as conclusive evidence, including retention period at data element level.

### Checklist points

- Verifies backup and restoration of defined payment data is compliant with guidelines.
- Verifies backup and restoration as per defined policy.
- Checks backup frequency and compliance.
- Checks restoration frequency and completion.
- Includes conclusive evidence.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Backup policy and schedule.
- Restoration policy and test/completion evidence.
- Payment data retention period.
- Data element-level backup/storage details.
- Jurisdiction of backup storage.
- Auditor conclusion.

## Clause 11: Data Security

### Requirement

The SAR must define security controls for safeguarding transaction data and state whether those controls are satisfactory under standard data security controls, applicable regulatory guidelines, and AiNxt guidelines. It must explicitly state whether any payment data is stored as an alias, such as a one-way hash, on systems outside India. If payment data is stored or accessed outside India for customer support, analytics, data mining, or other activities, all relevant information and controls must be explicitly brought out.

### Checklist points

- Verifies security controls such as masking, encryption, DLP, and database access monitoring.
- Covers applicable regulatory guidelines including RBI, UIDAI, UPI, and AiNxt.
- Verifies whether any payment data is stored as alias/hash outside India.
- Verifies whether payment data is stored/accessed outside India for analytics/mining/support activities.
- Covers data sharing with parent organization, sister organization, third party, or vendor if applicable.
- Includes findings confirming compliance with the data localization circular.
- Includes conclusive evidence.
- Auditor clearly concludes whether controls are satisfactory.

### Evidence to look for

- Security control descriptions and evidence.
- Encryption/masking/DLP/access monitoring references.
- Alias/hash/tokenization statements.
- Overseas support/analytics/mining access statements.
- Regulatory compliance statements.
- Auditor conclusion.

## Clause 12: Access Management

### Requirement

The SAR must define who has access to payments data and what kind of access has been granted to individuals/teams. It must define controls for access management and whether they are satisfactory. It must also provide locations where customer support, data analytics, data mining, dispute resolution, or other related activities operate and what payments data they access. If data is accessed from outside India, the SAR must detail access granted and controls.

### Checklist points

- Shows data access management controls implemented.
- Checks jurisdiction/location of customer support, analytics, dispute resolution, and other related activities.
- Checks what payments data those functions access.
- If accessed from outside India, details access granted to individuals/teams.
- Verifies access management controls are satisfactory.

### Evidence to look for

- Access matrix or role/privilege table.
- Team/function location and jurisdiction.
- Payments data access details.
- Overseas access details, if any.
- Access control evidence and auditor conclusion.

## Clause 13: Data Sharing

### Requirement

The SAR must explicitly mention data sharing with parties including parent, subsidiaries, vendors, or third parties. Agreements, evidence, and procedures performed to ascertain compliance must be part of the report. The SAR should adequately address RBI Data Localization FAQs dated June 26, 2019 and other applicable compliance requirements.

### Checklist points

- Explicitly brings out data sharing arrangements with parent, subsidiaries, vendors, or third parties.
- Includes agreements and evidence of procedures carried out to ascertain compliance.
- Addresses RBI Data Localization FAQs and other compliances.
- Includes evidence for controls basis which auditor concluded RBI data localization compliance.

### Evidence to look for

- Data sharing section/table.
- Parent/subsidiary/vendor/third-party arrangements.
- Agreement references.
- Compliance procedures and evidence.
- RBI Data Localization FAQ alignment.
- Auditor conclusion.

## Additional compliance notes

Also consider whether the SAR states that participants must inform AiNxt when application architecture changes and payment data jurisdiction changes, or when PR/DR site changes occur. These notes can support Clause 13 or broader compliance reasoning but should not replace clause-specific evidence.

## Final output schema

Return JSON-friendly state update:

```json
{
  "clause_results": [
    {"clause_id": "10", "clause_name": "Data Backup & Restoration", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "11", "clause_name": "Data Security", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "12", "clause_name": "Access Management", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []},
    {"clause_id": "13", "clause_name": "Data Sharing", "present": null, "inconclusive": true, "evidence_refs": [], "satisfactory": null, "raw_agent_output": "", "data_element_results": []}
  ],
  "points_not_concluded": []
}
```
