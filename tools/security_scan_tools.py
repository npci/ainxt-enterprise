# SPDX-License-Identifier: MIT
# ============================================================
# SECURITY SCAN TOOLS
# Integrates: SonarQube, Checkmarx (CxSAST), PMD, CPD
#
# All scanners are optional — configured via env vars.
# Results are normalised to CVSS scores and posted to GitLab MR.
#
# Env vars:
#   SONAR_HOST_URL          e.g. http://sonarqube.example.com:9000
#   SONAR_TOKEN             SonarQube user/project token
#   SONAR_SCANNER_PATH      path to sonar-scanner binary (default: sonar-scanner)
#   CHECKMARX_HOST_URL      e.g. https://checkmarx.example.com
#   CHECKMARX_CLIENT_ID     Checkmarx OAuth client ID
#   CHECKMARX_CLIENT_SECRET Checkmarx OAuth client secret
#   PMD_PATH                path to pmd binary (default: pmd)
#   SECURITY_CVSS_BLOCK_THRESHOLD   float, default 7.0 (CVSS HIGH threshold)
#   SECURITY_SCAN_ENABLED   true/false (default true)
# ============================================================

import json
import os
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse
from typing import Optional
from core.logger import logger

# ── Config ────────────────────────────────────────────────────

SONAR_HOST          = os.getenv("SONAR_HOST_URL", "").rstrip("/")
SONAR_TOKEN         = os.getenv("SONAR_TOKEN", "")
SONAR_SCANNER_BIN   = os.getenv("SONAR_SCANNER_PATH", "sonar-scanner")

CX_HOST             = os.getenv("CHECKMARX_HOST_URL", "").rstrip("/")
CX_CLIENT_ID        = os.getenv("CHECKMARX_CLIENT_ID", "")
CX_CLIENT_SECRET    = os.getenv("CHECKMARX_CLIENT_SECRET", "")

PMD_BIN             = os.getenv("PMD_PATH", "pmd")

CVSS_BLOCK_THRESHOLD = float(os.getenv("SECURITY_CVSS_BLOCK_THRESHOLD", "7.0"))
SCAN_ENABLED         = os.getenv("SECURITY_SCAN_ENABLED", "true").lower() == "true"


# ── CVSS severity mappings ────────────────────────────────────

# SonarQube severity → CVSS base score approximation
_SONAR_CVSS = {
    "BLOCKER":  9.0,
    "CRITICAL": 7.5,
    "MAJOR":    5.0,
    "MINOR":    2.5,
    "INFO":     0.5,
}

# PMD priority → CVSS
_PMD_CVSS = {
    1: 9.0,
    2: 7.5,
    3: 5.0,
    4: 2.5,
    5: 0.5,
}

# Checkmarx severity → CVSS
_CX_CVSS = {
    "Critical": 9.5,
    "High":     7.5,
    "Medium":   5.0,
    "Low":      2.5,
    "Info":     0.5,
}


# ── Shared HTTP helper ─────────────────────────────────────────

def _http(method: str, url: str, data: Optional[dict] = None,
          headers: Optional[dict] = None, timeout: int = 30) -> dict:
    h = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(data).encode() if data else None
    req  = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        logger.warning(f"security_scan_tools._http {method} {url} → {e}")
        raise


# ================================================================
# SONARQUBE
# ================================================================

def sonar_ensure_project(project_key: str, project_name: str) -> bool:
    """Create project in SonarQube if it doesn't exist. Returns True on success."""
    if not SONAR_HOST or not SONAR_TOKEN:
        return False
    try:
        url  = f"{SONAR_HOST}/api/projects/create"
        data = urllib.parse.urlencode({
            "project": project_key,
            "name":    project_name,
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Basic {_sonar_basic()}")
        with urllib.request.urlopen(req, timeout=15) as _:
            pass
        return True
    except Exception as e:
        # 400 = project already exists — that's fine
        if "already exists" in str(e).lower() or "400" in str(e):
            return True
        logger.warning(f"sonar_ensure_project: {e}")
        return False


def _sonar_basic() -> str:
    import base64
    return base64.b64encode(f"{SONAR_TOKEN}:".encode()).decode()


def sonar_trigger_scan(source_dir: str, project_key: str, branch: str) -> bool:
    """
    Run sonar-scanner CLI against source_dir.
    Returns True if the CLI exits successfully.
    """
    if not SONAR_HOST or not SONAR_TOKEN:
        logger.info("sonar_trigger_scan: SONAR_HOST_URL or SONAR_TOKEN not set — skipping")
        return False

    cmd = [
        SONAR_SCANNER_BIN,
        f"-Dsonar.host.url={SONAR_HOST}",
        f"-Dsonar.login={SONAR_TOKEN}",
        f"-Dsonar.projectKey={project_key}",
        f"-Dsonar.sources=.",
        f"-Dsonar.branch.name={branch}",
        f"-Dsonar.scm.disabled=true",
    ]
    try:
        result = subprocess.run(
            cmd, cwd=source_dir,
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            logger.warning(f"sonar-scanner exited {result.returncode}: {result.stderr[:500]}")
            return False
        return True
    except FileNotFoundError:
        logger.warning(f"sonar-scanner binary not found at '{SONAR_SCANNER_BIN}' — install sonar-scanner CLI")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("sonar-scanner timed out after 600s")
        return False


def sonar_wait_for_scan(project_key: str, max_wait: int = 300) -> bool:
    """Poll SonarQube CE task queue until the analysis for project_key completes."""
    if not SONAR_HOST or not SONAR_TOKEN:
        return False
    url = f"{SONAR_HOST}/api/ce/activity?component={project_key}&status=IN_PROGRESS,PENDING"
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Basic {_sonar_basic()}")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            if not data.get("tasks"):
                return True   # queue empty → analysis complete
        except Exception as e:
            logger.warning(f"sonar_wait_for_scan: poll error {e}")
        time.sleep(10)
    logger.warning("sonar_wait_for_scan: timed out waiting for analysis")
    return False


def sonar_get_vulnerabilities(project_key: str) -> list[dict]:
    """
    Fetch VULNERABILITY and BUG issues from SonarQube for a project.
    Returns list of normalised finding dicts with cvss_score.
    """
    if not SONAR_HOST or not SONAR_TOKEN:
        return []
    findings = []
    page = 1
    while True:
        url = (
            f"{SONAR_HOST}/api/issues/search"
            f"?componentKeys={project_key}"
            f"&types=VULNERABILITY,BUG"
            f"&severities=BLOCKER,CRITICAL,MAJOR"
            f"&resolved=false"
            f"&ps=100&p={page}"
        )
        try:
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Basic {_sonar_basic()}")
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            logger.warning(f"sonar_get_vulnerabilities: {e}")
            break

        for issue in data.get("issues", []):
            sev = issue.get("severity", "INFO")
            findings.append({
                "tool":       "SonarQube",
                "rule":       issue.get("rule", ""),
                "message":    issue.get("message", ""),
                "component":  issue.get("component", ""),
                "line":       issue.get("line"),
                "severity":   sev,
                "cvss_score": _SONAR_CVSS.get(sev, 2.5),
                "type":       issue.get("type", ""),
            })

        total = data.get("total", 0)
        if page * 100 >= total:
            break
        page += 1

    return findings


# ================================================================
# CHECKMARX (CxSAST REST API)
# ================================================================

_cx_token_cache: dict = {}


def _cx_get_token() -> str:
    """Fetch / reuse a Checkmarx OAuth2 token."""
    now = time.time()
    if _cx_token_cache.get("expires_at", 0) > now + 60:
        return _cx_token_cache["token"]

    url  = f"{CX_HOST}/cxrestapi/auth/identity/connect/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CX_CLIENT_ID,
        "client_secret": CX_CLIENT_SECRET,
        "scope":         "sast_api",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read())

    _cx_token_cache["token"]      = tok["access_token"]
    _cx_token_cache["expires_at"] = now + tok.get("expires_in", 3600)
    return _cx_token_cache["token"]


def _cx_headers() -> dict:
    return {
        "Authorization": f"Bearer {_cx_get_token()}",
        "Content-Type":  "application/json;v=1.0",
    }


def checkmarx_create_scan(project_name: str, source_zip_path: str) -> Optional[str]:
    """
    Upload source zip and create a Checkmarx SAST scan.
    Returns scan_id string or None on failure.
    """
    if not CX_HOST or not CX_CLIENT_ID or not CX_CLIENT_SECRET:
        logger.info("checkmarx_create_scan: Checkmarx not configured — skipping")
        return None
    try:
        # Step 1: ensure project exists
        project_id = _cx_get_or_create_project(project_name)
        if not project_id:
            return None

        # Step 2: upload source zip
        upload_url = f"{CX_HOST}/cxrestapi/projects/{project_id}/sourceCode/attachments"
        with open(source_zip_path, "rb") as f:
            zip_data = f.read()
        req = urllib.request.Request(upload_url, data=zip_data, method="POST")
        req.add_header("Authorization", f"Bearer {_cx_get_token()}")
        req.add_header("Content-Type", "application/zip")
        with urllib.request.urlopen(req, timeout=120) as _:
            pass

        # Step 3: create scan
        scan_payload = {
            "projectId":           project_id,
            "isIncremental":       False,
            "isPublic":            True,
            "forceScan":           True,
            "comment":             "AiNxt automated SAST scan",
        }
        url = f"{CX_HOST}/cxrestapi/sast/scans"
        data = json.dumps(scan_payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in _cx_headers().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        scan_id = str(result.get("id", ""))
        logger.info(f"checkmarx_create_scan: scan_id={scan_id} project_id={project_id}")
        return scan_id

    except Exception as e:
        logger.warning(f"checkmarx_create_scan failed: {e}")
        return None


def _cx_get_or_create_project(name: str) -> Optional[int]:
    """Get or create a Checkmarx project by name. Returns project_id."""
    try:
        url = f"{CX_HOST}/cxrestapi/projects?projectName={urllib.parse.quote(name)}"
        req = urllib.request.Request(url)
        for k, v in _cx_headers().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as resp:
            projects = json.loads(resp.read())
        if projects:
            return projects[0]["id"]

        # Create
        data = json.dumps({"name": name, "owningTeam": 1, "isPublic": True}).encode()
        req = urllib.request.Request(f"{CX_HOST}/cxrestapi/projects", data=data, method="POST")
        for k, v in _cx_headers().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as resp:
            created = json.loads(resp.read())
        return created["id"]
    except Exception as e:
        logger.warning(f"_cx_get_or_create_project: {e}")
        return None


def checkmarx_poll_scan(scan_id: str, max_wait: int = 600) -> bool:
    """Poll until scan reaches Finished/Failed/Canceled. Returns True if Finished."""
    if not CX_HOST or not scan_id:
        return False
    url      = f"{CX_HOST}/cxrestapi/sast/scans/{scan_id}"
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            for k, v in _cx_headers().items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=20) as resp:
                scan = json.loads(resp.read())
            status = (scan.get("status") or {}).get("name", "")
            logger.info(f"checkmarx_poll_scan: scan_id={scan_id} status={status}")
            if status == "Finished":
                return True
            if status in ("Failed", "Canceled", "Deleted"):
                logger.warning(f"checkmarx_poll_scan: scan ended with {status}")
                return False
        except Exception as e:
            logger.warning(f"checkmarx_poll_scan: {e}")
        time.sleep(20)
    logger.warning(f"checkmarx_poll_scan: timed out after {max_wait}s")
    return False


def checkmarx_get_findings(scan_id: str) -> list[dict]:
    """
    Retrieve SAST findings from a finished Checkmarx scan.
    Returns normalised list with cvss_score.
    """
    if not CX_HOST or not scan_id:
        return []
    try:
        url = f"{CX_HOST}/cxrestapi/sast/scans/{scan_id}/resultsStatistics"
        req = urllib.request.Request(url)
        for k, v in _cx_headers().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=20) as resp:
            stats = json.loads(resp.read())

        findings = []
        for sev in ("Critical", "High", "Medium", "Low"):
            count = stats.get(f"{sev.lower()}Severity", 0) or 0
            if count > 0:
                for _ in range(count):
                    findings.append({
                        "tool":       "Checkmarx",
                        "rule":       "SAST",
                        "message":    f"{sev} severity vulnerability detected",
                        "component":  "",
                        "line":       None,
                        "severity":   sev,
                        "cvss_score": _CX_CVSS.get(sev, 2.5),
                        "type":       "VULNERABILITY",
                    })
        return findings
    except Exception as e:
        logger.warning(f"checkmarx_get_findings: {e}")
        return []


# ================================================================
# PMD + CPD (CLI tools)
# ================================================================

# Security-focused rulesets per language
_PMD_RULESETS = {
    "java":   "category/java/security.xml,category/java/errorprone.xml,category/java/bestpractices.xml",
    "python": "category/python/security.xml",
    "js":     "category/ecmascript/security.xml",
    "default": "category/java/security.xml",
}


def pmd_scan(source_dir: str, language: str = "java") -> list[dict]:
    """
    Run PMD static analysis on source_dir.
    Returns normalised findings list with cvss_score.
    """
    ruleset = _PMD_RULESETS.get(language, _PMD_RULESETS["default"])
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_file = f.name

    cmd = [
        PMD_BIN, "check",
        "-d", source_dir,
        "-R", ruleset,
        "-f", "json",
        "-r", out_file,
        "--no-fail-on-violation",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode not in (0, 4):  # 4 = violations found but no error
            logger.warning(f"pmd_scan exited {result.returncode}: {result.stderr[:400]}")
            return []

        with open(out_file) as f:
            data = json.load(f)

        findings = []
        for file_result in data.get("files", []):
            for v in file_result.get("violations", []):
                pri = v.get("priority", 3)
                findings.append({
                    "tool":       "PMD",
                    "rule":       v.get("rule", ""),
                    "message":    v.get("description", ""),
                    "component":  file_result.get("filename", ""),
                    "line":       v.get("beginline"),
                    "severity":   f"P{pri}",
                    "cvss_score": _PMD_CVSS.get(pri, 2.5),
                    "type":       v.get("ruleset", ""),
                })
        return findings

    except FileNotFoundError:
        logger.warning(f"PMD binary not found at '{PMD_BIN}' — install PMD and set PMD_PATH")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("pmd_scan timed out after 300s")
        return []
    except Exception as e:
        logger.warning(f"pmd_scan error: {e}")
        return []
    finally:
        try:
            os.unlink(out_file)
        except Exception:
            pass


def cpd_scan(source_dir: str, language: str = "java", min_tokens: int = 100) -> list[dict]:
    """
    Run CPD (Copy-Paste Detector) on source_dir.
    Duplicate code is a compliance/maintainability risk — scored at CVSS 3.0.
    Returns normalised findings list.
    """
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
        out_file = f.name

    cmd = [
        PMD_BIN, "cpd",
        "--minimum-tokens", str(min_tokens),
        "--dir", source_dir,
        "--language", language,
        "--format", "xml",
        "--report-file", out_file,
        "--no-fail-on-violation",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode not in (0, 4):
            logger.warning(f"cpd_scan exited {result.returncode}")
            return []

        import xml.etree.ElementTree as ET
        tree = ET.parse(out_file)
        root = tree.getroot()
        findings = []
        for dup in root.findall("duplication"):
            tokens = dup.get("tokens", "0")
            locs   = [f.get("path", "") for f in dup.findall("file")]
            findings.append({
                "tool":       "CPD",
                "rule":       "DuplicateCode",
                "message":    f"Code duplication: {tokens} tokens duplicated across {len(locs)} locations",
                "component":  "; ".join(locs[:3]),
                "line":       None,
                "severity":   "MAJOR",
                "cvss_score": 3.0,
                "type":       "CODE_SMELL",
            })
        return findings

    except FileNotFoundError:
        logger.warning(f"CPD binary not found at '{PMD_BIN}' — install PMD")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("cpd_scan timed out after 300s")
        return []
    except Exception as e:
        logger.warning(f"cpd_scan error: {e}")
        return []
    finally:
        try:
            os.unlink(out_file)
        except Exception:
            pass


# ================================================================
# RISK GATE — CVSS aggregation + blocking decision
# ================================================================

def compute_risk_gate(findings: list[dict]) -> dict:
    """
    Given all findings from all tools, compute:
      - max_cvss: highest single CVSS score
      - critical_count: findings with cvss >= 9.0
      - high_count: findings with 7.0 <= cvss < 9.0
      - blocked: True if max_cvss >= CVSS_BLOCK_THRESHOLD
      - summary_by_tool: {tool: count}
    """
    if not findings:
        return {
            "max_cvss":       0.0,
            "critical_count": 0,
            "high_count":     0,
            "total":          0,
            "blocked":        False,
            "summary_by_tool": {},
        }

    scores  = [f.get("cvss_score", 0.0) for f in findings]
    max_cvss = max(scores)

    by_tool: dict = {}
    for f in findings:
        t = f.get("tool", "Unknown")
        by_tool[t] = by_tool.get(t, 0) + 1

    return {
        "max_cvss":        round(max_cvss, 1),
        "critical_count":  sum(1 for s in scores if s >= 9.0),
        "high_count":      sum(1 for s in scores if 7.0 <= s < 9.0),
        "total":           len(findings),
        "blocked":         max_cvss >= CVSS_BLOCK_THRESHOLD,
        "summary_by_tool": by_tool,
    }


# ================================================================
# GITLAB MR COMMENT — formatted scan report
# ================================================================

def format_scan_report(gate: dict, findings: list[dict], repo: str, pr_number: int) -> str:
    """Build a markdown PR comment with the full scan report."""
    status_icon = "🔴 BLOCKED" if gate["blocked"] else "🟢 PASSED"
    lines = [
        f"## 🔒 Security Scan Report — PR #{pr_number}",
        f"",
        f"**Status:** {status_icon}  |  "
        f"**Max CVSS:** `{gate['max_cvss']}`  |  "
        f"**Critical:** {gate['critical_count']}  |  "
        f"**High:** {gate['high_count']}  |  "
        f"**Total Findings:** {gate['total']}",
        f"",
        f"**Block threshold:** CVSS ≥ {CVSS_BLOCK_THRESHOLD}",
        f"",
    ]

    if gate["summary_by_tool"]:
        lines.append("### Findings by Tool")
        lines.append("")
        lines.append("| Tool | Findings |")
        lines.append("|------|---------|")
        for tool, count in sorted(gate["summary_by_tool"].items()):
            lines.append(f"| {tool} | {count} |")
        lines.append("")

    # Top findings (highest CVSS first, max 20 shown)
    top = sorted(findings, key=lambda x: x.get("cvss_score", 0), reverse=True)[:20]
    if top:
        lines.append("### Top Findings")
        lines.append("")
        lines.append("| CVSS | Tool | Severity | Rule | Location |")
        lines.append("|------|------|----------|------|----------|")
        for f in top:
            component = (f.get("component") or "")[-60:]
            line_info = f":{f['line']}" if f.get("line") else ""
            lines.append(
                f"| {f.get('cvss_score', 0):.1f} "
                f"| {f.get('tool', '')} "
                f"| {f.get('severity', '')} "
                f"| {f.get('rule', '')[:40]} "
                f"| `{component}{line_info}` |"
            )
        if len(findings) > 20:
            lines.append(f"")
            lines.append(f"_...and {len(findings) - 20} more findings. See full report in SonarQube / Checkmarx._")
        lines.append("")

    if gate["blocked"]:
        lines.append(
            f"> ⛔ **This PR is blocked.** One or more findings have CVSS score ≥ {CVSS_BLOCK_THRESHOLD}. "
            f"Fix all Critical/High vulnerabilities before merging."
        )
    else:
        lines.append(
            f"> ✅ No blocking vulnerabilities found. PR is cleared for merge from a security perspective."
        )

    lines.append(f"\n_Scanned by AiNxt Security Pipeline — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}_")
    return "\n".join(lines)


# ================================================================
# FAST TIER — local, server-less scanners for the generation-time gate
# (Semgrep / Bandit / secrets). Static analysis only — these NEVER execute
# the scanned code, so running them on a temp file is safe. Findings are
# normalised to the same dict shape (+ optional cwe/owasp tags) so
# compute_risk_gate() / format_scan_report() work unchanged.
# ================================================================

_SEMGREP_CVSS = {"ERROR": 8.0, "WARNING": 4.5, "INFO": 2.0}
_BANDIT_CVSS  = {"HIGH": 8.0, "MEDIUM": 5.0, "LOW": 2.5}


def semgrep_scan(target: str, language: str = "") -> list[dict]:
    """Run Semgrep (multi-language, OWASP/CWE-tagged) on a file or dir.
    Override rule packs with SEMGREP_CONFIG (comma-separated; point at a
    vendored ruleset dir in air-gapped deployments)."""
    # SEMGREP_PATH defaults to "semgrep" — relies on system PATH.
    # OSS: install via `pip install semgrep` or system package manager.
    # Set SEMGREP_PATH to your semgrep binary in .env, e.g.
    # SEMGREP_PATH=/path/to/venv/bin/semgrep
    semgrep_bin = os.getenv("SEMGREP_PATH", "semgrep")
    cfg = os.getenv("SEMGREP_CONFIG", "")
    configs = [c.strip() for c in cfg.split(",") if c.strip()] or [
        "p/ci", "p/owasp-top-ten", "p/secrets",
    ]
    cmd = [semgrep_bin, "--json", "-q", "--disable-version-check", "--metrics=off"]
    for c in configs:
        cmd += ["--config", c]
    cmd.append(target)

    findings: list[dict] = []
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        data = json.loads(proc.stdout or "{}")
        for r in data.get("results", []):
            extra = r.get("extra", {}) or {}
            meta  = extra.get("metadata", {}) or {}
            sev   = (extra.get("severity") or "WARNING").upper()
            cwe   = meta.get("cwe")
            owasp = meta.get("owasp")
            findings.append({
                "tool":       "Semgrep",
                "rule":       r.get("check_id", ""),
                "message":    (extra.get("message") or "").strip(),
                "component":  r.get("path", target),
                "line":       (r.get("start") or {}).get("line"),
                "severity":   sev,
                "cvss_score": _SEMGREP_CVSS.get(sev, 4.5),
                "type":       "VULNERABILITY",
                "cwe":        (cwe[0] if isinstance(cwe, list) and cwe else cwe),
                "owasp":      (owasp[0] if isinstance(owasp, list) and owasp else owasp),
            })
    except FileNotFoundError:
        logger.warning("semgrep_scan: semgrep binary not found (set SEMGREP_PATH)")
    except subprocess.TimeoutExpired:
        logger.warning("semgrep_scan: timed out")
    except Exception as e:
        logger.warning(f"semgrep_scan error: {e}")
    return findings


def bandit_scan(target: str) -> list[dict]:
    """Bandit (Python). Best-effort — returns [] if bandit isn't installed."""
    bandit_bin = os.getenv("BANDIT_PATH", "bandit")
    findings: list[dict] = []
    try:
        proc = subprocess.run(
            [bandit_bin, "-f", "json", "-q", "-r", target],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(proc.stdout or "{}")
        for r in data.get("results", []):
            sev = (r.get("issue_severity") or "MEDIUM").upper()
            findings.append({
                "tool":       "Bandit",
                "rule":       r.get("test_id", ""),
                "message":    r.get("issue_text", ""),
                "component":  r.get("filename", target),
                "line":       r.get("line_number"),
                "severity":   sev,
                "cvss_score": _BANDIT_CVSS.get(sev, 5.0),
                "type":       "VULNERABILITY",
                "cwe":        (r.get("issue_cwe") or {}).get("id"),
            })
    except FileNotFoundError:
        pass  # optional tool
    except Exception as e:
        logger.warning(f"bandit_scan error: {e}")
    return findings


def secrets_scan(content: str, path: str = "") -> list[dict]:
    """Hardcoded-secret / key detection — reuses the compliance engine's secret
    detectors (NOT the PAN/PII ones, to avoid Luhn false-positives on code
    numbers). Read-only: never redacts the code."""
    _SECRET_TYPES = {
        "SECRET", "API_KEY", "ACCESS_TOKEN", "PRIVATE_KEY_LEAK", "SSH_KEY_LEAK",
        "PAYMENT_KEY_LEAK", "KEY_ASSIGNMENT_LEAK", "CERTIFICATE_LEAK",
    }
    findings: list[dict] = []
    try:
        from agents.compliance_engine import compliance_engine
        res = compliance_engine.validate_input(content)
        for f in res.get("findings", []):
            t = f.get("type", "")
            if t in _SECRET_TYPES:
                findings.append({
                    "tool":       "Secrets",
                    "rule":       t,
                    "message":    f"Hardcoded {t} detected in generated code",
                    "component":  path,
                    "line":       None,
                    "severity":   "CRITICAL",
                    "cvss_score": 9.0,
                    "type":       "VULNERABILITY",
                    "cwe":        "CWE-798",
                })
    except Exception as e:
        logger.warning(f"secrets_scan error: {e}")
    return findings
