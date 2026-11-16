"""Bounded single-process HTTP budgets, keyed only by the trusted ASGI peer."""

import math
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class Policy:
    capacity: int
    refill_per_second: float


POLICIES = {
    "general": Policy(240, 40),
    "sessions": Policy(10, 0.5),
    "create": Policy(5, 1 / 6),
    "join": Policy(20, 1),
    "import": Policy(3, 1 / 300),
}


@dataclass
class Bucket:
    tokens: float
    updated: float


class HTTPRateLimiter:
    def __init__(self, *, max_keys: int = 4096, idle_seconds: float = 3600) -> None:
        self.max_keys, self.idle_seconds = max_keys, idle_seconds
        self.buckets: dict[tuple[str, str], Bucket] = {}
        self.sweep_at = 0.0

    def check(self, peer: str, method: str, path: str, *, now: float | None = None) -> int:
        """Return retry seconds, zero on acceptance. Does not inspect forwarded headers."""
        now = monotonic() if now is None else now
        if now >= self.sweep_at:
            self.buckets = {
                key: bucket
                for key, bucket in self.buckets.items()
                if now - bucket.updated < self.idle_seconds
            }
            self.sweep_at = now + 60
        categories = ["general"]
        if method == "POST":
            if path == "/api/v1/sessions":
                categories.append("sessions")
            elif path == "/api/v1/rooms":
                categories.append("create")
            elif path.endswith("/join"):
                categories.append("join")
            elif path.endswith("/cases/import"):
                categories.append("import")
        keys = [(peer, category) for category in categories]
        if len(self.buckets) + sum(key not in self.buckets for key in keys) > self.max_keys:
            return 60
        waiting = 0
        for key in keys:
            policy = POLICIES[key[1]]
            bucket = self.buckets.setdefault(key, Bucket(float(policy.capacity), now))
            bucket.tokens = min(
                policy.capacity,
                bucket.tokens + max(0, now - bucket.updated) * policy.refill_per_second,
            )
            bucket.updated = now
            if bucket.tokens < 1:
                waiting = max(waiting, math.ceil((1 - bucket.tokens) / policy.refill_per_second))
        if waiting:
            return max(1, waiting)
        for key in keys:
            self.buckets[key].tokens -= 1
        return 0
