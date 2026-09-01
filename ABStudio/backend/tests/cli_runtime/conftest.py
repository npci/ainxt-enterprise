# SPDX-License-Identifier: Apache-2.0
"""Test fixtures for the cli_runtime suite.

Two jobs:

1. **Bootstrap ``sys.path``** with the platform root, the same way
   ``app/main.py`` does at import time, so ``core.*`` and ``store.*`` resolve.
2. **Stub the heavy platform imports** that a unit test has no business
   requiring: ``core.logger`` pulls in ``structlog``, and importing
   ``app.core`` transitively pulls in Postgres. Stubbing them keeps this suite
   runnable on a bare interpreter (and in CI without a database), which is the
   whole point of having a fake-CLI harness rather than debugging by log.

Anything genuinely under test is imported for real.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover
    # ``run_checks.py`` imports this module purely for its sys.path bootstrap and
    # logger stub, on interpreters where pytest is not installed. A tiny shim lets
    # the fixture decorators below still evaluate.
    class _Raises:
        """Minimal ``pytest.raises`` replacement."""

        def __init__(self, expected):
            self.expected = expected
            self.value: BaseException | None = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, _tb):
            if exc_type is None:
                raise AssertionError(f"expected {self.expected} to be raised")
            if not issubclass(exc_type, self.expected):
                return False  # unexpected type — let it propagate
            self.value = exc
            return True  # swallow the expected exception

    class _PytestShim(types.ModuleType):
        @staticmethod
        def fixture(*args, **kwargs):
            def _decorator(fn):
                return fn
            return _decorator(args[0]) if args and callable(args[0]) else _decorator

        @staticmethod
        def raises(expected, **_kwargs):
            return _Raises(expected)

        @staticmethod
        def skip(reason: str = ""):
            raise _Skipped(reason)

    class _Skipped(Exception):
        """Raised by the shim's ``skip`` so the runner can report it."""

    pytest = _PytestShim("pytest")  # type: ignore[assignment]
    pytest.Skipped = _Skipped       # type: ignore[attr-defined]
    sys.modules.setdefault("pytest", pytest)

# ── sys.path: <root>/ABStudio/backend and <root> ────────────────────────────
_BACKEND = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _BACKEND.parents[1]
for _p in (str(_BACKEND), str(_PLATFORM_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── stub core.logger (avoids the structlog dependency) ──────────────────────
def _install_logger_stub() -> None:
    if "core.logger" in sys.modules:
        return
    try:
        import structlog  # noqa: F401
        return  # real logger is usable; don't shadow it
    except Exception:
        pass

    core_mod = sys.modules.get("core")
    if core_mod is None:
        core_mod = types.ModuleType("core")
        core_mod.__path__ = [str(_PLATFORM_ROOT / "core")]  # keep it a package
        sys.modules["core"] = core_mod

    class _Logger:
        """Records calls so a test can assert on them, prints nothing."""

        def __init__(self) -> None:
            self.records: list = []

        def _emit(self, level: str, msg: str, **kw) -> None:
            self.records.append((level, msg, kw))

        def debug(self, msg="", **kw): self._emit("debug", msg, **kw)
        def info(self, msg="", **kw): self._emit("info", msg, **kw)
        def warning(self, msg="", **kw): self._emit("warning", msg, **kw)
        def error(self, msg="", **kw): self._emit("error", msg, **kw)
        def exception(self, msg="", **kw): self._emit("exception", msg, **kw)

    logger_mod = types.ModuleType("core.logger")
    logger_mod.logger = _Logger()
    # Real ``core.logger`` exports these too, and platform modules import them by
    # name (``from core.logger import logger, LOG_LEVEL``). A stub missing one
    # raises ImportError, which callers then swallow — so a guard that should have
    # run is silently skipped and the test passes for the wrong reason.
    logger_mod.LOG_LEVEL = "INFO"
    logger_mod.bind_log_context = lambda **_kw: None
    logger_mod.set_client_source = lambda *_a, **_k: None
    logger_mod.get_logger = lambda *_a, **_k: logger_mod.logger
    sys.modules["core.logger"] = logger_mod
    setattr(core_mod, "logger", logger_mod)


_install_logger_stub()


@pytest.fixture(autouse=True)
def _clean_cli_env(monkeypatch):
    """Start every test from a known-empty CLI configuration.

    ``cli_runtime.config`` reads env at call time by design, so a leaked var from
    a previous test (or the developer's shell) would silently change behaviour.
    """
    for key in list(os.environ):
        if key.startswith("ABSTUDIO_CLI") or key.startswith("ABSTUDIO_MCP"):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def registry():
    """A clean session registry, torn down after the test."""
    from app.cli_runtime.session import get_registry

    reg = get_registry()
    reg.clear()
    yield reg
    reg.clear()
