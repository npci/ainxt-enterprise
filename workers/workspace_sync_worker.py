# SPDX-License-Identifier: Apache-2.0
"""
workers/workspace_sync_worker.py

Nightly workspace sync — keeps /opt/ainxt/workspaces/{repo}/ fresh via git.
Scheduled via RQ cron at 2 AM.  No containers are rebuilt.

Also handles:
  build_dep_repo()  — called by AiNxtDependencyResolver when a dep is missing
                      from Nexus.  Clones, compiles, and publishes the dep.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from core import config
from core.logger import logger

# Repos not accessed in this many days will have their workspace evicted
_EVICT_AFTER_DAYS = 30


def sync_all_workspaces() -> dict:
    """
    RQ job: fetch + reset --hard for every indexed repo.
    Meant to run nightly via RQ scheduler.
    """
    from db.database import engine
    from sqlalchemy import text

    with engine.connect() as sess:
        rows = sess.execute(text("""
            SELECT r.repo_name, r.git_url, r.branch,
                   COALESCE(r.workspace_synced_at, NOW() - INTERVAL '999 days') AS last_sync
            FROM   repo_index_status r
            WHERE  r.status = 'done'
            ORDER  BY last_sync ASC
        """)).fetchall()

    synced = 0
    failed = 0

    for row in rows:
        repo_slug = row.repo_name
        clone_url = row.git_url
        branch    = row.branch or "main"

        try:
            sync_workspace(repo_slug, clone_url, branch)
            synced += 1
        except Exception as exc:
            logger.error(f"workspace_sync: failed for {repo_slug}: {exc}")
            failed += 1

    logger.info(f"workspace_sync: done — synced={synced} failed={failed}")
    return {"synced": synced, "failed": failed}


def sync_workspace(repo_slug: str, clone_url: str, branch: str = "main") -> str:
    """
    Ensure /workspaces/{repo_slug}/ is up to date with origin/{branch}.
    Returns the local workspace path.
    """
    workspace = os.path.join(config.BUILDER_WORKSPACE_ROOT, repo_slug)
    Path(workspace).mkdir(parents=True, exist_ok=True)

    git_dir = os.path.join(workspace, ".git")

    # repo_index_status.git_url no longer carries credentials (they are stripped on
    # store — see index_worker). This is a background/service context (nightly sync,
    # dep builder) with no per-user identity, so authenticate the clone with the
    # service-account GITLAB_TOKEN env. strip+inject is idempotent if a token is
    # somehow still present.
    from core.platform_credentials import build_run_clone_url as _build_clone_url
    clone_url = _build_clone_url(clone_url)

    if not os.path.isdir(git_dir):
        # Fresh clone
        logger.info(f"workspace_sync: cloning {repo_slug} → {workspace}")
        _run(["git", "clone", "--depth=100", "--branch", branch, clone_url, workspace])
    else:
        # Fetch and reset to remote HEAD — discard any leftover modifications
        logger.info(f"workspace_sync: syncing {repo_slug} branch={branch}")
        _run(["git", "-C", workspace, "fetch", "--all", "--prune"], check=False)
        _run(["git", "-C", workspace, "reset", "--hard", f"origin/{branch}"])
        _run(["git", "-C", workspace, "clean", "-fdx", "--exclude=.mvn/"], check=False)

    # Update last-sync timestamp in DB
    _update_sync_time(repo_slug)

    logger.info(f"workspace_sync: {repo_slug} ready at {workspace}")
    return workspace


def build_dep_repo(dep_repo_slug: str, reason: str = "") -> None:
    """
    Called by AiNxtDependencyResolver when a dep is not in Nexus.
    1. Sync workspace
    2. Compile + install to local .m2
    3. Deploy to Nexus (if AiNxt_NEXUS_URL is set)
    """
    from db.database import engine
    from sqlalchemy import text

    logger.info(f"workspace_sync: building dep {dep_repo_slug} — {reason}")

    # Get clone URL. repo_index_status is keyed by the indexer's normalized slug
    # (routers/index_router._extract_repo_name: last path segment, lowercase,
    # hyphens/dots → underscores), so a dep passed as a namespace path or with
    # hyphens/dots ('nts/nts', 'nts-2.0') must be normalized for the lookup or it
    # misses an indexed repo. Try the normalized slug first, raw second, both
    # requiring a non-null git_url. The workspace path stays on the caller's
    # dep_repo_slug so callers still find it under that directory name.
    from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
    _canon_dep = _nrik(dep_repo_slug)
    row = None
    for _slug in (_canon_dep, dep_repo_slug):
        if not _slug:
            continue
        with engine.connect() as sess:
            _cand = sess.execute(text(
                "SELECT git_url, branch FROM repo_index_status WHERE repo_name=:slug"
            ), {"slug": _slug}).fetchone()
        if _cand and _cand.git_url:
            row = _cand
            break
    if not row or not row.git_url:
        logger.warning(
            f"workspace_sync: dep repo {dep_repo_slug} not indexed or missing git_url "
            f"(also tried normalized slug '{_canon_dep}') — cannot build"
        )
        return

    workspace = sync_workspace(dep_repo_slug, row.git_url, row.branch or "main")

    # Compile and install to shared Maven cache
    from core.build_manifest_resolver import BuildManifestResolver
    from sandbox.workspace_builder import WorkspaceBuilder

    manifest = BuildManifestResolver().resolve(dep_repo_slug, workspace_path=workspace)
    if not manifest:
        logger.warning(f"workspace_sync: no manifest for {dep_repo_slug} — cannot build")
        return

    builder = WorkspaceBuilder()
    result  = builder.compile(manifest, sdlc_run_id=f"dep_{dep_repo_slug}_{int(time.time())}")

    if result.status == "BUILD_SUCCESS":
        logger.info(f"workspace_sync: dep {dep_repo_slug} compiled successfully")
        # mvn deploy if Nexus URL is configured
        if config.AiNxt_NEXUS_URL and "jvm" in manifest.image:
            _deploy_to_nexus(workspace, manifest)
    else:
        logger.error(
            f"workspace_sync: dep {dep_repo_slug} build FAILED: {result.status} — {result.output_tail[:500]}"
        )


def prepare_run_workspace(
    run_id: str,
    repo_slug: str,
    clone_url: str,
    branch: str,
    pin_sha: str = "",
    reuse: bool = False,
    resume_in_place: bool = False,
) -> str:
    """
    Materialize a per-run workspace at /opt/ainxt/workspaces/runs/{run_id}_{repo_slug}/
    by cloning the working branch fresh from origin. Mirrors what a developer
    does locally — `git clone -b <feature-branch>` — so build/test results are
    reproducible against the same checkout.

    Each SDLC run gets its own workspace so that:
      - leftover modifications from prior runs cannot pollute this build
      - concurrent runs on the same repo cannot collide
      - the final build/test reflects exactly what `git pull <branch>` would yield

    pin_sha (optional): after clone, hard-checkout this exact commit so that every
      stage / gateway instance that materializes this run sees byte-identical code —
      closing the "different code pulled at different times" gap when a run is picked
      up by a different instance after an HITL gate. Best-effort: a --depth=50 clone
      may not contain the commit, so it is fetched first; on failure the branch tip is
      kept (logged, non-fatal).
    reuse (optional): if a checkout for this run is already present on local disk AND
      matches pin_sha, restore it to its pristine pinned state (cheap local reset, no
      network clone) and reuse it instead of wiping + re-cloning. A different instance
      simply finds nothing on its disk and clones — so reuse is a same-instance
      optimization, never a correctness dependency.
    resume_in_place (optional): when True and the run workspace already exists on
      local disk, return it AS-IS — skip both the `_restore_pristine` reset and the
      `_force_remove_dir` wipe — so uncommitted in-progress edits (e.g. an IMPLEMENT
      session that hit its turn cap) survive for a `--resume` continuation. Takes
      precedence over `reuse`/`pin_sha` and never runs git. If the dir does NOT exist
      (picked up on a different instance / evicted), falls through to the normal
      fresh clone below — degraded (files gone), but a resumed CLI session still
      recalls its own context and re-writes, so this stays functional. Default False
      so every existing caller is unchanged.
    """
    runs_root = os.path.join(config.BUILDER_WORKSPACE_ROOT, "runs")
    Path(runs_root).mkdir(parents=True, exist_ok=True)

    workspace = os.path.join(runs_root, f"{run_id}_{repo_slug}")

    # Vendored multi-repo dep checkouts (agents/multi_repo_workspace.py) live INSIDE
    # the primary workspace at <workspace>/.sdlc_deps/ — that is the only location the
    # workspace-jailed headless CLI can read them from — yet they are git-excluded so
    # they never enter the customer's diff. A blind wipe below therefore deletes deps
    # that were cloned (and possibly mvn-installed) at preflight, and the primary-only
    # re-clone does NOT bring them back. The deps would then have to be re-cloned by
    # prepare_and_install_deps, which fails when the worker holds no per-user GitLab
    # token (baseline-resume / skip-compile path). Preserving them across the wipe
    # keeps both checkouts intact and sidesteps the re-clone entirely.
    _preserved_deps = None   # abs path to a detached .sdlc_deps/, restored after re-clone
    if os.path.isdir(workspace):
        if resume_in_place:
            logger.info(
                "workspace_sync: resume-in-place — reusing existing run workspace AS-IS "
                f"(no reset/clone) at {workspace}"
            )
            return workspace
        if reuse and workspace_is_reusable(workspace, pin_sha):
            _restore_pristine(workspace, pin_sha)
            logger.info(
                f"workspace_sync: reusing present run workspace {workspace} "
                f"(pinned to {pin_sha[:8] if pin_sha else 'HEAD'}) — skipped re-clone"
            )
            return workspace
        # Idempotency: a retry / non-matching checkout — wipe to guarantee clean state,
        # but first detach .sdlc_deps/ so the vendored dep checkouts survive the wipe.
        _preserved_deps = _detach_sdlc_deps(workspace)
        _force_remove_dir(workspace)

    if resume_in_place:
        logger.info(
            "workspace_sync: resume-in-place requested but no local workspace present "
            f"for {workspace} — cloning fresh (session --resume will recall context)"
        )

    logger.info(
        f"workspace_sync: preparing run workspace {repo_slug}@{branch} → {workspace}"
    )
    try:
        _run(["git", "clone", "--depth=50", "--branch", branch, clone_url, workspace])
    except subprocess.CalledProcessError as exc:
        # Branch may not yet exist on origin (race with branch creation) — fall back to default
        logger.warning(
            f"workspace_sync: clone of branch '{branch}' failed ({exc.stderr or exc}); "
            f"falling back to default-branch clone"
        )
        _run(["git", "clone", "--depth=50", clone_url, workspace])
        # Best-effort checkout of the requested branch if it appears later
        _run(["git", "-C", workspace, "fetch", "origin", branch], check=False)
        _run(["git", "-C", workspace, "checkout", branch], check=False)

    if pin_sha:
        _checkout_pinned_sha(workspace, pin_sha)

    if _preserved_deps:
        _reattach_sdlc_deps(workspace, _preserved_deps)

    return workspace


def _git_head(workspace: str) -> str:
    """Return the current HEAD commit SHA of a checkout, or '' on any error."""
    try:
        r = _run(["git", "-C", workspace, "rev-parse", "HEAD"], check=False)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def workspace_is_reusable(workspace: str, pin_sha: str = "") -> bool:
    """True if an existing run checkout can be reused as-is.

    With a pin_sha, reuse is allowed only when the checkout's HEAD matches the pin
    exactly, so a reused checkout and a fresh clone are byte-identical. With no
    pin_sha, any valid git checkout is accepted. A directory without a .git is never
    reusable (a partial/aborted clone).
    """
    if not workspace or not os.path.isdir(os.path.join(workspace, ".git")):
        return False
    head = _git_head(workspace)
    if not head:
        return False
    if pin_sha:
        return head == pin_sha or head.startswith(pin_sha) or pin_sha.startswith(head)
    return True


def _checkout_pinned_sha(workspace: str, pin_sha: str) -> None:
    """Best-effort hard checkout of an exact commit so every materialization of this
    run sees identical code. The --depth=50 clone may not contain the commit, so it is
    fetched first. On failure the branch tip is left in place (logged, non-fatal)."""
    if _git_head(workspace) == pin_sha:
        return
    try:
        _run(["git", "-C", workspace, "fetch", "--depth=50", "origin", pin_sha], check=False)
        r = _run(["git", "-C", workspace, "checkout", "--force", pin_sha], check=False)
        if r.returncode != 0:
            logger.warning(
                f"workspace_sync: could not pin {workspace} to {pin_sha[:8]} "
                f"({(r.stderr or '').strip()[:200]}); keeping branch tip"
            )
        else:
            logger.info(f"workspace_sync: pinned {workspace} to {pin_sha[:8]}")
    except Exception as exc:
        logger.warning(f"workspace_sync: pin checkout failed for {workspace}: {exc}")


def _restore_pristine(workspace: str, pin_sha: str = "") -> None:
    """Restore a reused checkout to its pinned base: hard-reset tracked files and drop
    stray untracked files, while KEEPING gitignored build caches (target/, .gradle,
    node_modules) so reuse still benefits incremental builds. Far cheaper than a fresh
    network clone."""
    target = pin_sha or "HEAD"
    _run(["git", "-C", workspace, "reset", "--hard", target], check=False)
    _run(["git", "-C", workspace, "clean", "-fd"], check=False)


def cleanup_run_workspace(run_id: str, repo_slug: str) -> None:
    """Remove the per-run workspace once the run terminates. Best-effort."""
    runs_root = os.path.join(config.BUILDER_WORKSPACE_ROOT, "runs")
    workspace = os.path.join(runs_root, f"{run_id}_{repo_slug}")
    if not os.path.isdir(workspace):
        return
    try:
        _force_remove_dir(workspace)
        logger.info(f"workspace_sync: cleaned up run workspace {workspace}")
    except Exception as exc:
        logger.warning(f"workspace_sync: cleanup failed for {workspace}: {exc}")


def evict_stale_workspaces() -> dict:
    """Remove workspaces not synced in _EVICT_AFTER_DAYS days to free disk."""
    from db.database import engine
    from sqlalchemy import text

    with engine.connect() as sess:
        rows = sess.execute(text(f"""
            SELECT repo_name FROM repo_index_status
            WHERE  workspace_synced_at < NOW() - INTERVAL '{_EVICT_AFTER_DAYS} days'
               OR  workspace_synced_at IS NULL
        """)).fetchall()

    evicted = 0
    for row in rows:
        workspace = os.path.join(config.BUILDER_WORKSPACE_ROOT, row.repo_name)
        if os.path.isdir(workspace):
            import shutil
            try:
                shutil.rmtree(workspace)
                evicted += 1
                logger.info(f"workspace_sync: evicted stale workspace {row.repo_name}")
            except OSError as exc:
                logger.warning(f"workspace_sync: evict failed for {row.repo_name}: {exc}")

    return {"evicted": evicted}


# ── Internal helpers ───────────────────────────────────────────────────────────

_SDLC_DEPS_DIRNAME = ".sdlc_deps"


def _detach_sdlc_deps(workspace: str) -> str | None:
    """Move <workspace>/.sdlc_deps/ out to a sibling so it survives a workspace wipe.

    Returns the absolute path the deps were moved to (a sibling temp dir), or None
    when there is nothing to preserve. Best-effort: any failure logs a warning and
    returns None so the caller just proceeds with the wipe (deps get re-cloned by
    prepare_and_install_deps as before — degraded, not broken).

    A rename of the top-level dir needs write permission only on `workspace`
    itself, not on the (possibly chmod'd read-only) compile-only checkouts inside,
    so the read-only staging done by multi_repo_workspace.stage_deps_for_cli does
    not block the move.
    """
    import shutil
    src = os.path.join(workspace, _SDLC_DEPS_DIRNAME)
    if not os.path.isdir(src):
        return None
    keep = workspace.rstrip("/\\") + ".sdlc_deps.keep"
    try:
        # A stale keep-dir from an aborted prior run would make shutil.move nest
        # inside it — remove it first so the move lands exactly at `keep`.
        if os.path.exists(keep):
            _force_remove_dir(keep)
        shutil.move(src, keep)
        logger.info(f"workspace_sync: preserved {_SDLC_DEPS_DIRNAME}/ across wipe → {keep}")
        return keep
    except Exception as exc:
        logger.warning(
            f"workspace_sync: could not preserve {_SDLC_DEPS_DIRNAME}/ (deps will be "
            f"re-cloned): {exc}"
        )
        return None


def _reattach_sdlc_deps(workspace: str, preserved: str) -> None:
    """Move a previously detached .sdlc_deps/ back into the freshly re-cloned
    workspace and re-assert its git-exclude so a later `git add -A` can never stage
    the vendored dep trees into the customer's MR / VERIFIED_DIFF. Best-effort."""
    import shutil
    dest = os.path.join(workspace, _SDLC_DEPS_DIRNAME)
    try:
        if os.path.isdir(dest):
            # A fresh clone should not contain .sdlc_deps/ (it is git-excluded and
            # never committed), but guard anyway so the move can't nest/fail.
            _force_remove_dir(dest)
        shutil.move(preserved, dest)
        _reexclude_sdlc_deps(workspace)
        logger.info(f"workspace_sync: restored {_SDLC_DEPS_DIRNAME}/ into re-cloned workspace")
    except Exception as exc:
        logger.warning(
            f"workspace_sync: could not restore preserved {_SDLC_DEPS_DIRNAME}/ "
            f"(deps will be re-cloned): {exc}"
        )
        # Leave the preserved copy in place rather than deleting it — a later
        # detach overwrites it; deleting here would forfeit the only copy.


def _reexclude_sdlc_deps(workspace: str) -> None:
    """Append `.sdlc_deps/` to <workspace>/.git/info/exclude if not already present.

    The freshly re-cloned primary has a brand-new .git dir whose info/exclude does
    NOT carry the entry that multi_repo_workspace._make_workspace_dirs wrote on the
    original run, so re-assert it here. Line-based membership test mirrors
    multi_repo_workspace._exclude_pattern_present (a `#`-comment mentioning the
    pattern must not suppress the append)."""
    pattern = _SDLC_DEPS_DIRNAME + "/"
    try:
        info_dir = os.path.join(workspace, ".git", "info")
        if not os.path.isdir(info_dir):
            return
        exclude = os.path.join(info_dir, "exclude")
        existing = ""
        if os.path.isfile(exclude):
            with open(exclude, "r", encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        for line in existing.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and s == pattern:
                return  # already present
        with open(exclude, "a", encoding="utf-8") as fh:
            fh.write(("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")
    except Exception as exc:
        logger.warning(f"workspace_sync: re-exclude of {pattern!r} failed (non-fatal): {exc}")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=300, check=check
    )


def _force_remove_dir(path: str) -> None:
    """Remove a per-run workspace, including build artifacts the app user cannot
    delete on its own.

    The Dockerized builder (sandbox/workspace_builder.py) runs the compile/test
    container as root with the workspace bind-mounted rw, so generated dirs like
    target/ end up owned by root on the host. A plain shutil.rmtree by the (non-root)
    app/worker process then silently leaves those files behind — and the leftover,
    non-empty dir makes the next `git clone` here fail with
    'destination path already exists and is not an empty directory' (exit 128).

    Strategy: try a normal rmtree first; if anything survives, delete the residue
    with a throwaway root container (same privilege that created it). If residue
    STILL remains, raise so the failure is explicit rather than a cryptic git-128.
    """
    import shutil
    import stat as _stat
    import subprocess
    import threading

    # Governance skills are staged READ-ONLY inside the run workspace (files 0o444,
    # dirs 0o555 — see agents/sdlc_governance/engine.stage_skill_readonly). A plain
    # rmtree can't delete a read-only dir, so restore write bits across the tree
    # first. Harmless for a tree with none. (Done inline rather than via rmtree's
    # onerror/onexc callback — that kwarg was renamed across Python 3.12→3.14.)
    def _make_writable(root: str) -> None:
        _w = _stat.S_IWUSR | _stat.S_IWGRP | _stat.S_IWOTH
        for dirpath, dirnames, filenames in os.walk(root):
            for _n in [dirpath] + [os.path.join(dirpath, f) for f in filenames] \
                    + [os.path.join(dirpath, d) for d in dirnames]:
                try:
                    os.chmod(_n, os.stat(_n).st_mode | _w)
                except OSError:
                    pass

    if os.path.isdir(path):
        _make_writable(path)
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.isdir(path):
        return

    # Residue remains — almost certainly root-owned build artifacts. Remove them
    # with a root container that mounts the parent dir, so the run dir itself goes too.
    parent = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(os.path.abspath(path))

    # Spawn cleanup in a thread with timeout so hung Docker doesn't block the pipeline.
    # The finally block in the state machine must not hang indefinitely.
    _cleanup_timeout_secs = int(os.getenv("WORKSPACE_CLEANUP_TIMEOUT_SECS", "30"))

    _cleanup_image = getattr(config, "WORKSPACE_CLEANUP_IMAGE", None) or "python:3.11-slim"

    def _docker_cleanup():
        try:
            import docker as _docker
            client = _docker.from_env()
            # Guard against an implicit registry pull. containers.run() will
            # auto-pull a missing image from Docker Hub — on an air-gapped host
            # that DNS lookup fails ("lookup registry-1.docker.io … server
            # misbehaving"), the pull raises, and cleanup silently aborts,
            # leaving the root-owned residue that later gets misreported as a
            # clone/token failure. Verify the image is already cached and fail
            # fast with an actionable message instead of triggering a pull.
            try:
                client.images.get(_cleanup_image)
            except _docker.errors.ImageNotFound:
                logger.error(
                    f"workspace_sync: privileged cleanup of {path} skipped — cleanup image "
                    f"'{_cleanup_image}' is not present in the local Docker cache and an "
                    f"implicit registry pull is unsafe on this (possibly air-gapped) host. "
                    f"Set WORKSPACE_CLEANUP_IMAGE to an already-cached image "
                    f"(e.g. an ainxt-builder-* image or an internal-mirror python image)."
                )
                return
            client.containers.run(
                image=_cleanup_image,
                command=["rm", "-rf", f"/host/{name}"],
                volumes={parent: {"bind": "/host", "mode": "rw"}},
                network_mode="none",
                remove=True,
            )
            logger.info(f"workspace_sync: privileged cleanup removed leftover workspace {path}")
        except Exception as exc:
            logger.error(f"workspace_sync: privileged cleanup of {path} failed: {exc}")

    cleanup_thread = threading.Thread(target=_docker_cleanup, daemon=True)
    cleanup_thread.start()
    cleanup_thread.join(timeout=_cleanup_timeout_secs)

    if cleanup_thread.is_alive():
        logger.warning(
            f"workspace_sync: privileged cleanup of {path} exceeded timeout "
            f"({_cleanup_timeout_secs}s) — skipping. Pipeline will proceed."
        )

    # Belt-and-braces: in case the container left the now-empty shell behind.
    shutil.rmtree(path, ignore_errors=True)
    if os.path.isdir(path):
        raise RuntimeError(
            f"workspace_sync: could not clean leftover workspace {path} "
            f"(likely root-owned build artifacts left by the builder container); "
            f"clean it manually or run the builder as the host user"
        )


def _update_sync_time(repo_slug: str) -> None:
    from db.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as sess:
            sess.execute(text("""
                UPDATE repo_index_status
                SET    workspace_synced_at = NOW()
                WHERE  repo_name = :slug
            """), {"slug": repo_slug})
            sess.commit()
    except Exception as exc:
        logger.debug(f"workspace_sync: could not update sync time for {repo_slug}: {exc}")


def _deploy_to_nexus(workspace: str, manifest) -> None:
    """mvn deploy to AiNxt Nexus using the shared Maven settings (has mirror + credentials)."""
    try:
        nexus_url = config.AiNxt_NEXUS_URL.rstrip("/")
        deploy_cmd = (
            f"mvn deploy -DskipTests -q "
            f"-DaltDeploymentRepository=ainxt-nexus::default::{nexus_url}/repository/maven-releases/"
        )
        subprocess.run(
            ["bash", "-lc", deploy_cmd],
            cwd=workspace,
            timeout=300,
            check=True,
        )
        logger.info(f"workspace_sync: deployed to Nexus from {workspace}")
    except Exception as exc:
        logger.warning(f"workspace_sync: Nexus deploy failed: {exc}")