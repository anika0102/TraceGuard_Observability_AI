from app import domain

from .conftest import REQ


def test_reference_matches_pipeline_for_every_category_and_budget():
    for cat in domain.CATEGORIES:
        for budget in (10, 40, 60, 100, 130, 200, 1000):
            ranked = domain.rank(domain.candidates(cat, budget))
            expected = domain.expected_recommendation(cat, budget)
            assert (ranked[0] if ranked else None) == expected


def test_wrong_result_is_http_200_but_flagged_incorrect(client, spans):
    client.post("/simulate-failure", json={"scenario": "wrong_result"})
    r = client.post("/recommend", json=REQ)
    body = r.json()
    assert r.status_code == 200  # infrastructure view: perfectly healthy
    assert body["correctness"]["correct"] is False
    assert body["correctness"]["expected_sku"] == "E-100"
    assert body["correctness"]["actual_sku"] != "E-100"
    span = next(s for s in spans.get_finished_spans() if s.name == "recommend.validate_business_rules")
    assert span.attributes["traceguard.outcome_correct"] is False
    assert any(e.name == "business_outcome_mismatch" for e in span.events)


def test_health_checks_stay_green_during_silent_failure(client):
    client.post("/simulate-failure", json={"scenario": "wrong_result"})
    for _ in range(5):
        client.post("/recommend", json=REQ)
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_wrong_result_with_single_candidate_is_not_a_false_positive(client):
    client.post("/simulate-failure", json={"scenario": "wrong_result"})
    body = client.post("/recommend", json={**REQ, "budget": 60}).json()  # only E-102 and E-104 -> 2 candidates
    assert body["correctness"]["correct"] is False
    body = client.post("/recommend", json={**REQ, "budget": 20}).json()  # only E-104
    assert body["correctness"]["correct"] is True


def test_correctness_metric_counts_incorrect(client):
    from prometheus_client import REGISTRY
    before = REGISTRY.get_sample_value("traceguard_correctness_checks_total", {"result": "incorrect"}) or 0
    client.post("/simulate-failure", json={"scenario": "wrong_result"})
    client.post("/recommend", json=REQ)
    after = REGISTRY.get_sample_value("traceguard_correctness_checks_total", {"result": "incorrect"})
    assert after == before + 1
