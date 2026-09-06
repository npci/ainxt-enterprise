#!/usr/bin/env python3
"""
Compare a pytest run against the recorded known-failure baseline.

Purpose: block NEW test breakage without first requiring every pre-existing
failure to be fixed. A pipeline that is red on day one gets ignored; one that is
green and goes red only on real regressions gets trusted.

    pytest <paths> -q -rf | tee /tmp/pytest.out
    python scripts/ci/compare_test_failures.py /tmp/pytest.out

Exit codes:
    0  no new failures (baseline failures are reported, not fatal)
    1  at least one test failed that is not in the baseline

Baseline file: scripts/ci/known_failures.txt (override with --baseline).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_BASELINE = Path(__file__).resolve().parent / "known_failures.txt"


def load_baseline(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def parse_failures(text: str) -> set[str]:
    """Collect test ids from pytest's `-rf` short summary and FAILED/ERROR lines."""
    found = set()
    for line in text.splitlines():
        m = re.match(r"^(?:FAILED|ERROR)\s+(\S+)", line.strip())
        if m:
            found.add(m.group(1).split(" ")[0])
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("output", help="file containing pytest output")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    args = ap.parse_args()

    baseline = load_baseline(Path(args.baseline))
    try:
        failures = parse_failures(Path(args.output).read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        print(f"could not read pytest output: {exc}")
        return 1

    new = sorted(failures - baseline)
    fixed = sorted(baseline - failures)
    still = sorted(failures & baseline)

    print(f"baseline: {len(baseline)} known failure(s)")
    print(f"this run: {len(failures)} failure(s) "
          f"({len(still)} known, {len(new)} new)\n")

    if fixed:
        print(f"  {len(fixed)} baseline failure(s) now PASS — please remove them from")
        print(f"  {Path(args.baseline).name} so they cannot regress unnoticed:")
        for t in fixed[:20]:
            print(f"      {t}")
        if len(fixed) > 20:
            print(f"      … and {len(fixed) - 20} more")
        print()

    if new:
        print(f"  {len(new)} NEW failure(s) — these are regressions introduced by this change:")
        for t in new:
            print(f"      {t}")
        print("\n  If a failure is genuinely expected, add it to "
              f"{Path(args.baseline).name} with a reason in the commit message.")
        return 1

    print("  No new failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
