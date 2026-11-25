"""pulse_otel — one-call OpenTelemetry setup for Python services, plus GenAI / MCP span helpers.

Vendor-neutral: it only configures the standard OpenTelemetry SDK with an OTLP
exporter, so the same service can report to RapidPulse, an OTel Collector, Phoenix,
Jaeger, or any commercial APM.

    from pulse_otel import init_telemetry, llm_span, mcp_tool_span
    init_telemetry(service_name="orders-api", app="my-application", fastapi_app=app)

    with llm_span("gpt-4o", provider="openai") as span:
        resp = client.chat.completions.create(...)
        span.record_usage(resp.usage.prompt_tokens, resp.usage.completion_tokens)

    with mcp_tool_span("search_catalog", server="tools-mcp") as meta:
        result = await session.call_tool("search_catalog", args, meta=meta)   # meta carries traceparent

pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
       [opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx
        opentelemetry-instrumentation-requests opentelemetry-instrumentation-sqlalchemy
        opentelemetry-instrumentation-botocore opentelemetry-instrumentation-redis
        opentelemetry-instrumentation-psycopg opentelemetry-instrumentation-logging]
Every instrumentation is optional: whatever is installed is switched on.
"""
from __future__ import annotations

import logging
import os
import socket
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("pulse_otel")

_TRACER = None


def init_telemetry(service_name: str, *, app: str | None = None, environment: str | None = None,
                   version: str | None = None, endpoint: str | None = None, api_key: str | None = None,
                   fastapi_app: Any = None, logs: bool = True, metrics: bool = False,
                   excluded_urls: str = "health,healthz,readyz,metrics") -> None:
    """Configure tracing (and optionally logs/metrics) and auto-instrument installed libraries.

    endpoint defaults to $OTEL_EXPORTER_OTLP_ENDPOINT (base URL, e.g. http://pulse:4400).
    """
    global _TRACER
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = (endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "http://127.0.0.1:4400").rstrip("/")
    headers = {"x-pulse-key": api_key or os.environ.get("PULSE_KEY", "")} if (api_key or os.environ.get("PULSE_KEY")) else None
    attrs = {
        "service.name": service_name,
        "service.namespace": app or os.environ.get("PULSE_APP", "default"),
        "service.version": version or os.environ.get("SERVICE_VERSION", ""),
        "deployment.environment.name": environment or os.environ.get("DEPLOY_ENV", ""),
        "host.name": os.environ.get("HOST_NAME") or socket.gethostname(),
    }
    if os.environ.get("CONTAINER_NAME"):
        attrs["container.name"] = os.environ["CONTAINER_NAME"]
    resource = Resource.create({k: v for k, v in attrs.items() if v})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)))
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("pulse_otel")

    if logs:
        try:
            from opentelemetry._logs import set_logger_provider
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            lp = LoggerProvider(resource=resource)
            lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", headers=headers)))
            set_logger_provider(lp)
            logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=lp))  # carries trace_id
        except ImportError:
            log.info("OTLP log exporter not installed; logs not shipped")

    if metrics:
        try:
            from opentelemetry import metrics as m
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            m.set_meter_provider(MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers))]))
        except ImportError:
            pass

    _auto_instrument(fastapi_app, excluded_urls)
    log.info("telemetry -> %s as %s/%s", endpoint, attrs["service.namespace"], service_name)


def _auto_instrument(fastapi_app, excluded_urls: str) -> None:
    def try_(module, cls, **kw):
        try:
            mod = __import__(module, fromlist=[cls])
            getattr(mod, cls)().instrument(**kw)
            log.info("instrumented %s", module)
        except ImportError:
            pass
        except Exception as e:  # an instrumentor failing must never stop the service
            log.warning("could not instrument %s: %s", module, e)

    if fastapi_app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(fastapi_app, excluded_urls=excluded_urls)
        except ImportError:
            log.warning("opentelemetry-instrumentation-fastapi not installed: no server spans")
    try_("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")
    try_("opentelemetry.instrumentation.requests", "RequestsInstrumentor")
    try_("opentelemetry.instrumentation.botocore", "BotocoreInstrumentor")
    try_("opentelemetry.instrumentation.redis", "RedisInstrumentor")
    try_("opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor")
    try_("opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor", enable_commenter=False)
    try_("opentelemetry.instrumentation.logging", "LoggingInstrumentor", set_logging_format=False)


def _tracer():
    global _TRACER
    if _TRACER is None:
        from opentelemetry import trace
        _TRACER = trace.get_tracer("pulse_otel")
    return _TRACER


class _LLMSpan:
    def __init__(self, span):
        self.span = span

    def record_usage(self, input_tokens: int, output_tokens: int, *, cost_usd: float | None = None,
                     response_model: str | None = None, finish_reason: str | None = None) -> None:
        self.span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens or 0))
        self.span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens or 0))
        if cost_usd is not None:
            self.span.set_attribute("gen_ai.usage.cost", float(cost_usd))
        if response_model:
            self.span.set_attribute("gen_ai.response.model", response_model)
        if finish_reason:
            self.span.set_attribute("gen_ai.response.finish_reasons", finish_reason)

    def set_attribute(self, k, v):
        self.span.set_attribute(k, v)


@contextmanager
def llm_span(model: str, *, provider: str, operation: str = "chat") -> Iterator[_LLMSpan]:
    """A GenAI client span (OTel GenAI semantic conventions). Never records prompts or completions."""
    from opentelemetry.trace import SpanKind, Status, StatusCode
    with _tracer().start_as_current_span(f"{operation} {model}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.system", provider)
        span.set_attribute("gen_ai.operation.name", operation)
        span.set_attribute("gen_ai.request.model", model)
        try:
            yield _LLMSpan(span)
        except BaseException as e:
            span.set_attribute("error.type", type(e).__name__)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:300]))
            raise


@contextmanager
def mcp_tool_span(tool: str, *, server: str = "mcp") -> Iterator[dict]:
    """Client span for an MCP tools/call. Yields the `_meta` dict carrying W3C trace context,
    to pass as the request's `meta` so the MCP server can continue the same trace
    (OTel MCP semantic conventions: params._meta.traceparent)."""
    from opentelemetry.propagate import inject
    from opentelemetry.trace import SpanKind, Status, StatusCode
    with _tracer().start_as_current_span(f"tools/call {tool}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("mcp.method.name", "tools/call")
        span.set_attribute("gen_ai.tool.name", tool)
        span.set_attribute("server.address", server)
        meta: dict = {}
        inject(meta)
        try:
            yield meta
        except BaseException as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:300]))
            raise


@contextmanager
def mcp_server_span(tool: str, meta: dict | None):
    """Server side of a tool call: continue the caller's trace from `_meta`."""
    from opentelemetry.propagate import extract
    from opentelemetry.trace import SpanKind, Status, StatusCode
    ctx = extract(meta or {})
    with _tracer().start_as_current_span(f"tools/call {tool}", context=ctx, kind=SpanKind.SERVER) as span:
        span.set_attribute("mcp.method.name", "tools/call")
        span.set_attribute("gen_ai.tool.name", tool)
        try:
            yield span
        except BaseException as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:300]))
            raise
