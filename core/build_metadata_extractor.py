# SPDX-License-Identifier: MIT
"""
core/build_metadata_extractor.py

Extracts structured build configuration from already-indexed document_embeddings
chunks. Runs once per repo (at index time and on-demand), stores result in
repo_build_metadata. No re-index required — reads what is already stored.

Priority order for detection:
  1. .sdlc.yml  (our standard, confidence 1.0)
  2. pom.xml    (Maven, confidence 0.9)
  3. build.gradle / build.gradle.kts  (Gradle, confidence 0.9)
  4. package.json  (Node, confidence 0.9)
  5. go.mod        (Go, confidence 0.9)
  6. Cargo.toml    (Rust, confidence 0.85)
  7. pyproject.toml / requirements.txt  (Python, confidence 0.85)
  8. .gitlab-ci.yml  (fallback to CI script, confidence 0.8)
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import yaml
from sqlalchemy import text

from core import config

logger = logging.getLogger("ainxt.build_metadata_extractor")

# Build file sentinel names in priority order
_BUILD_FILE_PRIORITY = [
    ".sdlc.yml",
    "pom.xml",
    "build.gradle.kts",
    "build.gradle",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pyproject.toml",
    "requirements.txt",
    ".gitlab-ci.yml",
    "Makefile",
]

# CI image string → (builder image env key, version extractor pattern)
_CI_IMAGE_JAVA_PATTERN = re.compile(
    r"(?:openjdk|temurin|eclipse-temurin|jdk)[:\-](\d+)", re.IGNORECASE
)
_CI_IMAGE_NODE_PATTERN = re.compile(r"node[:\-](\d+)", re.IGNORECASE)
_CI_IMAGE_PYTHON_PATTERN = re.compile(r"python[:\-](\d+\.\d+)", re.IGNORECASE)
_CI_IMAGE_GO_PATTERN = re.compile(r"golang[:\-](\d+\.\d+)", re.IGNORECASE)

_MAVEN_NS = "http://maven.apache.org/POM/4.0.0"


def _canon_repo_slug(repo_slug: str) -> str:
    """Canonical repo slug — last path segment, lowercase, hyphens/dots/spaces
    → underscores. Must match the indexer (routers/index_router._extract_repo_name)
    and build_manifest_resolver._canonical_slug so document_embeddings lookups and
    repo_build_metadata PKs always agree, even for dotted names:
    'group/upi-2.0' → 'upi_2_0'."""
    if not repo_slug:
        return repo_slug
    name = repo_slug.split("/")[-1].strip().lower()
    return re.sub(r"[-.\s]+", "_", name)


def _repo_key(repo_slug: str) -> str:
    return "repo_" + _canon_repo_slug(repo_slug)


class BuildMetadataExtractor:
    """
    Extract and persist build metadata for a repo from indexed document_embeddings.
    Thread-safe — creates its own DB session.
    """

    def extract_and_store(self, repo_slug: str, local_path: str | None = None,
                          product_id: str = "") -> dict | None:
        """
        Extract build metadata for repo_slug.
        Returns the stored metadata dict or None if detection failed.

        local_path — optional workspace path for fallback filesystem read when
                     the file is not yet indexed (e.g. immediately after clone).
        product_id — the product this repo is being built under. repo_build_metadata
                     is keyed by (product_id, repo_slug) so the same physical repo can
                     carry different resolved versions across products (different
                     authoritative base branches). SDLC always supplies a real product;
                     index-time / non-SDLC callers pass "" (the repo-only sentinel).
        """
        from db.database import vector_read_engine

        # Canonicalize once so the document_embeddings lookup key AND the
        # repo_build_metadata.repo_slug PK we upsert both match the form the
        # resolver reads with (build_manifest_resolver._canonical_slug). Without
        # this, a dotted repo like 'upi-2.0' is indexed as 'upi_2_0' but looked
        # up as 'upi_2.0' → 0 build files → CODING fails.
        repo_slug = _canon_repo_slug(repo_slug)
        repo_key = _repo_key(repo_slug)

        with vector_read_engine.connect() as sess:
            # Assemble build file content from indexed chunks
            rows = sess.execute(text("""
                SELECT file_path, chunk_index, content
                FROM   document_embeddings
                WHERE  repo = :repo
                  AND  file_path = ANY(:files)
                ORDER BY file_path, chunk_index
            """), {
                "repo":  repo_key,
                "files": _BUILD_FILE_PRIORITY,
            }).fetchall()

        # Concatenate chunks per file
        files: dict[str, str] = {}
        for file_path, _, content in rows:
            files[file_path] = files.get(file_path, "") + (content or "")

        # Fallback: read directly from workspace if not yet indexed
        if local_path and not files:
            logger.debug(f"build_metadata: no indexed chunks for {repo_slug} — reading from workspace {type(local_path).__name__}")
            files = self._read_from_workspace(local_path)

        if not files:
            logger.warning(f"build_metadata: no build files found for {type(repo_slug).__name__}")
            return None

        meta = self._detect(repo_slug, files, product_id=product_id)
        if not meta:
            logger.warning(f"build_metadata: detection failed for {type(repo_slug).__name__}")
            return None

        self._upsert(meta)
        logger.info(
            f"build_metadata: stored for product={meta.get('product_id') or '(none)'} {repo_slug} "
            f"tool={meta['build_tool']} lang={meta['language']} "
            f"ver={meta['language_version']} from={meta['extracted_from']}"
        )
        return meta

    # ── Reconciliation API (used by the SDLC base-branch metadata gate) ──

    def detect_from_workspace(self, repo_slug: str, workspace_path: str,
                              product_id: str = "") -> dict | None:
        """
        Detect build metadata from a base-branch checkout WITHOUT persisting.

        Used by the SDLC reconciliation step to compare the detected version
        against what is stored for (product_id, repo_slug) BEFORE deciding whether
        to silently upsert (new repo) or raise a HITL confirmation (version drift).
        Reads only the checkout's build files — never queries GitLab or a default
        branch.
        """
        repo_slug = _canon_repo_slug(repo_slug)
        files = self._read_from_workspace(workspace_path)
        if not files:
            return None
        return self._detect(repo_slug, files, product_id=product_id)

    def read_stored(self, repo_slug: str, product_id: str = "") -> dict | None:
        """
        Return the stored repo_build_metadata row for (product_id, repo_slug),
        falling back to the repo-only ('') row written by index-time extraction.
        None when nothing is stored (a genuinely new / never-seen repo).
        """
        # Read from the SAME engine that writes repo_build_metadata (_upsert uses
        # `engine`) — the resolver reads it there too. Do NOT use vector_read_engine.
        from db.database import engine
        repo_slug = _canon_repo_slug(repo_slug)
        try:
            with engine.connect() as sess:
                row = sess.execute(text("""
                    SELECT product_id, repo_slug, build_tool, build_file, language,
                           language_version, build_cmd, test_cmd, extracted_from, confidence
                    FROM   repo_build_metadata
                    WHERE  repo_slug = :slug AND product_id IN (:pid, '')
                    ORDER BY (product_id = :pid) DESC
                    LIMIT  1
                """), {"slug": repo_slug, "pid": product_id or ""}).fetchone()
        except Exception:
            logger.warning("build_metadata: read_stored failed for %s", repo_slug)
            return None
        return dict(row._mapping) if row else None

    def store_confirmed_version(self, repo_slug: str, workspace_path: str,
                                product_id: str, chosen_version: str) -> dict | None:
        """
        Persist the human-confirmed language_version for (product_id, repo_slug).

        Detects the rest of the build metadata from the base-branch checkout,
        overrides the version with the operator's choice, and upserts. Called from
        the AWAITING_BUILD_METADATA_APPROVAL gate resume once the user picks which
        version to use.
        """
        meta = self.detect_from_workspace(repo_slug, workspace_path, product_id=product_id)
        if not meta:
            return None
        meta["language_version"] = str(chosen_version)
        meta["extraction_method"] = "hitl_confirmed"
        self._upsert(meta)
        logger.info(
            f"build_metadata: HITL-confirmed version stored for "
            f"product={product_id or '(none)'} {_canon_repo_slug(repo_slug)} ver={chosen_version}"
        )
        return meta

    # ── Detection ─────────────────────────────────────────────

    def _detect(self, repo_slug: str, files: dict[str, str],
                product_id: str = "") -> dict | None:
        logger.debug(
            f"build_metadata: detecting for {type(repo_slug).__name__} — "
            f"files found: {[f for f in _BUILD_FILE_PRIORITY if f in files]}"
        )
        for sentinel in _BUILD_FILE_PRIORITY:
            if sentinel not in files:
                continue
            content = files[sentinel]
            if not content or not content.strip():
                logger.debug(f"build_metadata: {type(sentinel).__name__} present but empty — skipping")
                continue

            extractor = getattr(self, f"_from_{_method_name(sentinel)}", None)
            if extractor is None:
                continue

            try:
                meta = extractor(content, repo_slug)
                if meta:
                    logger.debug(
                        f"build_metadata: {sentinel} matched for {type(repo_slug).__name__} "
                        f"tool={meta.get('build_tool')} ver={meta.get('language_version')}"
                    )
                    # Enrich with GitLab CI commands if build_cmd still missing
                    if ".gitlab-ci.yml" in files and not meta.get("build_cmd"):
                        _enrich_from_gitlab_ci(meta, files[".gitlab-ci.yml"])
                    # Stamp the product scope so (product_id, repo_slug) upserts
                    # land on the right composite-PK row. Callers that reach
                    # _detect directly (resolver._from_workspace) rely on this too.
                    meta["product_id"] = product_id
                    return meta
                logger.debug(f"build_metadata: {sentinel} extractor returned None for {type(repo_slug).__name__}")
            except Exception:
                logger.debug(f"build_metadata: {sentinel} extractor failed for {repo_slug}")

        logger.warning(f"build_metadata: no sentinel matched for {type(repo_slug).__name__}")
        return None

    # ── Per-format parsers ─────────────────────────────────────

    def _from__sdlc_yml(self, content: str, repo_slug: str) -> dict | None:
        try:
            cfg = yaml.safe_load(content)
            if not isinstance(cfg, dict):
                return None
            build = cfg.get("build", {})
            if not isinstance(build, dict):
                return None
            cmds = build.get("commands", {})
            return _meta(
                repo_slug=repo_slug,
                build_tool=_guess_tool_from_cmd(cmds.get("compile", "")),
                build_file=".sdlc.yml",
                language=_guess_language(build.get("image", "")),
                language_version=str(build.get("env", {}).get(
                    "JAVA_VERSION",
                    build.get("env", {}).get("NODE_VERSION",
                    build.get("env", {}).get("PY_VERSION",
                    build.get("env", {}).get("GO_VERSION", ""))),
                )),
                build_cmd=cmds.get("compile", ""),
                test_cmd=cmds.get("test", ""),
                extracted_from=".sdlc.yml",
                confidence=1.0,
            )
        except Exception:
            return None

    def _from_pom_xml(self, content: str, repo_slug: str) -> dict | None:
        # java.version in <properties>
        ver_match = (
            re.search(r"<java\.version>\s*(\d+)", content) or
            re.search(r"<maven\.compiler\.source>\s*(\d+)", content) or
            re.search(r"<maven\.compiler\.release>\s*(\d+)", content) or
            re.search(r"JavaVersion\.VERSION_(\d+)", content)
        )
        java_ver = ver_match.group(1) if ver_match else "21"

        group_id   = _xml_first(content, "groupId")
        artifact_id = _xml_first(content, "artifactId")
        is_multi   = bool(re.search(r"<modules\s*>", content))

        # AiNxt internal deps: <groupId>org.ainxt*</groupId><artifactId>...</artifactId>
        internal_deps = []
        for m in re.finditer(
            r"<dependency>.*?<groupId>(org\.ainxt[^<]+)</groupId>\s*"
            r"<artifactId>([^<]+)</artifactId>.*?</dependency>",
            content, re.DOTALL
        ):
            internal_deps.append(f"{m.group(1).strip()}:{m.group(2).strip()}")

        return _meta(
            repo_slug=repo_slug,
            build_tool="maven",
            build_file="pom.xml",
            language="java",
            language_version=java_ver,
            build_cmd="mvn clean install -DskipTests -q",
            test_cmd="mvn test -q",
            group_id=group_id,
            artifact_id=artifact_id,
            is_multimodule=is_multi,
            **{config.BUILD_DEPS_COLUMN: list(set(internal_deps))},
            extracted_from="pom.xml",
            confidence=0.9,
        )

    def _from_build_gradle(self, content: str, repo_slug: str) -> dict | None:
        return self._gradle_meta(content, repo_slug, "build.gradle")

    def _from_build_gradle_kts(self, content: str, repo_slug: str) -> dict | None:
        return self._gradle_meta(content, repo_slug, "build.gradle.kts")

    def _gradle_meta(self, content: str, repo_slug: str, fname: str) -> dict | None:
        ver_match = (
            re.search(r"sourceCompatibility\s*=\s*['\"]?(\d+)", content) or
            re.search(r"JavaVersion\.VERSION_(\d+)", content) or
            re.search(r"jvmTarget\s*=\s*['\"](\d+)", content)
        )
        java_ver = ver_match.group(1) if ver_match else "21"

        lang = "kotlin" if "kotlin" in content.lower() else "java"
        test_cmd = "./gradlew test -q" if "test" in content else "./gradlew test -q"

        return _meta(
            repo_slug=repo_slug,
            build_tool="gradle",
            build_file=fname,
            language=lang,
            language_version=java_ver,
            build_cmd="./gradlew classes -q",
            test_cmd=test_cmd,
            extracted_from=fname,
            confidence=0.9,
        )

    def _from_package_json(self, content: str, repo_slug: str) -> dict | None:
        try:
            pkg = json.loads(content)
        except json.JSONDecodeError:
            # Truncated JSON — extract what we can via regex
            node_ver = re.search(r'"node":\s*"[>=^~]*([\d]+)', content)
            test_script = re.search(r'"test":\s*"([^"]+)"', content)
            return _meta(
                repo_slug=repo_slug,
                build_tool="npm",
                build_file="package.json",
                language=_js_language(content),
                language_version=node_ver.group(1) if node_ver else "20",
                build_cmd="npm ci --prefer-offline",
                test_cmd=test_script.group(1) if test_script else "npm test -- --ci",
                extracted_from="package.json",
                confidence=0.7,
            )

        engines  = pkg.get("engines", {})
        node_str = str(engines.get("node", "20"))
        node_ver_m = re.search(r"[\d]+", node_str)
        node_ver = node_ver_m.group() if node_ver_m else "20"

        scripts = pkg.get("scripts", {})
        test_cmd = scripts.get("test", "npm test -- --ci")
        build_cmd = (
            "pnpm install --frozen-lockfile" if "pnpm" in content else
            "yarn install --frozen-lockfile" if "yarn" in content else
            "npm ci --prefer-offline"
        )

        return _meta(
            repo_slug=repo_slug,
            build_tool="npm",
            build_file="package.json",
            language=_js_language(content),
            language_version=node_ver,
            build_cmd=build_cmd,
            test_cmd=test_cmd,
            extracted_from="package.json",
            confidence=0.9,
        )

    def _from_go_mod(self, content: str, repo_slug: str) -> dict | None:
        ver_match = re.search(r"^go\s+([\d.]+)", content, re.MULTILINE)
        go_ver = ver_match.group(1) if ver_match else "1.21"
        # Trim to major.minor only
        go_ver = ".".join(go_ver.split(".")[:2])

        return _meta(
            repo_slug=repo_slug,
            build_tool="go",
            build_file="go.mod",
            language="go",
            language_version=go_ver,
            build_cmd="go build ./...",
            test_cmd="go test ./... -v -count=1",
            extracted_from="go.mod",
            confidence=0.9,
        )

    def _from_cargo_toml(self, content: str, repo_slug: str) -> dict | None:
        return _meta(
            repo_slug=repo_slug,
            build_tool="cargo",
            build_file="Cargo.toml",
            language="rust",
            language_version="stable",
            build_cmd="cargo build -q",
            test_cmd="cargo test -q",
            extracted_from="Cargo.toml",
            confidence=0.85,
        )

    def _from_pyproject_toml(self, content: str, repo_slug: str) -> dict | None:
        ver_match = re.search(r'python_requires\s*=\s*["\']>=\s*([\d.]+)', content)
        py_ver = ver_match.group(1)[:4] if ver_match else "3.11"
        install_cmd = (
            "poetry install --no-interaction" if "poetry" in content else
            "pip install -e '.[test]' -q"
        )
        return _meta(
            repo_slug=repo_slug,
            build_tool="pip" if "pip" in content or "setuptools" in content else "poetry",
            build_file="pyproject.toml",
            language="python",
            language_version=py_ver,
            build_cmd=install_cmd,
            test_cmd="pytest -q",
            extracted_from="pyproject.toml",
            confidence=0.85,
        )

    def _from_requirements_txt(self, content: str, repo_slug: str) -> dict | None:
        return _meta(
            repo_slug=repo_slug,
            build_tool="pip",
            build_file="requirements.txt",
            language="python",
            language_version="3.11",
            build_cmd="pip install -r requirements.txt -q",
            test_cmd="pytest -q",
            extracted_from="requirements.txt",
            confidence=0.8,
        )

    def _from__gitlab_ci_yml(self, content: str, repo_slug: str) -> dict | None:
        try:
            ci = yaml.safe_load(content)
            if not isinstance(ci, dict):
                return None
        except yaml.YAMLError:
            return None

        for job_name in ("build", "compile", "package", "assemble"):
            job = ci.get(job_name)
            if not isinstance(job, dict):
                continue
            scripts = job.get("script", [])
            if not isinstance(scripts, list) or not scripts:
                continue

            # Only take simple commands — skip lines with complex shell
            safe = [
                s for s in scripts
                if isinstance(s, str) and
                not re.search(r"(\$\{[^}]+\}|\bif\b|\bfor\b|\bwhile\b)", s)
            ]
            if not safe:
                continue

            image_str = job.get("image", "")
            image, env_key, env_ver = _builder_from_ci_image(image_str)

            build_cmd = " && ".join(safe[:4])
            test_cmd  = _find_test_cmd_in_ci(ci)

            lang = _guess_language(image_str)
            return _meta(
                repo_slug=repo_slug,
                build_tool=_guess_tool_from_cmd(build_cmd),
                build_file=".gitlab-ci.yml",
                language=lang,
                language_version=env_ver,
                build_cmd=build_cmd,
                test_cmd=test_cmd,
                extracted_from=".gitlab-ci.yml",
                confidence=0.8,
            )
        return None

    def _from_makefile(self, content: str, repo_slug: str) -> dict | None:
        has_build = bool(re.search(r"^build\s*:", content, re.MULTILINE))
        has_test  = bool(re.search(r"^test\s*:", content, re.MULTILINE))
        if not has_build and not has_test:
            return None
        return _meta(
            repo_slug=repo_slug,
            build_tool="make",
            build_file="Makefile",
            language="unknown",
            language_version="",
            build_cmd="make build" if has_build else "make",
            test_cmd="make test"  if has_test  else "",
            extracted_from="Makefile",
            confidence=0.6,
        )

    # ── Workspace fallback ─────────────────────────────────────

    def _read_from_workspace(self, local_path: str) -> dict[str, str]:
        import os
        files: dict[str, str] = {}
        for fname in _BUILD_FILE_PRIORITY:
            fpath = os.path.join(local_path, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        files[fname] = f.read()
                except OSError:
                    pass
        logger.debug(
            f"build_metadata: workspace read from {type(local_path).__name__} — "
            f"found: {list(files.keys()) or 'none'}"
        )
        return files

    # ── DB upsert ──────────────────────────────────────────────

    def _upsert(self, meta: dict) -> None:
        from db.database import engine
        deps_col = config.BUILD_DEPS_COLUMN
        with engine.connect() as sess:
            sess.execute(text(f"""
                INSERT INTO repo_build_metadata
                    (product_id, repo_slug, build_tool, build_file, language, language_version,
                     build_cmd, test_cmd, group_id, artifact_id, is_multimodule,
                     {deps_col}, extracted_from, extraction_method, confidence,
                     created_at, updated_at)
                VALUES
                    (:product_id, :repo_slug, :build_tool, :build_file, :language, :language_version,
                     :build_cmd, :test_cmd, :group_id, :artifact_id, :is_multimodule,
                     :{deps_col}, :extracted_from, :extraction_method, :confidence,
                     NOW(), NOW())
                ON CONFLICT (product_id, repo_slug) DO UPDATE SET
                    build_tool        = EXCLUDED.build_tool,
                    build_file        = EXCLUDED.build_file,
                    language          = EXCLUDED.language,
                    language_version  = EXCLUDED.language_version,
                    build_cmd         = EXCLUDED.build_cmd,
                    test_cmd          = EXCLUDED.test_cmd,
                    group_id          = EXCLUDED.group_id,
                    artifact_id       = EXCLUDED.artifact_id,
                    is_multimodule    = EXCLUDED.is_multimodule,
                    {deps_col}        = EXCLUDED.{deps_col},
                    extracted_from    = EXCLUDED.extracted_from,
                    extraction_method = EXCLUDED.extraction_method,
                    confidence        = EXCLUDED.confidence,
                    updated_at        = NOW()
            """), {**meta,
                   "product_id": meta.get("product_id", ""),
                   "extraction_method": meta.get("extraction_method", "on_demand")})
            sess.commit()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _meta(repo_slug: str, **kw) -> dict:
    defaults = {
        "product_id": "", "group_id": "", "artifact_id": "", "is_multimodule": False,
        config.BUILD_DEPS_COLUMN: [], "extraction_method": "on_demand",
    }
    return {"repo_slug": repo_slug, **defaults, **kw}


def _method_name(sentinel: str) -> str:
    """Convert sentinel filename to Python method suffix."""
    return sentinel.lstrip(".").replace(".", "_").replace("-", "_")


def _xml_first(content: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*([^<]+)\s*</{type(tag).__name__}>", content)
    return m.group(1).strip() if m else ""


def _js_language(content: str) -> str:
    if "typescript" in content.lower() or '"@types/' in content:
        return "typescript"
    return "javascript"


def _guess_language(image_str: str) -> str:
    s = image_str.lower()
    if any(x in s for x in ("java", "jdk", "temurin", "openjdk", "maven", "gradle")):
        return "java"
    if "node" in s or "npm" in s:
        return "javascript"
    if "python" in s:
        return "python"
    if "golang" in s or "go:" in s:
        return "go"
    if "rust" in s or "cargo" in s:
        return "rust"
    return "unknown"


def _guess_tool_from_cmd(cmd: str) -> str:
    cmd = cmd.lower()
    if "mvn" in cmd:      return "maven"
    if "gradle" in cmd:   return "gradle"
    if "npm" in cmd:      return "npm"
    if "yarn" in cmd:     return "yarn"
    if "pnpm" in cmd:     return "pnpm"
    if "pip" in cmd:      return "pip"
    if "poetry" in cmd:   return "poetry"
    if "pytest" in cmd:   return "pip"
    if "go " in cmd:      return "go"
    if "cargo" in cmd:    return "cargo"
    if "make" in cmd:     return "make"
    return "unknown"


def _builder_from_ci_image(image_str: str) -> tuple[str, str, str]:
    """Returns (builder_image_name, unused_env_key, version_string).
    The image_name is now version-specific (e.g. ainxt-builder-jvm-21).
    env_key is kept for API compatibility but is no longer used by the resolver."""
    m = _CI_IMAGE_JAVA_PATTERN.search(image_str)
    if m:
        major = m.group(1)
        tag = major if major in ("17", "21", "25") else "21"
        return f"ainxt-builder-jvm-{type(tag).__name__}", "JAVA_VERSION", major
    m = _CI_IMAGE_NODE_PATTERN.search(image_str)
    if m:
        major = m.group(1)
        tag = major if major in ("18", "20", "22") else "20"
        return f"ainxt-builder-node-{type(tag).__name__}", "NODE_VERSION", major
    m = _CI_IMAGE_PYTHON_PATTERN.search(image_str)
    if m:
        ver = m.group(1)          # e.g. "3.11"
        parts = ver.split(".")
        tag = f"{parts[0]}{parts[1]}" if len(parts) >= 2 else "311"
        tag = tag if tag in ("310", "311", "312") else "311"
        return f"ainxt-builder-python-{type(tag).__name__}", "PY_VERSION", ver
    m = _CI_IMAGE_GO_PATTERN.search(image_str)
    if m:
        return "ainxt-builder-systems", "GO_VERSION", m.group(1)
    return "ainxt-builder-jvm-21", "JAVA_VERSION", "21"


def _find_test_cmd_in_ci(ci: dict) -> str:
    for job_name in ("test", "unit-test", "unit_test", "verify"):
        job = ci.get(job_name)
        if isinstance(job, dict):
            scripts = job.get("script", [])
            if isinstance(scripts, list) and scripts:
                safe = [s for s in scripts if isinstance(s, str)]
                if safe:
                    return " && ".join(safe[:3])
    return ""


def _enrich_from_gitlab_ci(meta: dict, ci_content: str) -> None:
    """Attempt to fill empty build_cmd / test_cmd from .gitlab-ci.yml."""
    try:
        ci = yaml.safe_load(ci_content)
        if not isinstance(ci, dict):
            return
        if not meta.get("build_cmd"):
            for job_name in ("build", "compile", "package"):
                job = ci.get(job_name, {})
                scripts = job.get("script", []) if isinstance(job, dict) else []
                safe = [s for s in scripts if isinstance(s, str)]
                if safe:
                    meta["build_cmd"] = " && ".join(safe[:4])
                    break
        if not meta.get("test_cmd"):
            meta["test_cmd"] = _find_test_cmd_in_ci(ci)
    except Exception:
        pass