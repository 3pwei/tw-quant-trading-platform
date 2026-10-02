from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from threading import RLock
from typing import Callable


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("rate limit must be positive")
        if self.window_seconds <= 0:
            raise ValueError("rate limit window must be positive")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset_after: int


class SlidingWindowRateLimiter:
    """In-process, per-actor sliding-window limiter with bounded memory."""

    def __init__(
        self,
        rules: dict[str, RateLimitRule],
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        self._rules = dict(rules)
        self._clock = clock
        self._max_keys = max_keys
        self._windows: OrderedDict[
            tuple[str, str], deque[float]
        ] = OrderedDict()
        self._accepted = {scope: 0 for scope in rules}
        self._rejected = {scope: 0 for scope in rules}
        self._lock = RLock()

    def check(self, scope: str, actor: str) -> RateLimitDecision:
        rule = self._rules[scope]
        key = (scope, actor)
        now = self._clock()
        cutoff = now - rule.window_seconds
        with self._lock:
            events = self._windows.get(key)
            if events is None:
                self._evict_if_full()
                events = deque()
                self._windows[key] = events
            else:
                self._windows.move_to_end(key)
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= rule.limit:
                wait = max(1, math.ceil(events[0] + rule.window_seconds - now))
                self._rejected[scope] += 1
                return RateLimitDecision(
                    allowed=False,
                    limit=rule.limit,
                    remaining=0,
                    retry_after=wait,
                    reset_after=wait,
                )
            events.append(now)
            self._accepted[scope] += 1
            reset = max(1, math.ceil(events[0] + rule.window_seconds - now))
            return RateLimitDecision(
                allowed=True,
                limit=rule.limit,
                remaining=rule.limit - len(events),
                retry_after=0,
                reset_after=reset,
            )

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "algorithm": "sliding_window",
                "active_keys": len(self._windows),
                "max_keys": self._max_keys,
                "scopes": {
                    scope: {
                        "limit": rule.limit,
                        "window_seconds": rule.window_seconds,
                        "accepted": self._accepted[scope],
                        "rejected": self._rejected[scope],
                    }
                    for scope, rule in self._rules.items()
                },
            }

    def _evict_if_full(self) -> None:
        while len(self._windows) >= self._max_keys:
            self._windows.popitem(last=False)

