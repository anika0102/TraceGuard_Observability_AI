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


def test_no_header_is_trusted_by_default():
    spoof = request({"True-Client-IP": "198.51.100.1", "CF-Connecting-IP": "198.51.100.3", "X-Forwarded-For": "198.51.100.2"})
    assert client_key(spoof) == "203.0.113.7" and client_key(spoof, "") == "203.0.113.7"


def test_only_the_one_configured_header_is_used_and_it_must_be_a_single_valid_ip():
    configured = "True-Client-IP"
    assert client_key(request({"True-Client-IP": "198.51.100.1"}), configured) == "198.51.100.1"
    assert client_key(request({"true-client-ip": "2001:db8::1"}), configured) == "2001:db8::1"
    # other forwarding headers stay ignored even when one header is configured
    assert client_key(request({"CF-Connecting-IP": "198.51.100.3", "X-Forwarded-For": "198.51.100.2"}), configured) == "203.0.113.7"


@pytest.mark.parametrize("value", ["not-an-ip; DROP TABLE", "198.51.100.1, 10.0.0.1", "", "999.1.1.1", "1.2.3.4:80", " "])
def test_bad_or_list_values_fail_closed_to_the_socket_peer(value):
    """A malformed header must never mint a fresh bucket for the sender (that would defeat rate limiting)."""
    assert client_key(request({"True-Client-IP": value}), "True-Client-IP") == "203.0.113.7"
    assert client_key(request({"X-Forwarded-For": value}), "X-Forwarded-For") == "203.0.113.7"


def test_a_configured_header_that_is_missing_falls_back_to_the_peer():
    assert client_key(request({}), "True-Client-IP") == "203.0.113.7"


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


def test_configured_header_gives_each_real_client_its_own_bucket():
    c = build_client(rate_limit_runs_per_min=1, client_ip_header="True-Client-IP")
    assert c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"True-Client-IP": "198.51.100.1"}).status_code == 200
    assert c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"True-Client-IP": "198.51.100.1"}).status_code == 429
    assert c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"True-Client-IP": "198.51.100.2"}).status_code == 200


def test_garbage_in_the_configured_header_cannot_be_used_to_dodge_the_limit():
    c = build_client(rate_limit_runs_per_min=1, client_ip_header="True-Client-IP")
    assert c.post("/api/scenarios/A/run", json={"requests": 1}).status_code == 200
    for junk in ("x", "1.1.1.1, 2.2.2.2", "", "300.1.1.1"):
        r = c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"True-Client-IP": junk})
        assert r.status_code == 429, f"junk header value {junk!r} bypassed the limit"


def test_global_ceiling_holds_even_when_every_request_claims_a_new_client_ip():
    """Defence in depth: if the proxy header were spoofable, unlimited fake clients must still hit a total cap."""
    c = build_client(rate_limit_runs_per_min=100, rate_limit_runs_global_per_min=5, client_ip_header="True-Client-IP")
    codes = [c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"True-Client-IP": f"198.51.100.{i}"}).status_code
             for i in range(9)]
    assert codes == [200] * 5 + [429] * 4
    api = build_client(rate_limit_api_per_min=100, rate_limit_api_global_per_min=3, client_ip_header="True-Client-IP")
    assert [api.get("/api/meta", headers={"True-Client-IP": f"203.0.113.{i}"}).status_code for i in range(5)] == [200, 200, 200, 429, 429]


def test_one_abusive_client_cannot_drain_the_global_budget_for_everyone_else():
    c = build_client(rate_limit_runs_per_min=2, rate_limit_runs_global_per_min=4, client_ip_header="True-Client-IP")
    flood = [c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"True-Client-IP": "198.51.100.1"}).status_code for _ in range(20)]
    assert flood.count(200) == 2                      # blocked by its own limit; only 2 global tokens were spent
    others = [c.post("/api/scenarios/A/run", json={"requests": 1}, headers={"True-Client-IP": f"198.51.100.{i}"}).status_code for i in (2, 3)]
    assert others == [200, 200]


def test_public_recommend_also_honours_the_global_ceiling():
    c = build_client(public_demo=True, rate_limit_api_per_min=100, rate_limit_api_global_per_min=2, client_ip_header="True-Client-IP")
    body = {"user_id": "u", "category": "books", "budget": 100}
    codes = [c.post("/recommend", json=body, headers={"True-Client-IP": f"198.51.100.{i}"}).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_read_api_is_rate_limited_too():
    c = build_client(rate_limit_api_per_min=3)
    assert [c.get("/api/meta").status_code for _ in range(3)] == [200, 200, 200]
    assert c.get("/api/meta").status_code == 429


def test_probes_and_prometheus_are_never_rate_limited():
    c = build_client(rate_limit_api_per_min=1)
    for path in ("/health", "/ready", "/metrics"):
        assert all(c.get(path).status_code == 200 for _ in range(5))
