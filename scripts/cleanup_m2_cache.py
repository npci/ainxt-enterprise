# SPDX-License-Identifier: MIT
"""
scripts/cleanup_m2_cache.py

Weekly cleanup of /opt/ainxt/dep_cache — keeps the 10 most-recently-used
pom-hash cache dirs per repo, removes the rest.

Run via cron:
    0 2 * * 0  cd /opt/ainxt && venv/bin/python scripts/cleanup_m2_cache.py

Layout expected:
    /opt/ainxt/dep_cache/
        {repo_slug}/
            {pom_hash}/
                m2/   ← content-addressed .m2 repo populated by WorkspaceBuilder
"""

import os
import shutil
from pathlib import Path

CACHE_ROOT = Path(os.getenv("M2_DEP_CACHE_ROOT", "/opt/ainxt/dep_cache"))
KEEP = 10


def cleanup() -> None:
    if not CACHE_ROOT.is_dir():
        print(f"[cleanup_m2_cache] cache root not found: {CACHE_ROOT} — nothing to do")
        return

    total_removed = 0
    total_kept = 0

    for repo_dir in sorted(CACHE_ROOT.iterdir()):
        if not repo_dir.is_dir():
            continue

        # Each child is a pom_hash dir; sort by mtime descending (newest first)
        hash_dirs = sorted(
            (d for d in repo_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )

        keep = hash_dirs[:KEEP]
        remove = hash_dirs[KEEP:]

        for d in remove:
            try:
                shutil.rmtree(d)
                total_removed += 1
                print(f"[cleanup_m2_cache] removed {d}")
            except Exception as exc:
                print(f"[cleanup_m2_cache] WARN: could not remove {d}: {exc}")

        total_kept += len(keep)

    print(
        f"[cleanup_m2_cache] done — kept {total_kept} dirs, "
        f"removed {total_removed} dirs across {CACHE_ROOT}"
    )


if __name__ == "__main__":
    cleanup()
