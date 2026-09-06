# SPDX-License-Identifier: MIT
# ============================================================
# RustyCluster pipeline __exit__ behaviour.
#
# Covers Gap #13 — un-executed ops at context exit emit a warning
# log and the buffer is cleared.
#
# We do NOT use pytest's `caplog` because the warning is emitted via
# core.logger.logger (structlog), which bypasses the stdlib logging
# handlers caplog hooks into. Instead we patch `logger.warning` on
# the core.logger module so the test can assert directly.
#
# Skipped if py-rustycluster-client is not installed.
# ============================================================

from __future__ import annotations

import pytest


pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


class _WarnCapture:
    """Replaces logger.warning to record (fmt, args) tuples."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def __call__(self, fmt, *args, **kwargs):
        self.calls.append((str(fmt), args))

    @property
    def messages(self) -> list[str]:
        out = []
        for fmt, args in self.calls:
            try:
                out.append(fmt % args if args else fmt)
            except Exception:
                out.append(fmt)
        return out


@pytest.fixture
def patched_warn(monkeypatch):
    """Replace core.logger.logger.warning with a capturing stub."""
    import core.logger as _cl
    cap = _WarnCapture()
    monkeypatch.setattr(_cl.logger, "warning", cap)
    return cap


def _make_rc_pipeline():
    """Build an RC pipeline against a stub client that records ops.

    We don't need a real RC connection for this test — the warning
    behaviour is purely in-process Python, gated on the SDK being
    importable.
    """
    pytest.importorskip("rustycluster", reason="py-rustycluster-client not installed")
    from core.kv.rustycluster_impl import _RustyClusterPipeline

    class _StubRC:
        def __init__(self):
            self.calls = []

        # Methods the pipeline replays — record but don't fail.
        def __getattr__(self, name):
            def _fn(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return None
            return _fn

    stub = _StubRC()
    pipe = _RustyClusterPipeline(stub, db=9)
    return pipe, stub


def test_pipeline_exits_cleanly_when_executed(patched_warn):
    """Pipeline with execute() must not emit any warning at exit."""
    pipe, _ = _make_rc_pipeline()
    with pipe:
        pipe.set("k", "v")
        pipe.execute()
    assert not any("un-executed" in m.lower() for m in patched_warn.messages)
    assert pipe._ops == []


def test_pipeline_exit_with_unexecuted_ops_logs_warning(patched_warn):
    """If execute() is never called, the warning fires and ops are cleared."""
    pipe, _ = _make_rc_pipeline()
    with pipe:
        pipe.set("k1", "v1")
        pipe.set("k2", "v2")
        pipe.set("k3", "v3")
        # intentionally NO execute()
    assert pipe._ops == []
    assert any("un-executed" in m.lower() for m in patched_warn.messages), (
        f"expected an 'un-executed' warning, got: {patched_warn.messages}"
    )


def test_pipeline_exit_skips_warning_when_exception_propagates(patched_warn):
    """If the body raises, we should NOT also spam a warning about
    un-executed ops — the exception is the primary signal."""
    pipe, _ = _make_rc_pipeline()
    with pytest.raises(RuntimeError):
        with pipe:
            pipe.set("k1", "v1")
            raise RuntimeError("body failed")
    assert not any("un-executed" in m.lower() for m in patched_warn.messages)
