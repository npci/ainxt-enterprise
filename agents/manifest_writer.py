# SPDX-License-Identifier: Apache-2.0
"""
Manifest auto-update module for multi-repo SDLC pipeline (Phase 6).

When the user modifies the dependency table at trigger time (diverging from
what .sdlc.yml declared), this module detects that divergence and opens a
follow-up MR against the primary repo's .sdlc.yml dependencies: block so
future runs pick up the change without re-entering deps in the UI.

Public entry point:
    propose_manifest_update(run_id: str) -> str | None

Returns:
    - MR URL if a manifest divergence was detected and an MR was opened
    - None if (1) manifest and run_repos match, (2) any error, or
      (3) MR already exists (409 idempotency)

This is a best-effort ergonomic feature — all errors are logged but never
raised; the run is never blocked even if manifest update fails.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def propose_manifest_update(run_id: str) -> Optional[str]:
    """
    Detect manifest divergence and open a follow-up MR if needed.

    Args:
        run_id: SDLC run identifier

    Returns:
        MR URL (str) if MR was successfully created, None otherwise.

    On any error, logs a warning and returns None (never raises).
    """
    try:
        return _propose_manifest_update_impl(run_id)
    except Exception as exc:
        logger.warning(f"manifest_writer: proposal failed for run {run_id}: {exc}")
        return None


def _propose_manifest_update_impl(run_id: str) -> Optional[str]:
    from store.sdlc_store import list_run_repos

    repos = list_run_repos(run_id)
    if not repos:
        logger.warning(f"manifest_writer: no repos found for run {run_id}")
        return None

    primary_repo = _find_primary_repo(repos)
    if not primary_repo:
        logger.warning(f"manifest_writer: no primary repo found in run {run_id}")
        return None

    primary_branch = primary_repo.get("ref")
    repo_path = primary_repo.get("repo")
    if not repo_path or not primary_branch:
        logger.warning(
            f"manifest_writer: primary repo incomplete for run {run_id}: "
            f"repo={repo_path!r}, ref={primary_branch!r}"
        )
        return None

    manifest_deps = _read_manifest_deps(repo_path, primary_branch)
    run_deps = _build_run_deps_list(repos)

    if _deps_match(manifest_deps, run_deps):
        logger.info(f"manifest_writer: manifest matches run_repos for {run_id} — no MR needed")
        return None

    return _open_manifest_mr(run_id, repo_path, primary_branch, manifest_deps, run_deps)


def _find_primary_repo(repos: list) -> Optional[dict]:
    for repo_dict in repos:
        if repo_dict.get("kind") == "primary":
            return repo_dict
    return None


def _read_manifest_deps(repo_path: str, branch: str) -> list:
    """
    Read .sdlc.yml from repo and extract dependencies: block.

    Returns:
        List of dicts matching the manifest schema (or [] if absent/invalid).
    """
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_read_file as _scm_read_file
        else:
            from tools.gitlab_tools import gitlab_read_file as _scm_read_file
        import yaml

        content = _scm_read_file(repo_path, ".sdlc.yml", branch)
        if not content or content.startswith("[Error"):
            logger.debug(
                f"manifest_writer: .sdlc.yml read failed for {repo_path}@{branch}: {content}"
            )
            return []

        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            logger.debug(f"manifest_writer: .sdlc.yml in {repo_path} is not a mapping")
            return []

        deps_block = data.get("dependencies")
        if not deps_block:
            return []
        if not isinstance(deps_block, list):
            logger.warning(
                f"manifest_writer: .sdlc.yml dependencies in {repo_path} is not a list"
            )
            return []

        return deps_block

    except Exception as exc:
        logger.warning(
            f"manifest_writer: could not read manifest from {repo_path}@{branch}: {exc}"
        )
        return []


def _build_run_deps_list(repos: list) -> list:
    """
    Build the list of dependencies as they were used in the run.

    Excludes primary repo. Each entry is a dict with:
        repo, ref, kind, build_order (or None)
    """
    deps = []
    for repo_dict in repos:
        if repo_dict.get("kind") == "primary":
            continue
        deps.append({
            "repo": repo_dict.get("repo"),
            "ref": repo_dict.get("ref"),
            "kind": repo_dict.get("kind"),
            "build_order": repo_dict.get("build_order"),
        })
    return deps


def _deps_match(manifest_deps: list, run_deps: list) -> bool:
    """
    Check if manifest and run deps are identical.

    Compares sets of (repo, ref, kind, build_order) tuples after normalization.
    """
    def _normalize(dep_list: list) -> set:
        return {
            (d.get("repo"), d.get("ref"), d.get("kind"), d.get("build_order"))
            for d in dep_list
        }

    return _normalize(manifest_deps) == _normalize(run_deps)


def _open_manifest_mr(
    run_id: str,
    repo_path: str,
    primary_branch: str,
    manifest_deps: list,
    run_deps: list,
) -> Optional[str]:
    """
    Open an MR with the updated manifest.

    1. Check for existing branch/MR (idempotency)
    2. Compose new manifest YAML
    3. Create branch
    4. Write .sdlc.yml
    5. Create MR
    """
    run_id_short = run_id[:8]
    branch_name = f"sdlc-manifest-update/{run_id_short}"

    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import (
                github_read_file as gitlab_read_file,
                github_create_branch as gitlab_create_branch,
                github_create_or_update_file as gitlab_create_or_update_file,
                github_create_pr as gitlab_create_mr,
            )
            gitlab_list_mrs = None  # not used in the code path below
        else:
            from tools.gitlab_tools import (
                gitlab_read_file,
                gitlab_list_mrs,
                gitlab_create_branch,
                gitlab_create_or_update_file,
                gitlab_create_mr,
            )

        existing_mr = _find_existing_branch_mr(repo_path, branch_name)
        if existing_mr:
            logger.info(
                f"manifest_writer: MR already exists for {branch_name} in {repo_path}"
            )
            return existing_mr.get("web_url")

        raw_manifest = gitlab_read_file(repo_path, ".sdlc.yml", primary_branch)
        if raw_manifest.startswith("[Error"):
            raw_manifest = ""

        updated_yaml = _compose_manifest_yaml(raw_manifest, run_deps)

        logger.info(
            f"manifest_writer: creating branch {branch_name} in {repo_path} "
            f"from {primary_branch}"
        )
        gitlab_create_branch(repo_path, branch_name, from_branch=primary_branch)

        logger.info(f"manifest_writer: writing updated .sdlc.yml to {branch_name}")
        gitlab_create_or_update_file(
            repo_path,
            ".sdlc.yml",
            updated_yaml,
            f"Update .sdlc.yml dependencies (from SDLC run {run_id_short})",
            branch=branch_name,
        )

        title = f"Update .sdlc.yml dependencies (from SDLC run {run_id_short})"
        body = _compose_mr_body(run_id_short, manifest_deps, run_deps)

        logger.info(f"manifest_writer: creating MR {title}")
        mr_result = gitlab_create_mr(
            repo_path,
            title,
            body,
            head=branch_name,
            base=primary_branch,
        )

        if "[Error" in mr_result:
            logger.warning(f"manifest_writer: MR creation failed: {mr_result}")
            return None

        mr_url = _extract_mr_url(mr_result)
        logger.info(f"manifest_writer: manifest MR opened: {mr_url}")
        return mr_url

    except Exception as exc:
        logger.warning(
            f"manifest_writer: failed to open manifest MR for {run_id}: {exc}"
        )
        return None


def _find_existing_branch_mr(repo_path: str, branch_name: str) -> Optional[dict]:
    """
    Check if an MR already exists for the given branch.

    Returns the MR dict if found, None otherwise.
    """
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import _find_existing_pr as _find_existing_mr
        else:
            from tools.gitlab_tools import _find_existing_mr

        return _find_existing_mr(repo_path, branch_name)
    except Exception as exc:
        logger.debug(f"manifest_writer: could not check for existing MR: {exc}")
        return None


def _compose_manifest_yaml(existing_yaml: str, run_deps: list) -> str:
    """
    Compose an updated .sdlc.yml with the new dependencies: block.

    Preserves all other top-level keys from the existing manifest (or creates
    a minimal one if absent).

    Note: Uses yaml.safe_load + yaml.safe_dump, which preserves order in
    Python 3.7+ but does not preserve comments or exact formatting.
    """
    try:
        import yaml

        if existing_yaml:
            data = yaml.safe_load(existing_yaml)
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}

        data["dependencies"] = run_deps

        return yaml.safe_dump(
            data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    except Exception as exc:
        logger.warning(f"manifest_writer: YAML composition failed: {exc}")
        return ""


def _compose_mr_body(run_id_short: str, manifest_deps: list, run_deps: list) -> str:
    """
    Compose the MR description explaining what changed.

    Lists added repos, removed repos, and any kind/ref/build_order changes.
    """
    manifest_set = {d.get("repo") for d in manifest_deps}
    run_set = {d.get("repo") for d in run_deps}

    added = run_set - manifest_set
    removed = manifest_set - run_set
    modified = []

    manifest_map = {d.get("repo"): d for d in manifest_deps}
    run_map = {d.get("repo"): d for d in run_deps}

    for repo in manifest_set & run_set:
        m = manifest_map[repo]
        r = run_map[repo]
        changes = []
        if m.get("ref") != r.get("ref"):
            changes.append(f"ref: {m.get('ref')!r} → {r.get('ref')!r}")
        if m.get("kind") != r.get("kind"):
            changes.append(f"kind: {m.get('kind')!r} → {r.get('kind')!r}")
        if m.get("build_order") != r.get("build_order"):
            changes.append(f"build_order: {m.get('build_order')} → {r.get('build_order')}")
        if changes:
            modified.append((repo, changes))

    lines = [
        f"## Manifest Update from SDLC Run",
        f"",
        f"Run ID: {run_id_short}",
        f"",
        f"This MR updates the `.sdlc.yml` dependencies block to match the as-built "
        f"dependencies used in the SDLC run.",
        f"",
    ]

    if added:
        lines.append("### Added dependencies")
        for repo in sorted(added):
            r = run_map[repo]
            lines.append(f"- **{repo}** (`{r.get('kind')}`, ref: `{r.get('ref')}`)")
        lines.append("")

    if removed:
        lines.append("### Removed dependencies")
        for repo in sorted(removed):
            lines.append(f"- **{repo}**")
        lines.append("")

    if modified:
        lines.append("### Modified dependencies")
        for repo, changes in sorted(modified):
            lines.append(f"- **{repo}**")
            for change in changes:
                lines.append(f"  - {change}")
        lines.append("")

    lines.append(
        "Auto-generated by the SDLC manifest-update module (Phase 6). "
        "No manual edits needed."
    )

    return "\n".join(lines)


def _extract_mr_url(mr_result: str) -> Optional[str]:
    """
    Extract the MR URL from the result string.

    Expected format: "MR created: <url> (!<iid>)" or similar.
    """
    if "http" not in mr_result:
        return None
    parts = mr_result.split()
    for part in parts:
        if part.startswith("http"):
            return part
    return None
