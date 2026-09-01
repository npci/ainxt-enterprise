# SPDX-License-Identifier: Apache-2.0
"""
sandbox/workspace_builder.py

Universal build executor.  Runs compile or test commands inside a pre-built
ainxt-builder-{jvm|web|systems} container using the repo's workspace as a
volume mount.  No per-repo Docker image is built.

All language-specific knowledge lives in BuildManifest (resolved by
core/build_manifest_resolver.py).  This class is entirely language-agnostic.

Cache volumes:
  Persistent cache dirs are mounted from BUILDER_CACHE_ROOT (configured in
  core/config.py, defaults to /opt/ainxt/build-cache).  These are NEVER in
  /tmp — they survive across reboots and are shared across all builds.
"""

from __future__ import annotations

import os
import re
import shlex
import time
import uuid
from pathlib import Path

from core import config
from core.build_manifest_resolver import BuildManifest
from core.build_result_parser import BuildResultParser, PhaseResult
from core.logger import logger

_parser = BuildResultParser()

# Name of the directory where the SDLC multi-repo pipeline stages dependent-repo
# checkouts INSIDE the primary run workspace (<primary_workspace>/.sdlc_deps/{slug}/),
# so the deployed headless ainxt CLI — which jails its file tools to the session
# cwd — can still read them. Authoritative definition:
# agents/multi_repo_workspace.py::_SDLC_DEPS_DIRNAME. Kept as a local constant
# (not imported) to avoid a sandbox → agents dependency; keep the value in sync
# manually if it ever changes there.
_SDLC_DEPS_DIRNAME = ".sdlc_deps"

# Container path Maven uses for its local repository. Bound either to the shared
# host cache or, for a multi-repo run, to the run's own `_m2_cache`
# (see `_build_volumes`). Authoritative copy of
# core/build_manifest_resolver._CACHE_CONTAINER_MAP["m2"].
_M2_CONTAINER_PATH = "/root/.m2/repository"

# Headroom between the in-container `timeout` and the docker-side
# `container.wait(timeout=...)`. The inner timeout must always fire first so the
# `chown -R /workspace` trailer runs and no root-owned residue is left behind;
# this window is what the trailer gets to finish in before docker gives up and
# SIGKILLs the container. A chown over a large Maven `target/` tree is seconds,
# not minutes — 120s is generous.
_CONTAINER_TIMEOUT_GRACE_SECS = 120

# How long `timeout` waits after SIGTERM before escalating to SIGKILL. Maven and
# Gradle both exit promptly on SIGTERM; the escalation only matters for a build
# that ignores it.
_TIMEOUT_KILL_AFTER_SECS = 10


def m2_cache_chown_cmd() -> str:
    """
    In-container trailer that hands ownership of the Maven cache mount back to
    the host UID/GID, for the files this build actually created.

    Why this is needed: the builder container runs as **root**, so every artifact
    it downloads into the mounted Maven repository becomes root-owned on the
    host. The SDLC workers run as a non-root user
    (`deploy/ainxt-workers-sdlc.service`), so without this trailer they cannot:
      • create files inside the shared cache's directories, and
      • hardlink the per-run cache's files (`fs.protected_hardlinks=1`, the
        kernel default, forbids linking a file you neither own nor can write),
    which is exactly what `agents/multi_repo_workspace.merge_m2_cache_to_shared`
    does. The shared cache would therefore stay permanently cold.

    Same intent and the same UID source as the existing `/workspace` trailer —
    this just extends that established pattern to the cache mount.

    `find ! -user` restricts the actual `chown` calls to entries not already
    owned by the target UID. On a warm cache that is only the handful of
    artifacts this build downloaded, so the trailer costs a stat walk rather
    than a rewrite of the whole tree — it runs on the build's critical path and
    the shared repository can hold hundreds of thousands of files.

    Best-effort (`|| true`): on a root-squashed or read-only cache mount the
    chown simply cannot be honoured, and that must never fail a green build —
    the write-back degrades and now says so out loud. `find` also exits non-zero
    when it cannot stat part of the tree, which `|| true` absorbs so the build's
    own exit code is what propagates.
    """
    if not hasattr(os, "getuid"):
        # Windows dev box — containers never actually run here; keep the shell
        # command syntactically valid.
        return "true"
    uid, gid = os.getuid(), os.getgid()
    return (
        f"find {_M2_CONTAINER_PATH} ! -user {uid} "
        f"-exec chown {uid}:{gid} {{}} + 2>/dev/null || true"
    )


def container_resource_kwargs() -> dict:
    """
    docker-py resource kwargs (mem_limit / cpu_quota) for a builder container.

    These used to be hardcoded at `cpu_quota=50000` (half a core) and
    `mem_limit="2g"`, which throttled every compile/test to a fraction of a
    single core no matter how much capacity the host had — the dominant cost of
    a multi-module Maven reactor build. Now derived from
    config.BUILD_CONTAINER_CPUS / BUILD_CONTAINER_MEMORY, shared with the
    multi-repo dep installs so both get the same budget.
    """
    return config.build_container_resources()


class WorkspaceBuilder:
    """
    Executes compile and test phases in the appropriate ainxt-builder container.
    One instance per SDLC run is fine — stateless.
    """

    def compile(
        self,
        manifest: BuildManifest,
        sdlc_run_id: str,
        workspace_path: str | None = None,
        m2_cache_override: str | None = None,
    ) -> PhaseResult:
        return self._run(manifest, "compile", sdlc_run_id, workspace_path=workspace_path,
                         m2_cache_override=m2_cache_override)

    def test(
        self,
        manifest: BuildManifest,
        sdlc_run_id: str,
        workspace_path: str | None = None,
        m2_cache_override: str | None = None,
        test_cmd_override: str | None = None,
    ) -> PhaseResult:
        return self._run(manifest, "test", sdlc_run_id, workspace_path=workspace_path,
                         m2_cache_override=m2_cache_override,
                         test_cmd_override=test_cmd_override)

    def _run(
        self,
        manifest: BuildManifest,
        phase: str,
        sdlc_run_id: str,
        workspace_path: str | None = None,
        m2_cache_override: str | None = None,
        test_cmd_override: str | None = None,
    ) -> PhaseResult:
        import docker as _docker
        from docker.errors import DockerException, APIError
        from requests.exceptions import ReadTimeout, ConnectionError as _ReqConnErr

        # test_cmd_override lets the caller scope the test phase to just the run's
        # changed/added test files (see build_manifest_resolver.scoped_test_command).
        # Ignored for compile — that always runs the whole-project compile_cmd.
        if phase == "compile":
            cmd = manifest.compile_cmd
        else:
            cmd = test_cmd_override or manifest.test_cmd
        if not cmd:
            logger.warning(f"workspace_builder: empty {phase} command for {manifest.repo_slug}")
            return PhaseResult(status="UNKNOWN_BUILD_PATTERN", command="", image=manifest.image)

        run_id  = f"{sdlc_run_id}_{phase}"
        log_dir = Path(config.BUILD_LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"{run_id}.log")

        # When a per-run workspace is provided, use it directly so each SDLC run
        # builds against an isolated checkout (no contamination from prior runs).
        # Falls back to the legacy shared-per-repo path for callers that don't
        # set workspace_path (e.g. dep build pipeline).
        workspace = workspace_path or os.path.join(config.BUILDER_WORKSPACE_ROOT, manifest.repo_slug)
        if not os.path.isdir(workspace):
            logger.error(
                f"workspace_builder: workspace not found for {manifest.repo_slug}: {workspace}"
            )
            return PhaseResult(
                status="UNKNOWN_BUILD_PATTERN",
                command=cmd, image=manifest.image,
                output_tail=f"Workspace not found: {workspace}",
            )

        volumes = self._build_volumes(manifest, workspace, m2_cache_override=m2_cache_override)
        # Maven 3.9+ breaks shade-plugin ≤ 2.x: hyphenated manifest entries
        # (e.g. <Main-Class>) are no longer parsed as plain map entries.
        # Upgrade shade 2.x → 3.3.0 in the cloned workspace before building —
        # the actual repo is never touched.
        #
        # IMPORTANT: prune .sdlc_deps/ from this walk (the `_SDLC_DEPS_DIRNAME`
        # constant). Multi-repo runs stage dependent-repo checkouts at
        # /workspace/.sdlc_deps/{slug}/, and this container runs as root — the
        # read-only chmod (0444/0555) applied to `compile-only` deps after their
        # own `mvn install` is filesystem-mode protection only, which root ignores.
        # Without the prune, a JVM dependency that itself pins maven-shade-plugin
        # 2.x would get its pom.xml silently rewritten by the PRIMARY repo's build:
        # for an `editable` dep, `_collect_dep_edits` then sees pom.xml as modified
        # and the sibling MR pushes a platform-internal version bump into the
        # customer's dependency repo attributed to the AI's implementation; for a
        # `compile-only` dep it mutates a tree explicitly promised to be read-only.
        _shade_patch = ""
        if "mvn" in cmd:
            _shade_patch = (
                f"find /workspace -path '/workspace/{_SDLC_DEPS_DIRNAME}' -prune -o "
                r"-name 'pom.xml' -print | "
                r"xargs -r sed -zEi "
                r"'s|(<artifactId>maven-shade-plugin</artifactId>[[:space:]]*<version>)2\.[^<]*(</version>)|\13.3.0\2|g'"
                r" && "
            )
        # The container runs as root (it needs the baked-in /root/.m2/settings.xml
        # Nexus creds and the /root/.m2/repository cache mount, which a non-root UID
        # cannot traverse). As a side effect, build artifacts written to the
        # bind-mounted /workspace (e.g. Maven target/) end up root-owned on the host,
        # which the non-root app/worker process then cannot delete — leaving residue
        # that breaks the next `git clone` into that path. So after the build (pass or
        # fail) chown /workspace back to the host UID/GID so host-side cleanup works.
        # Only /workspace is chowned — the /root caches/settings are untouched.
        #
        # DELIBERATELY NOT pruned/scoped: this must keep covering the WHOLE tree,
        # including .sdlc_deps/ (the `_SDLC_DEPS_DIRNAME` constant) — dep builds
        # (mvn install for compile-only, or editable-dep builds) can also leave
        # root-owned artifacts under the staged dep checkouts, and host-side cleanup
        # needs to remove those too. .sdlc_deps/ never enters the primary diff (it's
        # excluded via .git/info/exclude), so chowning it back to the host UID has no
        # compliance implication — do not "fix" this into a scoped/pruned walk like
        # the shade patch above.
        #
        # The Maven cache mount gets the same treatment (`m2_cache_chown_cmd`),
        # so the non-root worker can afterwards merge newly downloaded public
        # artifacts into the shared cache. Only for JVM builds: the trailer walks
        # /root/.m2/repository, which no other toolchain mounts.
        if hasattr(os, "getuid"):
            _chown = f"chown -R {os.getuid()}:{os.getgid()} /workspace 2>/dev/null || true"
            if _M2_CONTAINER_PATH in (manifest.cache_paths or []):
                _chown = f"{_chown} ; {m2_cache_chown_cmd()}"
        else:  # Windows dev box — never actually runs containers, keep the cmd valid
            _chown = "true"
        # `full_cmd` is assembled after the timeout budget is resolved below — the
        # build is wrapped in an in-container `timeout` derived from it, so the
        # chown trailer still runs when the build overruns.
        # NOTE on test discovery in .sdlc_deps (F10, LOW, plausible — not fixed here):
        # Dependent repos staged under /workspace/.sdlc_deps/ are a latent test-discovery
        # hazard if any test runner walks the primary workspace tree without excluding
        # dot-prefixed directories. However, most runners are already safe:
        # - pytest: default `norecursedirs` includes `.*`, so skips .sdlc_deps/.
        # - Maven: only walks declared pom <modules>, never the tree, so safe.
        # - Go: the `go` command explicitly ignores `.` and `_` prefixed directories,
        #   so `go test ./...` never descends into .sdlc_deps/.
        # - Cargo: `cargo test` builds only declared targets from Cargo.toml; does not
        #   walk the tree, so safe.
        # - Jest: likely safe (default testMatch globs exclude dot-prefixed dirs unless
        #   `dot: true`), but not verified.
        # Residual risk: a NON-JVM primary with an `editable` dep staged in .sdlc_deps/,
        # where the primary's test command (`cmd` below, from manifest.compile_cmd /
        # manifest.test_cmd) is an arbitrary user-supplied string from .gitlab-ci.yml /
        # package.json scripts that does its own recursive walk with no dot-prefix
        # exclusion. (`compile-only` deps hard-fail in `_detect_build_tool` unless JVM,
        # so that combination can't occur.)
        # Not fixed here: `cmd` is resolved per-repo from repo_build_manifests /
        # repo_build_metadata / .gitlab-ci.yml / package.json scripts
        # (core/build_metadata_extractor.py). There is no single seam in this file where
        # all npm/go/cargo/etc. test invocations are constructed (unlike
        # `_python_build_commands()` for Python). A real fix would intercept command
        # construction at core/build_metadata_extractor.py (the point each tool's default
        # test command is chosen) and splice in runner-specific exclusion flags (e.g.
        # `--testPathIgnorePatterns` for Jest, exclude the .sdlc_deps import path for
        # `go test`, etc.) — only when an editable dep is actually staged for this run.
        # Deferred until that scenario is exercised.

        # ── Cold vs warm timeout selection (Part A + C) ──────────────
        # A single 300s budget strands cold/stale builds mid-download, so the
        # persistent cache never populates and the repo loops forever.  Ask the
        # resolver whether this repo's dependency cache is HIT/STALE/COLD (keyed
        # off the lockfile hash) and give non-HIT builds the cold budget so the
        # one-time population can finish.  current_hash is persisted after a
        # successful build so the next run is HIT.
        timeout = manifest.timeout
        cache_verdict = "UNKNOWN"
        current_hash = ""
        try:
            from core.build_manifest_resolver import cache_state
            cache_verdict, current_hash = cache_state(manifest, workspace)
            # cache_state's verdict is keyed off the lockfile hash and describes
            # the SHARED cache only. A multi-repo build does not use that cache:
            # the caller binds a per-run `{run_id}_multirepo/_m2_cache` over
            # /root/.m2/repository (see _build_volumes + agents/multi_repo_workspace),
            # seeded from the shared cache by a best-effort `cp -al` that silently
            # degrades to a slow copy (cross-filesystem) or to nothing at all. So a
            # HIT verdict can be paired with a cold/partial cache, and the build
            # gets the warm budget while it re-downloads everything from Nexus —
            # it is then killed mid-download, which also skips the chown trailer
            # below and leaves root-owned residue. Never trust HIT for an override
            # build: the per-run cache is not the cache the verdict measured.
            if m2_cache_override:
                cache_verdict = "OVERRIDE"
            if cache_verdict != "HIT":
                timeout = max(timeout, config.BUILD_COLD_TIMEOUT_SECS)
            logger.info(
                f"workspace_builder [{phase}]: cache {cache_verdict} for "
                f"{manifest.repo_slug} → timeout={timeout}s "
                f"(warm={manifest.timeout}s cold={config.BUILD_COLD_TIMEOUT_SECS}s)"
            )
        except Exception as exc:
            logger.warning(
                f"workspace_builder [{phase}]: cache_state failed for "
                f"{manifest.repo_slug} ({exc}); using warm timeout {timeout}s"
            )

        # ── In-container timeout so cleanup always runs ──────────────
        # Enforce the budget INSIDE the container, a little ahead of the
        # docker-side `container.wait(timeout=...)`. When docker times out we
        # SIGKILL the container (see the ReadTimeout handler below), which
        # bypasses the `chown -R` trailer entirely and strands root-owned build
        # artifacts on the host — the non-root worker then cannot rmtree the
        # workspace and the next clone into that path fails. `timeout` kills only
        # the build command, so the trailer still executes and the workspace goes
        # back to the host UID. The docker-side wait stays as a backstop for a
        # container that is wedged below the shell (e.g. unkillable D-state I/O).
        _inner_timeout = timeout
        timeout = timeout + _CONTAINER_TIMEOUT_GRACE_SECS
        # `timeout` is coreutils on the debian/temurin builders and busybox on any
        # alpine one; both accept `-k`. Fall back to a bare invocation if the image
        # somehow ships neither, so a missing binary can never fail every build.
        _q = shlex.quote(cmd)
        _guarded = (
            f"if command -v timeout >/dev/null 2>&1 ; then "
            f"timeout -k {_TIMEOUT_KILL_AFTER_SECS}s {_inner_timeout}s bash -lc {_q} ; "
            f"else bash -lc {_q} ; fi"
        )
        # Passed as a list: docker-py shlex.splits a string command on the HOST,
        # which would re-parse (and corrupt) any quoting inside the build command.
        full_cmd = [
            "bash", "-lc",
            f"cd /workspace && {{ {_shade_patch}{_guarded} ; }} ; "
            f"rc=$? ; {_chown} ; exit $rc",
        ]

        logger.info(
            f"workspace_builder [{phase}]: {manifest.repo_slug} "
            f"image={manifest.image} cmd={cmd!r}"
        )
        _res_kwargs = container_resource_kwargs()
        logger.debug(
            f"workspace_builder [{phase}]: docker params — "
            f"image={manifest.image} network=host "
            f"mem={_res_kwargs.get('mem_limit')} "
            f"cpu_quota={_res_kwargs.get('cpu_quota', 'unlimited')} "
            f"timeout={timeout}s env={manifest.env_vars} "
            f"volumes={list(volumes.keys())}"
        )

        t0 = time.time()
        container = None
        output = ""
        exit_code = -1
        surefire_bytes: bytes | None = None

        try:
            client = _docker.from_env()
            container = client.containers.run(
                image=manifest.image,
                command=full_cmd,
                environment=manifest.env_vars,
                volumes=volumes,
                network_mode="host",    # needs Nexus access
                remove=False,             # keep for log/archive extraction
                detach=True,
                **_res_kwargs,            # mem_limit / cpu_quota — see config
            )

            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", 1)
                logger.debug(
                    f"workspace_builder [{phase}]: container exited "
                    f"exit_code={exit_code} repo={manifest.repo_slug}"
                )
                output = container.logs(stdout=True, stderr=True).decode(errors="replace")
                # The in-container `timeout` fired: coreutils exits 124, or 137
                # (128+SIGKILL) when the build ignored SIGTERM and had to be killed.
                # Normalise to the -1 sentinel the parser maps to BUILD_TIMEOUT so
                # the gate still classifies this as transient/retryable instead of
                # a real compile failure. The container exited cleanly through the
                # trailer, so the workspace is already chowned back to the host.
                #
                # 137 is ambiguous — the kernel OOM killer returns it too, against
                # the configured mem_limit. Only treat it as a timeout if the build
                # actually ran out the clock; an early 137 is an OOM and must stay a
                # real failure (retrying it just burns another full budget).
                _elapsed = time.time() - t0
                if exit_code == 137 and _elapsed < _inner_timeout:
                    logger.warning(
                        f"workspace_builder [{phase}]: {manifest.repo_slug} killed with 137 "
                        f"after {int(_elapsed)}s, well inside the {_inner_timeout}s budget — "
                        f"treating as OOM (mem_limit={_res_kwargs.get('mem_limit')}), "
                        f"not a timeout"
                    )
                elif exit_code in (124, 137):
                    logger.warning(
                        f"workspace_builder [{phase}]: in-container TIMEOUT for "
                        f"{manifest.repo_slug} after {_inner_timeout}s "
                        f"(exit={exit_code} cache={cache_verdict}); workspace ownership restored"
                    )
                    output = (
                        f"BUILD_TIMEOUT: exceeded {_inner_timeout}s limit "
                        f"(killed inside container)\n{output}"
                    )
                    exit_code = -1
            except (ReadTimeout, _ReqConnErr) as _to_exc:
                # docker-py 7.x raises requests ConnectionError (wrapping
                # urllib3 ReadTimeoutError) when /wait exceeds its read
                # timeout — NOT ReadTimeout. Treat both as a build timeout so
                # the gate retries (transient) instead of hard-suspending.
                if "time" not in str(_to_exc).lower():
                    raise  # genuine daemon/connection failure — let it surface
                try:
                    container.kill()
                except Exception:
                    pass
                # Capture whatever the container emitted before the kill so the
                # parser can still see cache-degradation warnings (Part D) — a
                # bare "BUILD_TIMEOUT" masks a disabled cache as a plain timeout.
                partial = ""
                try:
                    partial = container.logs(stdout=True, stderr=True).decode(errors="replace")
                except Exception:
                    pass
                exit_code = -1
                output = f"BUILD_TIMEOUT: exceeded {timeout}s limit\n{partial}"
                logger.warning(
                    f"workspace_builder [{phase}]: TIMEOUT for {manifest.repo_slug} "
                    f"after {timeout}s (cache={cache_verdict})"
                )

        except DockerException as exc:
            output = f"Docker error: {exc}"
            exit_code = -2
            logger.error(f"workspace_builder [{phase}]: Docker error: {exc}")
        finally:
            if container:
                # Extract surefire test reports BEFORE removing the container
                if phase == "test" and "jvm" in manifest.image:
                    try:
                        stream, _ = container.get_archive(
                            "/workspace/target/surefire-reports"
                        )
                        surefire_bytes = b"".join(chunk for chunk in stream)
                        logger.debug(
                            f"workspace_builder [{phase}]: surefire archive extracted "
                            f"{len(surefire_bytes)} bytes for {manifest.repo_slug}"
                        )
                    except Exception as _sfe:
                        logger.debug(
                            f"workspace_builder [{phase}]: surefire archive not found "
                            f"for {manifest.repo_slug}: {_sfe}"
                        )
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        duration = int(time.time() - t0)

        # Write full log for debugging
        try:
            Path(log_path).write_text(
                f"REPO:     {manifest.repo_slug}\n"
                f"PHASE:    {phase}\n"
                f"CMD:      {cmd}\n"
                f"IMAGE:    {manifest.image}\n"
                f"ENV:      {manifest.env_vars}\n"
                f"EXIT:     {exit_code}\n"
                f"DURATION: {duration}s\n"
                f"\n{output}",
                encoding="utf-8",
            )
        except OSError:
            pass

        # Parse result
        if phase == "compile":
            result = _parser.parse_compile(exit_code, output, cmd, manifest.image, duration)
        else:
            result = _parser.parse_test(
                exit_code, output, cmd, manifest.image, duration,
                surefire_bytes=surefire_bytes,
            )

        result.command = cmd
        result.image   = manifest.image

        # ── Cache-key persistence + degradation surfacing (Part C + D) ──
        # On a good build the dependency cache now matches the current lockfile,
        # so record the hash → next run resolves to HIT and uses the warm budget.
        if result.status == "BUILD_SUCCESS" and current_hash:
            try:
                from core.build_manifest_resolver import write_cache_key
                write_cache_key(manifest.repo_slug, current_hash)
                logger.info(
                    f"workspace_builder [{phase}]: wrote cache key for "
                    f"{manifest.repo_slug} (hash={current_hash[:12]}…)"
                )
            except Exception as exc:
                logger.warning(
                    f"workspace_builder [{phase}]: cache-key write failed for "
                    f"{manifest.repo_slug}: {exc}"
                )

        # Fail loud when the build's dependency cache was disabled/degraded so it
        # no longer masquerades as a plain BUILD_TIMEOUT.  Also flag the case
        # where we resolved HIT yet still re-downloaded (warm cache ineffective).
        if getattr(result, "cache_degraded", False):
            logger.warning(
                f"workspace_builder [{phase}]: CACHE_DEGRADED for {manifest.repo_slug} "
                f"— builder cache was disabled/unwritable; deps re-downloaded and the "
                f"persistent cache did not warm. Check cache-dir ownership under "
                f"{config.BUILDER_CACHE_ROOT}. (status={result.status})"
            )
            # Prefix output_tail so the degradation is visible in build_runs too.
            marker = "[CACHE_DEGRADED] builder dependency cache was disabled/unwritable\n"
            if marker not in result.output_tail:
                result.output_tail = (marker + result.output_tail)[:4000]
        elif cache_verdict == "HIT" and re.search(r"\bDownloading\b", output):
            logger.warning(
                f"workspace_builder [{phase}]: warm cache INEFFECTIVE for "
                f"{manifest.repo_slug} — resolved HIT but build still downloaded "
                f"dependencies. Cache key may be stale or the cache volume is not "
                f"persisting."
            )

        # Persist build run record
        self._record_run(manifest.repo_slug, sdlc_run_id, phase, result, log_path)

        logger.info(
            f"workspace_builder [{phase}]: {manifest.repo_slug} "
            f"status={result.status} exit={exit_code} duration={duration}s "
            f"cache={cache_verdict}"
        )
        return result

    # ── Volume construction ────────────────────────────────────

    def _build_volumes(
        self,
        manifest: BuildManifest,
        workspace: str,
        m2_cache_override: str | None = None,
    ) -> dict:
        volumes: dict = {
            workspace: {"bind": "/workspace", "mode": "rw"},
        }
        cache_root = config.BUILDER_CACHE_ROOT

        # When the caller provides a per-run m2 cache (multi-repo builds), use
        # it instead of the global shared cache so deps installed by
        # install_compile_only_deps are visible to the compile container.
        _host_overrides: dict[str, str] = {
            config.PIP_CACHE_CONTAINER_PATH:    config.PIP_CACHE_HOST_PATH,
            config.POETRY_CACHE_CONTAINER_PATH: config.POETRY_CACHE_HOST_PATH,
        }
        if m2_cache_override and os.path.isdir(m2_cache_override):
            _host_overrides[_M2_CONTAINER_PATH] = m2_cache_override

        venv_container_path = config.PYTHON_VENV_CONTAINER_PATH
        for container_path in manifest.cache_paths:
            if container_path == venv_container_path:
                host_path = os.path.join(cache_root, "venvs", manifest.repo_slug)
            elif container_path in _host_overrides:
                host_path = _host_overrides[container_path]
            else:
                host_dir = container_path.strip("/").replace("/", "_")
                host_path = os.path.join(cache_root, host_dir)
            os.makedirs(host_path, exist_ok=True)
            volumes[host_path] = {"bind": container_path, "mode": "rw"}
        return volumes

    # ── Audit log ──────────────────────────────────────────────

    def _record_run(
        self,
        repo_slug: str,
        sdlc_run_id: str,
        phase: str,
        result: PhaseResult,
        log_path: str,
    ) -> None:
        from db.database import engine
        from sqlalchemy import text
        try:
            td = result.test_details
            with engine.connect() as sess:
                sess.execute(text("""
                    INSERT INTO build_runs
                        (repo_slug, sdlc_run_id, phase,
                         compile_status, test_status,
                         exit_code, command, image, duration_secs,
                         output_tail, error_lines, failed_tests,
                         missing_artifact, test_total, test_passed, test_failed,
                         log_path, created_at)
                    VALUES
                        (:repo_slug, :sdlc_run_id, :phase,
                         :compile_status, :test_status,
                         :exit_code, :command, :image, :duration_secs,
                         :output_tail, :error_lines, :failed_tests,
                         :missing_artifact, :test_total, :test_passed, :test_failed,
                         :log_path, NOW())
                """), {
                    "repo_slug":        repo_slug,
                    "sdlc_run_id":      sdlc_run_id,
                    "phase":            phase,
                    "compile_status":   result.status if phase == "compile" else None,
                    "test_status":      result.status if phase == "test"    else None,
                    "exit_code":        result.exit_code,
                    "command":          result.command,
                    "image":            result.image,
                    "duration_secs":    result.duration_secs,
                    "output_tail":      result.output_tail,
                    "error_lines":      result.error_lines,
                    "failed_tests":     td.failed_tests if td else [],
                    "missing_artifact": result.missing_artifact,
                    "test_total":       td.total   if td else None,
                    "test_passed":      td.passed  if td else None,
                    "test_failed":      td.failed  if td else None,
                    "log_path":         log_path,
                })
                sess.commit()
        except Exception as exc:
            logger.warning(f"workspace_builder: build_runs insert failed: {exc}")