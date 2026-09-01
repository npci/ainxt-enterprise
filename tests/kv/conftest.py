# SPDX-License-Identifier: Apache-2.0
# ============================================================
# tests/kv subtree conftest.
#
# All shared fixtures (kv, async_kv, key_prefix, clean_factory_cache,
# patched_module_client) now live in the root-level tests/conftest.py
# so they are visible to every test under tests/* — including
# tests/store/, tests/auth/, tests/memory/, tests/connectors/,
# tests/core/, tests/workers/.
#
# This file is intentionally empty; remove it once the rest of the
# tree has settled and there's no longer any chance an old reference
# resolves here.
# ============================================================
