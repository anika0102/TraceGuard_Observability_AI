"""Runtime configuration, read from environment variables (ConfigMap in Kubernetes)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    service_name: str = "traceguard"
    service_version: str = "0.1.0"
    # "production" is the safe default; fault injection only works when this is "local".
    environment: str = "production"
    enable_fault_injection: bool = False
    # Empty string disables OTLP export (unit tests, plain local runs).
    otlp_endpoint: str = ""
    # Incident thresholds (deterministic rules).
    latency_p95_threshold_ms: float = 1000.0
    error_rate_threshold: float = 0.2
    # Incident when the share of incorrect outcomes in the window is strictly above this.
    correctness_failure_threshold: float = 0.0
    incident_window_size: int = 50
    incident_min_samples: int = 5
    slow_fault_delay_ms: int = 1500
    jaeger_ui_url: str = "http://localhost:16686"

    @property
    def fault_injection_allowed(self) -> bool:
        """Both conditions are required, so a stray flag alone can't enable faults in production."""
        return self.environment == "local" and self.enable_fault_injection

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("OTEL_SERVICE_NAME", cls.service_name),
            service_version=os.getenv("SERVICE_VERSION", cls.service_version),
            environment=os.getenv("TRACEGUARD_ENV", cls.environment).strip().lower(),
            enable_fault_injection=_bool("TRACEGUARD_ENABLE_FAULT_INJECTION", False),
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            latency_p95_threshold_ms=float(os.getenv("LATENCY_P95_THRESHOLD_MS", cls.latency_p95_threshold_ms)),
            error_rate_threshold=float(os.getenv("ERROR_RATE_THRESHOLD", cls.error_rate_threshold)),
            correctness_failure_threshold=float(
                os.getenv("CORRECTNESS_FAILURE_THRESHOLD", cls.correctness_failure_threshold)
            ),
            incident_window_size=int(os.getenv("INCIDENT_WINDOW_SIZE", cls.incident_window_size)),
            incident_min_samples=int(os.getenv("INCIDENT_MIN_SAMPLES", cls.incident_min_samples)),
            slow_fault_delay_ms=int(os.getenv("SLOW_FAULT_DELAY_MS", cls.slow_fault_delay_ms)),
            jaeger_ui_url=os.getenv("JAEGER_UI_URL", cls.jaeger_ui_url),
        )
