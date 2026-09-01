# SPDX-License-Identifier: Apache-2.0
"""Dependency-free runner for the cli_runtime checks.

The suite is written as ordinary pytest tests, but pytest is not installed
everywhere this needs to run (and a bare interpreter is exactly the environment
where a missing dependency would otherwise hide a regression). This runner
discovers the same ``Test*`` classes and ``test_*`` functions, provides the two
fixtures they use, and reports pass/fail — so the checks work with or without
pytest, and CI can call either.

Run:  python tests/cli_runtime/run_checks.py [pattern]
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parents[1]
for _p in (str(_BACKEND), str(_BACKEND.parents[1]), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Install the same stubs conftest.py provides, so importing the modules under
# test does not require structlog or a database.
import conftest  # noqa: E402,F401  (import triggers the stubs)

MODULES = [
    "test_config",
    "test_session",
    "test_mcp_server",
    "test_workspace",
    "test_runner",
    "test_event_mapper",
    "test_bridge",
    "test_sanitize",
    "test_credentials",
    "test_integration",
    "test_flag_off",
    "test_real_cli",
]


# Switches that choose WHICH tests run, rather than configuring the runtime, so
# the per-test environment reset must not clear them.
_PRESERVED_ENV = frozenset({
    "ABSTUDIO_CLI_TEST_BINARY",
    "ABSTUDIO_CLI_SMOKE_MODEL",
})


class _MonkeyPatch:
    """The subset of pytest's monkeypatch these tests use, with undo."""

    def __init__(self) -> None:
        self._undo: list = []

    def setenv(self, name: str, value: str) -> None:
        self._undo.append((name, os.environ.get(name)))
        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        self._undo.append((name, os.environ.get(name)))
        del os.environ[name]

    def setattr(self, target, name, value):  # noqa: A003 - mirrors pytest
        self._undo.append(("__attr__", (target, name, getattr(target, name, None))))
        setattr(target, name, value)

    def undo(self) -> None:
        for name, old in reversed(self._undo):
            if name == "__attr__":
                target, attr, prev = old
                setattr(target, attr, prev)
            elif old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self._undo.clear()


def _make_fixtures(mp: _MonkeyPatch) -> dict:
    """Build the fixture values the tests request by parameter name."""
    from app.cli_runtime.session import get_registry

    # Mirror conftest._clean_cli_env: start from a known-empty CLI config, so a
    # leaked var cannot silently change behaviour. Opt-in test switches are
    # preserved — they select which tests run rather than configure the runtime.
    for key in list(os.environ):
        if key in _PRESERVED_ENV:
            continue
        if key.startswith("ABSTUDIO_CLI") or key.startswith("ABSTUDIO_MCP"):
            mp.delenv(key, raising=False)

    registry = get_registry()
    registry.clear()
    return {"monkeypatch": mp, "registry": registry, "tmp_path": None}


def _call(fn, fixtures: dict, instance=None):
    """Invoke a test callable, supplying only the fixtures it asks for."""
    signature = inspect.signature(fn)
    kwargs = {}
    for param in signature.parameters:
        if param == "self":
            continue
        if param in fixtures:
            value = fixtures[param]
            if param == "tmp_path" and value is None:
                import tempfile
                value = Path(tempfile.mkdtemp())
                fixtures["tmp_path"] = value
            kwargs[param] = value
    result = fn(**kwargs) if instance is None else fn(**kwargs)
    if inspect.iscoroutine(result):
        asyncio.run(result)


def _cases(module):
    """Return ``[(name, callable, instance)]`` for every test in a module.

    Materialised into a list rather than generated lazily: running a test can
    import modules, which mutates ``module.__dict__`` and would invalidate an
    open iterator over it.
    """
    found = []
    for name, obj in list(vars(module).items()):
        if name.startswith("test_") and inspect.isfunction(obj):
            found.append((f"{module.__name__}.{name}", obj, None))
        elif name.startswith("Test") and inspect.isclass(obj):
            for attr, fn in list(vars(obj).items()):
                if attr.startswith("test_") and inspect.isfunction(fn):
                    instance = obj()
                    found.append((f"{module.__name__}.{name}.{attr}",
                                  fn.__get__(instance, obj), instance))
    return found


def _skip_exceptions():
    """Exception types that mean "skipped", from real pytest or the shim."""
    import pytest as _pytest

    types_ = []
    for attr in ("Skipped", "skip"):
        candidate = getattr(_pytest, attr, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types_.append(candidate)
    outcomes = getattr(_pytest, "outcomes", None)
    if outcomes is not None and hasattr(outcomes, "Skipped"):
        types_.append(outcomes.Skipped)
    return tuple(types_) or (_NeverRaised,)


class _NeverRaised(Exception):
    """Placeholder so the except clause is always valid."""


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    passed = failed = skipped = 0
    failures: list = []

    for module_name in MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if module_name in str(exc):
                continue  # test file not present yet
            raise

        print(f"\n{module_name}")
        for name, fn, _instance in _cases(module):
            if pattern and pattern not in name:
                continue
            mp = _MonkeyPatch()
            fixtures = _make_fixtures(mp)
            try:
                _call(fn, fixtures)
                passed += 1
                print(f"  PASS  {name.split('.', 1)[1]}")
            except _skip_exceptions() as exc:  # noqa: BLE001
                skipped += 1
                print(f"  SKIP  {name.split('.', 1)[1]}  ({exc})")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                failures.append((name, traceback.format_exc()))
                print(f"  FAIL  {name.split('.', 1)[1]}  ({type(exc).__name__}: {exc})")
            finally:
                mp.undo()

    if failures:
        print("\n" + "=" * 70)
        for name, trace in failures:
            print(f"\nFAILED {name}\n{trace}")

    summary = f"\n{passed} passed, {failed} failed"
    if skipped:
        summary += f", {skipped} skipped"
    print(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
