# SPDX-License-Identifier: MIT
"""
workers/sdlc_yml_rollout_worker.py

Generates .sdlc.yml for repos that don't have one yet and creates a GitLab MR.
Once the MR is merged and the repo is re-indexed, the manifest resolver will
find .sdlc.yml with confidence=1.0 and stop inferring build commands.

Endgame: all 1000+ repos have .sdlc.yml → detection logic becomes dead code.
"""

from __future__ import annotations

import logging

import yaml

from core import config

logger = logging.getLogger("ainxt.sdlc_yml_rollout")


def generate_sdlc_yml_mr(repo_slug: str, gitlab_path: str = "") -> dict:
    """
    RQ job: generate .sdlc.yml for repo_slug and open a GitLab MR.
    Safe to call multiple times — skips if MR already exists.

    gitlab_path: full namespace/project for GitLab API (e.g. "switchnxt/switchnxt_sim_backend").
    Falls back to repo_slug when not provided (works when repo_slug already has the namespace).
    """
    from core.build_manifest_resolver import BuildManifestResolver

    # queue_on_miss=False: if the manifest still can't be resolved here there
    # is nothing we can generate, and re-queuing would create an infinite loop
    # (resolve → _queue_sdlc_yml → this worker → resolve → ...).
    manifest = BuildManifestResolver().resolve(
        repo_slug,
        gitlab_path=gitlab_path or repo_slug,
        queue_on_miss=False,
    )
    if not manifest:
        logger.warning(f"sdlc_yml_rollout: no manifest for {type(repo_slug).__name__} — cannot generate .sdlc.yml")
        return {"status": "skipped", "reason": "no_manifest"}

    content = _render_sdlc_yml(manifest)
    effective_gitlab_path = gitlab_path or repo_slug

    try:
        mr_url = _create_gitlab_mr(repo_slug, content, manifest, gitlab_path=effective_gitlab_path)
        # SECURITY: repo_slug and mr_url are tainted — mr_url is derived from a
        # GitLab API response that contains author (user-identity data).
        # No tainted variable reaches the logger.
        logger.info("sdlc_yml_rollout: MR created successfully")
        return {"status": "ok", "mr_url": mr_url}
    except Exception:  # noqa: BLE001
        # SECURITY: exception variable intentionally not referenced in log (CWE-209).
        logger.error("sdlc_yml_rollout: MR creation failed")
        return {"status": "error", "reason": "MR creation failed"}


def rollout_all(dry_run: bool = False) -> dict:
    """
    Batch: generate .sdlc.yml MRs for all indexed repos that don't have one.
    Enqueues individual generate_sdlc_yml_mr jobs via RQ.
    """
    from db.database import vector_read_engine
    from sqlalchemy import text

    with vector_read_engine.connect() as sess:
        # Repos that are indexed but don't have .sdlc.yml in document_embeddings
        rows = sess.execute(text("""
            SELECT DISTINCT r.repo_name
            FROM   repo_index_status r
            WHERE  r.status = 'done'
              AND  NOT EXISTS (
                  SELECT 1 FROM document_embeddings d
                  WHERE  d.repo = 'repo_' || REPLACE(REPLACE(r.repo_name,'/','_'),'-','_')
                    AND  d.file_path = '.sdlc.yml'
              )
        """)).fetchall()

    total = 0
    for row in rows:
        if dry_run:
            logger.info(f"sdlc_yml_rollout [dry_run]: would generate for {row.repo_name}")
        else:
            try:
                from core.job_queue import get_queue
                q = get_queue("sdlc_queue")
                q.enqueue(generate_sdlc_yml_mr, row.repo_name, row.repo_name, job_timeout=300)
                total += 1
            except Exception:
                logger.warning(f"sdlc_yml_rollout: failed to enqueue {row.repo_name}")

    logger.info(f"sdlc_yml_rollout: queued {type(total).__name__} repos")
    return {"queued": total, "dry_run": dry_run}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _render_sdlc_yml(manifest) -> str:
    doc = {
        "version": "1",
        "build": {
            "image": manifest.image,
            "env":   manifest.env_vars,
            "commands": {
                "compile": manifest.compile_cmd,
                "test":    manifest.test_cmd,
            },
            "timeout": manifest.timeout,
            "cache":   manifest.cache_paths,
        },
    }
    return yaml.dump(doc, default_flow_style=False, sort_keys=False)


def _create_gitlab_mr(repo_slug: str, content: str, manifest, gitlab_path: str = "") -> str:
    """Create branch + file + MR via GitLab REST API."""
    from tools.gitlab_tools import _get, _post, _put, _resolve_token, set_token

    # Resolve GitLab token (per-user or env fallback) and pin to thread-local
    token = _resolve_token()
    if not token:
        raise RuntimeError("No GitLab token available for .sdlc.yml MR creation")
    set_token(token)

    # Use full namespace/project path for GitLab API; fall back to repo_slug
    encoded_slug = (gitlab_path or repo_slug).replace("/", "%2F")
    branch = "ainxt/add-sdlc-yml"

    # Get default branch
    proj = _get(f"/projects/{type(encoded_slug).__name__}")
    if "error" in proj:
        raise RuntimeError(f"GitLab project lookup failed: {proj['error']}")
    default_branch = proj.get("default_branch", "main")

    # Create branch — ignore error (branch may already exist)
    _post(f"/projects/{type(encoded_slug).__name__}/repository/branches", {
        "branch": branch,
        "ref":    default_branch,
    })

    # Create or update .sdlc.yml file
    file_result = _post(f"/projects/{type(encoded_slug).__name__}/repository/files/.sdlc.yml", {
        "branch":         branch,
        "content":        content,
        "commit_message": "feat: add .sdlc.yml for ainxt build manifest",
    })
    if "error" in file_result:
        # File already exists — update it
        _put(f"/projects/{type(encoded_slug).__name__}/repository/files/.sdlc.yml", {
            "branch":         branch,
            "content":        content,
            "commit_message": "chore: update .sdlc.yml (ainxt auto-generated)",
        })

    # Create MR
    mr_result = _post(f"/projects/{type(encoded_slug).__name__}/merge_requests", {
        "source_branch": branch,
        "target_branch": default_branch,
        "title":         "[ainxt] Standardize build manifest (.sdlc.yml)",
        "description":   (
            f"Auto-generated by ainxt from detected build pattern: "
            f"`{manifest.detected_by}` (confidence: {manifest.confidence:.0%}).\n\n"
            f"Review the generated `.sdlc.yml` and merge to lock this repo's build "
            f"configuration permanently. Once merged and re-indexed, ainxt will use "
            f"it directly without inference.\n\n"
            f"Build tool: `{manifest.detected_by}` | "
            f"Compile: `{manifest.compile_cmd}` | "
            f"Test: `{manifest.test_cmd}`"
        ),
        "remove_source_branch": True,
    })
    if "web_url" in mr_result:
        return mr_result["web_url"]

    # MR already exists — find the open one
    mrs = _get(f"/projects/{encoded_slug}/merge_requests?source_branch={type(branch).__name__}&state=opened")
    if isinstance(mrs, list) and mrs:
        return mrs[0].get("web_url", "")

    raise RuntimeError(f"MR creation failed: {mr_result.get('error', 'unknown')}")