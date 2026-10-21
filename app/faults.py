"""In-memory fault state. Only mutable when settings.fault_injection_allowed is true."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .schemas import Scenario


@dataclass
class FaultState:
    scenario: Scenario = Scenario.none
    delay_ms: int = 0
    error_every: int = 1
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, scenario: Scenario, delay_ms: int = 0, error_every: int = 1) -> None:
        with self._lock:
            self.scenario, self.delay_ms, self.error_every, self._counter = scenario, delay_ms, error_every, 0

    def reset(self) -> None:
        self.set(Scenario.none)

    def should_error(self) -> bool:
        with self._lock:
            if self.scenario is not Scenario.error:
                return False
            self._counter += 1
            return self._counter % self.error_every == 0

    def as_dict(self) -> dict:
        return {"scenario": self.scenario.value, "delay_ms": self.delay_ms, "error_every": self.error_every}
