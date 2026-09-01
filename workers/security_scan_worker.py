# SPDX-License-Identifier: Apache-2.0
# ============================================================
# SECURITY SCAN WORKER
# RQ job: clone PR branch → run SonarQube + Checkmarx + PMD/CPD
#         → store results → post PR comment → block if CVSS ≥ threshold
# ============================================================

import os
import shutil
import subprocess
import tempfile
import zipfile
from typing import Optional

from core.logger import logger


def run_security_scan_job(pr_dict: dict) -> dict:
    """
    RQ entry point.

    pr_dict keys:
      repo         str   owner/repo (GitLab namespace/project)
      branch       str   head branch of the PR
      number       int   PR number
      clone_url    str   HTTPS clone URL
      run_id       str   optional SDLC run_id to attach results to
    """
    repo      = pr_dict.get("repo", "")
    branch    = pr_dict.get("branch", "")
    pr_number = pr_dict.get("number")
    clone_url = pr_dict.get("clone_url", "")
    run_id    = pr_dict.get("run_id", "")

    logger.info(f"[SecScan] Starting: repo={repo} branch={branch} pr=#{pr_number}")

    from tools.security_scan_tools import (
        SCAN_ENABLED, sonar_ensure_project, sonar_trigger_scan,
        sonar_wait_for_scan, sonar_get_vulnerabilities,
        checkmarx_create_scan, checkmarx_poll_scan, checkmarx_get_findings,
        pmd_scan, cpd_scan, compute_risk_gate, format_scan_report,
        SONAR_HOST, CX_HOST, PMD_BIN,
    )

    if not SCAN_ENABLED:
        logger.info("[SecScan] SECURITY_SCAN_ENABLED=false — skipping")
        return {"skipped": True}

    work_dir = tempfile.mkdtemp(prefix="ainxt_sec_")
    all_findings: list[dict] = []

    try:
        # ── 1. Clone the PR branch ──────────────────────────────
        _gl_url = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
        _clone_url = _inject_token(clone_url or f"{_gl_url}/{repo}.git")
        logger.info(f"[SecScan] Cloning {repo}@{branch}")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, _clone_url, work_dir],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            logger.warning(f"[SecScan] git clone failed: {result.stderr[:300]}")
            _post_error(repo, pr_number, "Failed to clone repository for security scan.")
            return {"error": "clone_failed"}
        logger.info(f"[SecScan] Clone complete: {work_dir}")

        # ── 2. SonarQube ───────────────────────────────────────
        project_key = repo.replace("/", "_").replace("-", "_").lower()
        if SONAR_HOST:
            logger.info(f"[SecScan] Running SonarQube scan (project={project_key})")
            sonar_ensure_project(project_key, repo)
            ok = sonar_trigger_scan(work_dir, project_key, branch)
            if ok:
                sonar_wait_for_scan(project_key, max_wait=300)
                sonar_findings = sonar_get_vulnerabilities(project_key)
                logger.info(f"[SecScan] SonarQube: {len(sonar_findings)} findings")
                all_findings.extend(sonar_findings)
            else:
                logger.warning("[SecScan] SonarQube scan failed or not configured")
        else:
            logger.info("[SecScan] SonarQube not configured — skipping")

        # ── 3. Checkmarx ───────────────────────────────────────
        if CX_HOST:
            logger.info(f"[SecScan] Running Checkmarx scan")
            zip_path = _zip_source(work_dir)
            if zip_path:
                scan_id = checkmarx_create_scan(project_key, zip_path)
                if scan_id:
                    ok = checkmarx_poll_scan(scan_id, max_wait=600)
                    if ok:
                        cx_findings = checkmarx_get_findings(scan_id)
                        logger.info(f"[SecScan] Checkmarx: {len(cx_findings)} findings")
                        all_findings.extend(cx_findings)
                try:
                    os.unlink(zip_path)
                except Exception:
                    pass
        else:
            logger.info("[SecScan] Checkmarx not configured — skipping")

        # ── 4. PMD + CPD ───────────────────────────────────────
        lang = _detect_primary_language(work_dir)
        logger.info(f"[SecScan] Running PMD/CPD (language={lang})")

        try:
            pmd_findings = pmd_scan(work_dir, lang)
            logger.info(f"[SecScan] PMD: {len(pmd_findings)} findings")
            # Only include P1/P2 (CVSS >= 7.0) from PMD to reduce noise
            all_findings.extend(f for f in pmd_findings if f.get("cvss_score", 0) >= 7.0)
        except Exception as e:
            logger.warning(f"[SecScan] PMD skipped: {e}")

        try:
            cpd_findings = cpd_scan(work_dir, lang)
            logger.info(f"[SecScan] CPD: {len(cpd_findings)} code duplication blocks")
            all_findings.extend(cpd_findings)
        except Exception as e:
            logger.warning(f"[SecScan] CPD skipped: {e}")

        # ── 5. Aggregate + risk gate ────────────────────────────
        gate = compute_risk_gate(all_findings)
        logger.info(
            f"[SecScan] Gate: max_cvss={gate['max_cvss']} "
            f"blocked={gate['blocked']} total={gate['total']}"
        )

        # ── 6. Persist to DB ────────────────────────────────────
        scan_record_id = _persist_scan(repo, branch, pr_number, run_id, gate, all_findings)

        # ── 7. Post PR comment ──────────────────────────────────
        if pr_number:
            report_md = format_scan_report(gate, all_findings, repo, pr_number)
            _post_pr_comment(repo, pr_number, report_md)

        # ── 8. Block merge if CVSS threshold exceeded ───────────
        if gate["blocked"] and pr_number:
            _set_commit_status(
                repo=repo,
                branch=branch,
                work_dir=work_dir,
                state="failure",
                description=f"Security scan BLOCKED — Max CVSS {gate['max_cvss']} ≥ threshold",
                context="ainxt/security-scan",
            )
        else:
            _set_commit_status(
                repo=repo,
                branch=branch,
                work_dir=work_dir,
                state="success",
                description=f"Security scan passed — Max CVSS {gate['max_cvss']}",
                context="ainxt/security-scan",
            )

        return {
            "repo":         repo,
            "pr":           pr_number,
            "scan_id":      scan_record_id,
            "max_cvss":     gate["max_cvss"],
            "blocked":      gate["blocked"],
            "total":        gate["total"],
            "findings_by_tool": gate["summary_by_tool"],
        }

    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────

def _inject_token(url: str) -> str:
    """Inject GITLAB_TOKEN into clone URL for private repos."""
    token = os.getenv("GITLAB_TOKEN", "")
    gl_url = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    gl_host = gl_url.replace("https://", "").replace("http://", "")
    if token and gl_host in url:
        return url.replace("https://", f"https://oauth2:{token}@")
    return url


def _zip_source(source_dir: str) -> Optional[str]:
    """Create a zip of source_dir for Checkmarx upload. Returns zip path."""
    try:
        zip_fd, zip_path = tempfile.mkstemp(suffix=".zip")
        os.close(zip_fd)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(source_dir):
                # Skip .git, node_modules, venv, target
                dirs[:] = [d for d in dirs if d not in {
                    ".git", "node_modules", "venv", "target", "__pycache__", ".tox"
                }]
                for file in files:
                    full = os.path.join(root, file)
                    arc  = os.path.relpath(full, source_dir)
                    zf.write(full, arc)
        return zip_path
    except Exception as e:
        logger.warning(f"_zip_source failed: {e}")
        return None


def _detect_primary_language(source_dir: str) -> str:
    """Count file extensions to determine primary language for PMD."""
    counts: dict = {}
    ext_map = {
        ".java": "java", ".py": "python", ".js": "javascript",
        ".ts": "typescript", ".go": "go", ".kt": "kotlin",
        ".scala": "scala",
    }
    for root, _, files in os.walk(source_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in ext_map:
                lang = ext_map[ext]
                counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return "java"
    return max(counts, key=counts.get)


def _persist_scan(repo: str, branch: str, pr_number: Optional[int],
                  run_id: str, gate: dict, findings: list[dict]) -> Optional[str]:
    """Write scan results to security_scan_results table."""
    try:
        import uuid
        from db.database import SessionLocal
        from sqlalchemy import text as _text
        db = SessionLocal()
        scan_id = str(uuid.uuid4())
        db.execute(_text("""
            INSERT INTO security_scan_results
              (id, repo, branch, pr_number, run_id,
               max_cvss, critical_count, high_count, total_findings,
               blocked, findings_json, scanned_at)
            VALUES
              (:id, :repo, :branch, :pr_number, :run_id,
               :max_cvss, :critical_count, :high_count, :total_findings,
               :blocked, :findings_json::jsonb, NOW())
        """), {
            "id":             scan_id,
            "repo":           repo,
            "branch":         branch,
            "pr_number":      pr_number,
            "run_id":         run_id or None,
            "max_cvss":       gate["max_cvss"],
            "critical_count": gate["critical_count"],
            "high_count":     gate["high_count"],
            "total_findings": gate["total"],
            "blocked":        gate["blocked"],
            "findings_json":  __import__("json").dumps(findings),
        })
        db.commit()
        db.close()
        return scan_id
    except Exception as e:
        logger.warning(f"_persist_scan: DB write failed: {e}")
        return None


def _post_pr_comment(repo: str, pr_number: int, body: str):
    """Post a comment on a GitLab MR."""
    try:
        from tools.gitlab_tools import gitlab_comment_on_mr
        gitlab_comment_on_mr(repo=repo, mr_iid=pr_number, body=body)
        logger.info(f"[SecScan] Posted MR comment on {repo}!{pr_number}")
    except Exception as e:
        logger.warning(f"[SecScan] Failed to post MR comment: {e}")


def _post_error(repo: str, pr_number: Optional[int], msg: str):
    if pr_number:
        _post_pr_comment(repo, pr_number, f"⚠️ **AiNxt Security Scan Error:** {msg}")


def _set_commit_status(repo: str, branch: str, work_dir: str,
                       state: str, description: str, context: str):
    """
    Post a GitLab commit status against the HEAD SHA of the branch.
    state: "pending" | "running" | "success" | "failed" | "canceled"
    """
    # Map GitHub state names to GitLab equivalents
    _state_map = {"success": "success", "failure": "failed", "pending": "pending", "error": "failed"}
    gl_state = _state_map.get(state, state)

    try:
        from tools.gitlab_tools import gitlab_set_commit_status
        gitlab_set_commit_status(
            repo=repo, work_dir=work_dir,
            state=gl_state, description=description, context=context,
        )
    except Exception as e:
        logger.warning(f"[SecScan] Failed to post commit status: {e}")
