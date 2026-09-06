# SPDX-License-Identifier: MIT
# ============================================================
# OpenTelemetry export (Cowork enterprise observability)
#
# Optional, zero-dependency-at-rest tracing for Cowork. Emits OTLP spans/events
# for tool calls, connector access, document generation, and per-turn usage so
# an enterprise can pipe Cowork activity into Datadog / Honeycomb / Grafana
# Tempo / any OTLP collector for audit + cost dashboards.
#
# DESIGN:
#   - Fully NO-OP unless BOTH (a) the `opentelemetry` SDK is importable AND
#     (b) an endpoint is configured (OTEL_EXPORTER_OTLP_ENDPOINT) or
#     BUDDY_OTEL_ENABLED=true. This keeps prod imports clean and never adds
#     latency or a hard dependency when telemetry is off.
#   - Span attributes are LOW-CARDINALITY + NON-SENSITIVE only: tool name,
#     connector slug, user/department id, status, token counts, cost. NEVER the
#     tool arguments, results, screen pixels, or any payload — compliance redacts
#     content elsewhere; we only record the *event*, exactly like the audit rule.
#   - Model-agnostic: nothing here assumes Claude/OpenAI/etc.
# ============================================================

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

from core.logger import logger

_TRACER = None          # opentelemetry Tracer once initialised
_INIT_DONE = False       # init attempted (success or not)
_ENABLED = False


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _should_enable() -> bool:
    if _truthy(os.getenv("BUDDY_OTEL_ENABLED")):
        return True
    # The standard OTLP env var being set is taken as "telemetry on".
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def _init() -> None:
    """Lazily configure the tracer provider once. Safe to call repeatedly."""
    global _TRACER, _INIT_DONE, _ENABLED
    if _INIT_DONE:
        return
    _INIT_DONE = True
    if not _should_enable():
        logger.info("otel: Buddy telemetry disabled (no OTEL endpoint / BUDDY_OTEL_ENABLED)")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        service = os.getenv("OTEL_SERVICE_NAME", "ainxt-cowork")
        resource = Resource.create({"service.name": service, "service.namespace": "ainxt"})
        provider = TracerProvider(resource=resource)
        # OTLP endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT (standard env var).
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer("ainxt.cowork")
        _ENABLED = True
        logger.info(f"otel: Cowork telemetry ENABLED → service={service} "
                    f"endpoint={os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')}")
    except Exception as e:
        # SDK not installed or misconfigured → stay no-op, never break the app.
        logger.warning(f"otel: telemetry requested but unavailable → {e}; running no-op")
        _TRACER = None
        _ENABLED = False


def enabled() -> bool:
    _init()
    return _ENABLED


@contextmanager
def cowork_span(name: str, **attributes):
    """Context manager wrapping a Cowork operation in an OTLP span.

    No-op (zero overhead beyond a dict) when telemetry is disabled. Attributes
    must be low-cardinality + non-sensitive (names/ids/counts) — never payloads.
    On exception the span is marked error and the exception re-raised.
    """
    _init()
    if not _TRACER:
        yield None
        return
    span_cm = _TRACER.start_as_current_span(name)
    span = span_cm.__enter__()
    try:
        for k, v in (attributes or {}).items():
            if v is not None:
                try:
                    span.set_attribute(k, v)
                except Exception:
                    pass
        yield span
    except Exception as exc:  # noqa: BLE001 — record then propagate
        try:
            span.record_exception(exc)
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, str(exc)))
        except Exception:
            pass
        raise
    finally:
        try:
            span_cm.__exit__(None, None, None)
        except Exception:
            pass


def record_event(name: str, **attributes):
    """Emit a one-shot span for a discrete Cowork event (e.g. usage, publish).

    Convenience wrapper over cowork_span for fire-and-forget events. Never raises.
    """
    try:
        with cowork_span(name, **attributes):
            pass
    except Exception:
        pass
