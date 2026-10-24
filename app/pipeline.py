"""The simulated recommendation workflow, parameterised by the fault state it runs under.

`/recommend` runs it with the app-wide fault state (local development). The public-demo sandbox runs it with a
private fault state per simulated request, so one visitor's scenario can never affect anyone else's request.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from opentelemetry.trace import Span, Status, StatusCode, Tracer

from . import domain, metrics, telemetry
from .config import Settings
from .faults import FaultState
from .schemas import Correctness, ProductOut, RecommendRequest, RecommendResponse, Scenario

log = logging.getLogger("traceguard.app")


class InvalidInput(Exception):
    pass


class UpstreamFailure(Exception):
    """Injected failure in the recommendation step."""


@dataclass
class PipelineResult:
    status_code: int
    content: dict            # JSON body for the HTTP response
    correct: Optional[bool]  # None when no business check ran (the request failed first)
    trace_id: Optional[str]
    duration_ms: float
    scenario: str

    @property
    def is_error(self) -> bool:
        return self.status_code >= 500


def _product_out(p: Optional[domain.Product]) -> Optional[ProductOut]:
    return ProductOut(sku=p.sku, name=p.name, category=p.category, price=p.price, score=p.score) if p else None


def _hash_user(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]  # pseudonymised; never a metric label


def record_request_metric(result: PipelineResult) -> None:
    status = "ok" if result.status_code < 400 else ("client_error" if result.status_code < 500 else "error")
    metrics.RECOMMEND_REQUESTS.labels(status, result.scenario).inc()
    metrics.RECOMMEND_DURATION.observe(result.duration_ms / 1000)


class RecommendPipeline:
    def __init__(self, tracer: Tracer, settings: Settings):
        self.tracer = tracer
        self.settings = settings

    @contextmanager
    def operation(self, name: str, faults: FaultState):
        """Child span with operation name, duration and status attributes."""
        start = time.perf_counter()
        with self.tracer.start_as_current_span(f"recommend.{name}") as span:
            span.set_attribute("traceguard.operation", name)
            span.set_attribute("traceguard.fault_scenario", faults.scenario.value)
            try:
                yield span
                span.set_attribute("traceguard.status", "ok")
            except Exception as exc:
                span.set_attribute("traceguard.status", "error")
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                span.set_attribute("traceguard.duration_ms", round((time.perf_counter() - start) * 1000, 2))

    async def run(self, req: RecommendRequest, faults: FaultState, server_span: Span) -> PipelineResult:
        """Execute the workflow under `faults`. `server_span` is the request's root span (current span)."""
        start = time.perf_counter()
        scenario = faults.scenario
        server_span.set_attribute("traceguard.fault_scenario", scenario.value)
        trace_id = telemetry.current_trace_id()
        status_code, correct = 200, None
        content: dict = {}

        try:
            with self.operation("validate_input", faults) as sp:
                sp.set_attribute("traceguard.user_hash", _hash_user(req.user_id))
                sp.set_attribute("traceguard.category", req.category)
                if req.category not in domain.CATEGORIES:
                    raise InvalidInput(f"unknown category '{req.category}'")

            with self.operation("lookup_catalog", faults) as sp:  # simulated database / tool lookup
                if scenario is Scenario.slow:
                    delay = faults.delay_ms or self.settings.slow_fault_delay_ms
                    sp.set_attribute("traceguard.injected_delay_ms", delay)
                    await asyncio.sleep(delay / 1000)
                found = domain.candidates(req.category, req.budget)
                sp.set_attribute("traceguard.candidate_count", len(found))

            with self.operation("compute_recommendation", faults) as sp:
                if faults.should_error():
                    raise UpstreamFailure("injected failure in recommendation model")
                ranked = domain.rank(found)
                # wrong_result: a stale ranking serves the runner-up while everything looks healthy.
                pick = ranked[1] if scenario is Scenario.wrong_result and len(ranked) > 1 else (
                    ranked[0] if ranked else None)
                sp.set_attribute("traceguard.actual_sku", pick.sku if pick else "none")

            with self.operation("validate_business_rules", faults) as sp:
                expected = domain.expected_recommendation(req.category, req.budget)
                exp_sku, act_sku = (expected.sku if expected else None), (pick.sku if pick else None)
                # Invariants (budget, stock) plus comparison with the golden reference.
                invariants_ok = pick is None or (pick.price <= req.budget and pick.in_stock)
                correct = invariants_ok and exp_sku == act_sku
                sp.set_attribute("traceguard.expected_sku", exp_sku or "none")
                sp.set_attribute("traceguard.actual_sku", act_sku or "none")
                sp.set_attribute("traceguard.outcome_correct", correct)
                if not correct:
                    sp.add_event("business_outcome_mismatch",
                                 {"expected_sku": exp_sku or "none", "actual_sku": act_sku or "none"})
                metrics.CORRECTNESS.labels("correct" if correct else "incorrect").inc()
                server_span.set_attribute("traceguard.outcome_correct", correct)

            with self.operation("build_response", faults):
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                body = RecommendResponse(
                    trace_id=trace_id,
                    recommendation=_product_out(pick),
                    correctness=Correctness(
                        checked=True, correct=correct, expected_sku=exp_sku, actual_sku=act_sku,
                        detail="matches expected business result" if correct
                        else "HTTP 200 but result differs from expected business result"),
                    fault_scenario=scenario.value,
                    latency_ms=latency_ms,
                )
            content = body.model_dump()
        except InvalidInput as exc:
            status_code = 400
            server_span.set_status(Status(StatusCode.ERROR, "invalid input"))
            content = {"error": "invalid_input", "detail": str(exc), "trace_id": trace_id}
        except UpstreamFailure as exc:
            status_code = 500
            server_span.set_status(Status(StatusCode.ERROR, str(exc)))
            log.error("recommendation failed", extra={"fields": {"fault_scenario": scenario.value}})
            content = {"error": "upstream_failure", "detail": str(exc), "trace_id": trace_id}
        else:
            log.info("recommendation served", extra={"fields": {
                "fault_scenario": scenario.value, "outcome_correct": correct}})

        duration_ms = (time.perf_counter() - start) * 1000
        server_span.set_attribute("traceguard.duration_ms", round(duration_ms, 2))
        return PipelineResult(status_code=status_code, content=content, correct=correct, trace_id=trace_id,
                              duration_ms=duration_ms, scenario=scenario.value)
