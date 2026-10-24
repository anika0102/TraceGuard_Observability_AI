"""Prometheus metrics. Every label is a small, bounded enum: no user IDs, trace IDs or raw paths."""
from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "traceguard_http_requests_total", "HTTP requests by route template, method and status code",
    ["route", "method", "status"],
)
HTTP_DURATION = Histogram(
    "traceguard_http_request_duration_seconds", "HTTP request latency by route template",
    ["route", "method"], buckets=LATENCY_BUCKETS,
)
RECOMMEND_REQUESTS = Counter(
    "traceguard_recommend_requests_total", "Recommendation requests by outcome and active fault scenario",
    ["status", "scenario"],
)
CORRECTNESS = Counter(
    "traceguard_correctness_checks_total", "Business-outcome validation results (correct|incorrect)",
    ["result"],
)
READY = Gauge("traceguard_ready", "1 if the app currently reports ready, else 0")
FAULT_ACTIVE = Gauge("traceguard_fault_active", "1 if the fault scenario is active", ["scenario"])
INCIDENTS = Counter(
    "traceguard_incidents_total", "Incidents opened by the deterministic detector",
    ["type", "severity"],
)
ACTIVE_INCIDENTS = Gauge("traceguard_active_incidents", "Currently open incidents by type", ["type"])
# Latency of the recommendation workflow itself (covers /recommend and sandboxed demo runs, which the HTTP
# histogram above cannot see because they are not HTTP /recommend requests).
RECOMMEND_DURATION = Histogram(
    "traceguard_recommend_duration_seconds", "Recommendation workflow latency", buckets=LATENCY_BUCKETS,
)


def init_label_sets() -> None:
    """Create every bounded counter series at 0 so Prometheus sees 0 -> N on the first event.

    A counter series that first appears with a non-zero value makes rate()/increase() report 0 for
    that first burst. All label values below are fixed enums, so this adds ~40 series in total.
    """
    from .incident_detection import INCIDENT_TYPES
    from .schemas import Scenario

    for status in ("ok", "client_error", "error"):
        for scenario in Scenario:
            RECOMMEND_REQUESTS.labels(status, scenario.value)
    for result in ("correct", "incorrect"):
        CORRECTNESS.labels(result)
    for itype in INCIDENT_TYPES:
        for severity in ("warning", "critical"):
            INCIDENTS.labels(itype, severity)
    for code in ("200", "400", "422", "500"):
        HTTP_REQUESTS.labels("/recommend", "POST", code)


init_label_sets()
