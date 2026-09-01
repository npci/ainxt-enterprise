#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Lint guard — fails the build if any *.py file (outside the allow-list)
imports the `redis` package directly.

Application code must go through ``core.kv.get_kv()`` (sync) or
``core.kv.async_get_kv()`` (async). The only files allowed to talk to
``redis`` directly are:

  * core/kv/redis_impl.py         — the RedisKVClient wrapper
  * core/kv/async_redis_impl.py   — the AsyncRedisKVClient wrapper
  * core/kv/queue.py              — the rq queue connection factory
  * core/config.py                — the redis_client() legacy helper (kept
                                    for callers that still need a raw
                                    redis.Redis instance during rollout)
  * tests/**                      — fixtures may instantiate clients directly

Run from the repo root:

    python scripts/check_no_redis_imports.py

Exits non-zero with a per-file list when a violation is found.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


# Paths (relative to repo root) that are explicitly allowed to
# import the `redis` package. Anything else must use core.kv.
ALLOW_LIST: set[str] = {
    "core/kv/redis_impl.py",
    "core/kv/async_redis_impl.py",
    "core/kv/queue.py",
    "core/config.py",
    "scripts/check_no_redis_imports.py",
}

# Directories to skip entirely.
SKIP_DIRS: set[str] = {
    ".venv",
    ".kilo",
    ".git",
    "__pycache__",
    "node_modules",
    "ai-ui",
    "tests",
}

# Patterns considered violations.
PATTERNS = [
    re.compile(r"^\s*import\s+redis\b(?!\.asyncio)"),
    re.compile(r"^\s*from\s+redis\s+import\b"),
    re.compile(r"\bredis\.Redis\s*\("),
]


def _walk_python_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _check_file(path: Path, repo_root: Path) -> list[tuple[int, str]]:
    rel = path.relative_to(repo_root).as_posix()
    if rel in ALLOW_LIST:
        return []
    violations: list[tuple[int, str]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                # Skip comments quickly — a "#" at the start of the stripped line
                # means the rest of the line is a comment.
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                for pat in PATTERNS:
                    if pat.search(line):
                        violations.append((lineno, line.rstrip()))
                        break
    except OSError:
        pass
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    bad: dict[str, list[tuple[int, str]]] = {}
    for py in _walk_python_files(repo_root):
        viol = _check_file(py, repo_root)
        if viol:
            rel = py.relative_to(repo_root).as_posix()
            bad[rel] = viol

    if not bad:
        print("OK — no disallowed `import redis` found.")
        return 0

    print("FAIL — disallowed direct `redis` imports found:\n")
    for rel, lines in sorted(bad.items()):
        print(f"  {rel}")
        for ln, src in lines:
            print(f"    line {ln}: {src.strip()}")
    print("\nApplication code must use `core.kv.get_kv()` instead.")
    print("If a file genuinely needs direct redis access, add it to "
          "ALLOW_LIST in scripts/check_no_redis_imports.py.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
