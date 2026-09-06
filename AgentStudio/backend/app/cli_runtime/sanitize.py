# SPDX-License-Identifier: MIT
"""cli_runtime.sanitize — keep internal filesystem details out of user-facing text.

A spawned CLI runs in a private per-run workspace and uses ``code_executor`` to
write files into an artifact directory. Left to itself, a chatty model narrates
those absolute paths ("The file exists at D:\\...\\ABStudio\\tmp\\...", "copied to
runtime_artifacts\\workflows\\...") straight into its response. In a hosted
deployment the end user must never see server paths, temp directories, or the
internal layout.

Two complementary defences, both CLI-mode only (native output is untouched):

1. ``download_guidance`` — a short directive appended to the prompt telling the
   model to reference generated files by name / download link, never by absolute
   or internal path.
2. ``scrub_paths`` — a redactor applied to the final response text as a
   backstop, because prompt instructions alone are not reliable: the model
   discovers real paths through tool results and may echo them regardless.

The download link the UI needs (``download_url`` / ``/generated-files/...``) is
produced by the tool plane and carried in structured file metadata, so scrubbing
narrative paths never removes the user's ability to download the file.
"""

from __future__ import annotations

import re
from typing import List

# A neutral label shown in place of a redacted path.
_REDACTION = "[file]"

# ── Patterns, most specific first ────────────────────────────────────────────
# Order matters: the internal-tree patterns run before the generic absolute-path
# patterns so a match like ``runtime_artifacts\workflows\x\a.docx`` is replaced
# as a unit rather than leaving a dangling ``runtime_artifacts\`` fragment.
_PATTERNS: List[re.Pattern] = [
    # Windows absolute path ending in (optionally) a filename:
    #   C:\Users\...\file.docx   D:\Java\...\ABStudio\tmp\x_MR.docx
    re.compile(r"[A-Za-z]:\\[^\s'\"()<>|]+"),
    # UNC path: \\host\share\...
    re.compile(r"\\\\[^\s'\"()<>|]+"),
    # Our internal relative trees, either slash style:
    #   runtime_artifacts/workflows/...  runtime_artifacts\cli_runs\...
    #   ABStudio/tmp/...  ABStudio\tmp\...
    re.compile(r"(?:runtime_artifacts|ABStudio[\\/]tmp)[\\/][^\s'\"()<>|]+"),
    # POSIX absolute paths that point into known internal roots. Deliberately
    # NOT a blanket ``/.../`` match — that would clobber URLs and prose.
    re.compile(r"/(?:var|opt|home|tmp|Users)/[^\s'\"()<>|]*"),
]

# Never scrub a genuine download link the UI relies on. These are matched and
# restored so a broad path rule cannot eat them.
_DOWNLOAD_URL = re.compile(r"/generated-files/[^\s'\"()<>|]+")


def scrub_paths(text: str) -> str:
    """Redact internal filesystem paths from user-facing text.

    Download URLs (``/generated-files/...``) are preserved. Everything that looks
    like a server/local path is replaced with ``[file]`` so the sentence still
    reads naturally ("saved the document" rather than a raw path). Idempotent and
    never raises — a redaction bug must not take down a response.
    """
    if not text:
        return text or ""
    try:
        # Stash download URLs behind placeholders so path rules cannot touch them.
        saved: List[str] = []

        def _stash(m: "re.Match") -> str:
            saved.append(m.group(0))
            return f"\x00DL{len(saved) - 1}\x00"

        work = _DOWNLOAD_URL.sub(_stash, text)

        for pat in _PATTERNS:
            work = pat.sub(_REDACTION, work)

        # Restore the download URLs.
        for i, url in enumerate(saved):
            work = work.replace(f"\x00DL{i}\x00", url)

        # Collapse an artefact like "saved to [file]." staying readable.
        work = re.sub(r"\[file\](?:[\\/][^\s'\"()<>|]*)?", _REDACTION, work)
        return work
    except Exception:
        # Backstop must be fail-safe: if anything goes wrong, return the original
        # rather than dropping the whole response.
        return text


def download_guidance() -> str:
    """Prompt directive: reference files by name/link, never by internal path.

    Appended to the CLI prompt (CLI mode only). Kept short and imperative — long
    directives get ignored by smaller local models.
    """
    return (
        "\n\nIMPORTANT — file output & user-facing rules:\n"
        "- To produce ANY downloadable file (md, txt, docx, pdf, pptx, xlsx, csv, "
        "images, etc.), you MUST create it with the `code_executor` tool. Do NOT "
        "use your own built-in file-writing tools — files written that way are "
        "NOT downloadable by the user.\n"
        "- After `code_executor` returns, share the `download_url` from its "
        "`generated_files` result as the link. NEVER present a bare filename as a "
        "link (e.g. `rose_description.md`) — a bare name is not a valid URL and "
        "breaks the download.\n"
        "- Never reveal or print absolute filesystem paths, temp directories, or "
        "internal folder names (a drive letter, 'runtime_artifacts', "
        "'ABStudio/tmp', or a home/opt/var path).\n"
        "- Refer to a produced file only by its filename and its download link; "
        "do not describe where it was saved on disk, and do not narrate internal "
        "steps like copying files between directories."
    )


def neutralize_artifact_path(instructions: str) -> str:
    """Rewrite the engine's injected raw artifact path into a neutral form.

    The workflow engine appends 'Runtime artifact directory for this workflow
    run: <absolute path>. Use WORKFLOW_ARTIFACT_DIR ...' to the instructions.
    That absolute path is what the model then echoes. In CLI mode we replace the
    concrete path with the symbolic ``WORKFLOW_ARTIFACT_DIR`` the same sentence
    already refers to, so the model still knows where to write (the CLI runs in
    that directory) without ever being handed a literal path to repeat.
    """
    if not instructions:
        return instructions or ""
    return re.sub(
        r"(Runtime artifact directory for this workflow run:\s*)([^\n.]+)(\.)",
        r"\1the current working directory (WORKFLOW_ARTIFACT_DIR)\3",
        instructions,
    )


# ── Generated-file filtering ─────────────────────────────────────────────────
# An agent that uses ``code_executor`` iteratively writes scratch files (a diff
# it dumps to read around output truncation, a per-file split, a temp JSON) into
# the same artifact directory as the real deliverable. Every one of those is
# reported as a "generated file" and shown as downloadable, so a single
# "make me a DOCX" turn can surface diffs.txt + four *.diff files next to the
# one MR_698_Overview.docx the user actually wanted.
#
# We keep only files whose type is a plausible *deliverable* and drop obvious
# intermediates — UNLESS the user's prompt explicitly asked for that type (so a
# genuine "give me the raw diff as a .txt" still comes through).

# Extensions that are almost always the actual deliverable.
_DELIVERABLE_EXTS = {
    ".docx", ".pdf", ".pptx", ".xlsx", ".xls", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".md", ".zip",
}
# Extensions that are almost always scratch/intermediate work.
_INTERMEDIATE_EXTS = {
    ".diff", ".patch", ".txt", ".json", ".log", ".tmp", ".temp",
    ".bak", ".py", ".pyc", ".ndjson", ".jsonl", ".yaml", ".yml",
}


def _ext_of(entry: dict) -> str:
    """Lowercase extension for a generated-file entry (``.docx`` etc.)."""
    fmt = str(entry.get("format") or "").strip().lower()
    if fmt:
        return fmt if fmt.startswith(".") else "." + fmt
    name = str(entry.get("filename") or entry.get("disk_name") or "")
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


# Boilerplate the workflow engine injects that mentions a format WITHOUT the
# user asking for that file type. Stripped before scanning so, e.g., "Node
# outputs must remain strict JSON." never makes a scratch ``.json`` look wanted.
_PROMPT_BOILERPLATE = re.compile(
    r"node outputs? must remain strict json\.?"
    r"|outputs? must be (?:strict|valid) json\.?"
    r"|respond(?:ing)? (?:in|with) (?:strict|valid)? ?json\.?"
    r"|return(?:ing)? (?:strict|valid)? ?json\.?",
    re.IGNORECASE,
)


def _prompt_requested_exts(prompt: str) -> set:
    """Extensions the user explicitly named as a DESIRED FILE (so we never hide a
    type they actually asked for).

    Deliberately strict to avoid false positives. The bare word alone does NOT
    count — workflow prompts routinely say things like "Node outputs must remain
    strict JSON" (a return-format instruction, not a request for a ``.json``
    download). An extension counts only when it appears in a genuine file/output
    request context:

      * dotted form: ``.json``, ``.txt``
      * ``<ext> file`` / ``<ext> document`` / ``<ext> report`` / ``<ext> output``
      * ``as (a/an) <ext>`` / ``in <ext> format`` / ``generate|create|export ... <ext>``
      * ``download ... <ext>``
    """
    if not prompt:
        return set()
    low = _PROMPT_BOILERPLATE.sub(" ", prompt.lower())
    found = set()
    for ext in _DELIVERABLE_EXTS | _INTERMEDIATE_EXTS:
        word = re.escape(ext.lstrip("."))
        patterns = [
            rf"\.{word}(?![A-Za-z0-9])",                              # ".json"
            rf"(?<![A-Za-z0-9]){word}\s+(?:file|document|doc|report|output|format|attachment)",
            rf"\bas\s+(?:an?\s+)?{word}\b",
            rf"\bin\s+{word}\s+format\b",
            rf"\b(?:generate|create|produce|export|make|build|save|write|download|output)\b[^.]*?(?<![A-Za-z0-9]){word}(?![A-Za-z0-9])",
        ]
        if any(re.search(p, low) for p in patterns):
            found.add(ext)
    return found


def filter_deliverables(files: List[dict], prompt: str = "") -> List[dict]:
    """Return only the files a user should see as downloads.

    Rules, per file:
      * keep if the prompt explicitly requested that type (a real file request,
        not boilerplate — see ``_prompt_requested_exts``);
      * keep if the extension is a known deliverable type;
      * drop if the extension is a known intermediate type (scratch);
      * keep anything with an unknown extension — better to over-show a real
        output than to silently hide it.

    If EVERY file is dropped, that means every file was a KNOWN intermediate
    (only those are ever dropped). In a multi-node workflow the real deliverable
    comes from another node, so returning an empty list here is correct — that is
    exactly the "my GitLab node dumped a scratch .json/.txt" case. An unknown or
    deliverable type would have been kept, so we never silently lose a genuine or
    unrecognised output.
    """
    if not files:
        return files or []
    try:
        requested = _prompt_requested_exts(prompt)
        kept: List[dict] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            ext = _ext_of(f)
            if ext in requested:
                kept.append(f)
            elif ext in _DELIVERABLE_EXTS:
                kept.append(f)
            elif ext in _INTERMEDIATE_EXTS:
                continue  # scratch — hide
            else:
                kept.append(f)  # unknown → keep, don't hide a real output
        return kept
    except Exception:
        return list(files)


__all__ = [
    "scrub_paths",
    "download_guidance",
    "neutralize_artifact_path",
    "filter_deliverables",
]
