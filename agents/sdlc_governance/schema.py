# SPDX-License-Identifier: MIT
"""
agents/sdlc_governance/schema.py — Step 2: the platform-owned governance output
contract + the security-sensitive finding fingerprint.

WHY THIS FILE IS CRITICAL
-------------------------
Two things live here and every downstream consumer (persist / report / UI /
suppression) depends on them staying stable:

1. `GOVERNANCE_SCHEMA` — the JSON Schema the governance CLI review session is
   forced to emit (`structured_output`). The PLATFORM owns this schema, not the
   skill teams: skills (EA/IS/DPDP/…) come and go and change over time, but the
   shape of a finding stays constant, so persistence and reporting never break.
   Skill `SKILL.md`s (or the review prompt in engine.py) instruct the model to
   emit findings conforming to THIS schema.

2. `fingerprint()` — a stable, LINE-INDEPENDENT content fingerprint used to
   match a finding against per-(product, repo) false-positive suppressions. A
   wrong fingerprint is a compliance hazard in BOTH directions: too broad and a
   real NEW violation is silently hidden under an old suppression; too narrow
   (e.g. hashing line numbers) and a suppressed false-positive resurfaces the
   moment an unrelated edit shifts its line. We hash rule + normalized path +
   normalized snippet, and deliberately EXCLUDE the line number.

HARD CONSTRAINTS
----------------
- Ajv-safe schema: the `ainxt-v2` binary validates the output schema with Ajv in
  strict mode, which REJECTS union `type` arrays like `{"type": ["integer",
  "null"]}` (see memory `project_sdlc_plan_schema_union_stall` — that exact shape
  made StructuredOutput silently unusable and every PLAN run stalled to a 124
  idle-watchdog kill). Optional/nullable fields therefore use `anyOf`, never a
  list-valued `type`.
- Import side-effect-free: stdlib only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional


# ════════════════════════════════════════════════════════════════════════════
# The platform-owned output contract (Ajv strict-mode safe)
# ════════════════════════════════════════════════════════════════════════════
#
# NOTE on `line`: expressed as `{"anyOf": [{"type": "integer"}, {"type":
# "null"}]}` — NOT `{"type": ["integer", "null"]}`. The union-`type` array is the
# shape that broke the binary's Ajv strict mode. `anyOf` is the Ajv-safe way to
# say "an integer or null". Every property is listed in `required` (structured-
# output validators are happiest when the object is fully specified); text fields
# the model has nothing to say about are emitted as empty strings, and `line` as
# null.
_FINDING_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "file": {"type": "string"},
        "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "rule": {"type": "string"},
        "title": {"type": "string"},
        "detail": {"type": "string"},
        "fix_hint": {"type": "string"},
        "snippet": {"type": "string"},
    },
    "required": [
        "severity", "file", "line", "rule", "title", "detail", "fix_hint", "snippet",
    ],
}

_SKILL_RESULT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "skill": {"type": "string"},
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": _FINDING_SCHEMA},
    },
    "required": ["skill", "verdict", "summary", "findings"],
}

GOVERNANCE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overall_verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "skills": {"type": "array", "items": _SKILL_RESULT_SCHEMA},
    },
    "required": ["overall_verdict", "skills"],
}


# ════════════════════════════════════════════════════════════════════════════
# Severity ordering + blocking predicate
# ════════════════════════════════════════════════════════════════════════════

# Higher int == more severe. Used both to rank findings for the report and to
# decide whether a finding blocks the pipeline (>= the configured threshold).
SEVERITY_ORDER: dict = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Fallback rank for any unexpected/garbage severity string coming back from the
# model: treat it as the LOWEST so an unknown value never accidentally blocks a
# run on its own — but see is_blocking(), which fails toward the threshold only
# when the severity is recognised.
_DEFAULT_SEVERITY_RANK = 0


def severity_rank(sev: Optional[str]) -> int:
    """Rank a severity string (case-insensitive). Unknown → lowest."""
    if not isinstance(sev, str):
        return _DEFAULT_SEVERITY_RANK
    return SEVERITY_ORDER.get(sev.strip().lower(), _DEFAULT_SEVERITY_RANK)


# ════════════════════════════════════════════════════════════════════════════
# Finding dataclass — the parsed, in-platform representation of one finding
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Finding:
    """One governance finding, tagged with the owning skill. Mirrors a finding
    object from `GOVERNANCE_SCHEMA` plus `skill` and a mutable `status` the
    platform stamps as it processes the finding (open → fixed | suppressed)."""
    skill: str
    severity: str
    file: str
    rule: str
    title: str
    detail: str = ""
    fix_hint: str = ""
    snippet: str = ""
    line: Optional[int] = None
    # Platform-side lifecycle marker (never comes from the model):
    #   "open"       — a live, non-suppressed finding
    #   "suppressed" — matched an active per-(product,repo) suppression
    #   "fixed"      — resolved by the governance fixer loop
    status: str = "open"
    # Owning governance domain (EA / IS / DPDP / …). NOT emitted by the model —
    # the platform tags it from the skill definition in run_scan_session (which
    # writes it onto the raw finding dict) and parse_findings carries it here so
    # persist_findings / the approval gate can group findings by domain. Empty
    # string when the skill declares no domain.
    domain: str = ""

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "severity": self.severity,
            "file": self.file,
            "rule": self.rule,
            "title": self.title,
            "detail": self.detail,
            "fix_hint": self.fix_hint,
            "snippet": self.snippet,
            "line": self.line,
            "status": self.status,
            "domain": self.domain,
            "fingerprint": fingerprint(self),
            "content_key": content_fingerprint(self),
        }


# ════════════════════════════════════════════════════════════════════════════
# Normalization + fingerprint (line-independent, content-addressed)
# ════════════════════════════════════════════════════════════════════════════

_WS_RE = re.compile(r"\s+")


def normalize_path(path: Optional[str]) -> str:
    """Normalize a file path for fingerprinting: forward slashes, no leading
    './', trimmed. NOT lowercased — the runtime is Linux where paths are
    case-sensitive, so lowercasing would collide distinct files."""
    if not isinstance(path, str):
        return ""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def normalize_snippet(text: Optional[str]) -> str:
    """Collapse all runs of whitespace to a single space and strip. This makes
    the fingerprint robust to reformatting/reindentation of the offending code
    (which should NOT resurface a suppressed finding) while still keying on the
    actual content (which SHOULD, if it changes materially)."""
    if not isinstance(text, str):
        return ""
    return _WS_RE.sub(" ", text).strip()


def fingerprint(f: Finding) -> str:
    """Stable content fingerprint for suppression matching.

    `gv1:` + sha256(skill | normalized_path | rule | normalized_snippet). The
    `gv1:` prefix versions the scheme so it can evolve without silently
    mis-matching old suppression rows. **Line number is deliberately excluded**
    so a trivial edit that shifts lines does not resurface a suppressed finding.
    Snippet falls back to the title when the model emitted no snippet, so a
    finding without a code excerpt still fingerprints deterministically."""
    basis = "|".join([
        (f.skill or "").strip(),
        normalize_path(f.file),
        (f.rule or "").strip(),
        normalize_snippet(f.snippet or f.title),
    ])
    return "gv1:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def content_fingerprint(f: Finding) -> str:
    """Skill-INDEPENDENT content correlation key for cross-domain dedup.

    `gvc1:` + sha256(normalized_path | rule | normalized_snippet). Identical to
    the `fingerprint()` basis MINUS the `skill` component, so the same code issue
    flagged by two different skills (e.g. an EA and an IS rule hitting the same
    line) yields the SAME `content_key` while `fingerprint()` stays distinct per
    skill (suppression back-compat depends on that). Line-independent for the same
    reason as `fingerprint()`. The `gvc1:` prefix versions the scheme separately
    from `gv1:` so the two key spaces never collide."""
    basis = "|".join([
        normalize_path(f.file),
        (f.rule or "").strip(),
        normalize_snippet(f.snippet or f.title),
    ])
    return "gvc1:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def is_blocking(f: Finding, threshold: str) -> bool:
    """True if finding `f` is severe enough (>= `threshold`) to block the
    pipeline. Unknown severities rank lowest, so they only block if the
    threshold itself is the lowest tier."""
    return severity_rank(f.severity) >= severity_rank(threshold)


# ════════════════════════════════════════════════════════════════════════════
# Parsing — structured_output dict → list[Finding]
# ════════════════════════════════════════════════════════════════════════════

def parse_findings(structured: Any) -> List[Finding]:
    """Convert a `GOVERNANCE_SCHEMA`-shaped `structured_output` dict into a flat
    list of `Finding` (each tagged with its owning skill). Lenient: tolerates
    missing/oddly-typed fields so a slightly-off model emission still yields
    usable findings rather than raising. Returns [] on anything unparseable."""
    out: List[Finding] = []
    if not isinstance(structured, dict):
        return out
    skills = structured.get("skills")
    if not isinstance(skills, list):
        return out
    for sk in skills:
        if not isinstance(sk, dict):
            continue
        skill_name = str(sk.get("skill") or "").strip() or "unknown"
        findings = sk.get("findings")
        if not isinstance(findings, list):
            continue
        for raw in findings:
            if not isinstance(raw, dict):
                continue
            line = raw.get("line")
            if not isinstance(line, int):
                line = None
            out.append(Finding(
                skill=skill_name,
                severity=str(raw.get("severity") or "low").strip().lower(),
                file=str(raw.get("file") or "").strip(),
                rule=str(raw.get("rule") or "").strip(),
                title=str(raw.get("title") or "").strip(),
                detail=str(raw.get("detail") or ""),
                fix_hint=str(raw.get("fix_hint") or ""),
                snippet=str(raw.get("snippet") or ""),
                line=line,
                # Domain is a platform annotation run_scan_session writes onto the
                # raw finding dict (the model never emits it). Carry it through so
                # findings group under their owning domain (EA/IS/DPDP) instead of
                # falling into an unusable empty-domain bucket.
                domain=str(raw.get("domain") or "").strip(),
            ))
    return out


def overall_verdict_of(structured: Any) -> str:
    """Read the top-level verdict; anything not explicitly "PASS" is treated as
    "FAIL" (fail-closed)."""
    if isinstance(structured, dict):
        v = str(structured.get("overall_verdict") or "").strip().upper()
        if v == "PASS":
            return "PASS"
    return "FAIL"
