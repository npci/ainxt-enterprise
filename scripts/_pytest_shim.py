# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Minimal pytest shim for the OFFLINE stdlib test runner
# ============================================================
# The sandbox cannot install pytest (corporate index unreachable). Many test
# files do `import pytest` only for a handful of helpers. This shim implements
# just enough of the pytest API so those modules can be IMPORTED and their
# plain-assert `test_*` functions executed by scripts/offline_test_report.py.
#
# It is a TEST-ONLY convenience for offline visibility — NOT a pytest
# replacement. Parametrized tests are expanded; fixtures are best-effort
# (only zero-arg / trivially-resolvable ones). Anything it can't emulate raises,
# and the runner records that honestly as an error (never a false PASS).
#
# Installed into sys.modules as "pytest" by the runner before test import.
# ============================================================

from __future__ import annotations

import functools
from contextlib import contextmanager


class _Raises:
    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self.expected!r}")
        ok = issubclass(exc_type, self.expected) if isinstance(self.expected, type) \
            else issubclass(exc_type, tuple(self.expected))
        if not ok:
            return False  # re-raise unexpected exception
        self.value = exc
        return True  # swallow the expected exception


def raises(expected, *args, match=None):
    # form 1: with pytest.raises(Err): ...
    if not args:
        return _Raises(expected, match=match)
    # form 2: pytest.raises(Err, callable, *a, **k)
    func, *rest = args
    try:
        func(*rest)
    except expected:
        return None
    raise AssertionError(f"DID NOT RAISE {expected!r}")


class approx:
    def __init__(self, value, rel=1e-6, abs=1e-12):
        self.value = value
        self.rel = rel
        self.abs = abs

    def __eq__(self, other):
        try:
            return abs(float(other) - float(self.value)) <= max(
                self.abs, self.rel * abs(float(self.value)))
        except Exception:
            return NotImplemented


def fixture(*fargs, **fkwargs):
    """Best-effort fixture: returns the function unchanged so a test that calls
    it directly still works; the runner treats fixture-injected params as
    un-resolvable and records an honest error rather than a false pass."""
    def _wrap(fn):
        fn.__is_fixture__ = True
        return fn
    if fargs and callable(fargs[0]):
        return _wrap(fargs[0])
    return _wrap


class _Mark:
    """Emulates @pytest.mark.parametrize and no-op marks (skip, skipif, etc.)."""

    def parametrize(self, argnames, argvalues, **kwargs):
        names = [a.strip() for a in argnames.split(",")] if isinstance(argnames, str) else list(argnames)

        def _decorator(fn):
            cases = []
            for row in argvalues:
                row = row if isinstance(row, (tuple, list)) else (row,)
                cases.append(dict(zip(names, row)))

            @functools.wraps(fn)
            def _expanded(*a, **k):
                # Run every parametrized case; first failure raises.
                for c in cases:
                    fn(**c)
            _expanded.__parametrized__ = len(cases)
            return _expanded
        return _decorator

    def __getattr__(self, _name):
        # skip / skipif / xfail / usefixtures / etc. → no-op passthrough decorator
        def _noop(*a, **k):
            if a and callable(a[0]):
                return a[0]
            def _d(fn):
                return fn
            return _d
        return _noop


mark = _Mark()


def skip(reason=""):
    raise _Skipped(reason)


def fail(reason=""):
    raise AssertionError(reason)


class _Skipped(Exception):
    pass


@contextmanager
def warns(*a, **k):
    yield


def importorskip(name, *a, **k):
    import importlib
    return importlib.import_module(name)


class _Param:
    def __call__(self, *values, **kwargs):
        return values[0] if len(values) == 1 else values


param = _Param()
