# SPDX-License-Identifier: MIT
"""
agents/_stage_lock.py — shared lock for the `.git/info/exclude` read-modify-append.

Two independent subsystems both append patterns to the SAME
`<workspace>/.git/info/exclude` file for the same underlying reason (keep
staged-but-not-yet-reviewed material out of the diff / VERIFIED_DIFF):

  - `agents/multi_repo_workspace.py` — stages internal dep checkouts under
    `<workspace>/.sdlc_deps/` for multi-repo SDLC runs.
  - `agents/sdlc_governance/engine.py` — stages governance skill materials
    under `<workspace>/.governance_skills/` for the governance review phase.

A read-modify-append on a shared file needs ONE lock object shared by both
writers — two separate `threading.Lock()` instances (one per module) would
each serialize its own module's writes but do nothing to prevent the two
modules' writers from interleaving with each other, making the "mutual"
exclusion illusory.

This lock lives in its own module, independent of both subsystems, so
neither one has to import the other to share it (avoiding a cross-module
import that would pull one module's dependency chain into the other).

This module MUST NOT import anything beyond `threading` — it is
intentionally dependency-free so it can be imported from lightweight,
import-light modules without side effects.
"""

import threading

STAGE_LOCK = threading.Lock()
