from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal
from uuid import uuid4

from ...auth import AuthUser
from ...backtest import (
    MAX_BACKTEST_DAYS,
    run_composite_backtest,
    run_historical_events,
    run_strategy_backtest,
    validate_date_range,
)
from ...market import (
    KBar,
    SUPPORTED_TIMEFRAMES,
    TIMEFRAME_LABELS,
    aggregate_kbars,
    validate_timeframe,
)
from ...paper import PaperOrderCommand
from ...replay import ReplaySessionNotFound, ReplayTradingSessionRegistry
from ...strategy_registry import (
    get_strategy_services,
    supported_strategies,
    analyze_strategies,
    generate_composite_signals,
    strategy_catalog,
    validate_strategy_parameters,
)
from ..runtime_data import strategy_lineage
from ..storage import BacktestRepository, MarketRepository, StrategyRepository
from .errors import BadRequestError, InvalidInputError, ResourceNotFoundError


@dataclass(frozen=True)
class ReplayPrepareInput:
    symbol: str
    trading_date: date
    session: str
    interval: str
    strategies: tuple[str, ...]


@dataclass(frozen=True)
class ReplayOrderInput:
    strategy_id: str
    strategy_version: int
    side: Literal["buy", "sell"]
    quantity: int
    stop_loss_price: float | None
    reduce_only: bool


@dataclass(frozen=True)
class BacktestInput:
    symbol: str
    strategy: str
    interval: str
    start: date
    end: date
    version: int | None = None


class ResearchApplicationService:
    """Replay and backtest orchestration without transport concerns."""

    def __init__(
        self,
        market_repository: MarketRepository,
        strategy_repository: StrategyRepository,
        backtest_repository: BacktestRepository,
        replay_sessions: ReplayTradingSessionRegistry,
        symbol: str,
    ):
        self.market_repository = market_repository
        self.strategy_repository = strategy_repository
        self.backtest_repository = backtest_repository
        self.replay_sessions = replay_sessions
        self.symbol = symbol

    def backtest_options(
        self, owner_id: str, symbol: str
    ) -> dict[str, object]:
        self._require_symbol(symbol)
        first, last = self.market_repository.date_bounds(self.symbol)
        catalog = analyze_strategies(
            [],
            supported_strategies(),
            parameters=self.strategy_repository.strategy_parameters(owner_id),
        )["strategies"]
        return {
            "symbol": self.symbol,
            "available_start": first.isoformat() if first else None,
            "available_end": last.isoformat() if last else None,
            "max_days": MAX_BACKTEST_DAYS,
            "intervals": [
                {"key": key, "name": TIMEFRAME_LABELS[key]}
                for key in SUPPORTED_TIMEFRAMES
            ],
            "strategies": [
                {"key": item["key"], "name": item["name"]}
                for item in catalog
            ]
            + [
                {
                    "key": f"composite:{item['id']}",
                    "name": f"{item['name']} · v{item['version']}",
                    "kind": "composite",
                }
                for item in self.strategy_repository.composite_strategies(
                    owner_id
                )
            ],
        }

    def replay_options(
        self, owner_id: str, symbol: str
    ) -> dict[str, object]:
        self._require_symbol(symbol)
        first, last = self.market_repository.date_bounds(self.symbol)
        catalog = strategy_catalog(
            self.strategy_repository.strategy_parameters(owner_id)
        )
        return {
            "symbol": self.symbol,
            "available_start": first.isoformat() if first else None,
            "available_end": last.isoformat() if last else None,
            "available_dates": self.market_repository.replay_availability(
                self.symbol
            ),
            "intervals": [
                {"key": key, "name": TIMEFRAME_LABELS[key]}
                for key in SUPPORTED_TIMEFRAMES
                if key not in {"1d", "1w"}
            ],
            "strategies": [
                {
                    "key": item["key"],
                    "name": item["name"],
                    "kind": "atomic",
                    "color": item["color"],
                }
                for item in catalog
            ]
            + [
                {
                    "key": f"composite:{item['id']}",
                    "name": f"{item['name']} · v{item['version']}",
                    "kind": "composite",
                    "color": "#a78bfa",
                }
                for item in self.strategy_repository.composite_strategies(
                    owner_id
                )
            ],
            "max_strategies": 3,
            "sessions": [
                {"key": "day", "name": "日盤"},
                {"key": "night", "name": "夜盤"},
            ],
        }

    def prepare_replay(
        self,
        preparation: ReplayPrepareInput,
        owner_id: str,
        replay_owner: AuthUser,
    ) -> dict[str, object]:
        self._require_symbol(preparation.symbol)
        selected = list(
            dict.fromkeys(
                value.strip().lower()
                for value in preparation.strategies
                if value.strip()
            )
        )
        if not selected:
            raise InvalidInputError("至少選擇一個策略")
        if len(selected) > 3:
            raise InvalidInputError("回放最多同時顯示 3 個策略")
        try:
            interval = validate_timeframe(preparation.interval)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        if interval in {"1d", "1w"}:
            raise BadRequestError("回放僅支援 1 分鐘至 1 小時 K")

        source_bars = [
            bar
            for bar in self.market_repository.between_trading_dates(
                self.symbol,
                preparation.trading_date,
                preparation.trading_date,
            )
            if bar.session == preparation.session
        ]
        if not source_bars:
            raise ResourceNotFoundError("所選交易日與時段沒有歷史 K 棒")
        display_bars = aggregate_kbars(source_bars, interval)
        atomic = [
            key for key in selected if not key.startswith("composite:")
        ]
        unsupported = sorted(set(atomic) - set(supported_strategies()))
        if unsupported:
            raise BadRequestError(
                f"unsupported strategies: {', '.join(unsupported)}"
            )
        results = analyze_strategies(
            display_bars,
            atomic,
            parameters=self.strategy_repository.strategy_parameters(owner_id),
            interval=interval,
        )["strategies"]
        by_key = {str(item["key"]): item for item in results}
        for key in selected:
            if not key.startswith("composite:"):
                continue
            strategy_id = key.removeprefix("composite:")
            item = self.strategy_repository.composite_strategy(
                strategy_id, owner_user_id=owner_id
            )
            if (
                item is None
                or self.strategy_repository.composite_strategy_archived(
                    strategy_id, owner_id
                )
            ):
                raise ResourceNotFoundError("找不到可用的組合策略")
            signals, _trace = generate_composite_signals(
                source_bars, item["definition"]
            )
            by_key[key] = {
                "key": key,
                "name": f"{item['name']} · v{item['version']}",
                "color": "#a78bfa",
                "parameters": {},
                "signals": signals,
                "kind": "composite",
                "version": item["version"],
            }

        for key in selected:
            result = by_key[key]
            is_composite = key.startswith("composite:")
            event_run = run_historical_events(
                source_bars if is_composite else display_bars,
                result["signals"],
                strategy_id=(
                    key.removeprefix("composite:") if is_composite else key
                ),
                strategy_version=int(result.get("version", 1)),
                owner_id=owner_id,
                timeframe="1m" if is_composite else interval,
            )
            result["execution"] = {
                "engine": "deterministic_event_engine",
                "event_counts": event_run.event_counts,
                "events": event_run.execution_events,
            }

        snapshot_id = uuid4().hex
        if replay_owner.user_id != owner_id:
            replay_owner = replace(replay_owner, user_id=owner_id)
        trading_session = self.replay_sessions.create(
            snapshot_id, replay_owner, display_bars
        )
        return {
            "snapshot_id": snapshot_id,
            "created_at": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "symbol": self.symbol,
            "trading_date": preparation.trading_date.isoformat(),
            "session": preparation.session,
            "interval": interval,
            "interval_name": TIMEFRAME_LABELS[interval],
            "bars": [self._bar_message(bar) for bar in display_bars],
            "strategies": [by_key[key] for key in selected],
            "trading_session": trading_session.state(),
        }

    def replay_state(self, session_id: str, owner_id: str) -> dict[str, object]:
        return self._session(session_id, owner_id).state()

    def seek_replay(
        self, session_id: str, cursor: int, owner_id: str
    ) -> dict[str, object]:
        try:
            return self._session(session_id, owner_id).seek(cursor)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def submit_replay_order(
        self,
        session_id: str,
        order_input: ReplayOrderInput,
        idempotency_key: str,
        owner_id: str,
    ) -> dict[str, object]:
        try:
            command = PaperOrderCommand(
                strategy_id=order_input.strategy_id,
                strategy_version=order_input.strategy_version,
                side=order_input.side,
                quantity=order_input.quantity,
                stop_loss_price=order_input.stop_loss_price,
                reduce_only=order_input.reduce_only,
                reason="manual_replay_order",
            )
            order, created, state = self._session(
                session_id, owner_id
            ).submit(command, idempotency_key=idempotency_key)
        except (RuntimeError, ValueError) as exc:
            raise InvalidInputError(str(exc)) from exc
        return {
            "mode": "replay",
            "created": created,
            "order": order,
            "session": state,
        }

    def reset_replay(
        self, session_id: str, owner_id: str
    ) -> dict[str, object]:
        return self._session(session_id, owner_id).reset()

    def execute_atomic(
        self,
        execution: BacktestInput,
        owner_id: str,
        *,
        parameter_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._require_symbol(execution.symbol)
        key = execution.strategy.lower()
        if key not in supported_strategies():
            raise BadRequestError("unsupported strategy")
        try:
            interval = validate_timeframe(execution.interval)
            validate_date_range(execution.start, execution.end)
            bars = aggregate_kbars(
                self.market_repository.between_trading_dates(
                    self.symbol, execution.start, execution.end
                ),
                interval,
            )
            return run_strategy_backtest(
                bars,
                key,
                execution.start,
                execution.end,
                interval=interval,
                parameters=(
                    parameter_snapshot
                    if parameter_snapshot is not None
                    else self.strategy_repository.strategy_parameters(owner_id).get(key)
                ),
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc

    def execute_composite(
        self, execution: BacktestInput, owner_id: str
    ) -> dict[str, object]:
        self._require_symbol(execution.symbol)
        item = self.strategy_repository.composite_strategy(
            execution.strategy, execution.version, owner_id
        )
        if item is None:
            raise ResourceNotFoundError("找不到組合策略版本")
        try:
            validate_date_range(execution.start, execution.end)
            return run_composite_backtest(
                self.market_repository.between_trading_dates(
                    self.symbol, execution.start, execution.end
                ),
                item["definition"],
                str(item["id"]),
                int(item["version"]),
                execution.start,
                execution.end,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc

    def create_backtest_run(
        self, execution: BacktestInput, owner_id: str
    ) -> dict[str, object]:
        if execution.strategy.startswith("composite:"):
            strategy_id = execution.strategy.removeprefix("composite:")
            item = self.strategy_repository.composite_strategy(
                strategy_id, execution.version, owner_id
            )
            if item is None:
                raise ResourceNotFoundError("找不到組合策略版本")
            normalized = replace(
                execution,
                strategy=strategy_id,
                version=int(item["version"]),
            )
            result = self.execute_composite(normalized, owner_id)
            saved = self.backtest_repository.save_backtest_run(
                result,
                "composite",
                strategy_id,
                int(item["version"]),
                item["definition"],
                owner_id,
                strategy_lineage(
                    "composite",
                    strategy_id,
                    int(item["version"]),
                    get_strategy_services().identity_snapshot(),
                ),
            )
        else:
            key = execution.strategy.lower()
            parameters = validate_strategy_parameters(
                key,
                self.strategy_repository.strategy_parameters(owner_id).get(key),
            )
            version = self.strategy_repository.ensure_strategy_version(
                key,
                parameters,
                get_strategy_services().identity_snapshot(),
                owner_id,
            )
            result = self.execute_atomic(
                replace(execution, strategy=key),
                owner_id,
                parameter_snapshot=dict(version["parameters"]),
            )
            saved = self.backtest_repository.save_backtest_run(
                result,
                "atomic",
                key,
                None,
                dict(version["parameters"]),
                owner_id,
                dict(version["lineage"]),
            )
        result["history_run_id"] = saved["run_id"]
        result["history_created_at"] = saved["created_at"]
        return result

    def backtest_runs(
        self,
        owner_id: str,
        limit: int,
        offset: int,
        strategy_key: str | None,
        strategy_kind: str | None = None,
        outcome: str | None = None,
        search: str | None = None,
    ) -> dict[str, object]:
        total = self.backtest_repository.count_backtest_runs(
            strategy_key, owner_id, strategy_kind, outcome, search
        )
        runs = self.backtest_repository.backtest_runs(
            limit, offset, strategy_key, owner_id, strategy_kind, outcome, search
        )
        return {
            "runs": runs,
            "total": total,
            "has_more": offset + len(runs) < total,
            "limit": limit,
            "offset": offset,
        }

    def backtest_run(self, run_id: str, owner_id: str) -> dict[str, object]:
        item = self.backtest_repository.backtest_run(run_id, owner_id)
        if item is None:
            raise ResourceNotFoundError("找不到回測紀錄")
        response = dict(item)
        result = item.get("result")
        if isinstance(result, dict):
            response["result"] = {
                key: value for key, value in result.items()
                if key != "visualization"
            }
        return response

    @staticmethod
    def _chart_bar(bar: KBar) -> dict[str, object]:
        return {
            "timestamp": bar.time.isoformat(timespec="milliseconds"),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "contract": bar.contract,
            "session": bar.session,
            "trading_date": bar.trading_date.isoformat(),
        }

    @staticmethod
    def _trade_session(
        trade: dict[str, object], source_bars: list[KBar]
    ) -> tuple[str, str]:
        stored_session = str(trade.get("session") or "")
        stored_date = str(trade.get("trading_date") or "")
        if stored_session in {"day", "night"} and stored_date:
            return stored_session, stored_date
        occurred_at = datetime.fromisoformat(str(trade["entry_time"]))
        contract = str(trade.get("contract") or "")
        candidates = [
            bar for bar in source_bars
            if not contract or bar.contract == contract
        ]
        if stored_date:
            candidates = [
                bar for bar in candidates
                if bar.trading_date.isoformat() == stored_date
            ]
        if not candidates:
            raise ResourceNotFoundError("找不到這筆交易使用的歷史 K 棒")
        matched = min(
            candidates,
            key=lambda bar: abs((bar.time - occurred_at).total_seconds()),
        )
        return matched.session, matched.trading_date.isoformat()

    @staticmethod
    def _chart_overlays(
        raw: object, visible_times: set[str]
    ) -> list[dict[str, object]]:
        overlays: list[dict[str, object]] = []
        if not isinstance(raw, list):
            return overlays
        for overlay in raw:
            if not isinstance(overlay, dict):
                continue
            points = overlay.get("points")
            if not isinstance(points, list):
                continue
            selected = [
                point for point in points
                if isinstance(point, dict)
                and str(point.get("time") or "")[:16] in visible_times
            ]
            if selected:
                overlays.append({**overlay, "points": selected})
        return overlays

    @classmethod
    def _chart_visualization(
        cls, raw: object, visible_times: set[str]
    ) -> dict[str, object] | None:
        """Scope precomputed diagnostic points without recalculating indicators."""
        if not isinstance(raw, dict):
            return None
        result = dict(raw)
        for bucket in ("overlays", "diagnostics"):
            scoped = cls._chart_overlays(raw.get(bucket), visible_times)
            raw_series = raw.get(bucket)
            if isinstance(raw_series, list):
                retained = {str(item.get("key")) for item in scoped}
                for item in raw_series:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "threshold"
                        and str(item.get("key")) not in retained
                    ):
                        scoped.append({**item, "points": []})
            result[bucket] = scoped
        return result

    def backtest_run_chart(
        self, run_id: str, trade_index: int, owner_id: str
    ) -> dict[str, object]:
        """Build one lazy chart scope without duplicating bars in saved results."""
        item = self.backtest_repository.backtest_run(run_id, owner_id)
        if item is None:
            raise ResourceNotFoundError("找不到回測紀錄")
        result = item.get("result")
        trades = result.get("trades") if isinstance(result, dict) else None
        if not isinstance(trades, list) or not 0 <= trade_index < len(trades):
            raise BadRequestError("交易序號超出回測結果範圍")
        selected_trade = trades[trade_index]
        if not isinstance(selected_trade, dict):
            raise BadRequestError("回測交易資料格式錯誤")

        contract = str(selected_trade.get("contract") or "")
        if not contract:
            raise BadRequestError("回測交易缺少合約資訊")
        interval = str(item["interval"])
        display_interval = "1m" if interval == "multi" else interval
        try:
            validate_timeframe(display_interval)
        except ValueError as exc:
            raise BadRequestError("回測紀錄使用不支援的 K 棒週期") from exc

        range_mode = display_interval in {"1d", "1w"}
        start = date.fromisoformat(str(item["start_date"]))
        end = date.fromisoformat(str(item["end_date"]))
        selected_date = str(selected_trade.get("trading_date") or "")
        query_start = start
        query_end = end
        if not range_mode and selected_date:
            query_start = query_end = date.fromisoformat(selected_date)
        source_bars = self.market_repository.between_trading_dates(
            str(item["symbol"]), query_start, query_end
        )
        if range_mode:
            scoped_source = [bar for bar in source_bars if bar.contract == contract]
            scoped_trades = [
                (index, trade) for index, trade in enumerate(trades)
                if isinstance(trade, dict)
                and str(trade.get("contract") or "") == contract
            ]
            scope = {
                "key": f"range:{display_interval}:{contract}",
                "kind": "range",
                "label": f"{item['start_date']} ～ {item['end_date']}",
                "interval": display_interval,
                "contract": contract,
            }
        else:
            session, trading_date = self._trade_session(
                selected_trade, source_bars
            )
            scoped_source = [
                bar for bar in source_bars
                if bar.contract == contract
                and bar.session == session
                and bar.trading_date.isoformat() == trading_date
            ]
            scoped_trades = []
            for index, trade in enumerate(trades):
                if (
                    not isinstance(trade, dict)
                    or str(trade.get("contract") or "") != contract
                    or (
                        trade.get("trading_date")
                        and str(trade["trading_date"]) != trading_date
                    )
                ):
                    continue
                trade_session, trade_date = self._trade_session(trade, source_bars)
                if trade_session == session and trade_date == trading_date:
                    scoped_trades.append((index, trade))
            scope = {
                "key": f"session:{display_interval}:{contract}:{trading_date}:{session}",
                "kind": "session",
                "label": f"{trading_date} · {'日盤' if session == 'day' else '夜盤'}",
                "interval": display_interval,
                "contract": contract,
                "trading_date": trading_date,
                "session": session,
            }
        bars = aggregate_kbars(scoped_source, display_interval)
        if not bars:
            raise ResourceNotFoundError("這筆交易的歷史 K 棒已不存在")
        visible_times = {
            bar.time.isoformat(timespec="milliseconds")[:16] for bar in bars
        }
        payload = {
            "scope": scope,
            "bars": [self._chart_bar(bar) for bar in bars],
            "trades": [
                {**trade, "trade_index": index}
                for index, trade in scoped_trades
            ],
            "overlays": self._chart_overlays(
                result.get("overlays", []), visible_times
            ),
        }
        chart_visualization = self._chart_visualization(
            result.get("visualization"), visible_times
        )
        if chart_visualization is not None:
            payload["visualization"] = chart_visualization
        return payload

    def delete_backtest_run(
        self, run_id: str, owner_id: str
    ) -> dict[str, object]:
        item = self.backtest_repository.delete_backtest_run(run_id, owner_id)
        if item is None:
            raise ResourceNotFoundError("找不到回測紀錄")
        return {
            "deleted_run_id": run_id,
            "strategy_key": item["strategy_key"],
            "strategy_version": item["strategy_version"],
            "released_strategy_reference": item[
                "released_strategy_reference"
            ],
        }

    def delete_backtest_runs(
        self,
        run_ids: list[str],
        delete_all: bool,
        owner_id: str,
        exclude_run_ids: list[str] | None = None,
    ) -> dict[str, object]:
        normalized = list(dict.fromkeys(item.strip() for item in run_ids if item.strip()))
        excluded = list(dict.fromkeys(
            item.strip() for item in (exclude_run_ids or []) if item.strip()
        ))
        if delete_all and normalized:
            raise BadRequestError("全部刪除不可同時指定個別回測紀錄")
        if not delete_all and excluded:
            raise BadRequestError("排除紀錄僅適用於全部刪除")
        if not delete_all and not normalized:
            raise BadRequestError("至少需要選擇一筆回測紀錄")
        result = self.backtest_repository.delete_backtest_runs(
            normalized, delete_all, owner_id, excluded
        )
        if result is None:
            raise ResourceNotFoundError(
                "部分回測紀錄不存在，未刪除任何資料"
            )
        return result

    def _session(self, session_id: str, owner_id: str):
        try:
            return self.replay_sessions.get(session_id, owner_id)
        except ReplaySessionNotFound as exc:
            raise ResourceNotFoundError("找不到回放交易 Session") from exc

    def _require_symbol(self, symbol: str) -> None:
        if symbol.upper() != self.symbol:
            raise ResourceNotFoundError("unsupported symbol")

    @staticmethod
    def _bar_message(bar) -> dict[str, object]:
        return {
            "time": bar.time.isoformat(timespec="milliseconds"),
            "end_time": bar.exchange_time.isoformat(timespec="milliseconds"),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "contract": bar.contract,
            "session": bar.session,
            "trading_date": bar.trading_date.isoformat(),
            "no_trade": bar.no_trade,
        }
