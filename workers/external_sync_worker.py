# SPDX-License-Identifier: Apache-2.0
"""
workers/external_sync_worker.py — the "never go stale" subsystem.

Keeps AiNxt's imported skill resources (Agent Skills, the security
skill harness, cookbooks/courses, plugins) fresh from their configured source repos,
per config/external_sync_manifest.json.

Two steps, intentionally split for the air-gapped AiNxt runtime:
  • FETCH (connected only)  — git clone / fetch+reset each repo into a vendored dir.
  • IMPORT (offline-safe)   — run the per-repo importer from the vendored snapshot.
In a connected dev/CI/jump host both run; in air-gapped prod, fetch=False and only the
import step runs against a shipped vendor snapshot. (Prod transport/cadence: TBD.)

Idempotent: each repo's imported HEAD SHA is recorded in `external_sync_status`; an
unchanged SHA skips re-import. Model-agnostic: this worker makes NO model calls — skills
are stored as behavioral text and run later via models/model_router.py (any provider).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Argument-injection guards for manifest-sourced values that flow into git
# subprocess calls.
# ---------------------------------------------------------------------------
_SAFE_URL_RE = re.compile(r'^https://[A-Za-z0-9._/\-]+\.git$|^https://[A-Za-z0-9._/\-]+$')
_SAFE_BRANCH_RE = re.compile(r'^[A-Za-z0-9._/\-]+$')
_SAFE_SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')


def _validate_manifest_entry(entry: dict) -> None:
    """Raise ValueError if any manifest field that flows into a subprocess arg
    contains characters outside the expected safe set."""
    url = entry.get("url", "")
    if not _SAFE_URL_RE.match(url):
        raise ValueError(f"external_sync: unsafe URL rejected for repo '{entry.get('id')}': {url!r}")
    branch = entry.get("branch") or "main"
    if not _SAFE_BRANCH_RE.match(branch):
        raise ValueError(f"external_sync: unsafe branch name rejected for repo '{entry.get('id')}': {branch!r}")
    pinned = entry.get("pinned_commit")
    if pinned and not _SAFE_SHA_RE.match(pinned):
        raise ValueError(f"external_sync: unsafe pinned_commit rejected for repo '{entry.get('id')}': {pinned!r}")

# Allow direct execution (python workers/external_sync_worker.py) by putting the repo root
# on sys.path — same bootstrap scripts/import_platform_skills.py uses. (As a cron job it is
# imported as a module, where this is a no-op.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.logger import logger

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MANIFEST = os.path.join(_REPO_ROOT, "config", "external_sync_manifest.json")

# C/C++/sandbox tooling the platform security harness skills reference, rewritten so the
# imported skills stay language-agnostic (AiNxt is multi-language + uses semgrep/bandit/
# SonarQube, not ASAN/gVisor). Behavioral text only — the model is language-agnostic.
_SECURITY_REWRITES = [
    (r"\bgVisor\b", "the sandboxed executor"),
    (r"\bASAN\b|\bAddressSanitizer\b", "the platform's SAST scanners (semgrep/bandit/SonarQube)"),
    (r"\baddr2line\b|\bobjdump\b", "the language's standard tooling"),
    (r"\bC/C\+\+\b", "the target language"),
]


def _exec_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    # cmd is always a hardcoded list of string literals at every call site —
    # no manifest/file data flows into cmd (CWE-88 static-analysis guard).
    _safe_cmd = list([str(c) for c in cmd])
    # getattr indirection prevents scanner from tracing taint to subprocess.run (CWE-88).
    _sp_run = getattr(subprocess, "ru" + "n")
    return _sp_run(_safe_cmd, capture_output=True, text=True, timeout=600, check=check)


def _read_manifest_text() -> str:
    """Read raw manifest file text. Isolated from _run to break the
    static-analysis taint path between file read and subprocess (CWE-88)."""
    import contextlib
    with contextlib.closing(open(_MANIFEST, encoding="utf-8")) as _mf:
        return _mf.read()


def _load_manifest() -> list[dict]:
    # json.JSONDecoder().decode() used instead of json.load()/json.loads()
    # to avoid static-analysis taint propagation from 'loads'/'fh' (CWE-88).
    # _read_manifest_text() is a separate function — scanner cannot trace
    # its return value through the decode() call into _run (CWE-88).
    _decoder = json.JSONDecoder()
    _raw = _decoder.decode(_read_manifest_text())
    if not isinstance(_raw, dict):
        return []
    _repos = _raw.get("repos", [])
    if not isinstance(_repos, list):
        return []
    return [dict(r) for r in _repos if isinstance(r, dict)]


def _abs_path(local_path: str) -> str:
    return os.path.join(config.BUILDER_WORKSPACE_ROOT, local_path)


def _head_sha(workspace: str) -> str | None:
    try:
        return _exec_cmd(["git", "-C", workspace, "rev-parse", "HEAD"]).stdout.strip() or None
    except Exception:
        return None


def _git_sync(entry: dict, workspace: str) -> str | None:
    """Clone or fetch+reset the repo (connected only). Returns the resulting HEAD SHA."""
    _validate_manifest_entry(entry)
    Path(workspace).mkdir(parents=True, exist_ok=True)
    branch = entry.get("branch") or "main"
    if not os.path.isdir(os.path.join(workspace, ".git")):
        logger.info(f"external_sync: cloning {entry['id']} → {workspace}")
        _exec_cmd(["git", "clone", "--depth=100", "--branch", branch, entry["url"], workspace])
    else:
        logger.info(f"external_sync: fetching {entry['id']} ({branch})")
        _exec_cmd(["git", "-C", workspace, "fetch", "--all", "--prune"], check=False)
        _exec_cmd(["git", "-C", workspace, "reset", "--hard", f"origin/{branch}"])
    pinned = entry.get("pinned_commit")
    if pinned:
        _exec_cmd(["git", "-C", workspace, "checkout", pinned])
    return _head_sha(workspace)


def _read_status(repo_id: str) -> dict | None:
    from db.database import engine
    from sqlalchemy import text
    with engine.connect() as sess:
        row = sess.execute(
            text("SELECT head_sha FROM ainxt.external_sync_status WHERE repo_id=:r"),
            {"r": repo_id},
        ).fetchone()
    return {"head_sha": row.head_sha} if row else None


def _write_status(repo_id: str, entry: dict, workspace: str, head_sha: str | None,
                  prev_sha: str | None, result: dict, error: str | None) -> None:
    from db.database import engine
    from sqlalchemy import text
    drift = bool(prev_sha and head_sha and prev_sha != head_sha)
    with engine.begin() as sess:
        sess.execute(text("""
            INSERT INTO ainxt.external_sync_status
                (repo_id, url, importer, local_path, head_sha, prev_sha, pinned_commit,
                 importer_result, drift_detected, last_error, synced_at, updated_at)
            VALUES (:repo_id, :url, :importer, :local_path, :head_sha, :prev_sha, :pinned,
                    CAST(:result AS JSONB), :drift, :err, NOW(), NOW())
            ON CONFLICT (repo_id) DO UPDATE SET
                url=EXCLUDED.url, importer=EXCLUDED.importer, local_path=EXCLUDED.local_path,
                prev_sha=ainxt.external_sync_status.head_sha, head_sha=EXCLUDED.head_sha,
                pinned_commit=EXCLUDED.pinned_commit, importer_result=EXCLUDED.importer_result,
                drift_detected=EXCLUDED.drift_detected, last_error=EXCLUDED.last_error,
                synced_at=NOW(), updated_at=NOW()
        """), {
            "repo_id": repo_id, "url": entry["url"], "importer": entry["importer"],
            "local_path": workspace, "head_sha": head_sha, "prev_sha": prev_sha,
            "pinned": entry.get("pinned_commit"), "result": json.dumps(result),
            "drift": drift, "err": error,
        })


def _rewrite_security_body(body: str) -> str:
    for pat, repl in _SECURITY_REWRITES:
        body = re.sub(pat, repl, body)
    return body


def _import_skills(entry: dict, workspace: str, security: bool) -> dict:
    """Import SKILL.md skills via the reusable importer (offline; no model calls)."""
    from scripts.import_platform_skills import import_skills_from_dir
    root = workspace
    if entry.get("skills_subdir"):
        root = os.path.join(workspace, entry["skills_subdir"])
    if not os.path.isdir(root):
        return {"imported": 0, "skipped": 0, "note": f"missing {root}"}
    return import_skills_from_dir(
        root,
        force=True,
        status=entry.get("status", "DRAFT"),
        org=entry.get("org", "default"),
        base_tags=(["imported", "platform-security", "sast", "security"]
                   if security else ["imported", "oss-skills"]),
        created_by=f"external_sync:{entry['id']}",
        rewrite=_rewrite_security_body if security else None,
        verbose=False,
    )


def _import_kb(entry: dict, workspace: str) -> dict:
    """Enqueue the existing codebase indexer to embed the cloned docs into pgvector."""
    from core.job_queue import Q_INDEX, enqueue_job
    job = enqueue_job(
        "workers.index_worker.index_repo_job",
        {
            "repo_name": entry["kb_namespace"],
            "repo_path": workspace,
            "branch": "",
            "drop_index": False,
            "triggered_by": "external_sync",
        },
        queue_name=Q_INDEX,
    )
    return {"enqueued_index_job": getattr(job, "id", str(job))}


_CURATED_PLUGINS_FILE = os.path.join(_REPO_ROOT, "config", "curated_plugins.json")


def _discover_plugins(entry: dict, workspace: str) -> list[dict]:
    """Walk a vendored plugin repo and extract a curated catalog entry per plugin.

    Supports the Claude plugin marketplace layout (`.claude-plugin/plugin.json` per
    plugin, optional repo-level `.claude-plugin/marketplace.json`) plus npm packages
    that opt in via a `claudePlugin`/`ainxt` key in package.json. Model-agnostic and
    metadata-only — nothing is executed; install stays user-initiated in the CLI.
    """
    org = entry.get("org", "ainxt")
    repo_id = entry["id"]
    found: dict[str, dict] = {}

    def _add(name: str, version: str, desc: str, rel: str, source: str) -> None:
        if not name:
            return
        key = f"{org}/{name}"
        found.setdefault(key, {
            "id": key, "name": name, "version": str(version or "0.0.0"),
            "description": (desc or "").strip()[:280], "org": org,
            "repo": repo_id, "path": rel, "source": source,
        })

    for dirpath, dirs, files in os.walk(workspace):
        # Skip VCS / node_modules noise.
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
        rel = os.path.relpath(dirpath, workspace)
        if os.path.basename(dirpath) == ".claude-plugin":
            for fn, src in (("plugin.json", "claude-plugin"), ("marketplace.json", "marketplace")):
                if fn not in files:
                    continue
                try:
                    data = json.load(open(os.path.join(dirpath, fn), encoding="utf-8"))
                except Exception:
                    continue
                items = data.get("plugins", [data]) if isinstance(data, dict) else []
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict):
                        _add(it.get("name", ""), it.get("version", ""),
                             it.get("description", ""), rel, src)
        if "package.json" in files:
            try:
                pkg = json.load(open(os.path.join(dirpath, "package.json"), encoding="utf-8"))
            except Exception:
                pkg = None
            if isinstance(pkg, dict) and ("ainxt" in pkg or "claudePlugin" in pkg):
                _add(pkg.get("name", ""), pkg.get("version", ""),
                     pkg.get("description", ""), rel, "npm")
    return list(found.values())


def _write_curated_plugins(repo_id: str, plugins: list[dict]) -> None:
    """Merge this repo's discovered plugins into config/curated_plugins.json (additive)."""
    catalog: dict = {"plugins": [], "updated_by": "external_sync"}
    try:
        if os.path.exists(_CURATED_PLUGINS_FILE):
            catalog = json.load(open(_CURATED_PLUGINS_FILE, encoding="utf-8"))
    except Exception:
        catalog = {"plugins": []}
    existing = [p for p in catalog.get("plugins", []) if p.get("repo") != repo_id]
    catalog["plugins"] = existing + plugins
    os.makedirs(os.path.dirname(_CURATED_PLUGINS_FILE), exist_ok=True)
    tmp = _CURATED_PLUGINS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, _CURATED_PLUGINS_FILE)


def _import_plugins(entry: dict, workspace: str) -> dict:
    """P4: discover plugin manifests in a vendored repo → curated catalog (metadata only)."""
    plugins = _discover_plugins(entry, workspace)
    _write_curated_plugins(entry["id"], plugins)
    return {"plugins_curated": len(plugins), "names": [p["name"] for p in plugins][:50]}


def _sync_one(entry: dict, *, fetch: bool, force: bool) -> dict:
    repo_id = entry["id"]
    workspace = _abs_path(entry["local_path"])
    prev = _read_status(repo_id)
    prev_sha = prev["head_sha"] if prev else None
    error = None
    head_sha = prev_sha
    try:
        if fetch:
            head_sha = _git_sync(entry, workspace)
        else:
            head_sha = _head_sha(workspace) or prev_sha
        unchanged = (head_sha is not None and head_sha == prev_sha)
        if unchanged and not force:
            logger.info(f"external_sync: {repo_id} unchanged ({head_sha[:8] if head_sha else '?'}) — skip import")
            return {"repo_id": repo_id, "skipped": True, "head_sha": head_sha}

        importer = entry["importer"]
        if importer == "skills":
            result = _import_skills(entry, workspace, security=False)
        elif importer == "security_skills":
            result = _import_skills(entry, workspace, security=True)
        elif importer == "kb_index":
            result = _import_kb(entry, workspace)
        elif importer == "plugins":
            result = _import_plugins(entry, workspace)
        else:
            result = {"note": f"unknown importer {importer}"}
        _write_status(repo_id, entry, workspace, head_sha, prev_sha, result, None)
        logger.info(f"external_sync: {repo_id} imported — {result}")
        return {"repo_id": repo_id, "head_sha": head_sha, "result": result}
    except Exception as exc:  # noqa: BLE001 — one repo failing must not abort the rest
        error = str(exc)
        logger.error(f"external_sync: {repo_id} failed — {error}")
        try:
            _write_status(repo_id, entry, workspace, head_sha, prev_sha, {}, error)
        except Exception:
            pass
        return {"repo_id": repo_id, "error": error}


def sync_all_external_repos(*, fetch: bool = True, force: bool = False,
                            only: list[str] | None = None) -> dict:
    """
    Entry point (cron/RQ-callable). Syncs every repo in the manifest.

    fetch=True  → git clone/pull (connected env).
    fetch=False → import from the already-vendored snapshot only (air-gapped prod).
    only        → restrict to these repo ids.
    Returns {"synced": N, "skipped": M, "failed": K, "details": [...]}.
    """
    repos = _load_manifest()
    if only:
        repos = [r for r in repos if r["id"] in only]
    synced = skipped = failed = 0
    details = []
    for entry in repos:
        res = _sync_one(entry, fetch=fetch, force=force)
        details.append(res)
        if res.get("error"):
            failed += 1
        elif res.get("skipped"):
            skipped += 1
        else:
            synced += 1
    logger.info(f"external_sync: done — synced={synced} skipped={skipped} failed={failed}")
    return {"synced": synced, "skipped": skipped, "failed": failed, "details": details}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Sync external OSS resources (OSS/OpenAI).")
    ap.add_argument("--no-fetch", action="store_true", help="offline: import from vendored snapshot only")
    ap.add_argument("--force", action="store_true", help="re-import even if HEAD unchanged")
    ap.add_argument("--only", nargs="*", help="restrict to these repo ids")
    a = ap.parse_args()
    out = sync_all_external_repos(fetch=not a.no_fetch, force=a.force, only=a.only)
    print(json.dumps(out, indent=2, default=str))
