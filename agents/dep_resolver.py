# SPDX-License-Identifier: MIT
"""
Multi-repo dependency resolver.

Pure-function module. Phase 1 status: defined but not yet called by the
pipeline. Phase 2 wires it into _preflight_check.

Resolves the list of dependent repos for an SDLC run from three sources, in
strict precedence order:

    1. User overrides (UI form / Jira description) — highest priority
    2. .sdlc.yml `dependencies:` block in the primary repo
    3. Build-file fallback (pom.xml / build.gradle) — lowest, inferred only

Why this order: the user is the final authority at trigger time. The manifest
is the durable repo-owned answer for steady-state runs. The build-file fallback
exists only so day-zero repos (no manifest yet) still work — its outputs are
always tagged `source='build-file'` and `kind='compile-only'` so the LLM
cannot silently promote an inferred dep to editable without a human in the
loop.

Internal-vs-external classification for build-file parsing uses the
`INTERNAL_GROUP_PREFIXES` env var (default: 'org.ainxt.'). Only deps whose
groupId starts with one of these prefixes are returned; everything else is
treated as third-party and left to Maven/Gradle to resolve.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────

# Comma-separated groupId prefixes that identify internal AiNxt artifacts.
_INTERNAL_GROUP_PREFIXES = tuple(
    p.strip().lower()
    for p in os.getenv("INTERNAL_GROUP_PREFIXES", "org.ainxt.").split(",")
    if p.strip()
)

# Default GitLab group used when constructing a repo path from an artifactId
# (only when no explicit mapping is provided). Override via env if internal
# artifacts live under a different namespace.
from core.config import APP_OWNER as _app_owner
_INTERNAL_GITLAB_GROUP = os.getenv("INTERNAL_GITLAB_GROUP", _app_owner).strip()

# Optional JSON dict mapping "groupId:artifactId" -> "gitlab/namespace/path"
# for cases where the heuristic (group/artifactId) is wrong.
# Format: {"org.ainxt.payments-sdk:payments-sdk": "ainxt-payments/payments-sdk"}
def _load_artifact_map() -> dict:
    raw = os.getenv("INTERNAL_ARTIFACT_TO_REPO_MAP", "").strip()
    if not raw:
        return {}
    try:
        import json
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        logger.warning("INTERNAL_ARTIFACT_TO_REPO_MAP could not be parsed as JSON")
        return {}


_VALID_KINDS = ("primary", "editable", "compile-only")
_VALID_SOURCES = ("user", "manifest", "build-file", "primary")


# ── DepSpec ──────────────────────────────────────────────────────────────────

@dataclass
class DepSpec:
    """
    One repo participating in an SDLC run.

    repo:        gitlab namespace/project path (e.g. "ainxt/payments-sdk")
    ref:         branch or tag name supplied (or inferred from primary)
    kind:        'primary' | 'editable' | 'compile-only'
    source:      where this entry came from — 'user' | 'manifest' | 'build-file' | 'primary'
    build_order: optional manual override; None means "let topological sort decide"
    """
    repo: str
    ref: str
    kind: str
    source: str
    build_order: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"DepSpec.kind must be one of {_VALID_KINDS}, got {self.kind!r}")
        if self.source not in _VALID_SOURCES:
            raise ValueError(f"DepSpec.source must be one of {_VALID_SOURCES}, got {self.source!r}")
        self.repo = self.repo.strip()
        self.ref = self.ref.strip() or "main"

    def as_dict(self) -> dict:
        return asdict(self)


# ── Public entry point ───────────────────────────────────────────────────────

def resolve_dependencies(
    primary_repo: str,
    primary_branch: str,
    user_overrides: Iterable[dict] | None = None,
    *,
    fetch_manifest: bool = True,
    fetch_build_files: bool = True,
) -> list[DepSpec]:
    """
    Resolve all dependent repos for an SDLC run on `primary_repo@primary_branch`.

    Returns a list of DepSpecs that does NOT include the primary repo itself
    (callers prepend that separately). Order in the returned list is not
    significant — Phase 2 computes build_order via topological sort.

    The precedence rule (user > manifest > build-file) is applied per-repo:
    if `user_overrides` mentions `ainxt/payments-sdk`, the manifest's entry for
    that same repo is dropped from the result; same for build-file inferences.

    Args:
        primary_repo:       gitlab namespace/project path of the primary repo
        primary_branch:     branch the run is targeting on the primary; used as
                            the default ref for any dep that doesn't specify one
        user_overrides:     list of dicts shaped like
                            {"repo": "...", "ref": "...", "kind": "...",
                             "build_order": int?} — typically from the UI form
                            or parsed from Jira description
        fetch_manifest:     read .sdlc.yml from the primary repo (default True;
                            set False in tests / dry-runs)
        fetch_build_files:  parse pom.xml / build.gradle (default True; set
                            False to disable inferred deps for a run)
    """
    primary_repo = (primary_repo or "").strip()
    primary_branch = (primary_branch or "main").strip()
    if not primary_repo:
        return []

    # Layer 1: user input wins outright.
    user_specs = _parse_user_overrides(user_overrides or [], primary_branch)
    covered = {s.repo for s in user_specs}

    # Layer 2: manifest.
    manifest_specs: list[DepSpec] = []
    if fetch_manifest:
        for spec in _read_manifest_deps(primary_repo, primary_branch):
            if spec.repo not in covered:
                manifest_specs.append(spec)
                covered.add(spec.repo)

    # Layer 3: build-file fallback.
    build_specs: list[DepSpec] = []
    if fetch_build_files:
        for spec in _read_build_file_deps(primary_repo, primary_branch):
            if spec.repo not in covered:
                build_specs.append(spec)
                covered.add(spec.repo)

    return user_specs + manifest_specs + build_specs


# ── Layer 1: user overrides ──────────────────────────────────────────────────

def _parse_user_overrides(items: Iterable[dict], primary_branch: str) -> list[DepSpec]:
    """
    Validate and normalize user-supplied dep entries.

    User entries MUST include `repo` and `kind`. `ref` defaults to the primary
    branch. Invalid entries are dropped with a warning (never silently
    accepted, never raised — preflight is the place to hard-fail).
    """
    out: list[DepSpec] = []
    for raw in items:
        if not isinstance(raw, dict):
            logger.warning(f"dep_resolver: user override is not a dict, skipping: {raw!r}")
            continue
        repo = str(raw.get("repo", "")).strip()
        kind = str(raw.get("kind", "")).strip().lower()
        if not repo or "/" not in repo:
            logger.warning(f"dep_resolver: user override missing repo path, skipping: {raw!r}")
            continue
        if kind not in ("editable", "compile-only"):
            logger.warning(
                f"dep_resolver: user override for {repo!r} must specify kind "
                f"as 'editable' or 'compile-only' (got {kind!r}); skipping"
            )
            continue
        ref = str(raw.get("ref", "")).strip() or primary_branch
        build_order_raw = raw.get("build_order")
        try:
            build_order = int(build_order_raw) if build_order_raw is not None else None
        except (TypeError, ValueError):
            build_order = None
        try:
            out.append(DepSpec(repo=repo, ref=ref, kind=kind, source="user", build_order=build_order))
        except ValueError:
            logger.warning(f"dep_resolver: invalid user override for {repo!r}")
    return out


# ── Layer 2: .sdlc.yml dependencies block ────────────────────────────────────

def _read_manifest_deps(primary_repo: str, primary_branch: str) -> list[DepSpec]:
    """
    Read the `dependencies:` block from .sdlc.yml in the primary repo.

    The block is optional. Absence is normal — most repos don't have multi-repo
    deps. Returns an empty list on any read/parse failure (preflight surfaces
    GitLab errors separately).
    """
    raw = _read_manifest_yaml(primary_repo, primary_branch)
    if not raw:
        return []
    deps_block = raw.get("dependencies")
    if not deps_block:
        return []
    if not isinstance(deps_block, list):
        logger.warning(
            "dep_resolver: .sdlc.yml `dependencies:` in %r is not a list (got %s); ignoring",
            primary_repo, type(deps_block).__name__,
        )
        return []

    out: list[DepSpec] = []
    for idx, entry in enumerate(deps_block):
        if not isinstance(entry, dict):
            logger.warning(
                "dep_resolver: .sdlc.yml dependency #%d in %r is not a mapping; skipping",
                idx, primary_repo,
            )
            continue
        repo = str(entry.get("repo", "")).strip()
        if not repo or "/" not in repo:
            logger.warning(
                "dep_resolver: .sdlc.yml dependency #%d in %r missing or invalid repo path; skipping",
                idx, primary_repo,
            )
            continue
        ref = str(entry.get("ref", "")).strip() or primary_branch
        kind = str(entry.get("kind", "compile-only")).strip().lower()
        if kind not in ("editable", "compile-only"):
            logger.warning(
                "dep_resolver: .sdlc.yml dependency %r has invalid kind %r — defaulting to compile-only",
                repo, kind,
            )
            kind = "compile-only"
        try:
            build_order = int(entry["build_order"]) if "build_order" in entry else None
        except (TypeError, ValueError):
            build_order = None
        try:
            out.append(DepSpec(repo=repo, ref=ref, kind=kind, source="manifest", build_order=build_order))
        except ValueError:
            logger.warning("dep_resolver: invalid manifest dep for %r", repo)
    return out


def _read_manifest_yaml(primary_repo: str, primary_branch: str) -> dict | None:
    """Fetch and parse .sdlc.yml from the SCM provider; None on absence or error."""
    try:
        import yaml as _yaml
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_read_file as _scm_read_file
        else:
            from tools.gitlab_tools import gitlab_read_file as _scm_read_file
        raw = _scm_read_file(primary_repo, ".sdlc.yml", primary_branch)
        if not raw or raw.startswith("[Error"):
            return None
        data = _yaml.safe_load(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning(f"dep_resolver: could not read .sdlc.yml from {primary_repo}@{primary_branch}")
        return None


# ── Layer 3: build-file fallback (pom.xml / build.gradle[.kts]) ──────────────

def _read_build_file_deps(primary_repo: str, primary_branch: str) -> list[DepSpec]:
    """
    Parse pom.xml and build.gradle / build.gradle.kts for internal deps.

    Always returns kind='compile-only'. Build-file inferences must never
    auto-promote to editable — only the user can do that at trigger time.
    """
    out: list[DepSpec] = []
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_read_file as gitlab_read_file
        else:
            from tools.gitlab_tools import gitlab_read_file
    except Exception:
        logger.warning("dep_resolver: SCM tools unavailable")
        return out

    artifact_map = _load_artifact_map()
    seen: set[str] = set()

    for path, parser in (
        ("pom.xml", _parse_pom_xml),
        ("build.gradle", _parse_build_gradle),
        ("build.gradle.kts", _parse_build_gradle),
    ):
        try:
            content = gitlab_read_file(primary_repo, path, primary_branch)
        except Exception:
            logger.debug(f"dep_resolver: skip {path}")
            continue
        if not content or content.startswith("[Error"):
            continue
        for group_id, artifact_id in parser(content):
            if not _is_internal_group(group_id):
                continue
            repo_path = _resolve_artifact_to_repo(group_id, artifact_id, artifact_map)
            if not repo_path or repo_path in seen:
                continue
            seen.add(repo_path)
            try:
                out.append(DepSpec(
                    repo=repo_path,
                    ref=primary_branch,
                    kind="compile-only",
                    source="build-file",
                ))
            except ValueError:
                logger.warning("dep_resolver: invalid build-file dep %r", repo_path)
    return out


def _is_internal_group(group_id: str) -> bool:
    if not group_id:
        return False
    gid = group_id.strip().lower()
    return any(gid.startswith(prefix) for prefix in _INTERNAL_GROUP_PREFIXES)


def _resolve_artifact_to_repo(group_id: str, artifact_id: str, artifact_map: dict) -> str | None:
    """Map a Maven coordinate to a GitLab repo path."""
    key = f"{group_id}:{type(artifact_id).__name__}".lower()
    if key in artifact_map:
        return artifact_map[key]
    artifact_id = (artifact_id or "").strip()
    if not artifact_id:
        return None
    return f"{_INTERNAL_GITLAB_GROUP}/{type(artifact_id).__name__}"


def _parse_pom_xml(content: str) -> list[tuple[str, str]]:
    """
    Extract direct (groupId, artifactId) pairs from <dependencies> in pom.xml.

    Ignores <dependencyManagement> (those are version-only declarations, not
    actual deps). Best-effort: malformed XML returns []; the resolver treats
    missing pom data as "no inferred deps", which is safe.
    """
    try:
        from xml.etree import ElementTree as ET
    except Exception:
        return []
    try:
        root = ET.fromstring(_strip_xml_namespace(content))
    except ET.ParseError:
        logger.debug("dep_resolver: pom.xml parse error")
        return []

    out: list[tuple[str, str]] = []

    # Collect <dependency> nodes under <dependencies>, but not those nested
    # inside <dependencyManagement>. ElementTree doesn't have a clean "parent"
    # API in py3, so we walk the tree manually.
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


def _strip_xml_namespace(content: str) -> str:
    """
    pom.xml uses xmlns="http://maven.apache.org/POM/4.0.0" which makes
    ElementTree tags look like '{http://...}dependency'. Strip the default
    namespace so plain tag matches work.
    """
    return re.sub(r'\sxmlns="[^"]+"', "", content, count=1)


# Matches both Groovy and Kotlin DSL:
#   implementation 'org.ainxt.payments:sdk:1.2.3'
#   implementation("org.ainxt.payments:sdk:1.2.3")
#   api "org.ainxt.payments:sdk:1.2.3"
#   testImplementation 'org.ainxt.payments:sdk:1.2.3'
_GRADLE_DEP_RE = re.compile(
    r"""
    \b(?:implementation|api|compile|runtimeOnly|compileOnly|testImplementation|testCompile|annotationProcessor)
    \s*
    [\(\s]
    \s*
    ['"]([^'":]+):([^'":]+):[^'"]+['"]
    """,
    re.VERBOSE,
)


def _parse_build_gradle(content: str) -> list[tuple[str, str]]:
    """Extract (groupId, artifactId) pairs from build.gradle / build.gradle.kts."""
    out: list[tuple[str, str]] = []
    for m in _GRADLE_DEP_RE.finditer(content or ""):
        gid = (m.group(1) or "").strip()
        aid = (m.group(2) or "").strip()
        if gid and aid:
            out.append((gid, aid))
    return out
