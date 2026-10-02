from __future__ import annotations

from .models import Role

PERMISSIONS: dict[str, str] = {
    "market.read": "Read licensed market data",
    "strategy.read.own": "Read owned strategies",
    "strategy.write.own": "Create and change owned strategies",
    "backtest.run": "Run backtests",
    "backtest_history.read.own": "Read owned backtest history",
    "backtest_history.delete.own": "Delete owned backtest history",
    "broker.connect.own": "Connect an owned broker account",
    "orders.paper": "Submit simulated orders",
    "orders.live": "Submit live orders after risk approval",
    "orders.live.read": "Read owned live canary orders",
    "orders.live.arm": "Arm an owned manual live canary account",
    "orders.live.manual": "Reserve owned manual live canary orders",
    "orders.live.cancel": "Cancel owned platform live canary orders",
    "orders.live.close": "Close owned live canary positions reduce-only",
    "orders.live.auto.read": "Read owned Strategy Auto Live runtimes",
    "orders.live.auto.arm": "Arm an owned Strategy Auto Live runtime",
    "orders.live.auto.pause": "Pause an owned Strategy Auto Live runtime",
    "orders.live.auto.stop": "Stop an owned Strategy Auto Live runtime",
    "positions.read.own": "Read owned positions",
    "admin.settings.read": "Read platform settings",
    "admin.settings.write": "Change platform settings",
    "admin.providers.read": "Read provider diagnostics",
    "admin.providers.write": "Change provider configuration",
    "admin.users.manage": "Manage platform users",
    "audit.read": "Read the platform audit trail",
}


ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.RESEARCHER: frozenset(
        {
            "market.read",
            "strategy.read.own",
            "strategy.write.own",
            "backtest.run",
            "backtest_history.read.own",
            "backtest_history.delete.own",
        }
    ),
    Role.TRADER: frozenset(
        {
            "market.read",
            "strategy.read.own",
            "strategy.write.own",
            "backtest.run",
            "backtest_history.read.own",
            "backtest_history.delete.own",
            "broker.connect.own",
            "orders.paper",
            "orders.live",
            "orders.live.read",
            "orders.live.arm",
            "orders.live.manual",
            "orders.live.cancel",
            "orders.live.close",
            "orders.live.auto.read",
            "orders.live.auto.arm",
            "orders.live.auto.pause",
            "orders.live.auto.stop",
            "positions.read.own",
        }
    ),
    Role.ADMIN: frozenset(
        {
            "market.read",
            "strategy.read.own",
            "strategy.write.own",
            "backtest.run",
            "backtest_history.read.own",
            "backtest_history.delete.own",
            "orders.paper",
            "positions.read.own",
            "admin.settings.read",
            "admin.settings.write",
            "admin.providers.read",
            "admin.providers.write",
            "admin.users.manage",
            "audit.read",
        }
    ),
}
