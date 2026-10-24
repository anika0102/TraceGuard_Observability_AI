import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import Settings
from app.main import create_app

_EXPORTER = InMemorySpanExporter()


def make_settings(**kw) -> Settings:
    base = dict(environment="local", enable_fault_injection=True, incident_min_samples=3)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def spans():
    _EXPORTER.clear()
    return _EXPORTER


@pytest.fixture
def client(spans):
    app = create_app(make_settings(), extra_span_exporters=[spans] if not getattr(spans, "_attached", False) else [])
    spans._attached = True
    app.state.crash_fn = lambda: None  # never kill the test process
    return TestClient(app)


@pytest.fixture
def prod_client(spans):
    app = create_app(Settings(environment="production", enable_fault_injection=True))
    return TestClient(app)


def build_client(**settings_kw) -> TestClient:
    """A fresh app with custom settings and an empty embedded trace store."""
    from app import telemetry
    app = create_app(make_settings(**settings_kw))
    app.state.crash_fn = lambda: None
    telemetry.trace_store().clear()
    return TestClient(app)


@pytest.fixture
def dash():
    """Client for dashboard tests (local mode, fault injection allowed, clean span store)."""
    return build_client()


REQ = {"user_id": "user-1", "category": "electronics", "budget": 250}
