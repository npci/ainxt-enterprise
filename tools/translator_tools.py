# SPDX-License-Identifier: MIT
"""
AiNxt Agentic Platform — translator MCP tools.

Glossary-aware translation with a configurable MT backend. Used by UC-94
(document translation & localization).

  provider=glossary_demo  — annotates segments with glossary constraints
                             and returns them for the **agent** to translate
                             (the LLM does the actual translation).
  provider=mt_http        — POSTs to a configured internal MT endpoint and
                             returns its response verbatim.

Functions exposed:
  load_glossary        — load a CSV glossary (term + per-locale columns)
  translate_segments   — translate a list of segments to target_locale
  save_translation     — persist a translated document to the outbox

Companion server: mcp/servers/translator_server.py
Registered in:   mcp/registry.py:_register_tools()

Configuration (env vars):
  TRANSLATOR_PROVIDER       — "glossary_demo" or "mt_http"
                               (default glossary_demo)
  TRANSLATOR_MT_ENDPOINT    — URL of the internal MT engine (used when
                               provider=mt_http)
  TRANSLATOR_AUTH_TOKEN_ENV — name of the env var holding the bearer token
                               for the MT engine (default TRANSLATOR_MT_TOKEN)
  TRANSLATOR_OUTPUT_DIR     — where translated documents land
                               (default ./outbox/translations)
"""

import csv
import json
import os
import urllib.request
from typing import List


# ── Configuration ────────────────────────────────────────────────────────────

_PROVIDER       = os.getenv("TRANSLATOR_PROVIDER",       "glossary_demo")
_MT_ENDPOINT    = os.getenv("TRANSLATOR_MT_ENDPOINT",    "")
_AUTH_TOKEN_ENV = os.getenv("TRANSLATOR_AUTH_TOKEN_ENV", "TRANSLATOR_MT_TOKEN")
_OUTPUT_DIR     = os.getenv("TRANSLATOR_OUTPUT_DIR",     "./outbox/translations")


# ── Tool functions ───────────────────────────────────────────────────────────

def load_glossary(glossary_csv_path: str) -> List[dict]:
    """Load a glossary CSV (term, per-locale columns, instruction) to
    constrain translation."""
    with open(glossary_csv_path) as f:
        return list(csv.DictReader(f))


def translate_segments(segments: List[str],
                       target_locale: str,
                       glossary: List[dict] = None) -> dict:
    """Translate text segments to target_locale honouring glossary rules.

    glossary_demo  — returns segments annotated with glossary constraints
                      for the agent to translate.
    mt_http        — calls the configured MT engine and returns its response.
    """
    glossary = glossary or []
    keep = [
        g["term"] for g in glossary
        if "keep" in (g.get("instruction") or "").lower()
    ]
    if _PROVIDER == "mt_http" and _MT_ENDPOINT:
        token = os.environ.get(_AUTH_TOKEN_ENV, "") or ""
        req = urllib.request.Request(
            _MT_ENDPOINT,
            method="POST",
            data=json.dumps({
                "segments":         segments,
                "target":           target_locale,
                "do_not_translate": keep,
            }).encode(),
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    return {
        "mode":              "agent_translate",
        "target_locale":     target_locale,
        "do_not_translate":  keep,
        "glossary_mappings": [
            {g["term"]: g.get(target_locale)}
            for g in glossary if g.get(target_locale)
        ],
        "segments":          segments,
        "instruction":       (
            "Translate each segment to the target locale. Keep "
            "do_not_translate terms verbatim; use glossary_mappings "
            "where provided."
        ),
    }


def save_translation(filename: str, locale: str, content: str) -> dict:
    """Persist a translated document to the translations outbox as
    <filename>.<locale>.md."""
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    p = os.path.join(_OUTPUT_DIR, f"{filename}.{locale}.md")
    open(p, "w").write(content)
    return {"file": p}
