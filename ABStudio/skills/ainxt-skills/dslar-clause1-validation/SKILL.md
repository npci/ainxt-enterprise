---
name: dslar-clause1-validation
description: Validate AiNxt DL-SAR Clause 1 Payments Data Elements using extracted audit report content, metadata state, and all 68 configured data element rows.
---

# DSLAR Clause 1 Validation

Use this skill for the `clause1-data-elements-validator` agent in the DSLAR AiNxt Audit Validation workflow.

## Mandatory validation rule

Validate only **DL-SAR Clause 1: Payments Data Elements**. Read `WORKFLOW_ARTIFACT_DIR/enriched.json` as the source of truth using `code_executor`, especially:

- `extracted.full_text`
- `extracted.sections`
- `extracted.tables`
- `extracted.images`
- `metadata_checks`
- existing `points_not_concluded`

Do not invent evidence. If a data element cannot be validated from text, tables, or image descriptions/refs, mark that row inconclusive.

## Clause 1 requirement

A complete list of data elements must be included in the SAR and classified as **Payments Data** and **Non-Payments Data**. Payments data elements must cover Customer Data, Payment Sensitive Data, Payment Credentials, and Transaction Data as detailed in RBI Data Localization FAQs issued in June 2019.

The report should bring out storage details for every data element, including:

- jurisdiction of storage
- duration of storage, including less than 24 hours if processing is overseas
- application module name
- whether the prescribed payment data at rest is only in India
- whether any overseas transaction-leg data is purged within 24 hours or one business day
- evidence relied on by the CERT-In empaneled third-party auditor

## Checklist points

- Check all data elements and their classification as payments or non-payments data.
- Categorize each element into jurisdictions and whether the data has been brought back to India.
- Verify jurisdiction of all data elements, with conclusive evidence that payment data is not stored outside India.
- Verify non-payment data elements and look for a conclusive statement that no payment data is part of non-payments data.
- Verify where the data is processed during its lifecycle, including settlement and reconciliation.
- If data is processed outside India, verify evidence of purging within 24 hours or one business day, including process and batch validation screenshots where available.

## Evidence excerpt construction (page-chunked map-reduce)

Large audit reports overflow a single 50,000-character read, which silently drops
evidence on later pages. Use the **dslar-clause-chunking** skill to validate the
entire document chunk by chunk, then reduce per data element with present-if-any.

1. **Split** the document into page-chunks with `chunk_dslar_pages.py --mode
   split --chunk-pages 30`. Clause 1 validates all 68 data elements against every
   chunk, so it uses a larger 30-page window than the other clause branches to
   keep the total per-chunk judgment count (and thus the node's iteration budget)
   bounded on large reports. Read `metadata_checks`, `validation_type`, and
   `points_not_concluded` from `enriched.json` once.
2. **Evaluate chunks in batches.** Do NOT read one chunk per `code_executor`
   call — one tool call per chunk exhausts the node's iteration budget on large
   reports and the branch is truncated before it emits any `clause_results`.
   Instead loop `--mode read-batch --batch-start <n> --batch-size 4`, which
   returns several chunks' capped evidence at once (per-chunk caps unchanged:
   `full_text[:50000]`, `sections[:50]`, `tables[:20]`, `rows[:50]`,
   `images[:100]`) plus a `next_batch_start` cursor; repeat with that cursor
   until it is null. Validate all 68 data elements against EVERY chunk in each
   batch and record one compact partial per element per chunk (`serial`,
   `chunk_index`, `present`, `inconclusive`, `satisfactory`,
   `rest_or_processing`, `jurisdiction`, `brought_back_status`, `evidence_refs`
   citing the page). Many Clause 1 facts live in tables.
3. **Reduce** with `--mode reduce --reduce-kind data_element` (present-if-any):
   an element is present if **any** chunk found it; not-present only if **every**
   chunk clearly marked it absent; otherwise inconclusive. See the
   dslar-clause-chunking skill for the exact split/read/reduce commands.

A report with `total_pages <= chunk_pages` produces a single chunk, so small
reports behave exactly as before.

## Data element rows to validate

Validate all 68 rows against **each chunk**, then reduce per serial (above).

| Serial | Scope | Category | Label |
|---:|---|---|---|
| 1 | payments | Customer Data | Customer Name |
| 2 | payments | Customer Data | Mobile Number |
| 3 | payments | Customer Data | VPA |
| 4 | payments | Customer Data | Aadhar Number |
| 5 | payments | Customer Data | Email |
| 6 | payments | Customer Data | participant-defined / blank in template |
| 7 | payments | Customer Data | participant-defined / blank in template |
| 8 | payments | Customer Data | participant-defined / blank in template |
| 9 | payments | Transaction Data | Transaction Reference |
| 10 | payments | Transaction Data | Transaction Type |
| 11 | payments | Transaction Data | Amount |
| 12 | payments | Transaction Data | participant-defined / blank in template |
| 13 | payments | Transaction Data | participant-defined / blank in template |
| 14 | payments | Transaction Data | participant-defined / blank in template |
| 15 | payments | Transaction Data | participant-defined / blank in template |
| 16 | payments | Transaction Data | participant-defined / blank in template |
| 17 | payments | Transaction Data | participant-defined / blank in template |
| 18 | payments | Transaction Data | participant-defined / blank in template |
| 19 | payments | Transaction Data | participant-defined / blank in template |
| 20 | payments | Payment Sensitive Data | Payer VPA |
| 21 | payments | Payment Sensitive Data | Payee VPA |
| 22 | payments | Payment Sensitive Data | Account Number |
| 23 | payments | Payment Sensitive Data | OTP |
| 24 | payments | Payment Sensitive Data | participant-defined / blank in template |
| 25 | payments | Payment Sensitive Data | participant-defined / blank in template |
| 26 | payments | Payment Sensitive Data | participant-defined / blank in template |
| 27 | payments | Payment Sensitive Data | participant-defined / blank in template |
| 28 | payments | Payment Sensitive Data | participant-defined / blank in template |
| 29 | payments | Payment Sensitive Data | participant-defined / blank in template |
| 30 | payments | Payment Sensitive Data | participant-defined / blank in template |
| 31 | payments | Payment Credentials Data | UPI PIN |
| 32 | payments | Payment Credentials Data | Passwords |
| 33 | payments | Payment Credentials Data | participant-defined / blank in template |
| 34 | payments | Payment Credentials Data | participant-defined / blank in template |
| 35 | non_payments | Non-Payments Data | Data Element 35 |
| 36 | non_payments | Non-Payments Data | Data Element 36 |
| 37 | non_payments | Non-Payments Data | Data Element 37 |
| 38 | non_payments | Non-Payments Data | Data Element 38 |
| 39 | non_payments | Non-Payments Data | Data Element 39 |
| 40 | non_payments | Non-Payments Data | Data Element 40 |
| 41 | non_payments | Non-Payments Data | Data Element 41 |
| 42 | non_payments | Non-Payments Data | Data Element 42 |
| 43 | non_payments | Non-Payments Data | Data Element 43 |
| 44 | non_payments | Non-Payments Data | Data Element 44 |
| 45 | non_payments | Non-Payments Data | Data Element 45 |
| 46 | non_payments | Non-Payments Data | Data Element 46 |
| 47 | non_payments | Non-Payments Data | Data Element 47 |
| 48 | non_payments | Non-Payments Data | Data Element 48 |
| 49 | non_payments | Non-Payments Data | Data Element 49 |
| 50 | non_payments | Non-Payments Data | Data Element 50 |
| 51 | non_payments | Non-Payments Data | Data Element 51 |
| 52 | non_payments | Non-Payments Data | Data Element 52 |
| 53 | non_payments | Non-Payments Data | Data Element 53 |
| 54 | non_payments | Non-Payments Data | Data Element 54 |
| 55 | non_payments | Non-Payments Data | Data Element 55 |
| 56 | non_payments | Non-Payments Data | Data Element 56 |
| 57 | non_payments | Non-Payments Data | Data Element 57 |
| 58 | non_payments | Non-Payments Data | Data Element 58 |
| 59 | non_payments | Non-Payments Data | Data Element 59 |
| 60 | non_payments | Non-Payments Data | Data Element 60 |
| 61 | non_payments | Non-Payments Data | Data Element 61 |
| 62 | non_payments | Non-Payments Data | Data Element 62 |
| 63 | non_payments | Non-Payments Data | Data Element 63 |
| 64 | non_payments | Non-Payments Data | Data Element 64 |
| 65 | non_payments | Non-Payments Data | Data Element 65 |
| 66 | non_payments | Non-Payments Data | Data Element 66 |
| 67 | non_payments | Non-Payments Data | Data Element 67 |
| 68 | non_payments | Non-Payments Data | Data Element 68 |

Rows with participant-defined / blank labels are still expected rows in the AiNxt checklist. Validate whether the SAR provides an equivalent participant-defined data element in that serial slot/category.

## Row-level output schema

Return each data element with this exact shape:

```json
{
  "serial": 1,
  "scope": "payments",
  "category": "Customer Data",
  "label": "Customer Name",
  "present": true,
  "inconclusive": false,
  "satisfactory": true,
  "rest_or_processing": "data at rest in India / processed outside India / not stated / null",
  "jurisdiction": "India / overseas jurisdiction / not stated / null",
  "brought_back_status": "within 24 hours / close of business day / not applicable / not stated / null",
  "evidence_refs": ["Table 2 page 5", "Section: Data Elements"],
  "raw_agent_output": "short row reasoning or raw evidence summary"
}
```

## Confidence and inconclusive rule

Use threshold `0.7`.

Confidence and present/inconclusive are decided **per chunk** when building each
partial. The final per-element verdict is the present-if-any reduction across all
chunks (handled by the dslar-clause-chunking reduce step):

- If confidence is below `0.7` for a chunk, set that chunk's partial `inconclusive=true`.
- A chunk where the row is clearly absent sets that chunk's `present=false`.
- After reduction, a row is `present=false` only if **every** chunk marked it clearly absent; `present=null`/`inconclusive=true` if no chunk found grounded evidence and at least one was ambiguous.

## Parent Clause 1 rollup

Return one parent `ClauseResult` for `clause_id="1"`. Roll up over the **reduced**
68 data elements (after present-if-any across chunks):

- If any reduced data element is inconclusive:
  - `present=null`
  - `inconclusive=true`
  - `satisfactory=null`
- If no row is inconclusive:
  - `present=true` only if every row has `present=true`; otherwise `present=false`.
- Parent `satisfactory`:
  - `false` if any row has `satisfactory=false`
  - `true` if all rows have `satisfactory=true`
  - `null` otherwise
- Parent evidence refs should be a short de-duplicated rollup from row-level evidence refs.
- Append `Clause 1 (Payments Data Elements): could not be concluded` to `points_not_concluded` only when parent Clause 1 is inconclusive.

## Final output schema

Return JSON-friendly state update:

```json
{
  "clause_results": [
    {
      "clause_id": "1",
      "clause_name": "Payments Data Elements",
      "present": null,
      "inconclusive": true,
      "evidence_refs": [],
      "satisfactory": null,
      "raw_agent_output": "short parent reasoning",
      "data_element_results": []
    }
  ],
  "points_not_concluded": []
}
```
