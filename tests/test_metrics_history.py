"""Metric history derives rates/quantiles from counter deltas; no traffic means None, never 0 (for ratios)."""
import math

from app.metrics_history import INF, MetricsHistory, quantile_ms, read_registry
from .conftest import REQ, build_client


def snap(requests=0, errors=0, correct=0, incorrect=0, buckets=None, ready=1.0, active=0.0):
    return {"requests": requests, "errors": errors, "correct": correct, "incorrect": incorrect,
            "buckets": buckets or {0.1: 0.0, 1.0: 0.0, INF: 0.0}, "duration_count": 0.0,
            "incidents_opened": 0.0, "ready": ready, "active_incidents": active}


def test_quantile_interpolates_like_histogram_quantile():
    start = {0.1: 0, 1.0: 0, INF: 0}
    end = {0.1: 50, 1.0: 100, INF: 100}
    assert quantile_ms(0.5, start, end) == 100.0
    assert math.isclose(quantile_ms(0.95, start, end), 910.0, abs_tol=0.5)


def test_quantile_is_none_without_observations_or_after_a_reset():
    zero = {0.1: 0, 1.0: 0, INF: 0}
    assert quantile_ms(0.95, zero, zero) is None
    assert quantile_ms(0.95, {0.1: 9, 1.0: 9, INF: 9}, zero) is None  # counter went backwards


def test_quantile_beyond_the_last_finite_bucket_reports_that_bucket():
    end = {0.1: 0, 1.0: 0, INF: 10}
    assert quantile_ms(0.95, {0.1: 0, 1.0: 0, INF: 0}, end) == 1000.0


class Feed:
    """Fake clock + a reader whose counters we advance by hand."""
    def __init__(self):
        self.t = 1000.0
        self.state = snap()

    def history(self, **kw):
        return MetricsHistory(clock=lambda: self.t, reader=lambda: self.state, **kw)


def test_rates_and_percentages_come_from_deltas():
    f = Feed()
    hist = f.history()
    hist.sample()
    f.t += 10
    f.state = snap(requests=20, errors=2, correct=15, incorrect=5, buckets={0.1: 10, 1.0: 20, INF: 20})
    hist.sample()
    w = hist.window_stats(60)
    assert w["available"] and w["window_s"] == 10.0
    assert w["rps"] == 2.0 and w["requests"] == 20
    assert w["error_rate"] == 0.1 and w["incorrect_rate"] == 0.25
    assert w["p50_ms"] == 100.0 and w["p95_ms"] is not None


def test_no_traffic_gives_none_for_ratios_and_percentiles():
    f = Feed()
    hist = f.history()
    hist.sample(); f.t += 5; hist.sample()
    w = hist.window_stats(60)
    assert w["available"] is True and w["rps"] == 0.0 and w["requests"] == 0  # truthfully zero requests
    assert w["error_rate"] is None and w["incorrect_rate"] is None and w["p50_ms"] is None and w["p95_ms"] is None


def test_fewer_than_two_samples_means_not_available_not_zero():
    hist = Feed().history()
    assert hist.window_stats(60) == {"available": False, "window_s": None, "rps": None, "requests": None,
                                     "error_rate": None, "incorrect_rate": None, "p50_ms": None, "p95_ms": None}
    hist.sample()
    assert hist.window_stats(60)["available"] is False
    assert hist.series()["points"][0]["rps"] is None


def test_storage_is_bounded():
    f = Feed()
    hist = f.history(max_points=5)
    for _ in range(40):
        f.t += 5; f.state = snap(requests=f.state["requests"] + 1); hist.sample()
    assert len(hist) == 5 and hist.stats() == {"points": 5, "max_points": 5, "interval_s": 5.0}


def test_series_uses_a_trailing_window_and_marks_gaps():
    f = Feed()
    hist = f.history()
    hist.sample()
    for i in range(6):
        f.t += 5
        if i < 3:
            f.state = snap(requests=f.state["requests"] + 10, buckets={0.1: (i + 1) * 10, 1.0: (i + 1) * 10, INF: (i + 1) * 10})
        hist.sample()
    pts = hist.series(window_s=600, rate_window_s=10)["points"]
    assert pts[0]["rps"] is None                      # first sample has nothing before it
    assert pts[1]["rps"] == 2.0 and pts[1]["p95_ms"] is not None
    assert pts[-1]["rps"] == 0.0 and pts[-1]["p95_ms"] is None   # idle tail: zero requests, no latency to report


def test_counter_resets_produce_none_instead_of_negative_rates():
    f = Feed()
    hist = f.history()
    f.state = snap(requests=100); hist.sample()
    f.t += 5; f.state = snap(requests=3); hist.sample()
    assert hist.window_stats(60)["rps"] is None


def test_registry_reader_reflects_real_requests():
    c = build_client()
    before = read_registry()
    for _ in range(4):
        assert c.post("/recommend", json=REQ).status_code == 200
    after = read_registry()
    assert after["requests"] - before["requests"] == 4
    assert after["correct"] - before["correct"] == 4
    assert after["buckets"][INF] - before["buckets"][INF] == 4
