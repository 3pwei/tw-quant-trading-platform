import unittest

from tw_quant.live.api import _rate_limit_scope
from tw_quant.live.rate_limit import RateLimitRule, SlidingWindowRateLimiter


class MutableClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class SlidingWindowRateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.limiter = SlidingWindowRateLimiter(
            {"backtests": RateLimitRule(2, 60)}, clock=self.clock
        )

    def test_rejects_after_limit_and_recovers_when_window_expires(self):
        first = self.limiter.check("backtests", "user-1")
        second = self.limiter.check("backtests", "user-1")
        rejected = self.limiter.check("backtests", "user-1")

        self.assertTrue(first.allowed)
        self.assertEqual(first.remaining, 1)
        self.assertTrue(second.allowed)
        self.assertEqual(second.remaining, 0)
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.retry_after, 60)

        self.clock.value += 60
        recovered = self.limiter.check("backtests", "user-1")
        self.assertTrue(recovered.allowed)
        self.assertEqual(recovered.remaining, 1)

    def test_users_have_independent_windows(self):
        self.limiter.check("backtests", "user-1")
        self.limiter.check("backtests", "user-1")

        decision = self.limiter.check("backtests", "user-2")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.remaining, 1)

    def test_stats_expose_rejection_counts_without_actor_identifiers(self):
        self.limiter.check("backtests", "private-user-id")
        self.limiter.check("backtests", "private-user-id")
        self.limiter.check("backtests", "private-user-id")

        stats = self.limiter.stats()

        self.assertEqual(stats["active_keys"], 1)
        self.assertEqual(stats["scopes"]["backtests"]["accepted"], 2)
        self.assertEqual(stats["scopes"]["backtests"]["rejected"], 1)
        self.assertNotIn("private-user-id", str(stats))

    def test_actor_storage_is_bounded(self):
        limiter = SlidingWindowRateLimiter(
            {"backtests": RateLimitRule(2, 60)},
            clock=self.clock,
            max_keys=2,
        )
        limiter.check("backtests", "user-1")
        limiter.check("backtests", "user-2")
        limiter.check("backtests", "user-3")

        self.assertEqual(limiter.stats()["active_keys"], 2)


class RateLimitedRouteTests(unittest.TestCase):
    def test_only_expensive_mutating_or_execution_routes_are_limited(self):
        expected = {
            ("POST", "/api/access-requests"): "access_requests",
            ("GET", "/api/backtest"): "backtests",
            ("GET", "/api/composite-backtest"): "backtests",
            ("POST", "/api/backtest-runs"): "backtests",
            ("POST", "/api/replay/prepare"): "replay_prepares",
            ("POST", "/api/paper/orders"): "orders",
            (
                "POST",
                "/api/replay/sessions/session-1/orders",
            ): "orders",
        }
        for route, scope in expected.items():
            with self.subTest(route=route):
                self.assertEqual(_rate_limit_scope(*route), scope)

        self.assertIsNone(_rate_limit_scope("GET", "/api/backtest/options"))
        self.assertIsNone(_rate_limit_scope("GET", "/api/paper/orders"))
        self.assertIsNone(
            _rate_limit_scope("PUT", "/api/replay/sessions/session-1/cursor")
        )


if __name__ == "__main__":
    unittest.main()
