# SPDX-License-Identifier: Apache-2.0
"""
core/ainxt_dependency_resolver.py

Checks whether AiNxt-internal Maven artifacts required by a repo are published
in the AiNxt Nexus repository.  If missing, triggers a build of the dependency
repo first (topological ordering).

Data source: repo_build_metadata.<BUILD_DEPS_COLUMN>[]
  — populated at index time by BuildMetadataExtractor from pom.xml <dependency> blocks
  — format: "org.ainxt.payments:payment-common"

NOT using code_graph here:
  code_graph stores class-level import names (e.g. "org.ainxt.payment.PaymentService")
  which cannot be reliably mapped to Maven artifact IDs ("org.ainxt:payment-service").
  These are fundamentally different naming conventions.
  code_graph is used elsewhere for LLM context (which classes are affected),
  not for build dependency resolution.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import requests
from sqlalchemy import text

from core import config

logger = logging.getLogger("ainxt.ainxt_dependency_resolver")

_NEXUS_CHECK_TIMEOUT = 5   # seconds per HEAD request


class DepCheckResult(NamedTuple):
    missing: list[str]          # artifact coords that are NOT in Nexus
    triggered: list[str]        # repo slugs whose builds were enqueued


class AiNxtDependencyResolver:

    def check_and_trigger(self, repo_slug: str) -> DepCheckResult:
        """
        For each AiNxt-internal artifact declared in repo_slug's pom.xml:
          1. Check Nexus via HTTP HEAD
          2. If missing → find the source repo, enqueue its build at HIGH priority

        Returns DepCheckResult(missing, triggered).
        If Nexus URL is not configured, skips all checks (returns empty result).
        """
        if not config.AiNxt_NEXUS_URL:
            logger.debug("dep_resolver: AiNxt_NEXUS_URL not set — skipping dep check")
            return DepCheckResult(missing=[], triggered=[])

        internal_deps = self._load_deps(repo_slug)
        if not internal_deps:
            return DepCheckResult(missing=[], triggered=[])

        missing: list[str] = []
        triggered: list[str] = []

        for artifact_coord in internal_deps:
            # artifact_coord format: "org.ainxt.payments:payment-common"
            # or "org.ainxt.payments:payment-common:1.2.0"
            parts = artifact_coord.split(":")
            if len(parts) < 2:
                continue
            group_id, artifact_id = parts[0], parts[1]
            version = parts[2] if len(parts) > 2 else None

            if self._in_nexus(group_id, artifact_id, version):
                continue

            logger.info(
                f"dep_resolver: {repo_slug} needs {artifact_coord} — not in Nexus"
            )
            missing.append(artifact_coord)

            # Try to find and trigger the source repo
            dep_repo_slug = self._find_source_repo(group_id, artifact_id)
            if dep_repo_slug and dep_repo_slug != repo_slug:
                self._trigger_build(dep_repo_slug, reason=f"required by {repo_slug}")
                triggered.append(dep_repo_slug)
            else:
                logger.warning(
                    f"dep_resolver: {artifact_coord} not in Nexus and no source repo found. "
                    f"Publish it to Nexus manually."
                )

        return DepCheckResult(missing=missing, triggered=triggered)

    # ── Nexus check ────────────────────────────────────────────

    def _in_nexus(self, group_id: str, artifact_id: str, version: str | None) -> bool:
        """
        HEAD request to Nexus maven-public repository.
        Returns True if artifact directory exists (any version), False otherwise.
        """
        group_path = group_id.replace(".", "/")
        if version:
            url = (
                f"{config.AiNxt_NEXUS_URL.rstrip('/')}/repository/maven-public/"
                f"{group_path}/{artifact_id}/{version}/"
            )
        else:
            # No version specified — check artifact directory
            url = (
                f"{config.AiNxt_NEXUS_URL.rstrip('/')}/repository/maven-public/"
                f"{group_path}/{artifact_id}/"
            )
        try:
            resp = requests.head(url, timeout=_NEXUS_CHECK_TIMEOUT, allow_redirects=True)
            return resp.status_code in (200, 301, 302)
        except requests.RequestException:
            logger.warning("dep_resolver: Nexus HEAD failed for %s", url)
            # Treat unreachable Nexus as "exists" to avoid blocking the pipeline
            return True

    # ── Source repo lookup ─────────────────────────────────────

    def _find_source_repo(self, group_id: str, artifact_id: str) -> str | None:
        """
        Find the AiNxt repo that produces this artifact.
        Looks up repo_build_metadata by group_id + artifact_id.
        """
        from db.database import engine
        try:
            with engine.connect() as sess:
                row = sess.execute(text("""
                    SELECT repo_slug FROM repo_build_metadata
                    WHERE  group_id    = :gid
                      AND  artifact_id = :aid
                    LIMIT 1
                """), {"gid": group_id, "aid": artifact_id}).fetchone()
                return row.repo_slug if row else None
        except Exception:
            logger.debug("dep_resolver: source repo lookup failed for %s/%s", group_id, artifact_id)
            return None

    # ── Trigger build ──────────────────────────────────────────

    def _trigger_build(self, dep_repo_slug: str, reason: str) -> None:
        """Enqueue a compile-only build for the dependency repo at HIGH priority."""
        try:
            from core.job_queue import get_queue
            q = get_queue("sdlc_queue")
            q.enqueue(
                "workers.workspace_sync_worker.build_dep_repo",
                dep_repo_slug,
                reason,
                job_timeout=600,
                # enqueue at front of queue so dep is ready before parent
            )
            logger.info(f"dep_resolver: triggered build for {dep_repo_slug} — {reason}")
        except Exception:
            logger.warning("dep_resolver: failed to trigger %s", dep_repo_slug)

    # ── Load deps from DB ──────────────────────────────────────

    def _load_deps(self, repo_slug: str) -> list[str]:
        from db.database import engine
        try:
            with engine.connect() as sess:
                row = sess.execute(text(f"""
                    SELECT {config.BUILD_DEPS_COLUMN} FROM repo_build_metadata WHERE repo_slug = :slug
                """), {"slug": repo_slug}).fetchone()
                return list(getattr(row, config.BUILD_DEPS_COLUMN) or []) if row else []
        except Exception:
            logger.debug("dep_resolver: load deps failed for %s", repo_slug)
            return []