# SPDX-License-Identifier: Apache-2.0
# Benchmark fixtures for document generation.
# Each case exercises one of the day-to-day non-engineering scenarios and the
# intent it MUST resolve to. `expect_title_not` guards the title-leak regression.

CASES = [
    {
        "id": "generate_report",
        "prompt": "Generate a report on UPI adoption in rural India in 2025",
        "expect_intent": "generate",
        "expect_format": None,          # unspecified → author decides / pdf default
        "expect_title_not": ["Generate", "generate a report"],
    },
    {
        "id": "summarize_typo",   # the reported bug: "Summarizr this doic"
        "prompt": "Summarizr this doic",
        "expect_intent": "summarize",
        "expect_title_not": ["Summarizr This Doic", "Summarizr", "Summarize This Doc"],
    },
    {
        "id": "summarize_pdf",
        "prompt": "Summarize the attached PDF and give me the key points",
        "expect_intent": "summarize",
        "has_attachments": True,
    },
    {
        "id": "convert_format",
        "prompt": "Convert that document to PDF",
        "expect_intent": "convert",
        "expect_format": "pdf",
        "has_prior_doc": True,
    },
    {
        "id": "extract_merge",
        "prompt": "Combine these three uploaded reports into one consolidated summary",
        "expect_intent": "extract",
        "has_attachments": True,
    },
    {
        "id": "revise_intro",
        "prompt": "Make the introduction shorter and add a section on risks",
        "expect_intent": "revise",
        "has_prior_doc": True,
    },
    {
        "id": "not_a_doc",
        "prompt": "What's the weather like today?",
        "expect_intent": "none",
    },
]
