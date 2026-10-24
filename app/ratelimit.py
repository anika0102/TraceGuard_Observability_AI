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


def client_key(request: Request, client_ip_header: str = "") -> str:
    """Identify the caller for rate limiting.

    By default nothing in the request is trusted: the key is the socket peer. Only when a deployment names ONE
    header that its proxy overwrites with the real client IP (Render: True-Client-IP) is that header used, and
    only if it holds a single valid IP. Anything else (missing, malformed, a list such as a client-prefilled
    X-Forwarded-For) falls back to the peer, so a bad value can never mint a fresh bucket for an attacker."""
    if client_ip_header:
        ip = _valid_ip(request.headers.get(client_ip_header.lower()))
        if ip:
            return ip
    return request.client.host if request.client else "unknown"
