# SPDX-License-Identifier: MIT
# ============================================================
# workflows/context.py
# Re-exports Context from the installed workflows site-package
# so that llama_index.core.workflow.context can resolve:
#   from workflows.context import Context
#
# Strategy: temporarily remove the project root from sys.path
# and import the real site-packages `workflows.context` package.
# After import, restore our project's `workflows` package in
# sys.modules so our workflows.engine etc. still work.
# ============================================================

import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- Stash our package so we can restore it after loading site-packages ----
_our_workflows = sys.modules.pop("workflows", None)
# Remove any submodule entries that belong to our package
_our_submodules = {
    k: sys.modules.pop(k)
    for k in list(sys.modules)
    if k.startswith("workflows.") and "venv" not in sys.modules.get(k, object).__spec__.origin
    if hasattr(sys.modules.get(k, None), "__spec__")
    and sys.modules[k].__spec__ is not None
    and "venv" not in (sys.modules[k].__spec__.origin or "")
}

# ---- Temporarily remove project root from sys.path -------------------------
_saved_path = sys.path[:]
sys.path = [p for p in sys.path if os.path.abspath(p) != _project_root]

try:
    # Import the real site-packages workflows package.
    # This populates sys.modules["workflows"], ["workflows.context"],
    # ["workflows.context.context"], etc. with the real classes.
    import workflows as _sp_wf  # noqa
    from workflows.context import Context  # noqa — brings Context into our namespace
except ImportError:
    # Fallback stub if site-packages workflows is somehow missing
    class Context:  # type: ignore[no-redef]
        pass
finally:
    # Restore sys.path
    sys.path = _saved_path

    # Restore our project's `workflows` package as the canonical package
    # in sys.modules, BUT leave all the context/errors/etc. sub-modules
    # from site-packages so relative imports in those modules keep working.
    if _our_workflows is not None:
        sys.modules["workflows"] = _our_workflows
    for k, v in _our_submodules.items():
        sys.modules.setdefault(k, v)

__all__ = ["Context"]
