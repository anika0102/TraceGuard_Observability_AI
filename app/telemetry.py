"""OpenTelemetry setup (traces via OTLP/gRPC to the Collector) and trace-correlated JSON logging."""
from __future__ import annotations

import json
import logging
import time
from typing import Iterable, Optional

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from .config import Settings

_provider: Optional[TracerProvider] = None
_otlp_attached = False

# Probes and scrapes would drown real traffic in trace storage, so they are not traced.
EXCLUDED_URLS = "health,ready,metrics"


def init_tracing(settings: Settings, extra_exporters: Iterable[SpanExporter] = ()) -> TracerProvider:
    """Create the global TracerProvider once; later calls only attach additional exporters."""
    global _provider, _otlp_attached
    if _provider is None:
        resource = Resource.create({
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
        })
        _provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(_provider)
    if settings.otlp_endpoint and not _otlp_attached:
        # insecure=True: plaintext gRPC inside the local cluster/compose network only.
        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True)
        _provider.add_span_processor(BatchSpanProcessor(exporter))
        _otlp_attached = True
    for exp in extra_exporters:
        _provider.add_span_processor(SimpleSpanProcessor(exp))
    return _provider


def instrument_app(app: FastAPI, provider: TracerProvider) -> None:
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls=EXCLUDED_URLS)


def current_trace_id() -> Optional[str]:
    """Real trace ID of the active span, or None. Never fabricated."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


class JsonTraceFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else None,
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else None,
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonTraceFormatter())
    root = logging.getLogger("traceguard")
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False
