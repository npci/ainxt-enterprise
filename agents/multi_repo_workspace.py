# SPDX-License-Identifier: MIT
"""
Multi-repo workspace assembly + sandboxed `mvn install` for compile-only deps.

Status: LIVE. `prepare_and_install_deps` is invoked from
`CodingStateMachine._setup_multi_repo_workspace` (driven by `_phase_implement`
in `sdlc_state_machine.py` and by `_run_plan_phase` in `sdlc_pipeline.py`) and
from the WS-2 baseline build gate (`_run_baseline_build`). `stage_deps_for_cli`
is called at the end of every prep so the headless CLI can actually SEE the
dep checkouts.

Why this module exists
----------------------
AiNxt's Nexus proxies public Maven Central but does NOT host internal AiNxt
artifacts (`org.ainxt.*`). So when an SDLC run touches a repo that depends on
another internal repo, the dependent repo's jar must be *built from source*
before the primary can compile. We do that build inside the existing
`ainxt-builder-jvm-21:latest` container — the same image the rest of the SDLC
pipeline already uses for compile + test — so:

  • Image comes from `BUILDER_REGISTRY` (AiNxt internal Docker registry), not
    Docker Hub. The runtime host is air-gapped and cannot reach Hub anyway.
  • Container has `network_mode="host"` so it can reach
    `SANDBOX_MAVEN_REPO_URL` (Nexus Maven proxy) to resolve public deps.
  • `/root/.m2/settings.xml` is baked into the builder image with the Nexus
    user/password configured. We MUST NOT bind-mount over `/root/.m2`
    wholesale — we mount only `/root/.m2/repository` so settings.xml survives.

This module never invokes Docker Hub. It never invokes `docker run` via
subprocess — it uses the Docker SDK like `sandbox/workspace_builder.py`.

Workspace layout
----------------
Dep checkouts live INSIDE the primary repo's workspace, under
`.sdlc_deps/`. Only the build caches (`_m2_cache`, `_gradle_cache`) live in the
sibling `{run_id}_multirepo/` dir — they are Maven/Gradle scratch space and must
never enter the primary repo's git tree.

    {BUILDER_WORKSPACE_ROOT}/runs/{run_id}_{primary_slug}/   # primary workspace
      ├── ...                       # the primary repo working tree (unchanged)
      └── .sdlc_deps/               # git-excluded staging root for deps
          ├── {dep_slug_1}/         # git clone of dep_1 at pinned ref_sha
          └── {dep_slug_2}/         # compile-only => chmod'd read-only after install

    {BUILDER_WORKSPACE_ROOT}/runs/{run_id}_multirepo/         # caches only
      ├── _m2_cache/                # bind-mounted at /root/.m2/repository
      │                             # (NOT /root/.m2 — that would hide the
      │                             # baked-in settings.xml with Nexus creds)
      └── _gradle_cache/            # gradle publishToMavenLocal cache

Why deps moved inside the primary workspace (CLI workspace jail)
----------------------------------------------------------------
The deployed headless `ainxt` CLI jails its file tools to the session's
workspace cwd. `--add-dir` is a VERIFIED no-op for the read tool (an absolute
path outside cwd is invisible even under full permission bypass), and symlinks
out of the workspace are equally dead. So a dep checkout in a SIBLING directory
is simply invisible to PLAN/IMPLEMENT — the whole point of cloning it is lost.
The only pattern that works (already shipped for governance skills in
`agents/sdlc_governance/engine.py`) is: put the material INSIDE the workspace,
then defend the primary repo two ways:

  1. `.git/info/exclude` — `.sdlc_deps/` is excluded BEFORE the directory is
     created, so a concurrent `git add -A` can never stage the vendored trees
     into the customer's MR / VERIFIED_DIFF.
  2. Filesystem read-only chmod — `stage_deps_for_cli` strips the write bits
     from every `compile-only` checkout AFTER install completes (the CLI's own
     `--permission-mode plan` does NOT block writes; only EACCES does).
     `editable` deps stay writable on purpose — the coder is meant to change
     them, and they keep a real `.git` dir so they can be diffed independently.

Single-repo runs create no `.sdlc_deps/` at all — the directory only appears
when the run actually has dep rows.

Content-addressed jar cache
---------------------------
`mvn install` is slow (1-3 minutes for a small library, 10+ for a large one).
We cache the produced internal jars on the runtime host, keyed by the dep's
`(repo, commit_sha)`. The cache lives at
`{BUILDER_WORKSPACE_ROOT}/cache/multirepo_jars/{repo_slug}/{commit_sha}/`.
On a cache hit we copy the cached internal jars back into the run's
`_m2_cache` (which is the per-run /root/.m2/repository) and skip the install.

Concurrency
-----------
Clones run in parallel (`max_clone_concurrency`, default 4) since 2-4 deps is
the typical count per the design Q&A. `mvn install` runs strictly serially in
topological order — each dep's build may produce jars consumed by the next
dep's build (the whole point of the cache).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import shutil
import stat as _stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Shared with agents/sdlc_governance/engine.py (both append to the same
# <workspace>/.git/info/exclude file); lives in its own dependency-free
# module so this import-light module never pulls in sdlc_governance's
# (and therefore core.logger's) heavy import chain.
from agents._stage_lock import STAGE_LOCK

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────

BUILDER_WORKSPACE_ROOT = os.getenv("BUILDER_WORKSPACE_ROOT", "/opt/ainxt/workspaces")

# Dep checkouts are staged INSIDE the primary workspace under this dirname so the
# workspace-jailed headless CLI can read them (see the module docstring). Always
# `.git/info/exclude`d before creation and chmod'd read-only (compile-only deps)
# after install.
_SDLC_DEPS_DIRNAME = ".sdlc_deps"

# STAGE_LOCK is imported (above) from the dependency-free agents/_stage_lock.py,
# NOT re-created here: both this module and agents/sdlc_governance/engine.py do a
# read-modify-append on the SAME <workspace>/.git/info/exclude file in
# `_git_exclude`, so a module-local lock would only guard against races within
# one module and leave the two modules' writers unserialized against each other
# (Fix G). The lock lives in its own tiny stdlib-only module so neither writer
# has to import the other's (much heavier) module just to share one Lock().


def _resolve_jvm_builder_image(version: str | None = None) -> str:
    """
    Resolve the JVM builder image the same way `sandbox/workspace_builder.py`
    does — `{BUILDER_REGISTRY}/ainxt-builder-jvm-21:latest` by default.

    This is the image the rest of the SDLC pipeline already uses for compile
    and test phases. It is built and pushed to the AiNxt internal Docker
    registry with a baked-in `/root/.m2/settings.xml` that points at
    `SANDBOX_MAVEN_REPO_URL` (the Nexus Maven proxy) with the configured
    credentials. Using the same image here means:
      - no Docker Hub pulls (air-gapped network can't reach it)
      - mvn inside the container can resolve public deps via Nexus
      - the .m2 mount must NOT cover /root/.m2 wholesale (would hide
        settings.xml); we mount only /root/.m2/repository

    When `version` is supplied (a Java major like "17"/"21"/"25", or a fuller
    string such as "17.0.9" from which the major is taken), the exact
    version-tagged image is selected — `ainxt-builder-jvm-{17|21|25}:latest` —
    mirroring `core.build_manifest_resolver._select_versioned_image`. This is
    what lets the multi-repo compile-only dep build honour each dependency's
    OWN declared Java version instead of always defaulting to the jvm-21 image
    (the previous bug: a Java-17 dep was built under jvm-21). An
    unrecognized/empty version falls back to the configured BUILDER_IMAGE_JVM
    default.
    """
    # Lazy import — keeps multi_repo_workspace importable in tests without
    # forcing core.config to evaluate at module load.
    try:
        from core import config as _cfg
        registry = (_cfg.BUILDER_REGISTRY or "").rstrip("/")
        image    = _cfg.BUILDER_IMAGE_JVM or "ainxt-builder-jvm-21:latest"
        if version:
            major = str(version).split(".")[0].strip()
            if major in ("17", "21", "25"):
                image = f"ainxt-builder-jvm-{major}:latest"
        return f"{registry}/{image}" if registry else image
    except Exception:
        base = os.getenv("BUILDER_IMAGE_JVM") or "ainxt-builder-jvm-21:latest"
        if version:
            major = str(version).split(".")[0].strip()
            if major in ("17", "21", "25"):
                return f"ainxt-builder-jvm-{major}:latest"
        return base


def _detect_jvm_version(path: str, tool: str) -> str:
    """
    Best-effort read of the declared Java major version from a compile-only
    dep's OWN checkout (its pinned base ref clone under `.sdlc_deps/{slug}/`),
    so the dep install runs under the matching ainxt-builder-jvm-{17|21|25}
    image. Mirrors the detection regexes in
    `core/build_metadata_extractor.py::_from_pom_xml` / `_gradle_meta`.

    Reads only the dep's own build files — never queries GitLab or a default
    branch. Returns "" when no version is declared; the caller then falls back
    to the configured jvm default.
    """
    try:
        if tool == "maven":
            pom = os.path.join(path, "pom.xml")
            if not os.path.isfile(pom):
                return ""
            content = Path(pom).read_text(encoding="utf-8", errors="replace")
            m = (
                re.search(r"<java\.version>\s*(\d+)", content)
                or re.search(r"<maven\.compiler\.source>\s*(\d+)", content)
                or re.search(r"<maven\.compiler\.release>\s*(\d+)", content)
                or re.search(r"<maven\.compiler\.target>\s*(\d+)", content)
                or re.search(r"JavaVersion\.VERSION_(\d+)", content)
            )
            return m.group(1) if m else ""
        if tool == "gradle":
            for fn in ("build.gradle", "build.gradle.kts"):
                gp = os.path.join(path, fn)
                if not os.path.isfile(gp):
                    continue
                content = Path(gp).read_text(encoding="utf-8", errors="replace")
                m = (
                    re.search(r"sourceCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+)", content)
                    or re.search(r"targetCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+)", content)
                    or re.search(r"jvmTarget\s*=\s*['\"](\d+)", content)
                    or re.search(r"languageVersion\s*=\s*JavaLanguageVersion\.of\((\d+)\)", content)
                    or re.search(r"JavaVersion\.VERSION_(\d+)", content)
                )
                return m.group(1) if m else ""
    except Exception:
        return ""
    return ""


def _install_resource_kwargs() -> dict:
    """
    docker-py resource kwargs (mem_limit / cpu_quota) for a dep install.

    Defaults come from the shared builder budget in core/config
    (BUILD_CONTAINER_CPUS / BUILD_CONTAINER_MEMORY) so a dep install gets the
    same CPU and memory as the primary build — the old hardcoded 2g / half-a-core
    made `mvn install` of a multi-module library the slowest step of a run.

    MULTI_REPO_INSTALL_MEMORY / MULTI_REPO_INSTALL_CPU_QUOTA still override, for
    hosts that need the dep installs held to a smaller budget than the primary.
    A cpu quota of 0 (or a non-numeric value) means "no CPU limit".

    The shared default is read from `core.config` (not from
    sandbox.workspace_builder) so this module keeps its deliberately light import
    graph — see the note on the `agents._stage_lock` import above. Falls back to
    the previous fixed budget if config is unavailable, so a dep install can
    never fail merely because the resource lookup did.
    """
    try:
        from core import config as _cfg
        kwargs = _cfg.build_container_resources()
    except Exception:
        kwargs = {"mem_limit": "2g", "cpu_period": 100000, "cpu_quota": 50000}
    if mem := os.getenv("MULTI_REPO_INSTALL_MEMORY"):
        kwargs["mem_limit"] = mem
    raw_quota = os.getenv("MULTI_REPO_INSTALL_CPU_QUOTA")
    if raw_quota:
        try:
            quota = int(raw_quota)
        except ValueError:
            quota = 0
        if quota > 0:
            kwargs["cpu_quota"] = quota
        else:
            kwargs.pop("cpu_quota", None)
            kwargs.pop("cpu_period", None)
    return kwargs


def _cfg_maven_parallel_flag() -> str:
    """`config.MAVEN_PARALLEL_FLAG` ("" when disabled / config unavailable)."""
    try:
        from core import config as _cfg
        return (_cfg.MAVEN_PARALLEL_FLAG or "").strip()
    except Exception:
        return ""

# Internal groupId prefixes — used at install time to pick which paths inside
# `_m2_cache` to snapshot into the content-addressed cache. Mirrors the
# defaults in agents/dep_resolver.py so the two stay in sync.
_INTERNAL_GROUP_PREFIXES = tuple(
    p.strip().lower()
    for p in os.getenv("INTERNAL_GROUP_PREFIXES", "org.ainxt.").split(",")
    if p.strip()
)


# ── Data shapes ──────────────────────────────────────────────────────────────

@dataclass
class MultiRepoWorkspace:
    """All paths the state machine needs to drive a multi-repo run."""
    run_id: str
    root: str                                       # base dir for the multi-repo workspace
    m2_cache: str                                   # _m2_cache subdir
    gradle_cache: str                               # _gradle_cache subdir
    deps_root: str                                  # <primary_workspace>/.sdlc_deps (git-excluded)
    primary_workspace: str                          # path to the EXISTING single-repo workspace
    dep_paths: dict = field(default_factory=dict)   # repo -> absolute path to the dep's checkout


@dataclass
class CloneSpec:
    """Minimal input the cloner needs for one dep."""
    repo: str               # gitlab namespace/project, e.g. "ainxt/payments-sdk"
    ref: str                # branch or tag passed at trigger
    ref_sha: str            # commit SHA pinned at preflight (authoritative)
    clone_url: str          # git clone URL (https or ssh)
    kind: str               # 'editable' | 'compile-only'


# ── Public entry points ──────────────────────────────────────────────────────

def prepare_multi_repo_workspace(
    run_id: str,
    primary_workspace: str,
    clone_specs: list[CloneSpec],
    *,
    max_clone_concurrency: int = 4,
) -> MultiRepoWorkspace:
    """
    Create the multi-repo workspace and clone every dep in parallel.

    The primary repo's existing workspace is reused as-is (the path passed
    in `primary_workspace`). We never re-clone the primary because the state
    machine has already done that via `prepare_run_workspace`, the per-run
    working branch may already have unmerged commits we'd lose, and
    duplicating the clone wastes I/O.

    On any clone failure raises RuntimeError — callers should wrap and mark
    the run FAILED with the original message.
    """
    # create_deps_root=False when there are no deps — a single-repo run must leave
    # no `.sdlc_deps/` directory inside the primary workspace at all.
    ws = _make_workspace_dirs(
        run_id, primary_workspace, create_deps_root=bool(clone_specs),
    )

    if not clone_specs:
        return ws

    logger.info(
        f"[MR-WS {run_id}] cloning {len(clone_specs)} dep(s) at "
        f"concurrency={max_clone_concurrency} into {ws.deps_root}"
    )
    errors: list[str] = []

    def _clone_one(spec: CloneSpec) -> tuple[str, str]:
        slug = _slug_for(spec.repo)
        dest = os.path.join(ws.deps_root, slug)
        # ── Idempotent short-circuit ──────────────────────────────────────────
        # Deps are staged per-phase (PLAN, then IMPLEMENT), so the checkout is
        # usually already present and already at the pinned SHA. Re-cloning it
        # every phase is pure waste — and would also throw away an editable dep's
        # in-progress work. Only fall through to the clone-to-temp-then-swap below
        # when the SHA differs, the SHA is unknown, or rev-parse fails.
        if os.path.isdir(os.path.join(dest, ".git")) and spec.ref_sha:
            try:
                head = _run(["git", "-C", dest, "rev-parse", "HEAD"], check=True).stdout.strip()
            except Exception:
                head = ""
            if head and head == spec.ref_sha:
                logger.info(
                    f"[MR-WS {run_id}] dep already staged — skipping clone "
                    f"run_id={run_id} repo={spec.repo} ref_sha={spec.ref_sha} skipped=True"
                )
                return spec.repo, dest
        # ── Clone-to-temp, then atomically swap into place ────────────────────
        # NEVER wipe an existing good checkout before its replacement is known
        # good. A failed re-clone (absent/invalid per-user token, network blip)
        # must leave the prior checkout intact rather than destroying it and
        # leaving the dep missing — that destructive wipe-then-clone was exactly
        # how a re-clone failure made a staged dep vanish. So clone into a sibling
        # temp dir, and only once clone+reset succeed do we remove any old checkout
        # and move the fresh one into place (atomic same-filesystem rename).
        import tempfile
        tmp = tempfile.mkdtemp(prefix=f".{slug}.clone-", dir=ws.deps_root)
        try:
            # Clone the branch shallowly, then hard-reset to the pinned SHA so the
            # working tree exactly matches preflight's view of the repo (protects
            # against the branch moving between preflight and now).
            _run(["git", "clone", "--depth=50", "--branch", spec.ref, spec.clone_url, tmp])
            if spec.ref_sha:
                _run(["git", "-C", tmp, "fetch", "--depth=50", "origin", spec.ref_sha], check=False)
                _run(["git", "-C", tmp, "reset", "--hard", spec.ref_sha])
        except subprocess.CalledProcessError as exc:
            # Clone/reset failed — discard the temp and KEEP the existing checkout
            # (if any) untouched, so the dep stays available at its prior revision.
            _robust_rmtree(tmp)
            stderr = (exc.stderr or "").strip() if hasattr(exc, "stderr") else str(exc)
            return spec.repo, f"__ERROR__:{stderr or exc}"

        # Clone succeeded — replace any existing checkout with the fresh temp.
        # Root-aware removal: a prior run's dep build ran the container as root and
        # may have left root-owned files (e.g. Maven target/) in the old checkout;
        # a plain rmtree would silently fail and block the swap.
        if os.path.isdir(dest):
            _robust_rmtree(dest)
            if os.path.isdir(dest) and os.listdir(dest):
                _robust_rmtree(tmp)
                raise RuntimeError(
                    f"_clone_one: cannot clean existing dep checkout {dest!r}: "
                    f"it has un-removable (likely root-owned) leftovers from a "
                    f"prior build. Manually remove it (e.g. as root) and retry."
                )
        try:
            os.replace(tmp, dest)   # atomic same-filesystem rename
        except OSError:
            # Defensive fallback — deps_root is one filesystem so os.replace should
            # succeed; move handles any odd case without losing the fresh clone.
            shutil.move(tmp, dest)
        return spec.repo, dest

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_clone_concurrency) as pool:
        for repo, result in pool.map(_clone_one, clone_specs):
            if result.startswith("__ERROR__:"):
                errors.append(f"{repo}: {result[len('__ERROR__:'):]}")
            else:
                ws.dep_paths[repo] = result

    if errors:
        raise RuntimeError(
            f"prepare_multi_repo_workspace: {len(errors)} dep clone(s) failed:\n  - "
            + "\n  - ".join(errors)
        )

    logger.info(
        f"[MR-WS {run_id}] cloned all {len(ws.dep_paths)} dep(s); "
        f"workspace ready at {ws.root}"
    )
    return ws


def compute_build_order(
    clone_specs: list[CloneSpec],
    ws: MultiRepoWorkspace,
    *,
    manifest_overrides: dict | None = None,
) -> list[str]:
    """
    Decide the install order for compile-only deps.

    Strategy:
      1. Read each dep's pom.xml (Maven deps only — see note below for Gradle).
      2. Build an edge `A -> B` whenever dep A declares an internal-prefix
         dependency on dep B (i.e. B must be installed before A).
      3. Kahn's algorithm produces a topological ordering.
      4. `.sdlc.yml build_order:` overrides per-repo (lower = earlier).

    Cycles → log a warning and fall back to alphabetical order. We do not
    raise: a cycle still produces a build attempt that Maven will reject
    explicitly, which is a more useful error than ours.

    Gradle deps participate as nodes in the graph (so they appear in the
    returned order) but their *outgoing* edges are not inferred from
    build.gradle today — we don't extract gradle artifactId from groovy/kts
    source reliably enough. If a Maven dep depends on a Gradle dep, the edge
    won't be detected and the order may be wrong. Two ways to fix that:
      - Declare explicit `build_order:` in `.sdlc.yml dependencies:` on the
        primary repo (highest precedence, always correct).
      - Add Gradle artifactId extraction here (deferred — not in v1).
    """
    compile_only = {s.repo: s for s in clone_specs if s.kind == "compile-only"}
    if not compile_only:
        return []

    # Map artifactId -> repo path so we can resolve pom <dependency> entries.
    artifact_to_repo: dict[str, str] = {}
    pom_by_repo: dict[str, str] = {}
    for repo, path in ws.dep_paths.items():
        if repo not in compile_only:
            continue
        pom = os.path.join(path, "pom.xml")
        if not os.path.isfile(pom):
            continue
        try:
            content = Path(pom).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pom_by_repo[repo] = content
        aid = _read_pom_field(content, "artifactId")
        if aid:
            artifact_to_repo[aid] = repo

    edges: dict[str, set[str]] = {r: set() for r in compile_only}
    for repo, pom in pom_by_repo.items():
        for gid, aid in _pom_direct_deps(pom):
            if not _is_internal_group(gid):
                continue
            target = artifact_to_repo.get(aid)
            if target and target != repo:
                edges[repo].add(target)

    order = _kahn(edges)
    if order is None:
        logger.warning(
            "[MR-WS] build-order has a cycle in compile-only deps; "
            "falling back to alphabetical ordering"
        )
        order = sorted(compile_only.keys())

    # Apply manifest build_order overrides if provided. Repos with a lower
    # explicit build_order come first; ties resolved by the topological order
    # we just computed.
    if manifest_overrides:
        positional = {repo: idx for idx, repo in enumerate(order)}
        def _key(r: str) -> tuple[int, int]:
            mo = manifest_overrides.get(r)
            return (mo if mo is not None else 10_000, positional.get(r, 10_000))
        order = sorted(order, key=_key)

    return order


def install_compile_only_deps(
    ws: MultiRepoWorkspace,
    build_order: list[str],
    clone_specs: list[CloneSpec],
) -> None:
    """
    Install each compile-only dep into the per-run local Maven repo.

    Maven deps: `mvn -B -DskipTests install`.
    Gradle deps: `./gradlew -x test publishToMavenLocal` (or bare `gradle`).
    Both run inside the SAME `ainxt-builder-jvm-*` container — that image
    contains both Maven and Gradle and has the Nexus settings.xml baked in.

    Cache strategy is identical for both tools: check the content-addressed
    cache for `(repo, ref_sha)` first; on hit, copy cached internal-prefix
    jars into `_m2_cache` and skip the install entirely.

    Raises RuntimeError on the first install failure — callers mark the run
    FAILED with the original message.
    """
    spec_by_repo = {s.repo: s for s in clone_specs}

    for repo in build_order:
        spec = spec_by_repo.get(repo)
        if spec is None or spec.kind != "compile-only":
            continue
        path = ws.dep_paths.get(repo)
        if not path or not os.path.isdir(path):
            raise RuntimeError(f"install_compile_only_deps: no checkout for {repo!r}")

        slug = _slug_for(repo)
        sha = spec.ref_sha or ""

        # ── Cache hit short-circuit ── (tool-agnostic — cache layout is the
        # maven repository layout regardless of which tool produced the jars)
        if sha and _restore_jars_from_cache(slug, sha, ws.m2_cache):
            logger.info(f"[MR-WS {ws.run_id}] {repo}@{sha[:10]}: cache HIT — skipped install")
            continue

        tool = _detect_build_tool(path)
        # Select the builder image from THIS dep's own declared Java version
        # (read off its checkout), not the process-wide jvm-21 default. A dep
        # pinned to Java 17 must be built under ainxt-builder-jvm-17, else javac
        # target/source mismatches or toolchain-not-found failures occur.
        _jvm_ver = _detect_jvm_version(path, tool or "")
        _image = _resolve_jvm_builder_image(_jvm_ver)
        logger.info(
            f"[MR-WS {ws.run_id}] {repo}: dep builder image resolved "
            f"version={_jvm_ver or 'default'} image={_image}"
        )
        if tool == "maven":
            logger.info(f"[MR-WS {ws.run_id}] {repo}@{sha[:10] or 'HEAD'}: mvn install (cache MISS)")
            rc, output = _docker_mvn_install(
                path, ws.m2_cache, label=f"{ws.run_id}:{slug}", image=_image,
            )
        elif tool == "gradle":
            logger.info(f"[MR-WS {ws.run_id}] {repo}@{sha[:10] or 'HEAD'}: gradle publishToMavenLocal (cache MISS)")
            rc, output = _docker_gradle_install(
                path, ws.m2_cache, ws.gradle_cache, label=f"{ws.run_id}:{slug}",
                image=_image,
            )
        else:
            raise RuntimeError(
                f"install_compile_only_deps: {repo!r} has neither pom.xml nor "
                f"build.gradle[.kts]; cannot install. Add a manifest to the dep "
                f"or remove it from the run's dependencies."
            )

        if rc != 0:
            raise RuntimeError(
                f"install_compile_only_deps: {tool} install failed for {repo!r} "
                f"(exit={rc}):\n{output[-2000:]}"
            )
        if sha:
            _snapshot_jars_to_cache(slug, sha, ws.m2_cache)


def _detect_build_tool(path: str) -> str | None:
    """Return 'maven' if pom.xml exists, 'gradle' if build.gradle[.kts] exists, else None.

    Maven wins when both are present — projects that maintain both typically
    treat Maven as the source of truth.
    """
    if os.path.isfile(os.path.join(path, "pom.xml")):
        return "maven"
    if os.path.isfile(os.path.join(path, "build.gradle")) or \
       os.path.isfile(os.path.join(path, "build.gradle.kts")):
        return "gradle"
    return None


def stage_deps_for_cli(ws: MultiRepoWorkspace, clone_specs: list[CloneSpec]) -> None:
    """
    Make the staged dep checkouts safe for the headless CLI to see.

    The checkouts already live INSIDE the primary workspace (that's what makes
    them visible to the workspace-jailed CLI at all). This step strips the write
    bits from every `compile-only` checkout so the CLI can read them but cannot
    edit them — the binary does not honour `--permission-mode plan` as a write
    blocker, so EACCES is the only enforcement that actually holds.

    `editable` deps are deliberately left WRITABLE: the coder is supposed to
    change them, and they keep their real `.git` dir so they can be diffed
    independently.

    MUST be called AFTER `install_compile_only_deps` — `mvn install` writes
    `target/` into the checkout and would fail against a read-only tree.

    Caveat: this "would fail" only holds when a later `mvn install` is actually
    attempted as a non-root user. In practice a later install is normally
    short-circuited before it touches the tree at all (`_restore_jars_from_cache`
    on a content-addressed cache hit skips the install entirely), and the SDLC
    build container runs as **root**, which bypasses the mode bits this function
    strips — so the read-only chmod is defence-in-depth against accidental writes
    from tooling that honours permissions, not a hard guarantee against `mvn`
    itself re-writing the tree.

    Best-effort per dep: a chmod failure only means that dep stays writable, so
    it is logged as a WARNING and never raised.
    """
    if not clone_specs:
        return
    for spec in clone_specs:
        if spec.kind != "compile-only":
            continue
        path = ws.dep_paths.get(spec.repo)
        if not path:
            continue
        try:
            _chmod_tree(path, writable=False)
            try:
                rel = os.path.relpath(path, ws.primary_workspace).replace(os.sep, "/") \
                    if ws.primary_workspace else path.replace(os.sep, "/")
            except ValueError:
                rel = path.replace(os.sep, "/")
            logger.info(
                f"[MR-WS {ws.run_id}] staged dep for CLI run_id={ws.run_id} "
                f"repo={spec.repo} kind={spec.kind} rel_path={rel} readonly=True"
            )
        except Exception as e:
            logger.warning(
                f"[MR-WS {ws.run_id}] chmod read-only failed — dep stays writable "
                f"run_id={ws.run_id} repo={spec.repo} error={e}"
            )


def prepare_and_install_deps(
    run_id: str,
    primary_workspace: str,
    dep_rows: list,
    clone_url_resolver: Callable[[str], str],
    *,
    max_clone_concurrency: int = 4,
    skip_install: bool = False,
) -> "MultiRepoWorkspace":
    """
    End-to-end multi-repo prep from `sdlc_run_repos` rows: build CloneSpecs,
    clone every non-primary dep, topologically order the compile-only deps and
    `mvn install` / `gradle publishToMavenLocal` each one into the per-run
    `_m2_cache`, and return the populated MultiRepoWorkspace.

    This is the single shared entry point used by ALL of:
      * CodingStateMachine._setup_multi_repo_workspace, invoked from
        `_phase_implement` (agents/sdlc_state_machine.py),
      * the same helper invoked from `_run_plan_phase` (agents/sdlc_pipeline.py),
      * the WS-2 baseline build gate (_run_baseline_build in sdlc_pipeline.py), and
      * the baseline agent-fix loop (agents/sdlc_pipeline.py:4885-4893), which
        passes the primary workspace and builds internal deps once so the
        agent-fix recompile oracle sees the same locally-built jars,
    so the baseline build compiles the primary against the same locally-built
    internal jars the PLAN/IMPLEMENT builds do — otherwise a repo whose internal
    `org.ainxt.*` deps aren't published to Nexus fails the baseline compile with
    DEPENDENCY_MISSING and suspends a repo that actually builds fine.
    (There is no CODING phase any more — the 2026-07-01 three-phase CLI cutover
    replaced it with PLAN / IMPLEMENT / REVIEW.)

    Always ends with `stage_deps_for_cli`, which chmods the compile-only
    checkouts read-only. That runs strictly AFTER install, never before —
    `mvn install` writes `target/` into the checkout. See `stage_deps_for_cli`'s
    docstring for the caveat on how durable that read-only guarantee actually is
    against a later re-install (cache short-circuit + root-in-container).

    Args:
      dep_rows: the raw `sdlc_run_repos` rows for the run (primary + deps). Rows
        with kind == 'primary' are ignored — the primary workspace is passed in.
      clone_url_resolver: (gitlab_path) -> authenticated clone URL. Raises if a
        token is unavailable; the exception propagates so callers can fail/retry.
      skip_install: clone every dep (so editable deps are present for the coder)
        but DO NOT run the compile-only `mvn install` step. Used when the run has
        compilation globally skipped — there is no point building dep jars no
        downstream compile will consume.

    Returns the MultiRepoWorkspace (with `.m2_cache` populated). Raises
    RuntimeError on any clone or install failure — callers decide what that
    means for their phase: IMPLEMENT (`_phase_implement`) suspends the run;
    the WS-2 baseline build gate treats it as a non-transient baseline
    breakage (DEPENDENCY_MISSING, not a retryable transient); PLAN
    (`_run_plan_phase`) logs the failure and continues without the dep jars.
    """
    deps = [r for r in (dep_rows or []) if r.get("kind") != "primary"]
    if not deps:
        # Single-repo run — make the workspace dirs (cheap, idempotent) so the
        # return value is uniform, but no deps to clone/build.
        return prepare_multi_repo_workspace(run_id, primary_workspace, [])

    clone_specs = []
    for row in deps:
        gp = row.get("repo", "")
        clone_specs.append(CloneSpec(
            repo=gp,
            ref=row.get("ref") or "main",
            ref_sha=row.get("ref_sha") or "",
            clone_url=clone_url_resolver(gp),
            kind=row.get("kind") or "compile-only",
        ))

    ws = prepare_multi_repo_workspace(
        run_id, primary_workspace, clone_specs,
        max_clone_concurrency=max_clone_concurrency,
    )

    if skip_install:
        logger.info(
            f"[MR-WS {run_id}] compile-only install skipped (skip_install=True) — "
            f"deps cloned but not built"
        )
        stage_deps_for_cli(ws, clone_specs)
        return ws

    manifest_overrides = {
        row["repo"]: row.get("build_order")
        for row in deps
        if row.get("build_order") is not None
    }
    build_order = compute_build_order(clone_specs, ws, manifest_overrides=manifest_overrides)
    install_compile_only_deps(ws, build_order, clone_specs)
    # AFTER install — mvn writes target/ into the checkout, so read-only staging
    # must never precede it.
    stage_deps_for_cli(ws, clone_specs)
    return ws


def cleanup_multi_repo_workspace(run_id: str) -> None:
    """
    Remove the sibling multi-repo dir (NOT the primary's existing workspace).

    That sibling dir now holds only the `_m2_cache` / `_gradle_cache` build
    caches. Dep checkouts live INSIDE the primary workspace at
    `<primary_workspace>/.sdlc_deps/{slug}/`, so they die with the primary
    workspace and need no deletion path here — `_force_remove_dir` in
    `workers/workspace_sync_worker.py` already restores write bits across the
    tree before `rmtree`, so the read-only compile-only checkouts cannot wedge
    cleanup.

    Idempotent: safe to call when the workspace was never created.
    """
    if os.getenv("AINXT_KEEP_FAILED_WORKSPACE") == "1":
        return
    root = os.path.join(BUILDER_WORKSPACE_ROOT, "runs", f"{run_id}_multirepo")
    if not os.path.isdir(root):
        return
    try:
        shutil.rmtree(root, ignore_errors=False)
        logger.info(f"[MR-WS {run_id}] cleaned up multi-repo workspace {root}")
    except OSError as exc:
        logger.warning(f"[MR-WS {run_id}] cleanup failed for {root}: {exc}")


# ── Read-only staging helpers (moved from agents/sdlc_governance/engine.py) ──

def _chmod_tree(path: str, *, writable: bool) -> None:
    """Add/remove the write bit across a tree (read+execute bits untouched, so the
    CLI can still read files and run scripts). Removing write from the
    directories too is what actually blocks new-file creation inside them."""
    wbits = _stat.S_IWUSR | _stat.S_IWGRP | _stat.S_IWOTH
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, f) for f in files] \
                + [os.path.join(root, d) for d in dirs]:
            try:
                mode = os.stat(name).st_mode
                os.chmod(name, (mode | wbits) if writable else (mode & ~wbits))
            except OSError:
                pass


def _robust_rmtree(path: str) -> None:
    """Remove a dep checkout, coping with root-owned build residue.

    A prior run's dep build ran the container as root and may have left root-owned
    files (e.g. Maven `target/`) in the checkout, and `stage_deps_for_cli` may have
    stripped the write bits. So: restore write bits, try a hard rmtree; on a
    permission/OS error reclaim ownership (host `chown -R`, same as the build's
    chown-on-exit) and retry once. Best-effort — never raises; the caller inspects
    the path afterwards if it needs to know cleanup fully succeeded."""
    if not os.path.isdir(path):
        return
    _chmod_tree(path, writable=True)
    try:
        shutil.rmtree(path)
    except (PermissionError, OSError):
        _host_chown_dir(path)
        try:
            shutil.rmtree(path)
        except (PermissionError, OSError):
            pass


def _exclude_pattern_present(contents: str, pattern: str) -> bool:
    """Line-based membership test for a `.git/info/exclude`-style file.

    A `.gitignore`/exclude file is one pattern per line; a line starting with
    `#` is a comment, not a pattern. Splitting the whole file on ALL whitespace
    (the naive approach) misreads a comment such as
    `# .sdlc_deps/ handled elsewhere` as containing the pattern `.sdlc_deps/`,
    which would wrongly suppress the append while the exclude line was never
    actually written. Compare stripped, non-comment lines instead."""
    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == pattern:
            return True
    return False


def _git_exclude(workspace_root: str, pattern: str) -> bool:
    """Best-effort: add `pattern` to <workspace>/.git/info/exclude so the staged
    dep checkouts never show up in the review diff / VERIFIED_DIFF. Never raises.

    Returns True only when `pattern` is verifiably present in the exclude file
    after this call (already-present counts as True); returns False when
    `.git/info` doesn't exist or any error occurs — the caller (Fix C) uses this
    to distinguish "excluded" from "exclude silently failed" for its audit log.

    Membership (both the pre-write check and the post-write verification
    re-read) is line-based via `_exclude_pattern_present`, not a naive
    whitespace-token split — see that helper's docstring for why."""
    try:
        info_dir = os.path.join(workspace_root, ".git", "info")
        if not os.path.isdir(info_dir):
            return False
        exclude = os.path.join(info_dir, "exclude")
        # Lock the read-modify-append so parallel prep passes can't interleave-write
        # or double-append the pattern into the shared exclude file.
        with STAGE_LOCK:
            existing = ""
            if os.path.isfile(exclude):
                with open(exclude, "r", encoding="utf-8", errors="replace") as fh:
                    existing = fh.read()
            if not _exclude_pattern_present(existing, pattern):
                with open(exclude, "a", encoding="utf-8") as fh:
                    fh.write(("" if existing.endswith("\n") or not existing else "\n") + pattern + "\n")
            with open(exclude, "r", encoding="utf-8", errors="replace") as fh:
                return _exclude_pattern_present(fh.read(), pattern)
    except Exception:
        return False


# ── Workspace layout helpers ─────────────────────────────────────────────────

def _make_workspace_dirs(
    run_id: str,
    primary_workspace: str,
    *,
    create_deps_root: bool = True,
) -> MultiRepoWorkspace:
    """
    Create the sibling cache dirs and (when `create_deps_root`) the in-workspace
    `.sdlc_deps/` staging root.

    `deps_root` lives at `<primary_workspace>/.sdlc_deps` — INSIDE the primary
    workspace, because the headless CLI's file tools are jailed to the workspace
    cwd (see module docstring). The `_m2_cache` / `_gradle_cache` dirs stay in the
    sibling `{run_id}_multirepo/` dir: they are build scratch space and must never
    enter the primary repo's git tree.

    `create_deps_root=False` (single-repo runs, i.e. no dep rows) still resolves
    and returns the `deps_root` path but creates NOTHING and writes no exclude —
    a run with no deps must leave no `.sdlc_deps/` behind at all.
    """
    root = os.path.join(BUILDER_WORKSPACE_ROOT, "runs", f"{run_id}_multirepo")
    m2 = os.path.join(root, "_m2_cache")
    gr = os.path.join(root, "_gradle_cache")
    if primary_workspace:
        deps = os.path.join(primary_workspace, _SDLC_DEPS_DIRNAME)
    else:
        # NOTE: no primary workspace was resolved (callers should always pass one;
        # this only happens on degenerate/mock paths). Fall back to the legacy
        # sibling location rather than rooting the path at "" — the CLI won't see
        # these deps, but nothing is written to an unexpected filesystem location.
        deps = os.path.join(root, "deps")
    for d in (root, m2, gr):
        Path(d).mkdir(parents=True, exist_ok=True)
    if create_deps_root:
        # Exclude BEFORE creating the dir — reversing the order leaves a window in
        # which a concurrent `git add -A` could stage the whole vendored dep tree
        # into the customer's MR.
        excluded = False
        if primary_workspace:
            excluded = _git_exclude(primary_workspace, _SDLC_DEPS_DIRNAME + "/")
        Path(deps).mkdir(parents=True, exist_ok=True)
        if primary_workspace:
            if not excluded:
                # Loud on purpose: this is the audit signal that vendored dep source
                # could enter the diff. Fail-open (still create the dir, don't raise) —
                # the defence-in-depth guard in _collect_workspace_edits is the backstop.
                logger.error(
                    f"[MR-WS {run_id}] git exclude FAILED for dep staging root "
                    f"workspace={primary_workspace!r} deps_root={deps} — vendored dep "
                    "source could enter the diff"
                )
        else:
            # Degenerate/mock path: no primary workspace was resolved, so `deps`
            # is the legacy sibling location, not inside any git work tree. There
            # is no git exclude to fail here and nothing that can "enter the diff"
            # — the real consequence is that the workspace-jailed headless CLI
            # cannot see these deps at all (see module docstring).
            logger.warning(
                f"[MR-WS {run_id}] no primary workspace resolved — staging deps "
                f"outside the git tree at legacy sibling location deps_root={deps}; "
                "these deps will be invisible to the workspace-jailed CLI"
            )
        logger.info(
            f"[MR-WS {run_id}] dep staging root resolved run_id={run_id} "
            f"deps_root={deps} excluded={excluded}"
        )
    # Seed the per-run Maven cache from the shared cache so public deps (Spring,
    # JUnit, etc.) appear pre-cached and `mvn install` doesn't re-download every
    # transitive dep from Nexus inside a 30-min RQ job budget. Internal-dep
    # installs still write only to the per-run cache (no shared pollution).
    _seed_m2_cache_from_shared(m2)
    _seed_gradle_cache_from_shared(gr)
    return MultiRepoWorkspace(
        run_id=run_id,
        root=root,
        m2_cache=m2,
        gradle_cache=gr,
        deps_root=deps,
        primary_workspace=primary_workspace,
    )


def _seed_m2_cache_from_shared(per_run_m2: str) -> None:
    """
    Hardlink the shared maven cache contents into the per-run m2 cache.

    Why hardlinks (cp -al): hardlinks share inodes, so the per-run cache costs
    near-zero disk space for warm-up. Maven writes new downloads as new files
    (new inodes) so the per-run cache diverges from shared without modifying
    the shared cache's inodes. The shared cache survives as a permanent warm
    cache across runs and hosts.

    Fallback: if `cp -al` fails (cross-filesystem mount, permission, etc.), we
    fall back to a regular `cp -a` (full copy). Slower but still correct. If
    even that fails, the per-run cache stays empty and the build re-downloads
    everything — same behaviour as before this fix, just no speedup.
    """
    shared = _shared_m2_cache_path()
    if not shared or not os.path.isdir(shared):
        logger.info(f"[MR-WS] shared m2 cache not found at {shared!r}; per-run cache will start empty")
        return
    try:
        # Use shell-level `cp -al` for speed — Python's shutil.copytree doesn't
        # support hardlink mode. Pipe stderr away because cp prints a warning
        # for each file when -l can't be honoured (cross-fs), which would
        # flood the log; the return code is what matters.
        proc = subprocess.run(
            ["cp", "-al", f"{shared}/.", per_run_m2],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 0:
            logger.info(f"[MR-WS] seeded per-run m2 cache from {shared!r} via hardlinks")
            return
        logger.warning(
            f"[MR-WS] hardlink seed of m2 cache failed (rc={proc.returncode}); "
            f"falling back to full copy. stderr-tail={proc.stderr[-200:]!r}"
        )
    except subprocess.TimeoutExpired:
        logger.warning("[MR-WS] hardlink seed of m2 cache timed out; falling back to copy")
    except FileNotFoundError:
        logger.warning("[MR-WS] cp not available; falling back to shutil copy")
    except Exception as exc:
        logger.warning(f"[MR-WS] hardlink seed of m2 cache raised {exc!r}; falling back to copy")

    # Fallback: full copy. Slower but correct.
    try:
        for entry in os.listdir(shared):
            src = os.path.join(shared, entry)
            dst = os.path.join(per_run_m2, entry)
            if os.path.exists(dst):
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        logger.info(f"[MR-WS] seeded per-run m2 cache from {shared!r} via shutil.copytree")
    except Exception as exc:
        logger.warning(
            f"[MR-WS] full-copy seed of m2 cache failed ({exc}); per-run cache will start empty"
        )


# Maven writes per-repository bookkeeping files next to each artifact
# (`_remote.repositories`, `resolver-status.properties`, `*.lastUpdated`).
# They record which remote a file came from and which resolution attempts
# FAILED, and are only valid for the local repository that produced them —
# copying them into the shared cache can make a later build refuse to
# re-resolve an artifact ("was cached in the local repository, resolution will
# not be reattempted"). Never write these back.
_M2_LOCAL_STATE_SUFFIXES = (
    "_remote.repositories",
    "resolver-status.properties",
    ".lastUpdated",
    ".part",
    ".tmp",
    ".lock",
)

# Upper bound on files copied by one write-back so a pathological per-run cache
# can never turn cleanup into an unbounded job on a shared host.
_M2_WRITEBACK_MAX_FILES = int(os.getenv("M2_WRITEBACK_MAX_FILES", "20000"))
_M2_WRITEBACK_MAX_SECS = int(os.getenv("M2_WRITEBACK_MAX_SECS", "180"))


def merge_m2_cache_to_shared(per_run_m2: str, *, label: str = "") -> int:
    """
    Merge third-party artifacts from a per-run Maven cache back into the shared
    cache. Call ONLY after the run's build went green. Returns files written.

    Why: `_seed_m2_cache_from_shared` seeds each run FROM the shared cache but
    nothing ever wrote back, so the shared cache stayed cold and every run
    re-downloaded the same public dependencies from Nexus. This closes that loop
    while keeping the per-run cache isolated during the build.

    Safety properties (Maven is NOT safe for concurrent writes to one local
    repository, which is exactly why the per-run cache exists):
      • ADD-ONLY — a path already present in the shared cache is left alone, so
        a concurrent build reading that artifact never sees it change.
      • Each file lands via hardlink/copy to a temp name + `os.replace`, so a
        reader sees either no file or the complete file, never a partial one.
        Two runs racing on the same new artifact both write identical bytes.
      • Internal (`_INTERNAL_GROUP_PREFIXES`) artifacts are EXCLUDED — those are
        built from a specific dep SHA and belong in the content-addressed jar
        cache (`_snapshot_jars_to_cache`), not a version-keyed shared repo where
        a later run could silently pick up another run's snapshot build.
      • Maven's local bookkeeping files are excluded (see the suffix list).

    Bounded by _M2_WRITEBACK_MAX_FILES / _M2_WRITEBACK_MAX_SECS, both enforced
    per file scanned (not just per file written) so a warm cache cannot turn this
    into a full walk of the whole shared tree on the build's critical path.

    Best-effort: never raises — a failed write-back only means the shared cache
    stays as cold as it was before. It is NOT silent, though: publishing nothing
    while attempts failed is logged as a warning, because the likely cause is a
    root-owned shared cache this process cannot write into.

    Requires the shared cache tree to be writable by THIS process. Builder
    containers run as root and create the group directories inside it, so on a
    host where the workers run as a non-root user (the normal deployment) that
    ownership has to be reconciled for the write-back to do anything.
    """
    try:
        from core import config as _cfg
        if not _cfg.M2_SHARED_CACHE_WRITEBACK:
            return 0
    except Exception:
        return 0

    shared = _shared_m2_cache_path()
    if not per_run_m2 or not os.path.isdir(per_run_m2) or not shared:
        return 0
    try:
        Path(shared).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(f"[MR-WS {label}] m2 write-back skipped — shared cache not writable: {exc}")
        return 0

    # Top-level dirs owned by internal groups (e.g. "org" for "org.acme.") are
    # still traversed — only the internal prefix path itself is pruned, so
    # `org/springframework/...` is written back while `org/acme/...` is not.
    internal_rel = {
        os.path.join(*p.rstrip(".").split(".")) for p in _INTERNAL_GROUP_PREFIXES if p.strip(".")
    }

    written = failed = skipped_internal = scanned = 0
    started = time.monotonic()
    truncated = False

    for root, dirs, files in os.walk(per_run_m2):
        rel_dir = os.path.relpath(root, per_run_m2)
        rel_dir = "" if rel_dir == "." else rel_dir
        # Prune internal group subtrees in place so os.walk never descends them.
        # No `if rel_dir` guard: a SINGLE-component prefix (e.g. "acme.") lives at
        # the walk root, where a guarded block would skip it and leak internal
        # artifacts into the shared cache.
        kept = []
        for d in dirs:
            if (os.path.join(rel_dir, d) if rel_dir else d) in internal_rel:
                skipped_internal += 1
            else:
                kept.append(d)
        dirs[:] = kept

        for name in files:
            # Bound checked BEFORE the per-file `os.path.exists` probe below.
            # On a warm cache almost every file already exists, so a check placed
            # after that guard is never reached and the full shared-cache-sized
            # tree gets walked regardless of the limit — on the build's critical
            # path, up to 3x per run.
            scanned += 1
            if written >= _M2_WRITEBACK_MAX_FILES or \
                    (time.monotonic() - started) > _M2_WRITEBACK_MAX_SECS:
                truncated = True
                break
            if name.endswith(_M2_LOCAL_STATE_SUFFIXES):
                continue
            rel = os.path.join(rel_dir, name) if rel_dir else name
            dst = os.path.join(shared, rel)
            if os.path.exists(dst):
                continue
            if _publish_one_file(os.path.join(root, name), dst):
                written += 1
            else:
                failed += 1
        if truncated:
            break

    logger.info(
        f"[MR-WS {label}] m2 write-back → shared cache {shared}: "
        f"files_added={written} publish_failed={failed} scanned={scanned} "
        f"internal_subtrees_skipped={skipped_internal} "
        f"truncated={truncated} elapsed={time.monotonic() - started:.1f}s"
    )
    # A write-back that could not publish ANYTHING it tried is a degraded cache,
    # not a warm one: the usual cause is that the shared cache tree is owned by
    # root (every builder container runs as root) while this process is the
    # non-root worker, so both the temp-file create and the hardlink are denied.
    # Warn loudly — silence here is indistinguishable from "nothing new to add",
    # and the seeding direction already logs this class of degradation.
    if failed and not written:
        logger.warning(
            f"[MR-WS {label}] m2 write-back published NOTHING ({failed} attempt(s) "
            f"failed) — the shared cache stays cold. Most likely {shared} is "
            f"root-owned (builder containers run as root) and this worker process "
            f"cannot write into it; check ownership/permissions of that tree."
        )
    return written


def _publish_one_file(src: str, dst: str) -> bool:
    """
    Atomically add `src` to the shared cache at `dst` (hardlink, else copy).

    The temp file is created in the DESTINATION directory so `os.replace` is a
    same-filesystem rename (atomic). A hardlink keeps the shared cache free of
    duplicate bytes — the per-run cache is on the same filesystem here, and a
    cross-device link just falls back to a copy.
    """
    tmp = ""
    try:
        Path(os.path.dirname(dst)).mkdir(parents=True, exist_ok=True)
        tmp = f"{dst}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True
    except (OSError, shutil.Error):
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def _seed_gradle_cache_from_shared(per_run_gradle: str) -> None:
    """Same as _seed_m2_cache_from_shared but for the Gradle cache."""
    shared = _shared_gradle_cache_path()
    if not shared or not os.path.isdir(shared):
        return
    try:
        proc = subprocess.run(
            ["cp", "-al", f"{shared}/.", per_run_gradle],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 0:
            logger.info(f"[MR-WS] seeded per-run gradle cache from {shared!r} via hardlinks")
            return
    except Exception:
        pass
    # Skip the full-copy fallback for gradle — caches are typically big and
    # gradle is uncommon in our multi-repo dep set right now.


def _shared_m2_cache_path() -> str:
    """
    Resolve the shared maven cache path the WorkspaceBuilder uses.

    `sandbox/workspace_builder.py::_build_volumes` derives the host path as
    `{BUILDER_CACHE_ROOT}/{container_path.strip('/').replace('/', '_')}` and
    the container path for maven is `/root/.m2/repository` (per
    `core/build_manifest_resolver.py::_CACHE_CONTAINER_MAP['m2']`). So the
    host path is `{BUILDER_CACHE_ROOT}/root_.m2_repository`. We replicate that
    here rather than importing the map so this module stays standalone-callable.
    """
    try:
        from core import config as _cfg
        cache_root = _cfg.BUILDER_CACHE_ROOT
    except Exception:
        cache_root = os.getenv("BUILDER_CACHE_ROOT", "/opt/ainxt/build-cache")
    return os.path.join(cache_root, "root_.m2_repository")


def _shared_gradle_cache_path() -> str:
    try:
        from core import config as _cfg
        cache_root = _cfg.BUILDER_CACHE_ROOT
    except Exception:
        cache_root = os.getenv("BUILDER_CACHE_ROOT", "/opt/ainxt/build-cache")
    return os.path.join(cache_root, "root_.gradle")


def _slug_for(repo: str) -> str:
    """`group/project` -> `group__project` (filesystem-safe, reversible)."""
    return repo.replace("/", "__").replace("..", "_").strip()


# ── pom.xml parsing (subset of dep_resolver, duplicated to avoid coupling) ──

def _read_pom_field(content: str, tag: str) -> str:
    """Return the top-level <tag> text, ignoring nested occurrences."""
    m = re.search(
        rf"<project\b[^>]*>(?:(?!</project>).)*?<{tag}>([^<]+)</{tag}>",
        content,
        flags=re.DOTALL,
    )
    return (m.group(1).strip() if m else "")


def _pom_direct_deps(content: str) -> list[tuple[str, str]]:
    """Return (groupId, artifactId) pairs from direct <dependencies>; ignore <dependencyManagement>."""
    try:
        from xml.etree import ElementTree as ET
    except Exception:
        return []
    try:
        root = ET.fromstring(re.sub(r'\sxmlns="[^"]+"', "", content, count=1))
    except ET.ParseError:
        return []
    out: list[tuple[str, str]] = []

    def _walk(node, inside_mgmt: bool):
        for child in list(node):
            tag = child.tag
            if tag == "dependencyManagement":
                _walk(child, inside_mgmt=True)
            elif tag == "dependencies":
                for dep in child.findall("dependency"):
                    if inside_mgmt:
                        continue
                    gid = (dep.findtext("groupId") or "").strip()
                    aid = (dep.findtext("artifactId") or "").strip()
                    if gid and aid:
                        out.append((gid, aid))
            else:
                _walk(child, inside_mgmt)

    _walk(root, inside_mgmt=False)
    return out


def _is_internal_group(group_id: str) -> bool:
    if not group_id:
        return False
    g = group_id.strip().lower()
    return any(g.startswith(p) for p in _INTERNAL_GROUP_PREFIXES)


# ── Topological sort (Kahn) ──────────────────────────────────────────────────

def _kahn(edges: dict[str, set[str]]) -> list[str] | None:
    """
    Edges are 'depends-on': A -> B means A depends on B (so B installs first).
    Returns a list with prerequisites earlier than dependents, or None if there's a cycle.
    """
    # In-degree counts the number of nodes that A depends ON.
    in_deg = {n: len(deps) for n, deps in edges.items()}
    # Reverse adjacency so we can decrement in-degrees on B's dependents when B is emitted.
    rev: dict[str, set[str]] = {n: set() for n in edges}
    for a, deps in edges.items():
        for b in deps:
            if b in rev:  # ignore edges pointing to nodes outside the dep set
                rev[b].add(a)
    ready = [n for n, d in in_deg.items() if d == 0]
    out: list[str] = []
    while ready:
        ready.sort()  # deterministic order
        n = ready.pop(0)
        out.append(n)
        for dependent in rev[n]:
            in_deg[dependent] -= 1
            if in_deg[dependent] == 0:
                ready.append(dependent)
    return out if len(out) == len(edges) else None


# ── Docker invocation ────────────────────────────────────────────────────────

def _dep_chown_cmd() -> str:
    """
    In-container shell snippet that chowns /workspace back to the host
    UID/GID as the last build step (run after the build, pass or fail).

    Mirrors sandbox/workspace_builder.py exactly: the dep build container
    runs as root, so artifacts it writes into the bind-mounted /workspace
    (e.g. Maven target/) become root-owned on the host and the non-root
    worker cannot delete them on the next run. Chowning /workspace back to
    os.getuid()/os.getgid() lets host-side cleanup (_clone_one's rmtree)
    succeed. Best-effort — `2>/dev/null || true` so a chown failure never
    fails the build. On a dev box without os.getuid (Windows) we emit a
    no-op `true` so the command stays valid; containers never actually run
    there.

    Also hands back ownership of the per-run Maven cache mount, for the same
    reason and by the same rule (see `_m2_chown_cmd`): the non-root worker later
    hardlinks artifacts out of that cache into the shared one, which the kernel
    forbids for root-owned files it cannot write.
    """
    if hasattr(os, "getuid"):
        return (
            f"chown -R {os.getuid()}:{os.getgid()} /workspace 2>/dev/null || true"
            f" ; {_m2_chown_cmd()}"
        )
    return "true"


def _m2_chown_cmd() -> str:
    """
    In-container trailer handing the per-run Maven cache mount back to the host
    UID/GID. Delegates to `sandbox.workspace_builder.m2_cache_chown_cmd` when
    importable so the rule lives in exactly one place, and otherwise reproduces
    it inline — this module deliberately keeps a light import graph and must stay
    standalone-callable (see the `agents._stage_lock` note at the top).

    Scoped with `find ! -user` so a warm cache costs a stat walk instead of a
    full-tree rewrite. Best-effort: never fails the build.
    """
    if not hasattr(os, "getuid"):
        return "true"
    try:
        from sandbox.workspace_builder import m2_cache_chown_cmd
        return m2_cache_chown_cmd()
    except Exception:
        uid, gid = os.getuid(), os.getgid()
        return (
            f"find /root/.m2/repository ! -user {uid} "
            f"-exec chown {uid}:{gid} {{}} + 2>/dev/null || true"
        )


def _host_chown_dir(path: str) -> bool:
    """
    Best-effort host-side `chown -R <host-uid>:<host-gid> path` used by
    _clone_one to reclaim ownership of root-owned leftovers before retrying
    the cleanup. Uses the same uid/gid source as _dep_chown_cmd
    (os.getuid()/os.getgid()). Returns True if the chown was attempted and
    exited 0, False otherwise (including on Windows where os.getuid is
    absent). Never raises.
    """
    if not hasattr(os, "getuid"):
        return False
    try:
        proc = subprocess.run(
            ["chown", "-R", f"{os.getuid()}:{os.getgid()}", path],
            capture_output=True, text=True, timeout=120,
        )
        return proc.returncode == 0
    except Exception as exc:
        logger.warning(f"[MR-WS] host chown of {path!r} failed: {exc}")
        return False


def _docker_mvn_install(source_dir: str, m2_cache_dir: str, *, label: str,
                        image: str | None = None) -> tuple[int, str]:
    """
    Run `mvn -B -DskipTests install` inside the existing ainxt-builder-jvm-*
    container. Mirrors `sandbox/workspace_builder.py::_run` so the multi-repo
    dep install reuses every existing convention (image, network, Nexus
    settings.xml, cache layout).

    `image`: the exact builder image to run in. Callers pass the version-tagged
    image resolved from the dep's own declared Java version
    (`_resolve_jvm_builder_image(<major>)`). When omitted, falls back to the
    configured jvm default.

    Critical alignments vs the rest of the platform:
      • Image: `{BUILDER_REGISTRY}/ainxt-builder-jvm-21:latest` — pulled from
        the internal Docker registry, NOT Docker Hub. Air-gapped envs cannot
        reach Hub.
      • Network: `host` so the container can reach SANDBOX_MAVEN_REPO_URL
        (Nexus proxy). `--network none` would break every dep resolution.
      • Cache mount: bind the per-run `_m2_cache` at /root/.m2/REPOSITORY,
        not at /root/.m2. The builder image has /root/.m2/settings.xml baked
        in with Nexus credentials; covering the whole .m2 dir would hide it.
      • Command: `bash -lc "cd /workspace && mvn -B -DskipTests install"` so
        the container's login shell loads the same env (MAVEN_OPTS, JAVA_HOME)
        the rest of the build pipeline relies on.
      • Memory / CPU: the shared builder budget (see `_install_resource_kwargs`)
        — matches WorkspaceBuilder.

    Returns (exit_code, combined_stdout_stderr). Never raises.
    """
    image = image or _resolve_jvm_builder_image()
    res_kwargs = _install_resource_kwargs()

    # ── Up-front validation. Any empty value here would crash docker far
    # downstream with an opaque "invalid reference format" so catch it now.
    problems: list[str] = []
    if not (image and image.strip()):
        problems.append("builder image is empty (check BUILDER_REGISTRY / BUILDER_IMAGE_JVM env vars)")
    if not source_dir or ":" in source_dir:
        problems.append(f"source_dir invalid (must be non-empty, no colons): {source_dir!r}")
    if not m2_cache_dir or ":" in m2_cache_dir:
        problems.append(f"m2_cache_dir invalid (must be non-empty, no colons): {m2_cache_dir!r}")
    if problems:
        return 2, "mvn install aborted before docker invocation:\n  - " + "\n  - ".join(problems)

    # Maven writes its local cache at /root/.m2/repository — bind the per-run
    # cache there so settings.xml (one level up at /root/.m2/settings.xml,
    # baked into the image) survives.
    volumes = {
        source_dir:    {"bind": "/workspace",            "mode": "rw"},
        m2_cache_dir:  {"bind": "/root/.m2/repository",  "mode": "rw"},
    }
    # Maven 3.9+ breaks shade-plugin ≤ 2.x (same fix as workspace_builder.py).
    _shade_patch = (
        r"find /workspace -name 'pom.xml' | "
        r"xargs -r sed -zEi "
        r"'s|(<artifactId>maven-shade-plugin</artifactId>[[:space:]]*<version>)2\.[^<]*(</version>)|\13.3.0\2|g'"
        r" && "
    )
    # The container runs as root (it needs the baked-in /root/.m2/settings.xml
    # Nexus creds + the /root/.m2/repository cache mount, which a non-root UID
    # cannot traverse). As a side effect, build artifacts written to the
    # bind-mounted /workspace (e.g. Maven target/) end up root-owned on the host,
    # which the non-root app/worker process then cannot delete — leaving residue
    # that breaks the next `git clone` into the dep checkout. So after the build
    # (pass or fail) chown /workspace back to the host UID/GID so host-side
    # cleanup (_clone_one's rmtree) works. Mirrors sandbox/workspace_builder.py.
    # Only /workspace is chowned — the /root caches/settings are untouched.
    # `-T <n>` gives Maven one build thread per core for a multi-module dep;
    # without it the install stays single-threaded no matter how much CPU the
    # container is granted. Same flag the manifest resolver splices into the
    # primary build's default command (config.MAVEN_PARALLEL_FLAG).
    _par = (_cfg_maven_parallel_flag() + " ").lstrip()
    full_cmd = (
        f'bash -lc "cd /workspace && {{ {_shade_patch}mvn -B {_par}-DskipTests install ; }} ; '
        f'rc=$? ; {_dep_chown_cmd()} ; exit $rc"'
    )

    logger.info(
        f"[MR-WS {label}] mvn install: image={image} workspace={source_dir} "
        f"m2={m2_cache_dir} network=host mem={res_kwargs.get('mem_limit')} "
        f"cpu_quota={res_kwargs.get('cpu_quota', 'unlimited')}"
    )

    timeout = int(os.getenv("MULTI_REPO_INSTALL_TIMEOUT") or "1800")

    try:
        import docker as _docker
        from docker.errors import DockerException
        from requests.exceptions import ReadTimeout
    except Exception as exc:
        return 1, f"docker SDK unavailable on runtime host: {exc}"

    container = None
    try:
        client = _docker.from_env()
        container = client.containers.run(
            image=image,
            command=full_cmd,
            volumes=volumes,
            network_mode="host",
            remove=False,
            detach=True,
            **res_kwargs,
        )
        try:
            result = container.wait(timeout=timeout)
            exit_code = int(result.get("StatusCode", 1))
        except ReadTimeout:
            try:
                container.kill()
            except Exception:
                pass
            return 124, f"mvn install timed out after {timeout}s"
        output = container.logs(stdout=True, stderr=True).decode(errors="replace")
        return exit_code, output
    except DockerException as exc:
        return 1, f"docker error: {exc}"
    except Exception as exc:
        return 1, f"docker invocation failed: {exc}"
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass


def _docker_gradle_install(
    source_dir: str,
    m2_cache_dir: str,
    gradle_cache_dir: str,
    *,
    label: str,
    image: str | None = None,
) -> tuple[int, str]:
    """
    Run `gradle publishToMavenLocal` inside the SAME `ainxt-builder-jvm-*`
    container Maven uses. That image bundles both `mvn` and `gradle`, plus
    the baked-in Nexus credentials at `/root/.gradle/init.d/*.gradle`.

    Why publishToMavenLocal: it writes the produced jar(s) to
    `/root/.m2/repository`, which is bound to the per-run `_m2_cache` — so
    the downstream Maven compile (and downstream Gradle if we add one) finds
    the dep at the standard local-Maven coordinate exactly as if it were an
    `mvn install`.

    Requires the dep's build.gradle to apply the `maven-publish` plugin AND
    declare a publication block. If the dep is a library produced by Spring
    Initializr or follows the common Gradle library convention, this is
    usually already in place. If `publishToMavenLocal` produces no artifacts
    we still return exit 0 — the downstream compile will fail with a
    Maven-not-found error pointing at the missing GAV, which is the most
    actionable signal for the dep owner.

    Returns (exit_code, combined_stdout_stderr). Never raises.
    """
    image = image or _resolve_jvm_builder_image()
    res_kwargs = _install_resource_kwargs()

    problems: list[str] = []
    if not (image and image.strip()):
        problems.append("builder image is empty (check BUILDER_REGISTRY / BUILDER_IMAGE_JVM env vars)")
    if not source_dir or ":" in source_dir:
        problems.append(f"source_dir invalid (must be non-empty, no colons): {source_dir!r}")
    if not m2_cache_dir or ":" in m2_cache_dir:
        problems.append(f"m2_cache_dir invalid (must be non-empty, no colons): {m2_cache_dir!r}")
    if not gradle_cache_dir or ":" in gradle_cache_dir:
        problems.append(f"gradle_cache_dir invalid (must be non-empty, no colons): {gradle_cache_dir!r}")
    if problems:
        return 2, "gradle install aborted before docker invocation:\n  - " + "\n  - ".join(problems)

    # Prefer the wrapper if present so the dep's pinned gradle version wins
    # over whatever the builder image ships with. `chmod +x` is harmless even
    # if gradlew is already executable on the host (git on Linux usually
    # preserves the bit, but clones over an NFS share with `noexec` can drop
    # it and that's the failure mode this guards against).
    gradlew = os.path.join(source_dir, "gradlew")
    if os.path.isfile(gradlew):
        gradle_cmd = "chmod +x ./gradlew && ./gradlew -x test publishToMavenLocal --no-daemon"
    else:
        gradle_cmd = "gradle -x test publishToMavenLocal --no-daemon"

    # --no-daemon: ephemeral container — no point spawning a long-lived daemon
    # that exits with the container anyway.

    # /root/.gradle hosts gradle's caches + the baked-in init.d scripts. We
    # bind the per-run gradle cache there. If the builder image stores init
    # scripts UNDER /root/.gradle, this mount would hide them — confirm with
    # ops if Nexus access fails inside the container. Mirrors the
    # _CACHE_CONTAINER_MAP convention used by sandbox/workspace_builder.py.
    volumes = {
        source_dir:        {"bind": "/workspace",            "mode": "rw"},
        m2_cache_dir:      {"bind": "/root/.m2/repository",  "mode": "rw"},
        gradle_cache_dir:  {"bind": "/root/.gradle",         "mode": "rw"},
    }
    # Same root-owned-residue fix as the mvn path: chown /workspace back to the
    # host UID/GID after the build (pass or fail) so the next run's _clone_one
    # rmtree can clean the dep checkout. Mirrors sandbox/workspace_builder.py.
    full_cmd = (
        f'bash -lc "cd /workspace && {{ {gradle_cmd} ; }} ; '
        f'rc=$? ; {_dep_chown_cmd()} ; exit $rc"'
    )

    logger.info(
        f"[MR-WS {label}] gradle install: image={image} workspace={source_dir} "
        f"m2={m2_cache_dir} gradle={gradle_cache_dir} network=host "
        f"mem={res_kwargs.get('mem_limit')} "
        f"cpu_quota={res_kwargs.get('cpu_quota', 'unlimited')}"
    )

    timeout = int(os.getenv("MULTI_REPO_INSTALL_TIMEOUT") or "1800")

    try:
        import docker as _docker
        from docker.errors import DockerException
        from requests.exceptions import ReadTimeout
    except Exception as exc:
        return 1, f"docker SDK unavailable on runtime host: {exc}"

    container = None
    try:
        client = _docker.from_env()
        container = client.containers.run(
            image=image,
            command=full_cmd,
            volumes=volumes,
            network_mode="host",
            remove=False,
            detach=True,
            **res_kwargs,
        )
        try:
            result = container.wait(timeout=timeout)
            exit_code = int(result.get("StatusCode", 1))
        except ReadTimeout:
            try:
                container.kill()
            except Exception:
                pass
            return 124, f"gradle install timed out after {timeout}s"
        output = container.logs(stdout=True, stderr=True).decode(errors="replace")
        return exit_code, output
    except DockerException as exc:
        return 1, f"docker error: {exc}"
    except Exception as exc:
        return 1, f"docker invocation failed: {exc}"
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass


# ── Content-addressed jar cache ──────────────────────────────────────────────

def _cache_dir_for(slug: str, sha: str) -> str:
    return os.path.join(BUILDER_WORKSPACE_ROOT, "cache", "multirepo_jars", slug, sha)


def _snapshot_jars_to_cache(slug: str, sha: str, m2_cache_dir: str) -> None:
    """
    After a successful `mvn install`, copy the just-produced internal jars
    from `_m2_cache` to the content-addressed cache.

    `m2_cache_dir` is bound at `/root/.m2/repository` inside the container,
    so on the host its contents are the maven repository layout directly
    (e.g. `_m2_cache/org/ainxt/payments-sdk/1.0.0/*.jar`).

    We only snapshot artifacts under the configured internal group prefixes —
    third-party jars are produced by mvn anyway and would bloat the cache.
    """
    cache = _cache_dir_for(slug, sha)
    Path(cache).mkdir(parents=True, exist_ok=True)
    if not os.path.isdir(m2_cache_dir):
        return
    count = 0
    for prefix in _INTERNAL_GROUP_PREFIXES:
        sub = os.path.join(m2_cache_dir, *prefix.rstrip(".").split("."))
        if not os.path.isdir(sub):
            continue
        # Mirror the maven repo layout under the cache dir so restore is a
        # simple copy back. cache/{slug}/{sha}/org/ainxt/...
        dest = os.path.join(cache, *prefix.rstrip(".").split("."))
        Path(os.path.dirname(dest)).mkdir(parents=True, exist_ok=True)
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(sub, dest, dirs_exist_ok=True)
        count += 1
    logger.info(f"[MR-WS] snapshotted {count} internal prefix(es) to {cache}")


def _restore_jars_from_cache(slug: str, sha: str, m2_cache_dir: str) -> bool:
    """
    Copy cached jars for (slug, sha) into _m2_cache. Returns True on cache hit.

    `m2_cache_dir` is the per-run maven repository (bound at
    /root/.m2/repository inside the container), so we copy directly into it.

    We *copy* rather than symlink because a subsequent install in the same run
    will mutate this dir; symlinks would corrupt the cache snapshot.
    """
    cache = _cache_dir_for(slug, sha)
    if not os.path.isdir(cache):
        return False
    Path(m2_cache_dir).mkdir(parents=True, exist_ok=True)
    restored = False
    for prefix in _INTERNAL_GROUP_PREFIXES:
        src = os.path.join(cache, *prefix.rstrip(".").split("."))
        if not os.path.isdir(src):
            continue
        dst = os.path.join(m2_cache_dir, *prefix.rstrip(".").split("."))
        Path(os.path.dirname(dst)).mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        restored = True
    return restored


# ── subprocess wrapper (mirrors workers/workspace_sync_worker._run) ──────────

def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
    return proc
