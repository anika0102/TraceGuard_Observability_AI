"""Small in-memory token-bucket rate limiter, keyed per client, with bounded memory."""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional, Tuple

from fastapi import Request


class RateLimiter:
    def __init__(self, per_minute: int, burst: Optional[int] = None, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        self.rate = per_minute / 60.0
        self.capacity = float(burst if burst is not None else per_minute)
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: "OrderedDict[str, Tuple[float, float]]" = OrderedDict()  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def allow(self, key: str) -> Tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed, retry = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed, retry = False, (1.0 - tokens) / self.rate if self.rate > 0 else 60.0
            while len(self._buckets) > self.max_keys:  # bounded: forget the least recently seen clients
                self._buckets.popitem(last=False)
            return allowed, retry


def _valid_ip(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_key(request: Request, trust_proxy_headers: bool) -> str:
    """Identify the caller. Proxy headers are client-controlled unless a proxy you run sets them, so they are
    ignored by default; when trusted, only a syntactically valid IP is accepted."""
    if trust_proxy_headers:
        ip = _valid_ip(request.headers.get("cf-connecting-ip"))
        if ip is None:
            forwarded = request.headers.get("x-forwarded-for", "")
            ip = _valid_ip(forwarded.split(",")[0]) if forwarded else None
        if ip:
            return ip
    return request.client.host if request.client else "unknown"
