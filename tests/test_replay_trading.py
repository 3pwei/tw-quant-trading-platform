from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from tw_quant.auth import AccountStatus, AuthUser, Role, TradingMode
from tw_quant.market import KBar
from tw_quant.paper import PaperOrderCommand
from tw_quant.replay import ReplaySessionNotFound, ReplayTradingSessionRegistry


def user(owner_id: str) -> AuthUser:
    return AuthUser(
        user_id=owner_id,
        email=f"{owner_id}@example.com",
        role=Role.RESEARCHER,
        status=AccountStatus.ACTIVE,
        trading_mode=TradingMode.DISABLED,
        permissions=("backtest.run",),
    )


def replay_bars() -> list[KBar]:
    taipei = ZoneInfo("Asia/Taipei")
    start = datetime(2024, 5, 6, 8, 45, tzinfo=taipei)
    result = []
    for index in range(4):
        at = start + timedelta(minutes=index)
        close = 20_000 + index * 10
        result.append(KBar(
            symbol="TMF", contract="TMF202405", time=at,
            open=close - 2, high=close + 3, low=close - 4, close=close,
            volume=10, status="closed", session="day",
            trading_date=at.date(), first_tick_time=at,
            last_tick_time=at + timedelta(seconds=59),
            exchange_time=at + timedelta(seconds=59),
            received_time=at + timedelta(seconds=59), latency_ms=0,
        ))
    return result


class ReplayTradingSessionTests(unittest.TestCase):
    def setUp(self):
        self.registry = ReplayTradingSessionRegistry()

    def tearDown(self):
        self.registry.close()

    def test_account_is_owner_scoped_idempotent_and_resets_on_rewind(self):
        session = self.registry.create("snapshot-a", user("owner-a"), replay_bars())
        other = self.registry.create("snapshot-b", user("owner-b"), replay_bars())

        with self.assertRaises(ReplaySessionNotFound):
            self.registry.get(session.session_id, "owner-b")

        command = PaperOrderCommand(
            strategy_id="manual-replay", strategy_version=1,
            side="buy", quantity=1, stop_loss_price=19_950,
        )
        order, created, opened = session.submit(command, idempotency_key="tap-1")
        duplicate, duplicate_created, _ = session.submit(
            command, idempotency_key="tap-1"
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(order["order_id"], duplicate["order_id"])
        self.assertEqual(order["status"], "filled")
        self.assertEqual(len(opened["positions"]), 1)
        self.assertEqual(other.state()["positions"], [])

        advanced = session.seek(3)
        self.assertFalse(advanced["rewound"])
        self.assertEqual(advanced["cursor"], 3)
        self.assertGreater(advanced["positions"][0]["unrealized_pnl"], 0)

        rewound = session.seek(1)
        self.assertTrue(rewound["rewound"])
        self.assertEqual(rewound["cursor"], 1)
        self.assertEqual(rewound["positions"], [])
        self.assertEqual(rewound["orders"], [])
        self.assertEqual(rewound["fills"], [])

    def test_registry_is_bounded_per_owner(self):
        sessions = [
            self.registry.create(f"snapshot-{index}", user("owner-a"), replay_bars())
            for index in range(4)
        ]
        with self.assertRaises(ReplaySessionNotFound):
            self.registry.get(sessions[0].session_id, "owner-a")
        self.assertEqual(
            self.registry.get(sessions[-1].session_id, "owner-a").snapshot_id,
            "snapshot-3",
        )
