# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SECURE CODE GATE — generation-time SAST + LLM auto-fix
#
# Used by the CLI (full mode) and reusable by the SDLC pipeline. Scans
# generated code with the FAST tier (Semgrep / Bandit / secrets), and if
# HIGH/CRITICAL findings exist (CVSS >= threshold) runs an LLM fix loop:
#   scan → findings → model_router.generate(fix) → re-scan → repeat.
# Static analysis only — scanners never execute the code.
#
# Entry: run_secure_code_gate(payload) — a plain function (sync), callable
# directly by the router (fast tier) and enqueueable on Q_SECURITY.
#
# Env: SECURE_CODE_GATE_ENABLED (default true), SECURITY_CVSS_BLOCK_THRESHOLD
#      (default 7.0), MAX_GATE_ATTEMPTS (default 3).
# ============================================================

import os
import re
import shutil
import tempfile

from core.logger import logger
from tools.security_scan_tools import (
    CVSS_BLOCK_THRESHOLD,
    bandit_scan,
    compute_risk_gate,
    format_scan_report,
    secrets_scan,
    semgrep_scan,
)

GATE_ENABLED     = os.getenv("SECURE_CODE_GATE_ENABLED", "true").lower() == "true"
MAX_GATE_ATTEMPTS = int(os.getenv("MAX_GATE_ATTEMPTS", "3"))

# ext → language (semgrep auto-detects from extension; we only need it to gate
# bandit and to label the fix prompt).
_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".java": "java", ".go": "go", ".rb": "ruby", ".php": "php",
    ".cs": "csharp", ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".kt": "kotlin",
    ".scala": "scala", ".rs": "rust", ".swift": "swift", ".sh": "bash", ".sql": "sql",
}


def _lang_from_path(path: str) -> str:
    _, ext = os.path.splitext(path or "")
    return _LANG_BY_EXT.get(ext.lower(), "")


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    return (m.group(1) if m else (text or "")).strip()


def _scan_file(path: str, content: str, language: str) -> list[dict]:
    """Write content to a temp file (preserving extension) and run the fast tier.
    Findings' component is remapped back to the caller's logical path."""
    base = os.path.basename(path) or "snippet.txt"
    d = tempfile.mkdtemp(prefix="scgate_")
    fp = os.path.join(d, base)
    try:
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write(content)
        findings = semgrep_scan(fp, language)
        if language == "python" or base.endswith(".py"):
            findings += bandit_scan(fp)
        findings += secrets_scan(content, path)
        for x in findings:
            x["component"] = path
        return findings
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _load_security_skill_body(name: str) -> str | None:
    """
    Return an imported security skill's behavioral instructions (e.g. the platform harness
    'patch' skill) to enrich the fix prompt — ONLY if it has been PROMOTED to PRODUCTION via
    governance. Default-OFF: imports land as DRAFT, so this returns None and the gate prompt
    is byte-identical to before until an admin promotes the skill. Model-agnostic (plain text).
    """
    try:
        from db.database import SessionLocal
        from db.models import SkillRecord
        db = SessionLocal()
        try:
            rec = (
                db.query(SkillRecord)
                .filter(SkillRecord.name == name, SkillRecord.is_production.is_(True))
                .first()
            )
            return rec.code if rec and rec.code else None
        finally:
            db.close()
    except Exception:
        return None


def _llm_fix(path: str, content: str, findings: list[dict], language: str) -> str | None:
    """Ask the model to rewrite the whole file fixing every finding. Model-agnostic."""
    bullets = "\n".join(
        f"- [CVSS {x.get('cvss_score')}] {x.get('rule','')} "
        f"({x.get('owasp') or x.get('cwe') or x.get('tool','')}) "
        f"line {x.get('line')}: {x.get('message','')}"
        for x in findings[:20]
    )
    # Default-OFF enrichment: prepend the promoted 'patch' security-remediation playbook when
    # present; otherwise prefix is empty and the prompt is exactly as before.
    _playbook = _load_security_skill_body("patch")
    prefix = f"Security remediation playbook (follow it):\n{_playbook.strip()}\n\n" if _playbook else ""
    prompt = prefix + (
        f"You are a secure-coding assistant. Rewrite the ENTIRE {language or 'source'} file "
        f"below to FIX every static-analysis (SAST) finding while preserving ALL functionality "
        f"and the public API/signatures. Use safe patterns: parameterised queries (no string-built "
        f"SQL), validate/escape untrusted input, never use shell=True / os.system with user input "
        f"(use argument lists), no hardcoded secrets, strong crypto, no insecure deserialization, "
        f"no path traversal. Output ONLY the corrected file content — no prose, no markdown fences.\n\n"
        f"FINDINGS:\n{bullets}\n\nFILE ({path}):\n{content}"
    )
    try:
        from models.model_router import model_router
        out = model_router.generate(prompt, model_hint="complex") or ""
        fixed = _strip_fences(out)
        return fixed or None
    except Exception as e:
        logger.warning(f"secure_code_gate: LLM fix failed for {path}: {e}")
        return None


def run_secure_code_gate(payload: dict) -> dict:
    """
    payload = {
        files: [{path, content, language?}],
        threshold?: float,         # CVSS block threshold (default env 7.0)
        do_fix?: bool,             # run the LLM fix loop (default True)
        user_id?: str,
    }
    Returns {blocked, findings, files:[{path,findings,blocked,fixed_content,attempts}],
             fixed_files:[{path,content}], gate, report}
    """
    if not GATE_ENABLED:
        return {"blocked": False, "findings": [], "files": [], "fixed_files": [],
                "gate": compute_risk_gate([]), "report": "", "disabled": True}

    files = payload.get("files") or []
    do_fix = payload.get("do_fix", True)
    try:
        threshold = float(payload.get("threshold") or CVSS_BLOCK_THRESHOLD)
    except Exception:
        threshold = CVSS_BLOCK_THRESHOLD

    result_files: list[dict] = []
    all_findings: list[dict] = []

    for f in files:
        path = f.get("path") or "snippet"
        content = f.get("content") or ""
        language = f.get("language") or _lang_from_path(path)
        if not content.strip():
            continue

        findings = _scan_file(path, content, language)
        blocking = [x for x in findings if x.get("cvss_score", 0) >= threshold]
        attempts = 0
        fixed_content = None

        if do_fix and blocking:
            cur = content
            for attempt in range(1, MAX_GATE_ATTEMPTS + 1):
                attempts = attempt
                nxt = _llm_fix(path, cur, blocking, language)
                if not nxt or nxt == cur:
                    break
                cur = nxt
                findings = _scan_file(path, cur, language)
                blocking = [x for x in findings if x.get("cvss_score", 0) >= threshold]
                if not blocking:
                    break
            if cur != content and not blocking:
                # Don't hand back code that introduces NEW hardcoded secrets.
                if not secrets_scan(cur, path):
                    fixed_content = cur
                else:
                    logger.warning(f"secure_code_gate: fix for {path} introduced a secret — discarding")
                    blocking = blocking or [{"tool": "Secrets", "cvss_score": 9.0,
                                             "severity": "CRITICAL", "rule": "fix_introduced_secret",
                                             "message": "auto-fix introduced a hardcoded secret",
                                             "component": path, "line": None, "type": "VULNERABILITY"}]

        all_findings += findings
        result_files.append({
            "path": path,
            "findings": findings,
            "blocked": bool(blocking),
            "fixed_content": fixed_content,
            "attempts": attempts,
        })

    gate = compute_risk_gate(all_findings)
    blocked = any(rf["blocked"] for rf in result_files)
    fixed_files = [{"path": rf["path"], "content": rf["fixed_content"]}
                   for rf in result_files if rf["fixed_content"]]
    report = format_scan_report(gate, all_findings, "cli-generated", 0) if all_findings else ""

    logger.info(
        f"secure_code_gate: files={len(files)} findings={len(all_findings)} "
        f"blocked={blocked} fixed={len(fixed_files)} threshold={threshold}"
    )
    return {
        "blocked": blocked,
        "findings": all_findings,
        "files": result_files,
        "fixed_files": fixed_files,
        "gate": gate,
        "report": report,
    }
