# SPDX-License-Identifier: MIT
# ============================================================
# ENTERPRISE OBSERVABILITY — OpenTelemetry + Prometheus
#
# Dual-mode operation:
#
#   WITH OTLP_ENDPOINT set:
#     - Real OTel spans exported to Grafana Tempo / Jaeger / OTEL collector
#     - W3C traceparent propagation across all HTTP boundaries
#     - Auto-instrumentation: FastAPI, httpx, psycopg2
#
#   WITHOUT OTLP_ENDPOINT (local dev / fallback):
#     - In-memory span store (last 1,000 spans per process)
#     - GET /traces/{request_id} still works for per-request timeline
#
#   ALWAYS:
#     - Prometheus counters + histograms (GET /metrics)
#     - Seeded from DB on startup so counters survive restarts
#
# Env vars:
#   OTLP_ENDPOINT   — e.g. http://tempo:4317  (gRPC) or http://tempo:4318 (HTTP)
#   OTLP_PROTOCOL   — "grpc" (default) or "http"
#   ENABLE_TRACING  — "1" to enable (default: 1)
#   SERVICE_NAME    — default: ainxt-gateway
# ============================================================

import os
import time
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from core.logger import logger
from prometheus_client import Counter, Histogram, Gauge, generate_latest

SERVICE_NAME    = os.getenv("SERVICE_NAME", "ainxt-gateway")
OTLP_ENDPOINT   = os.getenv("OTLP_ENDPOINT", "")
OTLP_PROTOCOL   = os.getenv("OTLP_PROTOCOL", "grpc")   # "grpc" | "http"
ENABLE_TRACING  = os.getenv("ENABLE_TRACING", "1") == "1"


# ============================================================
# PROMETHEUS METRICS
# ============================================================
# All metric names are prefixed with METRIC_PREFIX (default: "ainxt").
# A migrating deployment may set METRIC_PREFIX to its legacy prefix to keep existing dashboards
# working. OSS users get ainxt_* metrics by default.
# Metric names are built at import time — METRIC_PREFIX must be set in .env
# before the gateway starts.

_MP = os.getenv("METRIC_PREFIX", "ainxt").lower().strip()

_prom_requests_total        = Counter(f'{_MP}_requests_total',        'Total HTTP requests')
_prom_agent_executions      = Counter(f'{_MP}_agent_executions_total', 'Agent executions')
_prom_workflow_executions   = Counter(f'{_MP}_workflow_executions_total', 'Workflow executions')
_prom_compliance_blocks     = Counter(f'{_MP}_compliance_blocks_total', 'Compliance violations blocked')
_prom_errors_total          = Counter(f'{_MP}_errors_total',           'Total errors')
_prom_cache_hits            = Counter(f'{_MP}_cache_hits_total',       'Cache hits')
_prom_agent_success         = Counter(f'{_MP}_agent_success_total',    'Agent successful executions')
_prom_agent_failure         = Counter(f'{_MP}_agent_failure_total',    'Agent failed executions')
_prom_latency               = Histogram(f'{_MP}_request_latency_seconds', 'Request latency in seconds',
                                        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0])
_prom_model_calls           = Counter(f'{_MP}_model_calls_total',      'API calls per model',   ['model'])
_prom_model_tokens          = Counter(f'{_MP}_model_tokens_total',     'Tokens consumed per model', ['model'])
_prom_model_cost_usd        = Counter(f'{_MP}_model_cost_usd_total',   'Cost in USD per model', ['model'])
_prom_tool_failures         = Counter(f'{_MP}_tool_failures_total',    'Agent tool failures',   ['tool'])
_prom_cache_type_hits       = Counter(f'{_MP}_cache_type_hits_total',  'Cache hits by type',    ['cache_type'])

# ── Operational metrics (Engine gap P1) ──────────────────────────────────────
_prom_tool_latency          = Histogram(f'{_MP}_tool_latency_seconds',
                                        'Tool execution latency per tool name', ['tool'],
                                        buckets=[0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 15.0, 30.0])
_prom_rag_latency           = Histogram(f'{_MP}_rag_retrieval_latency_seconds',
                                        'RAG hybrid retrieval latency',
                                        buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0])
_prom_react_iterations      = Histogram(f'{_MP}_react_loop_iterations',
                                        'ReAct loop tool-round count per run',
                                        buckets=[1, 2, 3, 4, 5, 6, 8, 10, 15])
_prom_confidence_score      = Histogram(f'{_MP}_confidence_score',
                                        'Hybrid confidence score distribution (0–1)',
                                        buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
_prom_verifier_loops        = Histogram(f'{_MP}_verifier_loop_count',
                                        'Goal-verifier recovery loop count per run',
                                        buckets=[1, 2, 3, 4, 5])


# ============================================================
# IN-MEMORY SPAN STORE (fallback when OTLP not configured)
# ============================================================

class _SpanStore:
    """Lightweight in-memory span store. Bounded to last 1,000 traces."""
    def __init__(self):
        self._lock  = threading.Lock()
        self._spans: list = []

    def add(self, span: dict):
        with self._lock:
            self._spans.append(span)
            if len(self._spans) > 1_000:
                self._spans = self._spans[-1_000:]

    def get_by_request(self, request_id: str) -> list:
        with self._lock:
            return [s for s in self._spans if s.get("request_id") == request_id]

    def list_recent(self, limit: int = 50) -> list:
        with self._lock:
            return list(reversed(self._spans[-limit:]))


span_store = _SpanStore()


# ============================================================
# TRACER
# ============================================================

class Tracer:
    """
    Unified tracer that works in two modes:

    1. OTLP mode (OTLP_ENDPOINT set):
       - Creates real OpenTelemetry spans exported to Tempo/Jaeger
       - start_span() returns a context manager wrapping a real OTel span
       - end_span() closes the OTel span

    2. Fallback mode (no OTLP):
       - Creates plain dict spans stored in _SpanStore
       - Same API surface — callers don't need to change
    """

    def __init__(self):
        self._otlp_enabled   = False
        self._otel_tracer    = None
        self._propagator     = None

        if ENABLE_TRACING and OTLP_ENDPOINT:
            self._try_init_otlp()

        mode = f"OTLP → {OTLP_ENDPOINT}" if self._otlp_enabled else "in-memory fallback"
        logger.info(f"Tracer initialized: {mode}")

    # ── OTLP initialisation ───────────────────────────────────────────────────

    def _try_init_otlp(self):
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import Resource, SERVICE_NAME as RES_SVC_NAME
            from opentelemetry.propagate import set_global_textmap
            from opentelemetry.propagators.composite import CompositePropagator
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
            from opentelemetry.baggage.propagation import W3CBaggagePropagator

            resource = Resource(attributes={RES_SVC_NAME: SERVICE_NAME})
            provider = TracerProvider(resource=resource)

            if OTLP_PROTOCOL == "http":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter(
                    endpoint=OTLP_ENDPOINT.rstrip("/") + "/v1/traces",
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter(
                    endpoint=OTLP_ENDPOINT,
                    insecure=not OTLP_ENDPOINT.startswith("https"),
                )

            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)

            # W3C traceparent + baggage propagation across all HTTP calls
            set_global_textmap(CompositePropagator([
                TraceContextTextMapPropagator(),
                W3CBaggagePropagator(),
            ]))

            self._otel_tracer = trace.get_tracer(SERVICE_NAME)
            self._propagator  = TraceContextTextMapPropagator()
            self._otlp_enabled = True

            logger.info(f"OpenTelemetry OTLP exporter → {OTLP_ENDPOINT} [{OTLP_PROTOCOL}]")

        except Exception as e:
            logger.warning(f"OTLP init failed (falling back to in-memory spans): {e}")
            self._otlp_enabled = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def start_span(
        self,
        name: str,
        request_id: str = "",
        attributes: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        Start a span.

        Returns a dict that is passed to end_span().
        In OTLP mode the dict also holds a reference to the real OTel span
        so end_span() can close it correctly.
        """
        attrs = attributes or {}
        if request_id:
            attrs["request_id"] = request_id

        if self._otlp_enabled and self._otel_tracer:
            try:
                from opentelemetry import trace
                otel_span = self._otel_tracer.start_span(name, attributes=attrs)
                ctx = trace.use_span(otel_span, end_on_exit=False)
                ctx.__enter__()
                span = {
                    "name":       name,
                    "request_id": request_id,
                    "start_ms":   time.time() * 1000,
                    "attributes": attrs,
                    "status":     "ok",
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                    "_otel_span": otel_span,
                    "_otel_ctx":  ctx,
                }
                return span
            except Exception as e:
                logger.debug(f"OTel start_span failed, using fallback: {e}")

        # Fallback: plain dict span
        return {
            "name":       name,
            "request_id": request_id,
            "start_ms":   time.time() * 1000,
            "attributes": attrs,
            "status":     "ok",
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }

    def end_span(self, span: dict, error: Optional[str] = None):
        """Close a span, recording duration and optional error."""
        span["duration_ms"] = (time.time() * 1000) - span.get("start_ms", 0)
        if error:
            span["status"] = "error"
            span["error"]  = error

        # Close real OTel span if present
        otel_span = span.pop("_otel_span", None)
        otel_ctx  = span.pop("_otel_ctx",  None)
        if otel_span is not None:
            try:
                from opentelemetry.trace import StatusCode
                if error:
                    otel_span.set_status(StatusCode.ERROR, error)
                    otel_span.record_exception(Exception(error))
                else:
                    otel_span.set_status(StatusCode.OK)
                otel_span.set_attribute("duration_ms", span["duration_ms"])
                otel_span.end()
                if otel_ctx:
                    otel_ctx.__exit__(None, None, None)
            except Exception as e:
                logger.debug(f"OTel end_span failed: {e}")

        # Always store in local span_store (for /traces endpoint)
        _clean = {k: v for k, v in span.items() if not k.startswith("_")}
        span_store.add(_clean)
        _prom_latency.observe(span["duration_ms"] / 1000.0)

    # ── Convenience helpers ───────────────────────────────────────────────────

    def trace_request(self, request_id: str, endpoint: str) -> dict:
        return self.start_span("http.request", request_id, {"http.target": endpoint})

    def trace_agent(self, request_id: str, agent_name: str) -> dict:
        return self.start_span("agent.execute", request_id, {"agent.name": agent_name})

    def trace_workflow(self, request_id: str, workflow_name: str) -> dict:
        return self.start_span("workflow.execute", request_id, {"workflow.name": workflow_name})

    def trace_model_call(self, request_id: str, model: str, tier: str) -> dict:
        return self.start_span("model.generate", request_id, {"model": model, "tier": tier})

    def trace_retrieval(self, request_id: str, repo: str, source: str) -> dict:
        return self.start_span("rag.retrieval", request_id, {"repo": repo, "source": source})

    def trace_compliance(self, request_id: str, direction: str) -> dict:
        return self.start_span("compliance.check", request_id, {"direction": direction})

    # ── Operational metric helpers (Engine gap P1) ────────────────────────────

    def record_tool_latency(self, tool_name: str, elapsed_sec: float) -> None:
        """Record wall-clock latency for a single tool execution."""
        try:
            _prom_tool_latency.labels(tool=tool_name).observe(elapsed_sec)
        except Exception:
            pass

    def record_rag_latency(self, elapsed_sec: float) -> None:
        """Record end-to-end hybrid retrieval latency."""
        try:
            _prom_rag_latency.observe(elapsed_sec)
        except Exception:
            pass

    def record_react_iteration(self, iteration_count: int) -> None:
        """Record the number of tool-use rounds completed in a ReAct run."""
        try:
            _prom_react_iterations.observe(float(iteration_count))
        except Exception:
            pass

    def record_confidence(self, score: float) -> None:
        """Record the final hybrid confidence score (0.0–1.0) for a run."""
        try:
            _prom_confidence_score.observe(max(0.0, min(1.0, score)))
        except Exception:
            pass

    def record_verifier_loops(self, loop_count: int) -> None:
        """Record the number of goal-verifier recovery iterations in a run."""
        try:
            _prom_verifier_loops.observe(float(loop_count))
        except Exception:
            pass

    # ── Context extraction (for propagating to outbound HTTP calls) ───────────

    def inject_headers(self) -> dict:
        """
        Return a dict of HTTP headers carrying the current trace context.
        Inject into outbound HTTP calls to embed_svc, model gateways, etc.

        Usage:
            headers = tracer.inject_headers()
            httpx.post(url, headers={**existing_headers, **headers})
        """
        if not self._otlp_enabled:
            return {}
        try:
            from opentelemetry.propagate import inject
            carrier: dict = {}
            inject(carrier)
            return carrier
        except Exception:
            return {}

    def extract_context(self, headers: dict):
        """
        Extract trace context from inbound HTTP headers and make it the current context.
        Call at the top of any worker/job that receives a traceparent header.

        Usage:
            tracer.extract_context(request.headers)
        """
        if not self._otlp_enabled:
            return
        try:
            from opentelemetry.propagate import extract
            from opentelemetry import context
            ctx = extract(headers)
            context.attach(ctx)
        except Exception:
            pass


tracer = Tracer()


# ============================================================
# AUTO-INSTRUMENTATION  (called once at app startup)
# ============================================================

def instrument_app(app) -> None:
    """
    Wire OpenTelemetry auto-instrumentation into the FastAPI app.

    Instruments:
      - FastAPI/Starlette — one span per HTTP request (method, route, status)
      - httpx           — one span per outbound HTTP call (embed_svc, model gateways)
      - psycopg2        — one span per DB query (Postgres)

    Call this ONCE after `app = FastAPI(...)` in gateway.py.
    Safe to call even when OTLP is disabled — instruments only when SDK is active.
    """
    if not ENABLE_TRACING or not OTLP_ENDPOINT:
        return

    # FastAPI / ASGI
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(
            app,
            server_request_hook=_fastapi_request_hook,
            client_request_hook=None,
            client_response_hook=None,
            excluded_urls="health,metrics,favicon",
        )
        logger.info("OTel: FastAPI auto-instrumentation active")
    except Exception as e:
        logger.warning(f"OTel FastAPI instrumentation failed: {e}")

    # httpx (all outbound HTTP — embed_svc, Claude, GPT, Gemini)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        logger.info("OTel: httpx auto-instrumentation active")
    except Exception as e:
        logger.warning(f"OTel httpx instrumentation failed: {e}")

    # psycopg2 (all Postgres queries)
    try:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
        Psycopg2Instrumentor().instrument(enable_commenter=True)
        logger.info("OTel: psycopg2 auto-instrumentation active")
    except Exception as e:
        logger.warning(f"OTel psycopg2 instrumentation failed: {e}")


def _fastapi_request_hook(span, scope):
    """Enrich FastAPI spans with AiNxt-specific attributes."""
    if span and span.is_recording():
        try:
            from core.logger import get_request_id, get_user_id, get_chat_id
            span.set_attribute("ainxt.request_id", get_request_id())
            span.set_attribute("ainxt.user_id",    get_user_id())
            span.set_attribute("ainxt.chat_id",    get_chat_id())
        except Exception:
            pass


# ============================================================
# _TelemetryMetrics — unified interface used by gateway.py
# ============================================================

class _TelemetryMetrics:
    """
    Dual-mode telemetry object.

    - inc(name)           — increments in-memory attr AND prometheus_client Counter
    - record_latency(ms)  — records a latency sample in histogram
    - record_model_usage  — per-model call/token/cost tracking
    - to_prometheus()     — Prometheus text format (for /metrics)
    - to_json()           — JSON summary (for /admin/metrics)
    """

    _COUNTER_MAP = {
        "requests_total":      "_prom_requests_total",
        "agent_executions":    "_prom_agent_executions",
        "workflow_executions": "_prom_workflow_executions",
        "compliance_blocks":   "_prom_compliance_blocks",
        "errors_total":        "_prom_errors_total",
        "cache_hits":          "_prom_cache_hits",
        "agent_success":       "_prom_agent_success",
        "agent_failure":       "_prom_agent_failure",
    }

    def __init__(self):
        self._lock = threading.Lock()
        self.requests_total      = 0
        self.agent_executions    = 0
        self.workflow_executions = 0
        self.compliance_blocks   = 0
        self.errors_total        = 0
        self.cache_hits          = 0
        self.agent_success       = 0
        self.agent_failure       = 0
        self._latencies: list    = []
        self.model_calls:    Dict[str, int]   = {}
        self.model_tokens:   Dict[str, int]   = {}
        self.model_cost_usd: Dict[str, float] = {}

    def inc(self, name: str, value: int = 1):
        with self._lock:
            setattr(self, name, getattr(self, name, 0) + value)
        prom_var = self._COUNTER_MAP.get(name)
        if prom_var:
            ctr = globals().get(prom_var)
            if ctr is not None:
                ctr.inc(value)

    def record_latency(self, ms: float):
        with self._lock:
            self._latencies.append(ms)
            if len(self._latencies) > 200:
                self._latencies = self._latencies[-200:]
        _prom_latency.observe(ms / 1000.0)

    def record_model_usage(self, model: str, tokens: int, cost_usd: float):
        with self._lock:
            self.model_calls[model]    = self.model_calls.get(model, 0) + 1
            self.model_tokens[model]   = self.model_tokens.get(model, 0) + tokens
            self.model_cost_usd[model] = self.model_cost_usd.get(model, 0.0) + cost_usd
        _prom_model_calls.labels(model=model).inc()
        _prom_model_tokens.labels(model=model).inc(tokens)
        _prom_model_cost_usd.labels(model=model).inc(cost_usd)

    def record_tool_failure(self, tool: str):
        _prom_tool_failures.labels(tool=tool).inc()

    def record_cache_hit(self, cache_type: str):
        self.inc("cache_hits")
        _prom_cache_type_hits.labels(cache_type=cache_type).inc()

    def to_prometheus(self) -> str:
        return generate_latest().decode("utf-8")

    def to_json(self) -> dict:
        with self._lock:
            lats = self._latencies
            avg_lat = sum(lats) / len(lats) if lats else 0.0
            p95_lat = sorted(lats)[int(len(lats) * 0.95)] if len(lats) >= 20 else 0.0
            return {
                "requests_total":      self.requests_total,
                "agent_executions":    self.agent_executions,
                "workflow_executions": self.workflow_executions,
                "errors_total":        self.errors_total,
                "agent_success":       self.agent_success,
                "agent_failure":       self.agent_failure,
                "cache_hits":          self.cache_hits,
                "compliance_blocks":   self.compliance_blocks,
                "avg_latency_ms":      round(avg_lat, 2),
                "p95_latency_ms":      round(p95_lat, 2),
                "model_calls":         dict(self.model_calls),
                "model_tokens":        dict(self.model_tokens),
                "model_cost_usd":      {k: round(v, 6) for k, v in self.model_cost_usd.items()},
                "otlp_enabled":        tracer._otlp_enabled,
                "otlp_endpoint":       OTLP_ENDPOINT or "—",
            }


telemetry_metrics = _TelemetryMetrics()


# ============================================================
# MODULE-LEVEL HELPERS (backward compat with existing callers)
# ============================================================

def inc_requests():            telemetry_metrics.inc("requests_total")
def inc_agent_executions():    telemetry_metrics.inc("agent_executions")
def inc_workflow_executions(): telemetry_metrics.inc("workflow_executions")
def inc_compliance_blocks():   telemetry_metrics.inc("compliance_blocks")
def inc_errors():              telemetry_metrics.inc("errors_total")
def inc_cache_hits():          telemetry_metrics.inc("cache_hits")
def inc_agent_success():       telemetry_metrics.inc("agent_success")
def inc_agent_failure():       telemetry_metrics.inc("agent_failure")

def record_model_usage(model: str, tokens: int, cost_usd: float):
    telemetry_metrics.record_model_usage(model, tokens, cost_usd)

def get_prometheus_metrics() -> str:
    return telemetry_metrics.to_prometheus()


# ============================================================
# DB SEED  (call once at startup)
# ============================================================

def seed_from_db():
    """Seed Prometheus counters from DB so they survive restarts."""
    try:
        from db.database import SessionLocal
        from db.models import ModelUsage, SDLCRun
        from sqlalchemy import func
        db = SessionLocal()
        try:
            req      = db.query(func.count(ModelUsage.id)).scalar() or 0
            agent    = db.query(func.count(ModelUsage.id)).filter(ModelUsage.agent_id.isnot(None)).scalar() or 0
            wf       = db.query(func.count(SDLCRun.id)).scalar() or 0
            agent_ok = db.query(func.count(ModelUsage.id)).filter(
                ModelUsage.agent_id.isnot(None), ModelUsage.latency_ms > 0
            ).scalar() or 0
            errors   = db.query(func.count(SDLCRun.id)).filter(SDLCRun.state == "FAILED").scalar() or 0

            if req:      _prom_requests_total.inc(req)
            if agent:    _prom_agent_executions.inc(agent)
            if wf:       _prom_workflow_executions.inc(wf)
            if agent_ok: _prom_agent_success.inc(agent_ok)
            if errors:   _prom_errors_total.inc(errors)

            telemetry_metrics.requests_total      = req
            telemetry_metrics.agent_executions    = agent
            telemetry_metrics.workflow_executions = wf
            telemetry_metrics.agent_success       = agent_ok
            telemetry_metrics.errors_total        = errors

            logger.info(f"Telemetry seeded from DB: requests={req} agents={agent} errors={errors}")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Telemetry DB seed failed (non-fatal): {e}")


logger.info("Telemetry module loaded")
