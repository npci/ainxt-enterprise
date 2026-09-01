# SPDX-License-Identifier: Apache-2.0
"""cli_runtime.workspace — the private per-run directory a CLI process runs in.

Each run gets its own directory, which is the CLI's ``cwd``. It holds:

    <workspace>/
        .ainxt/config.toml     ← declares our MCP server + the run's bearer token
        prompt.txt             ← only when the prompt is too large for argv
        repo/                  ← only when the agent works against a git repo
        <artefacts>            ← whatever the run produces

Two decisions worth stating.

**We reuse the engine's existing artefact directory** rather than inventing a
second workspace root. ``native_engine`` already creates
``{RUNTIME_ARTIFACTS_DIR}/workflows/{run_id}`` per run and ``code_executor``
already writes there, so adopting it means CLI-produced and natively-produced
files land in the same place and the existing download plumbing needs no change.

**The MCP config is project-scoped, and that requires folder trust.** ``ainxt``
reads ``.ainxt/config.toml`` from the cwd, but *silently refuses to start
repo-local MCP servers in an untrusted folder* — verified on 0.2.101, where
``mcp doctor`` reports ``folder untrusted (repo-local server not started)``. There
is no error on the run itself; the agent simply has no tools. This module writes
the config, and ``runner`` sets ``AINXT_FOLDER_TRUST=0`` in the child env. Both
halves are required.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from core.logger import logger

from app.owner_tag import owner_tag

from .config import CliRuntimeConfig

# ``run_id`` becomes a directory name, so it is whitelisted rather than escaped:
# anything outside this set is replaced, which makes traversal unrepresentable.
_SAFE_RUN_ID = re.compile(r"[^A-Za-z0-9._-]")

# Directory names we create inside a workspace.
_CONFIG_DIR = ".ainxt"
_CONFIG_FILE = "config.toml"
_PROMPT_FILE = "prompt.txt"
_REPO_DIR = "repo"
# Subdirectory (inside the CLI cwd) that holds user-uploaded files, staged so the
# agent can open and re-read them like any other file.
_INPUTS_DIR = "inputs"

# A clone containing only these entries has no real project content.
_SCAFFOLD_ONLY = {
    ".git", ".gitignore", ".gitattributes", ".gitkeep",
    "readme", "readme.md", "readme.txt", "license", "license.md", "license.txt",
}


def safe_run_id(run_id: str) -> str:
    """Return a filesystem-safe form of ``run_id`` (never empty)."""
    cleaned = _SAFE_RUN_ID.sub("_", str(run_id or "").strip())
    return cleaned or "run"


# ════════════════════════════════════════════════════════════════════════════
# Workspace root
# ════════════════════════════════════════════════════════════════════════════

def workspace_root() -> str:
    """Base directory for per-run CLI workspaces.

    Defaults to the same tree the engine already uses for run artefacts so both
    execution paths agree on where a run's files live.
    """
    explicit = (os.getenv("ABSTUDIO_CLI_WORKSPACE_ROOT", "") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    runtime_dir = (os.getenv("RUNTIME_ARTIFACTS_DIR", "") or "runtime_artifacts").strip()
    return os.path.abspath(os.path.join(runtime_dir, "cli_runs"))


def prepare_workspace(run_id: str) -> str:
    """Create (or reuse) the private workspace for ``run_id`` and return its path.

    Reuse is intentional: a resumed or retried run must see the files its earlier
    attempt produced.
    """
    path = os.path.join(workspace_root(), safe_run_id(run_id))
    os.makedirs(path, exist_ok=True)
    return path


# ════════════════════════════════════════════════════════════════════════════
# .ainxt/config.toml
# ════════════════════════════════════════════════════════════════════════════

def _toml_basic_string(value: str) -> str:
    """Quote a value as a TOML basic string.

    Hand-rolled because the stdlib has no TOML *writer* (``tomllib`` is read-only)
    and the file content is fully machine-generated. Control characters are
    escaped rather than stripped so a token containing one cannot terminate the
    string early and corrupt the file.
    """
    out = str(value or "")
    out = out.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    out = "".join(ch if ord(ch) >= 0x20 else f"\\u{ord(ch):04x}" for ch in out)
    return f'"{out}"'


def write_mcp_config(
    *,
    workspace: str,
    config: CliRuntimeConfig,
    run_id: str,
    token: str,
) -> str:
    """Write ``<workspace>/.ainxt/config.toml`` and return its path.

    Uses HTTP transport pointed back at this process. The bearer token goes in a
    ``[mcp_servers.<name>.headers]`` sub-table — the exact shape
    ``ainxt mcp add --transport http --header ...`` produces, confirmed by
    running it.

    The file is written ``0600`` where the platform supports it: it carries a live
    credential, even a narrow and short-lived one.
    """
    config_dir = os.path.join(workspace, _CONFIG_DIR)
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, _CONFIG_FILE)

    server = config.mcp_server_name
    body = "\n".join([
        "# Auto-generated per CLI run by ABStudio (app/cli_runtime/workspace.py).",
        "# Overwritten on every spawn — do not hand-edit.",
        "",
        f"[mcp_servers.{server}]",
        f"url = {_toml_basic_string(config.mcp_url_for(run_id))}",
        "enabled = true",
        f"startup_timeout_sec = {int(config.startup_timeout_s)}",
        "",
        f"[mcp_servers.{server}.headers]",
        f"Authorization = {_toml_basic_string('Bearer ' + token)}",
        f"X-Abstudio-Run = {_toml_basic_string(run_id)}",
        "",
    ])

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort: Windows has no equivalent mode

    logger.info(
        "[CLI-WS] MCP config written",
        run_id=run_id, path=path, server=server,
        url=config.mcp_url_for(run_id),
    )
    return path


def write_prompt_file(workspace: str, prompt: str) -> str:
    """Write a large prompt to a file and return its path.

    The CLI takes the prompt as a single argv token, so a long one (an inlined
    document, a large diff) exceeds the OS argument limit and the spawn dies with
    a usage error. ``--prompt-file`` avoids that entirely.
    """
    path = os.path.join(workspace, _PROMPT_FILE)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(prompt or "")
    return path


def _safe_file_name(name: str) -> str:
    """Filesystem-safe leaf name for a staged input file (never empty, no path)."""
    base = os.path.basename(str(name or "").strip().replace("\\", "/"))
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip("._ ") or "attachment"
    return cleaned[:180]


def stage_documents(workspace: str, documents: List[dict]) -> List[dict]:
    """Write each uploaded document into ``<workspace>/inputs/`` and return a
    manifest of what was staged.

    Every node gets a copy in its OWN working directory, regardless of the
    document's size — so any agent in the workflow can open and re-read the file
    as many times as it needs, rather than depending on the size-based prompt
    injection (which only feeds a small doc to the first node).

    Attachments carry the *extracted text* (``parsed_text``), not the original
    bytes, so we stage a readable text file. The original filename is preserved
    with a ``.txt`` suffix when it was a binary type (``report.docx`` ->
    ``report.docx.txt``) so the provenance is obvious. Returns a list of
    ``{"name": <relative path>, "chars": N}`` for the ones actually written.

    Never raises — a staging failure must not take down the run; it just means
    the file is not available on disk (the prompt injection still applies).
    """
    manifest: List[dict] = []
    if not documents:
        return manifest
    inputs_dir = os.path.join(workspace, _INPUTS_DIR)
    try:
        os.makedirs(inputs_dir, exist_ok=True)
    except OSError as exc:
        logger.warning("[CLI-WS] could not create inputs dir", error=str(exc))
        return manifest

    used: set = set()
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        text = doc.get("parsed_text") or doc.get("text") or ""
        if not str(text).strip():
            continue
        raw_name = doc.get("file_name") or doc.get("filename") or "attachment"
        leaf = _safe_file_name(raw_name)
        # Text extract of a binary doc → make the .txt explicit.
        if not leaf.lower().endswith(".txt"):
            leaf = f"{leaf}.txt"
        # Deduplicate within this node's inputs dir.
        candidate, n = leaf, 1
        while candidate in used:
            stem, dot, ext = leaf.rpartition(".")
            candidate = f"{stem}_{n}.{ext}" if dot else f"{leaf}_{n}"
            n += 1
        used.add(candidate)

        dest = os.path.join(inputs_dir, candidate)
        try:
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(str(text))
            manifest.append({
                "name": f"{_INPUTS_DIR}/{candidate}",
                "original": str(raw_name),
                "chars": len(str(text)),
            })
        except OSError as exc:
            logger.warning("[CLI-WS] could not stage document", name=candidate, error=str(exc))
    if manifest:
        logger.info(
            "[CLI-WS] staged uploaded files into the workspace",
            count=len(manifest), inputs_dir=inputs_dir,
            names=[m["name"] for m in manifest],
        )
    return manifest


# Files/dirs WE create in a workspace — never rescued as "model output".
_RESCUE_IGNORE = {
    _CONFIG_DIR, _CONFIG_FILE, _PROMPT_FILE, _INPUTS_DIR, ".mcp.json",
    ".git", ".gitignore", ".DS_Store",
}
# Extensions worth surfacing as a downloadable deliverable if the model wrote one
# directly (mirrors sanitize._DELIVERABLE_EXTS; kept local to avoid a cycle).
_RESCUE_DELIVERABLE_EXTS = {
    ".docx", ".pdf", ".pptx", ".xlsx", ".xls", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".md", ".txt", ".json", ".zip", ".html",
}


# Per-user download-dir name. Previously hand-copied here to keep this module
# import-light; ``app.owner_tag`` is stdlib-only, so it can now be imported
# directly and the copy is gone. Aliased to the old private name so existing
# call sites and tests are unaffected.
_owner_tag = owner_tag


def rescue_workspace_files(cwd: str, run_id: str, user_id: str = "") -> List[dict]:
    """Register files the model wrote DIRECTLY into its workspace as downloads.

    A model sometimes writes a file with the CLI's own built-in file tools (e.g.
    ``rose_description.md``) instead of going through ``code_executor``. Those
    files land in the private per-run workspace, are never moved to
    ``GENERATED_FILES_DIR``, and get no ``download_url`` — so the UI linkifies a
    bare filename and the SPA router redirects to the portal.

    This backstop scans the workspace top level, copies any real output file into
    ``GENERATED_FILES_DIR`` using the SAME ``<run_id>_<name>`` scheme and returns
    the SAME dict shape ``code_executor`` produces, so downstream handling and the
    ``/generated-files/<disk_name>`` route work identically. Best-effort; never
    raises. Only top-level files with a known deliverable extension are rescued
    (dirs, scaffolding, and unknown/scratch extensions are ignored).

    Broken Access Control / IDOR fix: when ``user_id`` is supplied, rescued
    files are stored under the caller's per-user owner-dir
    (``GENERATED_FILES_DIR/{owner_tag}/{name}``) and ``disk_name`` /
    ``download_url`` carry that prefix, so the download endpoint scopes them to
    this user. Without a ``user_id`` they stay flat (legacy behaviour).
    """
    rescued: List[dict] = []
    try:
        gen_dir = (os.getenv("GENERATED_FILES_DIR", "") or "").strip()
        if not gen_dir or not os.path.isdir(cwd):
            return rescued
        os.makedirs(gen_dir, exist_ok=True)
        from urllib.parse import quote

        tag = _owner_tag(user_id)
        write_dir = os.path.join(gen_dir, tag) if tag else gen_dir
        if tag:
            os.makedirs(write_dir, exist_ok=True)

        for entry in os.listdir(cwd):
            if entry in _RESCUE_IGNORE:
                continue
            src = os.path.join(cwd, entry)
            if not os.path.isfile(src):
                continue
            ext = os.path.splitext(entry)[1].lower()
            if ext not in _RESCUE_DELIVERABLE_EXTS:
                continue
            safe_rid = safe_run_id(run_id)
            base_name = f"{safe_rid}_{_safe_file_name(entry)}"
            dest = os.path.join(write_dir, base_name)
            if os.path.exists(dest):
                stem, _, e = base_name.rpartition(".")
                import uuid as _uuid
                base_name = f"{stem}_{_uuid.uuid4().hex[:6]}.{e}" if _ else f"{base_name}_{_uuid.uuid4().hex[:6]}"
                dest = os.path.join(write_dir, base_name)
            try:
                shutil.copy2(src, dest)
                # Anchor the download-store TTL to ingestion time. copy2
                # preserves the source mtime; a workspace file checked out or
                # written earlier could carry an mtime past the TTL and be
                # "born expired" (download 410). Reset to now. Best-effort.
                try:
                    os.utime(dest, None)
                except OSError:
                    pass
            except OSError as exc:
                logger.warning("[CLI-WS] rescue copy failed", name=entry, error=str(exc))
                continue
            # Relative key the download URL uses: prefixed with the owner-dir
            # when we have an identity, bare otherwise.
            disk_name = f"{tag}/{base_name}" if tag else base_name
            rescued.append({
                "filename":     entry,
                "disk_name":    disk_name,
                "download_url": f"/generated-files/{quote(disk_name, safe='/')}",
                "format":       ext,
                "path":         dest,
            })
        if rescued:
            logger.info(
                "[CLI-WS] rescued model-written files into the download store",
                run_id=run_id, count=len(rescued),
                names=[r["filename"] for r in rescued],
            )
    except Exception as exc:  # never let a rescue bug break the run
        logger.warning("[CLI-WS] rescue_workspace_files failed", run_id=run_id, error=str(exc))
    return rescued


# ════════════════════════════════════════════════════════════════════════════
# Git
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CloneResult:
    """Outcome of preparing a git checkout."""

    ok: bool
    path: str = ""
    state: str = "missing"      # cloned | empty | missing
    error: str = ""


def repo_dir(workspace: str) -> str:
    return os.path.join(workspace, _REPO_DIR)


def clone_state(workspace: str) -> str:
    """Classify an on-disk checkout without touching git or the network.

    ``cloned`` (usable) / ``empty`` (a ``.git`` but only scaffolding) /
    ``missing``. The ``empty`` case matters because a non-empty directory makes a
    fresh ``git clone`` abort, so it has to be cleared rather than reused.
    """
    path = repo_dir(workspace)
    try:
        if not os.path.isdir(os.path.join(path, ".git")):
            return "missing"
        for entry in os.listdir(path):
            if entry.lower() in _SCAFFOLD_ONLY:
                continue
            return "cloned"
        return "empty"
    except OSError:
        return "missing"


def _authenticated_url(repo: str, token: str) -> str:
    """Build a plain (non-authenticated) clone URL. A bare ``namespace/project``
    is resolved against ``GITLAB_URL``.

    ARCH-F-ABS1-008: the token is no longer embedded in the URL — see
    ``_git_auth_header()``, which passes it through git's ``http.extraHeader``
    mechanism instead so it never appears in ``ps`` output, ``/proc``, git
    error messages, or ``.git/config``.
    """
    base = str(repo or "").strip()
    if not base:
        return ""
    if not (base.startswith("http://") or base.startswith("https://")):
        gitlab = (os.getenv("GITLAB_URL", "")).rstrip("/")
        base = f"{gitlab}/{base.lstrip('/')}"
    if not base.endswith(".git"):
        base += ".git"
    return base


def _git_auth_header(token: str) -> list[str]:
    """Return git CLI args that pass the token via an HTTP Authorization
    header instead of embedding it in the clone URL."""
    if not token:
        return []
    return ["-c", f"http.extraHeader=Authorization: Bearer {token}"]


def _clean_url(repo: str) -> str:
    return _authenticated_url(repo, "")


def resolve_git_token(user_id: str = "", email: str = "") -> str:
    """Return the user's own GitLab PAT, or "" when they have none.

    Deliberately no service-account fallback: a run must act as the user, so an
    unconfigured token has to surface as "configure your token" rather than
    silently borrowing wider credentials.
    """
    try:
        from core.platform_credentials import extract_gitlab_pat, get_gitlab_token
    except Exception as exc:
        logger.warning("[CLI-WS] credential module unavailable", error=str(exc))
        return ""
    try:
        return extract_gitlab_pat(get_gitlab_token(user_id, email)) or ""
    except PermissionError:
        return ""
    except Exception as exc:
        logger.warning("[CLI-WS] git token lookup failed", error=str(exc))
        return ""


def ensure_repo(
    *,
    workspace: str,
    repo: str,
    ref: str = "",
    user_id: str = "",
    email: str = "",
    run_id: str = "",
    timeout: int = 300,
) -> CloneResult:
    """Ensure ``<workspace>/repo`` is a usable checkout of ``repo``.

    Reuses an existing clone, self-heals an empty one, and scrubs the token from
    the stored remote immediately after cloning so it is never persisted to disk.

    Success is judged by the presence of ``repo/.git``, not the exit code: git
    can return non-zero for warnings while having produced a perfectly good
    checkout.
    """
    target = repo_dir(workspace)
    state = clone_state(workspace)
    if state == "cloned":
        logger.info("[CLI-WS] reusing existing clone", run_id=run_id, path=target)
        return CloneResult(ok=True, path=target, state="cloned")

    if state == "empty":
        # A non-empty target makes `git clone` fail; clear the shell first.
        shutil.rmtree(target, ignore_errors=True)

    token = resolve_git_token(user_id, email)
    if not token:
        return CloneResult(
            ok=False, path=target, state="missing",
            error=("No GitLab personal access token found for this user. "
                   "Add one under Profile → GitLab Token."),
        )

    url = _authenticated_url(repo, token)
    if not url:
        return CloneResult(ok=False, path=target, state="missing",
                           error=f"could not resolve a clone URL for {repo!r}")

    cmd = ["git"] + _git_auth_header(token) + ["clone", "--depth", "1", "--quiet"]
    branch = (ref or "").split("@")[0].strip()
    if branch:
        cmd += ["--branch", branch]
    cmd += [url, target]

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return CloneResult(ok=False, path=target, state="missing",
                           error=f"git clone timed out after {timeout}s")
    except Exception as exc:
        return CloneResult(ok=False, path=target, state="missing",
                           error=f"git clone failed: {exc}")

    if not os.path.isdir(os.path.join(target, ".git")):
        # Never log stderr unfiltered — it can echo the authenticated URL.
        detail = _scrub_token((proc.stderr or "")[:500], token)
        logger.warning("[CLI-WS] clone failed", run_id=run_id, repo=repo, detail=detail)
        return CloneResult(ok=False, path=target, state="missing",
                           error=f"git clone did not produce a checkout: {detail}")

    # Remove the credential from .git/config so it isn't left on disk.
    try:
        subprocess.run(
            ["git", "-C", target, "remote", "set-url", "origin", _clean_url(repo)],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except Exception as exc:
        logger.warning("[CLI-WS] could not scrub remote URL", run_id=run_id, error=str(exc))

    logger.info("[CLI-WS] clone ready", run_id=run_id, repo=repo, path=target, ref=branch or "(default)")
    return CloneResult(ok=True, path=target, state="cloned")


def _scrub_token(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "***")
    return text


# ════════════════════════════════════════════════════════════════════════════
# TTL sweep
# ════════════════════════════════════════════════════════════════════════════

def sweep_workspaces(ttl_seconds: Optional[int] = None) -> Tuple[int, int]:
    """Delete workspaces older than the TTL. Returns ``(removed, kept)``.

    Age is taken from the directory's own mtime. Safe to call on a schedule and
    from multiple workers: a directory another worker removes first simply
    counts as kept.
    """
    root = workspace_root()
    if not os.path.isdir(root):
        return 0, 0
    if ttl_seconds is None:
        try:
            ttl_seconds = max(300, int(os.getenv("ABSTUDIO_CLI_WORKSPACE_TTL_SECONDS", "86400")))
        except (TypeError, ValueError):
            ttl_seconds = 86400

    cutoff = time.time() - ttl_seconds
    removed = kept = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) >= cutoff:
                kept += 1
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1 if not os.path.isdir(path) else 0
        except OSError:
            kept += 1
    if removed:
        logger.info("[CLI-WS] swept expired workspaces", removed=removed, kept=kept, ttl_seconds=ttl_seconds)
    return removed, kept


__all__ = [
    "safe_run_id",
    "workspace_root",
    "prepare_workspace",
    "write_mcp_config",
    "write_prompt_file",
    "CloneResult",
    "repo_dir",
    "clone_state",
    "resolve_git_token",
    "ensure_repo",
    "sweep_workspaces",
]
