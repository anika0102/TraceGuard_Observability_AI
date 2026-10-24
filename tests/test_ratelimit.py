from types import SimpleNamespace

import pytest

from app.ratelimit import RateLimiter, client_key
from .conftest import build_client


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_bucket_allows_a_burst_then_blocks_with_a_retry_hint():
    clock = Clock()
    rl = RateLimiter(per_minute=3, clock=clock)
    assert [rl.allow("a")[0] for _ in range(3)] == [True, True, True]
    allowed, retry = rl.allow("a")
    assert allowed is False and 0 < retry <= 20.5


def test_bucket_refills_over_time():
    clock = Clock()
    rl = RateLimiter(per_minute=6, clock=clock)
    for _ in range(6):
        rl.allow("a")
    assert rl.allow("a")[0] is False
    clock.t += 10  # 6 per minute = one token per 10 s
    assert rl.allow("a")[0] is True and rl.allow("a")[0] is False


def test_clients_are_isolated():
    rl = RateLimiter(per_minute=1, clock=Clock())
    assert rl.allow("a")[0] and rl.allow("b")[0]
    assert not rl.allow("a")[0] and not rl.allow("b")[0]


def test_memory_is_bounded_by_forgetting_the_least_recent_clients():
    rl = RateLimiter(per_minute=5, max_keys=100, clock=Clock())
    for i in range(1000):
        rl.allow(f"client-{i}")
    assert len(rl._buckets) == 100


def request(headers=None, host="203.0.113.7"):
    return SimpleNamespace(headers={k.lower(): v for k, v in (headers or {}).items()}, client=SimpleNamespace(host=host))


def test_proxy_headers_are_ignored_unless_explicitly_trusted():
    spoof = request({"CF-Connecting-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.2"})
    assert client_key(spoof, trust_proxy_headers=False) == "203.0.113.7"


def test_trusted_proxy_headers_must_be_valid_ips():
    assert client_key(request({"CF-Connecting-IP": "198.51.100.1"}), True) == "198.51.100.1"
    assert client_key(request({"X-Forwarded-For": "198.51.100.2, 10.0.0.1"}), True) == "198.51.100.2"
    assert client_key(request({"CF-Connecting-IP": "not-an-ip; DROP TABLE"}), True) == "203.0.113.7"
    assert client_key(request({"CF-Connecting-IP": "2001:db8::1"}), True) == "2001:db8::1"


def test_run_endpoint_is_rate_limited_per_client():
    c = build_client(rate_limit_runs_per_min=2)
    assert c.post("/api/scenarios/A/run", json={"requests": 1}).status_code == 200
    assert c.post("/api/scenarios/A/run", json={"requests": 1}).status_code == 200
    r = c.post("/api/scenarios/A/run", json={"requests": 1})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_spoofed_forwarding_headers_do_not_bypass_the_limit_by_default():
    c = build_client(rate_limit_runs_per_min=1)
    c.post("/api/scenarios/A/run", json={"requests": 1})
    for i in range(3):
        r = c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"X-Forwarded-For": f"198.51.100.{i}", "CF-Connecting-IP": f"192.0.2.{i}"})
        assert r.status_code == 429


def test_read_api_is_rate_limited_too():
    c = build_client(rate_limit_api_per_min=3)
    assert [c.get("/api/meta").status_code for _ in range(3)] == [200, 200, 200]
    assert c.get("/api/meta").status_code == 429


def test_probes_and_prometheus_are_never_rate_limited():
    c = build_client(rate_limit_api_per_min=1)
    for path in ("/health", "/ready", "/metrics"):
        assert all(c.get(path).status_code == 200 for _ in range(5))
