# SPDX-License-Identifier: Apache-2.0
"""
agents/sdlc_governance/bundle.py — Step 1: governance bundle resolution +
skill discovery.

Fail-safe contract (non-negotiable): `resolve_bundle()` and `discover_skills()`
NEVER raise. Any failure — bad URL, missing path, git error, timeout, unparsable
manifest — is logged as a WARN and the function returns None / [] so a missing
or broken governance bundle can never hard-fail an SDLC run.

Import side-effect-free: only stdlib + core.logger at module import time.
`yaml` (only needed for .yml manifests) is imported lazily inside the function
that needs it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

from core.logger import logger

from . import config

_GIT_TIMEOUT_SECS = 120
_GIT_CLONE_TIMEOUT_SECS = _GIT_TIMEOUT_SECS * 3

# Cache of discovered skills, keyed by (bundle.source, bundle.dir, bundle.ref) — a
# resolved git sha or a path+SKILL.md-mtime fingerprint, so a stale cache entry
# self-invalidates when the bundle content changes.
_SKILLS_CACHE: dict = {}

# Short-lived cache for resolve_bundle() itself — keyed on raw config so callers
# that invoke select_skills() multiple times per pipeline run (PLAN, IMPLEMENT,
# REVIEW) don't each trigger a separate git fetch. TTL is conservative (5 min) so
# a governance push mid-run is picked up within the next pipeline stage.
_BUNDLE_CACHE: dict = {}
_BUNDLE_CACHE_TTL_SECS = 300

_CREDENTIAL_RE = re.compile(r"://[^@/\s]+@")


@dataclass
class Bundle:
    dir: str
    source: str
    ref: str


@dataclass
class GovSkill:
    slug: str
    name: str
    plugin_name: str
    skill_md_path: str
    dir: str
    # Phases this skill applies to: subset of {"plan","implement","review"}. Empty
    # tuple = applies to ALL phases (the default / back-compat behaviour). Set via a
    # `phases: [...]` field in the manifest entry; the scan fallback leaves it empty.
    phases: tuple = ()
    domain: str = ""       # IS / EA / DPDP / "" (uppercased); "" means unclassified
    entrypoint: str = ""   # relative path to the skill's analyzer script (empty = none)


def _redact(text: str) -> str:
    """Best-effort scrub of embedded git credentials before logging."""
    return _CREDENTIAL_RE.sub("://***@", text or "")


def _sanitize_for_dirname(raw: str) -> str:
    """Collapse a URL/path into a filesystem-safe, reasonably short cache-dir slug."""
    if not raw:
        return "bundle"
    safe = "".join(c if c.isalnum() else "_" for c in raw)
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    tail = safe[-60:].strip("_") or "bundle"
    return f"{tail}_{digest}"


def _run_git(args: list, cwd: Optional[str] = None, timeout: int = _GIT_TIMEOUT_SECS) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _authenticated_clone_url(url: str) -> str:
    """Best-effort: inject the initiator's thread-local GitLab token (populated
    via tools.gitlab_tools.set_token by the pipeline) into an https clone URL so
    private GitLab-hosted bundles can be cloned. Falls back to the url unchanged
    for ssh URLs, missing tokens, or import failure — never raises. Never touches
    the process-wide GITLAB_TOKEN env var."""
    if not url.startswith("https://"):
        return url
    try:
        from tools.gitlab_tools import _resolve_token
        token = _resolve_token()
    except Exception:
        return url
    if not token:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest.split("/", 1)[0]:
        return url  # credentials already embedded
    return f"{scheme}://oauth2:{token}@{rest}"


def _resolve_git_bundle() -> Bundle:
    url = config.git_url()
    if not url:
        raise ValueError("SDLC_GOVERNANCE_GIT_URL not set")

    from core.config import BUILDER_WORKSPACE_ROOT
    cache_root = os.path.join(BUILDER_WORKSPACE_ROOT, "cache", "governance_bundles")
    os.makedirs(cache_root, exist_ok=True)
    repo_dir = os.path.join(cache_root, _sanitize_for_dirname(url))

    ref = config.git_ref()

    if os.path.isdir(os.path.join(repo_dir, ".git")):
        fetch = _run_git(["fetch", "--all", "--prune"], cwd=repo_dir)
        if fetch.returncode != 0:
            raise RuntimeError(f"git fetch failed: {_redact(fetch.stderr).strip()[:300]}")
    else:
        clone_url = _authenticated_clone_url(url)
        clone = _run_git(["clone", clone_url, repo_dir], timeout=_GIT_CLONE_TIMEOUT_SECS)
        if clone.returncode != 0:
            raise RuntimeError(f"git clone failed: {_redact(clone.stderr).strip()[:300]}")

    if ref:
        checkout = _run_git(["checkout", ref], cwd=repo_dir)
        if checkout.returncode != 0:
            raise RuntimeError(f"git checkout {ref!r} failed: {_redact(checkout.stderr).strip()[:300]}")

    rev_parse = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    if rev_parse.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {_redact(rev_parse.stderr).strip()[:300]}")
    sha = rev_parse.stdout.strip()
    if not sha:
        raise RuntimeError("git rev-parse HEAD returned empty sha")

    return Bundle(dir=repo_dir, source="git", ref=sha)


def _resolve_path_bundle() -> Bundle:
    path = config.bundle_path()
    if not path or not os.path.isdir(path):
        raise ValueError(f"SDLC_GOVERNANCE_PATH not set or not a directory: {path!r}")
    # Key on the max mtime of all first-level SKILL.md files rather than the
    # directory's own mtime — directory mtime is unreliable on NTFS/WSL DrvFS
    # mounts where editing a file inside doesn't update the parent dir's mtime.
    skill_mds = [
        os.path.join(path, entry, "SKILL.md")
        for entry in os.listdir(path)
        if os.path.isfile(os.path.join(path, entry, "SKILL.md"))
    ]
    mtime = max((os.path.getmtime(f) for f in skill_mds), default=os.path.getmtime(path))
    return Bundle(dir=path, source="path", ref=f"fs:{int(mtime)}")


def resolve_bundle() -> Optional[Bundle]:
    """Resolve the governance bundle per SDLC_GOVERNANCE_SOURCE ("git" | "path" |
    "auto"). NEVER raises — on any failure logs a WARN and returns None so the
    caller treats governance as skipped rather than failing the SDLC run.

    Results are cached in-process for _BUNDLE_CACHE_TTL_SECS (5 min) keyed on the
    raw config snapshot — collapses the 3-4 repeated calls per SDLC pipeline run
    (PLAN initial + PLAN fix round + IMPLEMENT + GOVERNANCE_REVIEW) into one
    git fetch for git bundles. Only successful resolutions are cached; transient
    failures are not, so a retry in the next stage can still recover."""
    src = config.source()
    cache_key = (src, config.git_url(), config.git_ref(), config.bundle_path())
    cached = _BUNDLE_CACHE.get(cache_key)
    if cached is not None:
        bundle, ts = cached
        if time.monotonic() - ts < _BUNDLE_CACHE_TTL_SECS:
            return bundle
    try:
        if src == "path":
            bundle = _resolve_path_bundle()
        elif src == "git":
            bundle = _resolve_git_bundle()
        else:  # auto
            path = config.bundle_path()
            if path and os.path.isdir(path):
                bundle = _resolve_path_bundle()
            elif config.git_url():
                bundle = _resolve_git_bundle()
            else:
                logger.warning(
                    "[SDLC-GOV] Bundle unavailable → governance skipped",
                    source=src,
                    error="no SDLC_GOVERNANCE_PATH or SDLC_GOVERNANCE_GIT_URL configured",
                )
                return None
    except Exception as exc:
        logger.warning(
            "[SDLC-GOV] Bundle unavailable → governance skipped",
            source=src, error=_redact(str(exc)),
        )
        return None

    logger.info("[SDLC-GOV] Bundle resolved", source=bundle.source, ref=bundle.ref, dir=bundle.dir)
    _BUNDLE_CACHE[cache_key] = (bundle, time.monotonic())
    return bundle


def _parse_manifest_entry(entry: dict, bundle_dir: str) -> Optional[GovSkill]:
    if not isinstance(entry, dict):
        return None
    slug = str(entry.get("slug") or entry.get("name") or "").strip().lower()
    if not slug:
        return None
    name = str(entry.get("name") or slug)
    plugin_name = str(entry.get("plugin_name") or entry.get("plugin") or slug)
    rel_dir = entry.get("path") or entry.get("dir") or slug
    skill_dir = os.path.join(bundle_dir, str(rel_dir))
    skill_md_path = str(entry.get("skill_md_path") or os.path.join(skill_dir, "SKILL.md"))
    # Optional per-phase scoping: phases: ["plan"] | ["implement"] | ["review"] | any
    # combination. Absent/empty → applies to all phases (back-compat).
    raw_phases = entry.get("phases")
    if isinstance(raw_phases, (list, tuple)):
        phases = tuple(str(p).strip().lower() for p in raw_phases if str(p).strip())
    else:
        phases = ()
    domain = str(entry.get("domain") or "").strip().upper()
    entrypoint = str(entry.get("entrypoint") or "").strip()
    return GovSkill(slug=slug, name=name, plugin_name=plugin_name,
                    skill_md_path=skill_md_path, dir=skill_dir, phases=phases,
                    domain=domain, entrypoint=entrypoint)


def _discover_from_manifest(bundle: Bundle) -> Optional[list]:
    """Parse governance.manifest.(json|yml|yaml) at the bundle root, if present.
    Returns None (not []) when no manifest is found or it can't be parsed, so
    the caller falls back to the SKILL.md filesystem scan."""
    for fname in ("governance.manifest.json", "governance.manifest.yml", "governance.manifest.yaml"):
        manifest_path = os.path.join(bundle.dir, fname)
        if not os.path.isfile(manifest_path):
            continue
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        if fname.endswith(".json"):
            data = json.loads(raw)
        else:
            try:
                import yaml
            except ImportError:
                logger.warning(
                    "[SDLC-GOV] yaml not importable — falling back to SKILL.md scan",
                    manifest=manifest_path,
                )
                return None
            data = yaml.safe_load(raw)
        entries = data.get("skills") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return None
        return [s for s in (_parse_manifest_entry(e, bundle.dir) for e in entries) if s is not None]
    return None


def _discover_by_scan(bundle: Bundle) -> list:
    """Fallback discovery: scan <bundle.dir>/*/SKILL.md AND <bundle.dir>/*/*/SKILL.md."""
    skills = []
    seen_slugs: set = set()
    try:
        entries = sorted(os.listdir(bundle.dir))
    except OSError:
        return skills
    for entry in entries:
        skill_dir = os.path.join(bundle.dir, entry)
        skill_md = os.path.join(skill_dir, "SKILL.md")
        # One-level: <entry>/SKILL.md (back-compat: ea/SKILL.md etc.)
        if os.path.isdir(skill_dir) and os.path.isfile(skill_md):
            slug = entry.strip().lower()
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                skills.append(GovSkill(slug=slug, name=slug, plugin_name=slug,
                                       skill_md_path=skill_md, dir=skill_dir,
                                       domain=entry.strip().upper()))
        # Two-level: <entry>/<sub>/SKILL.md (e.g. infosec/sast/SKILL.md).
        # Slug is namespaced as "<domain>/<sub>" so identical sub-folder names across
        # different domain folders (e.g. ea/access-control and infosec/access-control)
        # don't collide in seen_slugs and both get discovered.
        if os.path.isdir(skill_dir):
            try:
                sub_entries = sorted(os.listdir(skill_dir))
            except OSError:
                continue
            for sub in sub_entries:
                sub_dir = os.path.join(skill_dir, sub)
                sub_md = os.path.join(sub_dir, "SKILL.md")
                if os.path.isdir(sub_dir) and os.path.isfile(sub_md):
                    slug = f"{entry.strip().lower()}/{sub.strip().lower()}"
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        skills.append(GovSkill(slug=slug, name=sub.strip().lower(),
                                               plugin_name=sub.strip().lower(),
                                               skill_md_path=sub_md, dir=sub_dir,
                                               domain=entry.strip().upper()))
    return skills


def governance_bundle_version(bundle: Optional[Bundle]) -> str:
    """Stable version string for the whole governance bundle (end-gate overhaul
    2026-07-23). Uses `bundle.ref` — a resolved git sha for git bundles, or
    `fs:<mtime>` for path bundles — which already self-invalidates when bundle
    content changes (see resolve_bundle / _resolve_*_bundle). Returns "" when no
    bundle resolved. Never raises."""
    try:
        return (getattr(bundle, "ref", "") or "") if bundle is not None else ""
    except Exception:
        return ""


def skill_versions(bundle: Optional[Bundle], skills: Optional[list]) -> dict:
    """Per-skill version map {slug: version} captured on a scan snapshot (end-gate
    overhaul 2026-07-23). Version = short sha256 of the skill's SKILL.md content
    (so an edited skill yields a new version), falling back to the bundle ref when
    the file can't be read. Never raises — a failure yields the bundle ref (or "").
    """
    out: dict = {}
    fallback = governance_bundle_version(bundle)
    for sk in (skills or []):
        slug = getattr(sk, "slug", "") or ""
        if not slug:
            continue
        ver = fallback
        try:
            md_path = getattr(sk, "skill_md_path", "") or ""
            if md_path and os.path.isfile(md_path):
                with open(md_path, "rb") as fh:
                    ver = hashlib.sha256(fh.read()).hexdigest()[:16]
        except Exception:
            ver = fallback
        out[slug] = ver
    return out


def discover_skills(bundle: Bundle) -> list:
    """Discover governance skills inside `bundle`. NEVER raises — on any failure
    logs a WARN and returns []. Cached per (bundle.source, bundle.dir, bundle.ref)
    — the dir is part of the key so two different path bundles that happen to share
    a mtime-second ref never collide."""
    cache_key = (bundle.source, bundle.dir, bundle.ref)
    if cache_key in _SKILLS_CACHE:
        return _SKILLS_CACHE[cache_key]

    try:
        skills = _discover_from_manifest(bundle)
        if skills is None:
            skills = _discover_by_scan(bundle)
    except Exception as exc:
        logger.warning(
            "[SDLC-GOV] Skill discovery failed", source=bundle.source, ref=bundle.ref, error=str(exc),
        )
        skills = []

    _SKILLS_CACHE[cache_key] = skills
    logger.info("[SDLC-GOV] Skills discovered", count=len(skills), slugs=[s.slug for s in skills])
    return skills
