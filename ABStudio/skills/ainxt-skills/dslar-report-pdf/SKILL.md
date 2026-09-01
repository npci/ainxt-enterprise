---
name: dslar-report-pdf
description: Render the single DSLAR AiNxt validation-report PDF from enriched.json and return a downloadable link via the platform generated-files mechanism.
---

# DSLAR Report PDF

Use this skill for the `report-pdf-renderer` agent in the DSLAR AiNxt Audit
Validation workflow. It turns the final workflow state into ONE downloadable
PDF and cleans up chunk scratch files.

## What it does

`scripts/render_dslar_report.py` is a deterministic renderer (no LLM). It reads
the final `enriched.json` written by the decision-maker nodes and produces a
single PDF reproducing the canonical "Validation Report" layout:

- Header: Verdict, Validation Type, Report Detail, Job ID, Created At
- Metadata Checks (dlsar) / Report Metadata Checks (report)
- Clause Validation (13 clauses): each `#N Name -- present/not concluded`,
  Satisfactory, Evidence; Clause 1 expands into the 68-row AiNxt data-element
  checklist (Sr. | scope | category | label | present | Rest/Proc |
  Jurisdiction | Brought back + Evidence)
- Executive Summary (if present)
- Points Not Concluded

The PDF is written into **`OUTPUT_DIR`**. The platform's code_executor
auto-collects everything in `OUTPUT_DIR` into `GENERATED_FILES_DIR` and returns
it in `generated_files[]` with a ready `download_url` of the form
`/generated-files/<name>`. The agent must surface that `download_url`
**verbatim** as the markdown link — never construct a URL by hand.

After a successful render it deletes `chunk_*.json` from the artifact dir but
keeps `enriched.json` (audit trail / re-render).

## Mandatory pattern (single code_executor call)

```python
import os, json, runpy, sys

work_dir = os.environ["WORKFLOW_ARTIFACT_DIR"]
out_dir = os.environ["OUTPUT_DIR"]
script = "<ABSOLUTE path to render_dslar_report.py from the skill manifest>"

sys.argv = [
    script,
    "--enriched-json", os.path.join(work_dir, "enriched.json"),
    "--output-dir", out_dir,
    "--artifact-dir", work_dir,
]
runpy.run_path(script, run_name="__main__")
```

The script prints a compact JSON summary
(`{pdf_filename, pdf_path, chunks_deleted, verdict, validation_type}`). After it
runs, read the `code_executor` tool result's `generated_files[]`, take the
`download_url` of the produced PDF, and return it verbatim to the user as
`[<pdf_filename>](<download_url>)`.

## Notes

- Works for both `validation_type=="dlsar"` (13-clause complete format) and
  `validation_type=="report"` (lighter report-mode; renders whichever of
  `metadata_checks` / `report_metadata_checks` / `clause_results` are present).
- Missing clauses still render a labelled row using canonical clause names.
- Requires `reportlab` (already available in the environment).
