# SPDX-License-Identifier: Apache-2.0
"""``agents.sdlc_pipeline`` package facade.

Historically a single ~10k-line module. Part B of the 2026-08-04 decomposition
splits it into focused submodules while preserving every public and internal name
previously reachable as ``agents.sdlc_pipeline.<name>``. External callers (workers,
routers, Slack/Teams adapters, the state machine) do lazy in-function
``from agents.sdlc_pipeline import X`` imports; those keep working because this
facade re-exports the bulk from ``._core`` and lazily resolves the extracted
submodules (``._phases`` and ``agents.sdlc_governance.pipeline``) via module
``__getattr__``.

Stage 1 (2026-08-04): governance pipeline extracted to
``agents.sdlc_governance.pipeline``.
Stage 2 (2026-08-04): phase functions (normalize/validate/explore/PLAN/REVIEW +
governance review/scan-snapshot) extracted to ``._phases``.
Remaining bulk lives in ``._core`` and will be split further in later stages.
"""

# Bring the bulk (public names) into the package namespace. ``_core`` is a leaf:
# it imports nothing from this package, so this is a clean one-way load.
from ._core import *  # noqa: F401,F403

# Eagerly load AND bind the extracted submodules as package attributes. This is
# acyclic: ``_core`` (above) is fully initialised first, and ``_phases`` imports
# only from ``_core`` (never from this facade or from governance), so loading it
# here cannot cycle. Binding it now is also REQUIRED for correctness — without it,
# ``__getattr__`` below would run ``from . import _phases`` while resolving a
# phase-resident name, and because ``_phases`` would not yet be bound as an
# attribute that ``from . import`` re-enters ``__getattr__('_phases')`` and
# recurses infinitely. (The governance module stays lazy — it lives in a separate
# package and carries heavier optional deps, so we don't pull it into every
# ``import agents.sdlc_pipeline``.)
from . import _core as _core      # noqa: F401  (ensure the submodule attr is bound)
from . import _phases as _phases  # noqa: F401


def __getattr__(name):
    """Transparent attribute proxy (PEP 562).

    Resolves any attribute not already bound on the package to, in order: the
    leaf module ``._core`` (the bulk, incl. underscore internals like
    ``_cv_run_id``/``_transition`` imported lazily by the state machine), then the
    extracted ``._phases`` module (``_run_review_phase``,
    ``run_governance_scan_snapshot`` etc.), then the governance pipeline module.
    ``_core`` and ``_phases`` are already bound (eagerly, above), so the lookups
    here are plain attribute reads and never re-enter this ``__getattr__``. The
    governance module is imported lazily the first time a governance name is
    requested.
    """
    if hasattr(_core, name):
        return getattr(_core, name)
    if hasattr(_phases, name):
        return getattr(_phases, name)
    from agents.sdlc_governance import pipeline as _gov_pipeline
    if hasattr(_gov_pipeline, name):
        return getattr(_gov_pipeline, name)
    raise AttributeError(f"module 'agents.sdlc_pipeline' has no attribute {name!r}")
