# SPDX-License-Identifier: Apache-2.0
# ============================================================
# tests/models/ conftest — test-only environment setup.
#
# models.followup_condenser imports models.model_router lazily (inside the
# function body, not at module load), and model_router's import chain
# transitively reaches auth.jwt_handler, which raises at import time if
# JWT_SECRET is unset (see auth/jwt_handler.py — a deliberate fail-loud
# guard against misconfigured deployments). Setting a dummy JWT_SECRET
# here (test-only, never used for real signing) lets that import chain
# resolve so these unit tests can mock model_router.generate() without
# needing a real JWT secret configured.
# ============================================================

import os

os.environ.setdefault("JWT_SECRET", "test-secret-for-unit-tests-only-" + "x" * 32)
