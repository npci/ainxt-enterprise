# SPDX-License-Identifier: MIT
"""
core/build_manifest_resolver.py

Resolves a BuildManifest for a given repo.  Used by the SDLC execution layer
(workspace_builder.py) before every compile/test run.

Resolution priority:
  1. repo_build_manifests DB cache (previously resolved + feedback-adjusted)
  2. repo_build_metadata   (structured, extracted at index time)
  3. document_embeddings   (raw chunks — used when metadata not yet extracted)
  4. workspace filesystem  (last resort for recently-cloned, unindexed repos)
  5. None → UNKNOWN_BUILD_PATTERN (stops pipeline, queues .sdlc.yml MR)
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, asdict
from sqlalchemy import text
from core import config
from core.logger import logger


@dataclass
class BuildManifest:
    repo_slug:     str
    image:         str            # ainxt-builder-jvm-21 / ainxt-builder-node-20 / etc.
    compile_cmd:   str
    test_cmd:      str
    env_vars:      dict           # MAVEN_OPTS, NODE_OPTIONS, etc. (NOT version — version = image)
    cache_paths:   list[str]      # container paths mounted from BUILDER_CACHE_ROOT
    timeout:       int            # seconds
    detected_by:   str            # e.g. "pom.xml" | ".gitlab-ci.yml" | "repo_build_metadata"
    confidence:    float          # 0.0–1.0


# Build-tool → (fallback_image, cache_keys)
# The fallback_image is only used when version detection fails entirely.
# Normally _select_versioned_image() picks the exact version-tagged image.
_TOOL_MAP = {
    "maven":  (config.BUILDER_IMAGE_JVM,     ["m2"]),
    "gradle": (config.BUILDER_IMAGE_JVM,     ["m2", "gradle"]),
    "npm":    (config.BUILDER_IMAGE_NODE,    ["npm"]),
    "yarn":   (config.BUILDER_IMAGE_NODE,    ["yarn"]),
    "pnpm":   (config.BUILDER_IMAGE_NODE,    ["pnpm"]),
    "pip":    (config.BUILDER_IMAGE_PYTHON,  ["pip", "venv"]),
    "poetry": (config.BUILDER_IMAGE_PYTHON,  ["pip", "poetry", "venv"]),
    "go":     (config.BUILDER_IMAGE_SYSTEMS, ["go"]),
    "cargo":  (config.BUILDER_IMAGE_SYSTEMS, ["cargo"]),
    "make":   (config.BUILDER_IMAGE_JVM,     []),
}


def _select_versioned_image(build_tool: str, version: str, fallback: str) -> str:
    """
    Map build tool + detected version string → the specific pre-approved builder image.
    The version is detected from pom.xml / package.json / go.mod etc. and used to
    select the right image at resolve time — no runtime version switching needed.
    """
    registry = f"{config.BUILDER_REGISTRY}/" if config.BUILDER_REGISTRY else ""

    if build_tool in ("maven", "gradle"):
        major = version.split(".")[0] if version else ""
        tag = major if major in ("17", "21", "25") else "21"  # 21 = current LTS default
        return f"{registry}ainxt-builder-jvm-{tag}:latest"

    if build_tool in ("npm", "yarn", "pnpm"):
        major = version.split(".")[0] if version else ""
        tag = major if major in ("18", "20", "22") else "20"  # 20 = current LTS default
        return f"{registry}ainxt-builder-node-{tag}:latest"

    if build_tool in ("pip", "poetry"):
        parts = (version or "").split(".")
        tag = f"{parts[0]}{parts[1]}" if len(parts) >= 2 else ""
        tag = tag if tag in ("310", "311", "312") else "311"  # 3.11 = stable default
        return f"{registry}ainxt-builder-python-{tag}:latest"

    # go / cargo / make / unknown — use the fallback from _TOOL_MAP
    return fallback

# The platform's OWN default Maven compile command, in either its original or
# already-tuned shape (see `tune_maven_compile_command`). Matching the tuned
# shape too keeps the rewrite idempotent AND reversible: a manifest persisted
# with the tuned command is re-derived from current config on the next resolve,
# so flipping MAVEN_PARALLEL_FLAG / MAVEN_SKIP_CLEAN takes effect for repos that
# already have a cached row instead of being frozen at the value used when the
# row was written.
#
# The optional group matches ONE `-T` token group, which is what
# config.MAVEN_PARALLEL_FLAG is documented to hold. A multi-flag value would
# produce a command this pattern no longer recognises, costing reversibility
# (not correctness — an unrecognised command is simply left alone).
_MVN_DEFAULT_COMPILE_RE = re.compile(
    r"^mvn(?:\s+-T\s*\S+)?\s+(?:clean\s+)?install\s+-DskipTests\s+-q$"
)


def tune_maven_compile_command(cmd: str) -> str:
    """
    Apply the configured Maven build tuning to OUR default compile command.

    Two changes, both config-gated (core/config):
      • `-T <MAVEN_PARALLEL_FLAG>` — Maven is single-threaded by default, so a
        multi-module reactor compiles one module at a time and the container's
        CPU budget goes unused.
      • drop `clean` (MAVEN_SKIP_CLEAN) — `clean` deletes every `target/` dir,
        forcing a full recompile of the whole reactor on each run even when the
        dependency cache is warm.

    A command that is NOT our default (i.e. supplied by the repo through
    .sdlc.yml / .gitlab-ci.yml, or hand-tuned by a maintainer) is returned
    unchanged — those are the repo owner's and must never be rewritten.

    Caveat on dropping `clean`: an incremental build can retain a class file
    whose source was deleted. SDLC runs build in a per-run checkout, so the only
    exposure is the legacy shared-per-repo workspace path in WorkspaceBuilder;
    set MAVEN_SKIP_CLEAN=false to restore `mvn clean install` if that matters.
    """
    if not cmd or not _MVN_DEFAULT_COMPILE_RE.match(cmd.strip()):
        return cmd
    parts = ["mvn"]
    if flag := (config.MAVEN_PARALLEL_FLAG or "").strip():
        parts.append(flag)
    if not config.MAVEN_SKIP_CLEAN:
        parts.append("clean")
    parts += ["install", "-DskipTests", "-q"]
    return " ".join(parts)


def pip_cache_setup_cmd() -> str:
    """
    Shell snippet that makes the mounted pip cache dir usable by pip.

    pip's `check_path_owner` heuristic has two branches, and the fix differs:
      • euid 0 (container running as root): the dir must be OWNED by uid 0.
        Mode bits are irrelevant — a 0777 dir owned by the host app user is
        still rejected with "The directory '/cache/pip' … is not owned or is
        not writable by the current user … cache has been disabled", after
        which every build re-downloads its wheels from PyPI.
      • non-root: plain write access is enough.

    `chown` to the CONTAINER's own euid/egid satisfies both branches without the
    caller having to know which UID the image runs as — the earlier hardcoded
    `chown -R 0:0` only worked while the container was root.

    Best-effort throughout — a `chown` that cannot be honoured (e.g. a
    root-squashed NFS mount) must not fail the build, only lose the cache.
    Emitted for EVERY Python build, including when PYTHON_USE_PERSISTENT_VENV is
    off: the ownership mismatch is a property of the mounted host dir, not of the
    venv strategy, and previously the fix was reachable only through the venv
    wrapper — so with the venv disabled pip silently ran with caching off.
    """
    pip_c = config.PIP_CACHE_CONTAINER_PATH
    return (
        f'mkdir -p {pip_c} 2>/dev/null ; '
        f'chown -R "$(id -u):$(id -g)" {pip_c} 2>/dev/null || true ; '
        # Keep the dir group/other-writable as well: builder images do not all run
        # as the same UID, and a cache owned 0:0 with owner-only bits would be
        # unwritable (cache silently disabled) for a non-root one. `a+rwX` grants
        # the traverse/write access needed without marking cached wheels
        # executable the way the previous blanket `chmod 777` did.
        f'chmod -R a+rwX {pip_c} 2>/dev/null || true'
    )


def _python_build_commands(build_cmd: str, test_cmd: str) -> tuple[str, str]:
    """
    Wrap Python compile/test commands to use a persistent venv and the
    configured pip cache dir.  Called only when PYTHON_USE_PERSISTENT_VENV=true.

    Default commands (requirements.txt + pytest) are rewritten to use explicit
    venv paths.  Custom commands from .sdlc.yml / .gitlab-ci.yml are prefixed
    with PATH so that pip/pytest/python resolve to the venv binaries without
    rewriting every possible invocation style.
    """
    venv  = config.PYTHON_VENV_CONTAINER_PATH
    pip_c = config.PIP_CACHE_CONTAINER_PATH
    # Ensure the mounted pip cache dir exists and is writable inside the
    # container regardless of which UID created it on the host.  Without
    # this, pip emits "The directory '/cache/pip' … is not owned or is not
    # writable by the current user" and disables caching entirely.
    #
    # Create the venv with --system-site-packages so the test tooling baked into
    # the builder image (pytest, pytest-cov, pytest-asyncio, coverage — see
    # docker/ainxt-builder-python-3xx/Dockerfile) stays importable inside the
    # venv even when the repo's requirements.txt omits it.  Without the flag the
    # venv is fully ISOLATED, the image's system-wide pytest is invisible, and
    # {venv}/bin/pytest never exists → "/venv/bin/pytest: no such file/folder".
    # Re-running venv on the persistent mounted volume just refreshes pyvenv.cfg
    # (no --clear), so already-installed packages survive.
    setup = (
        f"{pip_cache_setup_cmd()} && "
        f"python -m venv --system-site-packages {venv} 2>/dev/null || true"
    )

    _default_compile = "pip install -r requirements.txt -q"
    _default_test    = "pytest -q"

    if not build_cmd or build_cmd.strip() == _default_compile:
        final_compile = (
            f"{setup} && "
            f"{venv}/bin/pip install --cache-dir {pip_c} -r requirements.txt -q"
        )
    else:
        final_compile = f"{setup} && export PATH={venv}/bin:$PATH && {build_cmd}"

    # The test phase runs in a SEPARATE container from compile.  Run setup here
    # too so the venv is guaranteed to exist (compile may have been skipped or
    # its cache populated on another host), and invoke pytest via the interpreter
    # module form ({venv}/bin/python -m pytest) instead of the {venv}/bin/pytest
    # console script.  The module form resolves through --system-site-packages,
    # so it runs whether pytest sits in the venv (from requirements.txt) or only
    # in the image's system Python, while still seeing the repo deps installed
    # into the venv by compile.
    if not test_cmd or test_cmd.strip() == _default_test:
        final_test = f"{setup} && {venv}/bin/python -m pytest -q"
    else:
        final_test = f"{setup} && export PATH={venv}/bin:$PATH && {test_cmd}"

    return final_compile, final_test


def _java_test_classes(rel_paths: list[str]) -> list[str]:
    """Simple class names from *.java / *.kt / *.scala test file paths. Both
    Surefire's `-Dtest=` and Gradle's `--tests` match on the simple class name,
    so the package prefix is dropped."""
    import posixpath
    out: list[str] = []
    for p in rel_paths:
        base = posixpath.basename((p or "").replace("\\", "/"))
        for ext in (".java", ".kt", ".scala"):
            if base.endswith(ext):
                cls = base[: -len(ext)]
                if cls and cls not in out:
                    out.append(cls)
                break
    return out


def scoped_test_command(test_cmd: str, rel_test_paths: list[str]) -> str | None:
    """Narrow the repo's full `test_cmd` to run ONLY the test files this SDLC run
    changed/added. Same methodology as compile — the command still runs in the
    builder container at cwd=/workspace against the mounted workspace — only the
    target set is scoped to the run's own tests (paths are repo-relative).

    Returns the scoped command, or None when the runner can't be scoped safely
    (unknown/custom runner, cargo, make, …). The caller falls back to the full
    suite on None, so this never blocks a run it can't scope.

    Runner detection is off the resolved `test_cmd` string (what actually runs),
    not the declared language — that stays correct through the venv/PATH wrappers
    `_python_build_commands` adds and through .gitlab-ci.yml overrides."""
    import shlex
    import posixpath

    if not test_cmd or not rel_test_paths:
        return None

    # Normalize to POSIX (container FS), strip leading slashes, de-dupe in order.
    paths: list[str] = []
    for p in rel_test_paths:
        p = (p or "").replace("\\", "/").lstrip("/")
        if p and p not in paths:
            paths.append(p)
    if not paths:
        return None

    tc = test_cmd
    tcl = tc.lower()

    # ── pytest (Python) — file-level ─────────────────────────────────
    # Valid for every form we emit: `pytest -q`, `python -m pytest -q`, and the
    # persistent-venv `… && {venv}/bin/python -m pytest -q`. pytest takes any
    # number of file paths positionally.
    if "pytest" in tcl:
        return tc + " " + " ".join(shlex.quote(p) for p in paths)

    # ── Jest / Vitest / Mocha (Node) — file-level ────────────────────
    # All three accept positional file paths/patterns. When run through a
    # package-manager script (`npm test`) the args must cross `--` to reach the
    # underlying runner.
    _node_runner = any(r in tcl for r in ("jest", "vitest", "mocha"))
    _pm_script = any(
        s in tcl for s in (
            "npm test", "npm run test", "yarn test", "yarn run test",
            "pnpm test", "pnpm run test",
        )
    )
    if _node_runner or _pm_script:
        joined = " ".join(shlex.quote(p) for p in paths)
        if _pm_script and " -- " not in f" {tc} ":
            return f"{tc} -- {joined}"
        return f"{tc} {joined}"

    # ── Go — package-level (go tests run per package, never per file) ─
    # Word-boundary match so `cargo test` (…car‹go test›) doesn't hit this branch.
    if re.search(r"\bgo\s+test\b", tcl):
        pkgs: list[str] = []
        for p in paths:
            d = posixpath.dirname(p)
            pkg = "./" + d if d else "."
            if pkg not in pkgs:
                pkgs.append(pkg)
        pkg_args = " ".join(shlex.quote(p) for p in pkgs)
        # Replace the whole-tree target if present; otherwise append the packages.
        if "./..." in tc:
            return tc.replace("./...", pkg_args)
        return f"{tc} {pkg_args}"

    # ── Maven — class-level via Surefire -Dtest ──────────────────────
    if "mvn" in tcl:
        classes = _java_test_classes(paths)
        if not classes:
            return None
        # failIfNoTests=false: a name that matches nothing must not hard-fail the
        # phase (e.g. a helper/support file mis-flagged as a test).
        return f"{tc} -Dtest={','.join(classes)} -DfailIfNoTests=false"

    # ── Gradle — class-level via --tests "*.Class" ───────────────────
    if "gradle" in tcl:
        classes = _java_test_classes(paths)
        if not classes:
            return None
        return tc + " " + " ".join(f'--tests {shlex.quote("*." + c)}' for c in classes)

    # Unknown / custom runner (cargo, make, bespoke scripts) — don't guess.
    return None


# Container-internal cache paths that map to host cache dirs.
# pip/poetry use /cache/* instead of /root/.cache/* to avoid root-ownership
# errors when the host directory is created by a different UID.
_CACHE_CONTAINER_MAP = {
    "m2":      "/root/.m2/repository",
    "gradle":  "/root/.gradle",
    "npm":     "/root/.npm",
    "yarn":    "/root/.cache/yarn",
    "pnpm":    "/root/.local/share/pnpm/store",
    "pip":     config.PIP_CACHE_CONTAINER_PATH,
    "poetry":  config.POETRY_CACHE_CONTAINER_PATH,
    "venv":    config.PYTHON_VENV_CONTAINER_PATH,
    "go":      "/root/go/pkg",
    "cargo":   "/root/.cargo/registry",
}


# ── Cache-state detection (Part C) ───────────────────────────────
#
# The persistent dependency cache (venv / m2 / npm / go / cargo) is only useful
# if it actually matches the repo's current dependency inputs.  Keying the cache
# by repo_slug alone (the historical behaviour) hides drift: when a lockfile
# changes the stale cache still
# holds the old version and the dependency step silently re-downloads gigabytes.
# We fix this by hashing the resolved dependency inputs per language and storing
# the hash next to the cache.  cache_state() compares current vs stored to return
# HIT (unchanged, use warm timeout) | STALE (lockfile changed, use cold timeout)
# | COLD (never built / no key, use cold timeout).  This is language-agnostic.

# Dependency (lockfile) filenames per builder-image family.  Order does not
# matter — the hash is computed over the sorted set of files that exist.
_DEP_FILES_BY_FAMILY = {
    "python":  ["requirements.txt", "requirements-dev.txt", "constraints.txt",
                "poetry.lock", "pyproject.toml", "Pipfile.lock", "setup.py", "setup.cfg"],
    "node":    ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
                "package.json"],
    "jvm":     ["pom.xml", "build.gradle", "build.gradle.kts",
                "gradle.lockfile", "settings.gradle", "settings.gradle.kts"],
    "systems": ["go.sum", "go.mod", "Cargo.lock", "Cargo.toml"],
}

# Directories never walked when collecting lockfiles — build output / vendored deps.
_HASH_SKIP_DIRS = {
    ".git", "node_modules", "target", "venv", ".venv", "build", "dist",
    "__pycache__", ".gradle", ".mvn", "vendor", ".idea", ".tox", "site-packages",
}

# Name of the directory where the SDLC multi-repo pipeline stages dependent-repo
# checkouts INSIDE the primary run workspace (<primary_workspace>/.sdlc_deps/{slug}/),
# so the deployed headless ainxt CLI — which jails its file tools to the session
# cwd — can still read them. Authoritative definition:
# agents/multi_repo_workspace.py::_SDLC_DEPS_DIRNAME. Kept as a local constant
# (not imported) to avoid a core → agents dependency; keep the value in sync
# manually if it ever changes there. Pruned only at the workspace ROOT level in
# compute_lockfile_hash() (see below), not at every depth, so a legitimately
# named nested directory deeper in a customer repo is never silently skipped.
_SDLC_DEPS_DIRNAME = ".sdlc_deps"


def _image_family(image: str) -> str:
    """Map a builder image name to its language family (python/node/jvm/systems)."""
    for fam in ("python", "node", "jvm", "systems"):
        if fam in (image or ""):
            return fam
    return ""


def compute_lockfile_hash(image: str, workspace: str) -> str:
    """
    SHA256 over every dependency/lockfile in the workspace that belongs to the
    manifest's language family.  Multi-module repos (nested pom.xml, workspace
    package.json) are covered by walking the tree, pruning build/vendored dirs.

    Returns "" when no recognizable lockfile exists — the caller then can't
    verify warmth and treats the cache as COLD (safe: generous timeout ceiling).
    """
    fam = _image_family(image)
    names = set(_DEP_FILES_BY_FAMILY.get(fam, []))
    if not names or not workspace or not os.path.isdir(workspace):
        return ""

    found: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _HASH_SKIP_DIRS]
        # Multi-repo runs stage dependent-repo checkouts inside the primary
        # workspace at <workspace>/.sdlc_deps/{slug}/ so the CLI's file tools
        # (jailed to the session cwd) can read them. Those dep repos' own
        # lockfiles (pom.xml, package.json, ...) are not part of the PRIMARY
        # repo's dependency surface — folding them in would make the primary's
        # cache-warmth hash change whenever a staged dep changes (or a
        # different dep set is staged), mis-selecting the HIT/STALE/COLD
        # timeout tier for a primary build whose own deps are unchanged.
        # Prune only at the workspace root — os.walk's first-yielded `root`
        # is always the literal `workspace` argument — so a legitimately
        # named nested directory deeper in a customer repo is not skipped.
        if root == workspace:
            dirs[:] = [d for d in dirs if d != _SDLC_DEPS_DIRNAME]
        for fn in files:
            if fn in names:
                found.append(os.path.join(root, fn))
    if not found:
        return ""

    h = hashlib.sha256()
    for path in sorted(found):
        try:
            rel = os.path.relpath(path, workspace).replace(os.sep, "/")
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def _cache_key_path(repo_slug: str) -> str:
    """Per-repo marker file recording the lockfile hash of the last good build.

    Stored under BUILDER_CACHE_ROOT/keys/{slug}.key rather than inside the tool
    cache dir, because several tool caches (m2, npm) are shared across repos and
    a per-repo marker cannot live there without colliding.
    """
    return os.path.join(
        config.BUILDER_CACHE_ROOT, "keys", _canonical_slug(repo_slug) + ".key"
    )


def read_cache_key(repo_slug: str) -> str:
    try:
        with open(_cache_key_path(repo_slug), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_cache_key(repo_slug: str, lockfile_hash: str) -> None:
    """Persist the lockfile hash after a successful build so the next run is HIT."""
    if not lockfile_hash:
        return
    path = _cache_key_path(repo_slug)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(lockfile_hash)
    except OSError as exc:
        logger.warning(f"manifest_resolver: could not write cache key for {repo_slug}: {exc}")


def cache_state(manifest: "BuildManifest", workspace: str) -> tuple[str, str]:
    """
    Return (verdict, current_hash) where verdict is HIT | STALE | COLD.

      HIT   — stored lockfile hash matches the workspace → deps already warm.
      STALE — stored hash exists but differs → lockfile changed, expect a rebuild.
      COLD  — no stored hash (never built) or no recognizable lockfile.

    Only HIT is safe to run under the warm BUILD_TIMEOUT_SECS budget; STALE and
    COLD both get BUILD_COLD_TIMEOUT_SECS so the one-time population can finish.
    """
    current = compute_lockfile_hash(manifest.image, workspace)
    if not current:
        return "COLD", ""
    stored = read_cache_key(manifest.repo_slug)
    if not stored:
        return "COLD", current
    if stored == current:
        return "HIT", current
    return "STALE", current


def _canonical_slug(repo_slug: str) -> str:
    """
    Normalise any repo reference to the canonical slug used as the PK in
    repo_build_manifests and repo_build_metadata.

    Callers may pass:
      - a full GitLab namespace path  "switchnxt/switchnxt_sim_backend"
      - a plain project name          "switchnxt-sim-backend"
      - an already-normalised slug    "switchnxt_sim_backend"

    All three normalise to the same key: last path segment, lowercase,
    hyphens/dots replaced with underscores.  This matches the form produced
    by normalize_repo_index_key_without_prefix() in sdlc_context.py and
    ensures consistent storage and lookup regardless of which caller built
    the slug.
    """
    if not repo_slug:
        return repo_slug
    name = repo_slug.split("/")[-1].strip().lower()
    name = re.sub(r"[-.\s]+", "_", name)
    return name


class BuildManifestResolver:

    def resolve(
        self,
        repo_slug: str,
        gitlab_path: str = "",
        queue_on_miss: bool = True,
        workspace_path: str = "",
        product_id: str = "",
    ) -> BuildManifest | None:
        """
        Resolve and return a BuildManifest.  Returns None if the repo's build
        pattern cannot be determined.

        gitlab_path: full namespace/project path for GitLab API calls (e.g.
        "switchnxt/switchnxt_sim_backend").  When omitted, repo_slug is used,
        which works only if it already contains the namespace.

        queue_on_miss: when True (default) a .sdlc.yml MR generation job is
        enqueued on a cache miss.  Pass False from within the rollout worker
        itself to prevent an infinite re-queue loop.

        workspace_path: absolute path to a freshly-cloned checkout of the repo.
        When provided, build files are read DIRECTLY off that checkout (tier ②
        below) — the authoritative, always-current source that requires NO prior
        indexing. SDLC callers pass the per-run workspace so a repo that was never
        indexed still resolves. The index-backed tiers remain as fallbacks.

        product_id: the product this repo is being built under. repo_build_metadata
        is keyed by (product_id, repo_slug); the metadata tiers read/write scoped to
        it (falling back to the repo-only '' row when no product row exists). The
        repo_build_manifests image cache (tier ①/_cache) stays repo-keyed — it is
        refreshed/invalidated by the pipeline when a version change is confirmed.
        """
        # Normalise to canonical form (last path segment, lowercase,
        # hyphens/dots → underscores) so storage and lookup always use the
        # same key regardless of whether the caller passed "group/repo-name"
        # or just "repo_name".
        repo_slug = _canonical_slug(repo_slug)
        logger.info(f"manifest_resolver: resolving for {repo_slug}")

        # ① DB cache (feedback-adjusted manifest persisted from prior runs)
        if m := self._from_db_cache(repo_slug):
            return m

        # ①.5 Human-confirmed metadata (product-scoped) — the SDLC build-metadata
        # gate recorded the AUTHORITATIVE language version for this (product, repo).
        # It must win over re-detecting from the checkout (tier ②), so that a
        # confirmed override that differs from the raw build file is honoured. Only
        # consulted when a product is in scope and a hitl_confirmed row exists.
        if product_id:
            if m := self._from_confirmed_metadata(repo_slug, product_id):
                return self._cache(m)

        # ② per-run workspace clone — authoritative & always current, needs NO
        #    index. Reads the real build files (pom.xml / build.gradle /
        #    package.json / .sdlc.yml) straight off the freshly-cloned checkout.
        #    Preferred over the index-backed tiers below, which are only
        #    periodically refreshed and therefore routinely stale.
        if workspace_path and os.path.isdir(workspace_path):
            if m := self._from_workspace(repo_slug, workspace_path, product_id=product_id):
                return self._cache(m)

        # ③ repo_build_metadata (structured, from index time)
        if m := self._from_build_metadata(repo_slug, product_id=product_id):
            return self._cache(m)

        # ④ document_embeddings chunks (raw, assembled from index)
        if m := self._from_indexed_chunks(repo_slug, product_id=product_id):
            return self._cache(m)

        # ⑤ legacy shared workspace filesystem (BUILDER_WORKSPACE_ROOT/<slug>)
        workspace = os.path.join(config.BUILDER_WORKSPACE_ROOT, repo_slug)
        if os.path.isdir(workspace):
            if m := self._from_workspace(repo_slug, workspace, product_id=product_id):
                return self._cache(m)

        # ⑥ Unknown — optionally queue .sdlc.yml generation and return None
        if queue_on_miss:
            self._queue_sdlc_yml(repo_slug, gitlab_path=gitlab_path)
        logger.warning(f"manifest_resolver: UNKNOWN_BUILD_PATTERN for {repo_slug}")
        return None

    # ── Sources ────────────────────────────────────────────────

    def _from_db_cache(self, repo_slug: str) -> BuildManifest | None:
        from db.database import engine
        with engine.connect() as sess:
            row = sess.execute(text("""
                SELECT image, compile_cmd, test_cmd, env_vars, cache_paths,
                       timeout_secs, detected_by, confidence
                FROM   repo_build_manifests
                WHERE  repo_slug = :slug AND invalidated = FALSE
            """), {"slug": repo_slug}).fetchone()
        if not row:
            logger.debug(f"manifest_resolver: DB cache MISS for {repo_slug}")
            return None
        logger.info(
            f"manifest_resolver: DB cache HIT for {repo_slug} "
            f"image={row.image} detected_by={row.detected_by} confidence={row.confidence:.2f}"
        )
        return BuildManifest(
            repo_slug=repo_slug,
            image=row.image,
            # Re-derive the Maven tuning from CURRENT config: the persisted row
            # may have been written under different MAVEN_PARALLEL_FLAG /
            # MAVEN_SKIP_CLEAN values, and only OUR default command is rewritten
            # (a repo-supplied command passes through untouched).
            compile_cmd=tune_maven_compile_command(row.compile_cmd),
            test_cmd=row.test_cmd,
            env_vars=row.env_vars or {},
            cache_paths=list(row.cache_paths or []),
            timeout=row.timeout_secs,
            detected_by=row.detected_by,
            confidence=row.confidence,
        )

    def _from_confirmed_metadata(self, repo_slug: str, product_id: str) -> BuildManifest | None:
        """Return the manifest built from a HUMAN-CONFIRMED (product, repo) metadata
        row (extraction_method='hitl_confirmed'), or None. This is what makes an
        operator's version decision at the build-metadata gate authoritative over
        auto-detection from the checkout."""
        from db.database import engine
        with engine.connect() as sess:
            row = sess.execute(text("""
                SELECT build_tool, language, language_version, build_cmd, test_cmd, confidence
                FROM   repo_build_metadata
                WHERE  repo_slug = :slug AND product_id = :pid
                  AND  extraction_method = 'hitl_confirmed'
            """), {"slug": repo_slug, "pid": product_id}).fetchone()
        if not row or not row.build_tool:
            return None
        logger.info(
            f"manifest_resolver: HITL-confirmed metadata HIT for product={product_id} "
            f"{repo_slug} tool={row.build_tool} ver={row.language_version}"
        )
        return self._meta_to_manifest(
            repo_slug=repo_slug,
            build_tool=row.build_tool,
            language=row.language or "java",
            version=row.language_version or "21",
            build_cmd=row.build_cmd or "",
            test_cmd=row.test_cmd or "",
            detected_by="hitl_confirmed",
            confidence=min((row.confidence or 0.9) + 0.05, 0.99),
        )

    def _from_build_metadata(self, repo_slug: str, product_id: str = "") -> BuildManifest | None:
        from db.database import engine
        with engine.connect() as sess:
            # Prefer the row scoped to this product; fall back to the repo-only
            # ('') row written by index-time / non-SDLC extraction.
            row = sess.execute(text("""
                SELECT build_tool, language, language_version,
                       build_cmd, test_cmd, confidence
                FROM   repo_build_metadata
                WHERE  repo_slug = :slug AND product_id IN (:pid, '')
                ORDER BY (product_id = :pid) DESC
                LIMIT  1
            """), {"slug": repo_slug, "pid": product_id or ""}).fetchone()
        if not row or not row.build_tool:
            logger.debug(f"manifest_resolver: repo_build_metadata MISS for product={product_id or '(none)'} {repo_slug}")
            return None
        logger.info(
            f"manifest_resolver: repo_build_metadata HIT for product={product_id or '(none)'} {repo_slug} "
            f"tool={row.build_tool} lang={row.language} ver={row.language_version}"
        )
        return self._meta_to_manifest(
            repo_slug=repo_slug,
            build_tool=row.build_tool,
            language=row.language or "java",
            version=row.language_version or "21",
            build_cmd=row.build_cmd or "",
            test_cmd=row.test_cmd or "",
            detected_by="repo_build_metadata",
            confidence=min(row.confidence + 0.05, 0.95),  # slight boost for cached
        )

    def _from_indexed_chunks(self, repo_slug: str, product_id: str = "") -> BuildManifest | None:
        """Trigger on-demand extraction from existing chunks, then read result."""
        from core.build_metadata_extractor import BuildMetadataExtractor
        logger.info(f"manifest_resolver: triggering on-demand extraction for {repo_slug}")
        try:
            meta = BuildMetadataExtractor().extract_and_store(repo_slug, product_id=product_id)
            if meta:
                logger.info(
                    f"manifest_resolver: on-demand extraction succeeded for {repo_slug} "
                    f"tool={meta.get('build_tool')} from={meta.get('extracted_from')}"
                )
                return self._from_build_metadata(repo_slug, product_id=product_id)
            logger.debug(f"manifest_resolver: on-demand extraction returned no metadata for {repo_slug}")
        except Exception as exc:
            logger.debug(f"manifest_resolver: on-demand extraction failed for {repo_slug}: {exc}")
        return None

    def _from_workspace(self, repo_slug: str, workspace: str, product_id: str = "") -> BuildManifest | None:
        """Read build files directly from workspace filesystem."""
        from core.build_metadata_extractor import BuildMetadataExtractor
        logger.info(f"manifest_resolver: falling back to workspace filesystem for {repo_slug} at {workspace}")
        extractor = BuildMetadataExtractor()
        files = extractor._read_from_workspace(workspace)
        if not files:
            logger.debug(f"manifest_resolver: no build files found in workspace for {repo_slug}")
            return None
        meta = extractor._detect(repo_slug, files, product_id=product_id)
        if not meta:
            return None
        extractor._upsert(meta)
        return self._meta_to_manifest(
            repo_slug=repo_slug,
            build_tool=meta.get("build_tool", "maven"),
            language=meta.get("language", "java"),
            version=meta.get("language_version", "21"),
            build_cmd=meta.get("build_cmd", ""),
            test_cmd=meta.get("test_cmd", ""),
            detected_by=meta.get("extracted_from", "workspace"),
            confidence=meta.get("confidence", 0.7),
        )

    # ── Helpers ────────────────────────────────────────────────

    def _meta_to_manifest(
        self, repo_slug: str, build_tool: str, language: str, version: str,
        build_cmd: str, test_cmd: str, detected_by: str, confidence: float,
    ) -> BuildManifest:
        fallback_image, cache_keys = _TOOL_MAP.get(build_tool, _TOOL_MAP["maven"])

        # Select the exact versioned image (e.g. ainxt-builder-jvm-21:latest).
        # Version is encoded in the image name — no runtime switching needed.
        image = _select_versioned_image(build_tool, version, fallback_image)

        cache_paths = [_CACHE_CONTAINER_MAP[k] for k in cache_keys if k in _CACHE_CONTAINER_MAP]

        # Python: set PIP_CACHE_DIR so pip uses the mounted cache volume,
        # and optionally wrap commands to create/reuse a persistent per-repo venv.
        env_vars: dict = {}
        if build_tool in ("pip", "poetry"):
            env_vars["PIP_CACHE_DIR"] = config.PIP_CACHE_CONTAINER_PATH
            if build_tool == "poetry":
                env_vars["POETRY_CACHE_DIR"] = config.POETRY_CACHE_CONTAINER_PATH
            if config.PYTHON_USE_PERSISTENT_VENV:
                build_cmd, test_cmd = _python_build_commands(build_cmd, test_cmd)
            else:
                # No venv wrapper — still repair the mounted pip cache's ownership,
                # otherwise pip disables caching and re-downloads every wheel.
                _pc = pip_cache_setup_cmd()
                build_cmd = f"{_pc} ; {build_cmd}" if build_cmd else build_cmd
                test_cmd = f"{_pc} ; {test_cmd}" if test_cmd else test_cmd

        logger.info(
            f"manifest_resolver: built manifest for {repo_slug} "
            f"tool={build_tool} version={version!r} → image={image} "
            f"detected_by={detected_by} confidence={confidence:.2f}"
            + (f" venv={config.PYTHON_VENV_CONTAINER_PATH}" if build_tool in ("pip", "poetry") else "")
        )

        return BuildManifest(
            repo_slug=repo_slug,
            image=image,
            compile_cmd=tune_maven_compile_command(
                build_cmd or "mvn clean install -DskipTests -q"
            ),
            test_cmd=test_cmd or "mvn test -q",
            env_vars=env_vars,
            cache_paths=cache_paths,
            timeout=config.BUILD_TIMEOUT_SECS,
            detected_by=detected_by,
            confidence=confidence,
        )

    def _cache(self, manifest: BuildManifest) -> BuildManifest:
        from db.database import engine
        try:
            with engine.connect() as sess:
                sess.execute(text("""
                    INSERT INTO repo_build_manifests
                        (repo_slug, image, compile_cmd, test_cmd, env_vars,
                         cache_paths, timeout_secs, detected_by, confidence,
                         created_at, updated_at)
                    VALUES
                        (:slug, :image, :compile_cmd, :test_cmd, CAST(:env_vars AS jsonb),
                         :cache_paths, :timeout, :detected_by, :confidence,
                         NOW(), NOW())
                    ON CONFLICT (repo_slug) DO UPDATE SET
                        image        = EXCLUDED.image,
                        compile_cmd  = EXCLUDED.compile_cmd,
                        test_cmd     = EXCLUDED.test_cmd,
                        env_vars     = EXCLUDED.env_vars,
                        cache_paths  = EXCLUDED.cache_paths,
                        timeout_secs = EXCLUDED.timeout_secs,
                        detected_by  = EXCLUDED.detected_by,
                        confidence   = EXCLUDED.confidence,
                        invalidated  = FALSE,
                        updated_at   = NOW()
                """), {
                    "slug":        manifest.repo_slug,
                    "image":       manifest.image,
                    "compile_cmd": manifest.compile_cmd,
                    "test_cmd":    manifest.test_cmd,
                    "env_vars":    __import__("json").dumps(manifest.env_vars),
                    "cache_paths": manifest.cache_paths,
                    "timeout":     manifest.timeout,
                    "detected_by": manifest.detected_by,
                    "confidence":  manifest.confidence,
                })
                sess.commit()
        except Exception as exc:
            logger.warning(f"manifest_resolver: cache write failed for {manifest.repo_slug}: {exc}")
            return manifest
        logger.info(
            f"manifest_resolver: cached manifest for {manifest.repo_slug} "
            f"image={manifest.image} detected_by={manifest.detected_by}"
        )
        return manifest

    def invalidate_cache(self, repo_slug: str) -> None:
        """
        Mark the cached resolved manifest invalid so the next resolve() skips the
        repo_build_manifests DB cache (tier ①) and re-detects from the base-branch
        checkout (tier ②). Called by the SDLC metadata gate after a confirmed
        language-version change, so the newly-selected versioned builder image is
        picked up this run. The manifest cache stays repo-keyed (product scoping
        lives in repo_build_metadata); invalidation is the refresh mechanism.
        """
        repo_slug = _canonical_slug(repo_slug)
        from db.database import engine
        try:
            with engine.connect() as sess:
                sess.execute(text("""
                    UPDATE repo_build_manifests
                       SET invalidated = TRUE, updated_at = NOW()
                     WHERE repo_slug = :slug
                """), {"slug": repo_slug})
                sess.commit()
            logger.info(f"manifest_resolver: invalidated cached manifest for {repo_slug} (version change confirmed)")
        except Exception as exc:
            logger.warning(f"manifest_resolver: invalidate_cache failed for {repo_slug}: {exc}")

    def update_after_run(self, repo_slug: str, status: str, missing_artifact: str = "") -> None:
        """Feedback loop — adjusts confidence based on run outcome."""
        logger.info(
            f"manifest_resolver: feedback update for {repo_slug} "
            f"status={status} missing={missing_artifact!r}"
        )
        from db.database import engine
        try:
            with engine.connect() as sess:
                if status == "BUILD_SUCCESS":
                    sess.execute(text("""
                        UPDATE repo_build_manifests SET
                            run_count     = run_count + 1,
                            success_count = success_count + 1,
                            confidence    = LEAST(0.95, confidence + 0.05),
                            updated_at    = NOW()
                        WHERE repo_slug = :slug
                    """), {"slug": repo_slug})
                elif status == "DEPENDENCY_MISSING" and missing_artifact:
                    sess.execute(text("""
                        UPDATE repo_build_manifests SET
                            run_count           = run_count + 1,
                            known_missing_deps  = array_append(known_missing_deps, :dep),
                            updated_at          = NOW()
                        WHERE repo_slug = :slug
                    """), {"slug": repo_slug, "dep": missing_artifact})
                else:
                    # For any failure: increment run_count; invalidate if 2 consecutive fails
                    sess.execute(text("""
                        UPDATE repo_build_manifests SET
                            run_count  = run_count + 1,
                            invalidated = CASE
                                WHEN run_count >= 1 AND success_count = 0 THEN TRUE
                                ELSE FALSE
                            END,
                            updated_at = NOW()
                        WHERE repo_slug = :slug
                    """), {"slug": repo_slug})
                sess.commit()

            # If invalidated → queue sdlc.yml generation
            if status not in ("BUILD_SUCCESS", "DEPENDENCY_MISSING"):
                from db.database import engine as _e
                with _e.connect() as s2:
                    row = s2.execute(text(
                        "SELECT invalidated FROM repo_build_manifests WHERE repo_slug=:slug"
                    ), {"slug": repo_slug}).fetchone()
                    if row and row.invalidated:
                        self._queue_sdlc_yml(repo_slug)
        except Exception as exc:
            logger.warning(f"manifest_resolver: feedback update failed for {repo_slug}: {exc}")

    def _queue_sdlc_yml(self, repo_slug: str, gitlab_path: str = "") -> None:
        try:
            from core.job_queue import get_queue
            q = get_queue("sdlc_queue")
            q.enqueue(
                "workers.sdlc_yml_rollout_worker.generate_sdlc_yml_mr",
                repo_slug,
                gitlab_path or repo_slug,
                job_timeout=300,
            )
        except Exception as exc:
            logger.debug(f"manifest_resolver: failed to queue sdlc_yml for {repo_slug}: {exc}")