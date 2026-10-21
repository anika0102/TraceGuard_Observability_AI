from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RecommendRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, examples=["user-1001"])
    category: str = Field(min_length=1, max_length=32, examples=["electronics"])
    budget: float = Field(gt=0, le=100000, examples=[200.0])


class ProductOut(BaseModel):
    sku: str
    name: str
    category: str
    price: float
    score: float


class Correctness(BaseModel):
    checked: bool
    correct: bool
    expected_sku: Optional[str]
    actual_sku: Optional[str]
    detail: str


class RecommendResponse(BaseModel):
    trace_id: Optional[str]
    recommendation: Optional[ProductOut]
    correctness: Correctness
    fault_scenario: str
    latency_ms: float


class Scenario(str, Enum):
    none = "none"
    slow = "slow"                  # added latency in the catalog lookup
    error = "error"                # recommendation step raises -> HTTP 500
    wrong_result = "wrong_result"  # stale ranking: HTTP 200 but not the best item
    not_ready = "not_ready"        # /ready returns 503 (kubelet removes pod from Service)
    unhealthy = "unhealthy"        # /health returns 503 (kubelet restarts the container)
    crash = "crash"                # process exits shortly after responding


class FaultRequest(BaseModel):
    scenario: Scenario
    delay_ms: Optional[int] = Field(default=None, ge=0, le=30000)
    error_every: int = Field(default=1, ge=1, le=1000, description="fail every Nth request")
    clear_incident_window: bool = False
