from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Protocol

from tw_quant_core.market.quotes import ExecutionQuote


class ExecutionQuoteSink(Protocol):
    def save(self, quote: ExecutionQuote) -> None: ...


class SQLiteExecutionQuoteRepository:
    """Latest canonical BidAsk shared across isolated application processes."""

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS live_execution_quotes ("
                "symbol TEXT NOT NULL, contract TEXT NOT NULL, best_bid REAL, "
                "best_ask REAL, last_price REAL, exchange_time TEXT NOT NULL, "
                "received_at TEXT NOT NULL, source TEXT NOT NULL, "
                "PRIMARY KEY(symbol, contract))"
            )
            self.connection.commit()

    def save(self, quote: ExecutionQuote) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO live_execution_quotes VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,contract) DO UPDATE SET "
                "best_bid=excluded.best_bid,best_ask=excluded.best_ask,"
                "last_price=excluded.last_price,exchange_time=excluded.exchange_time,"
                "received_at=excluded.received_at,source=excluded.source "
                "WHERE excluded.received_at>=live_execution_quotes.received_at",
                (
                    quote.symbol, quote.contract, quote.best_bid, quote.best_ask,
                    quote.last_price, quote.exchange_time.isoformat(timespec="microseconds"),
                    quote.received_at.isoformat(timespec="microseconds"), quote.source,
                ),
            )
            self.connection.commit()

    @staticmethod
    def _quote(row: sqlite3.Row) -> ExecutionQuote:
        return ExecutionQuote(
            symbol=str(row["symbol"]), contract=str(row["contract"]),
            best_bid=float(row["best_bid"]) if row["best_bid"] is not None else None,
            best_ask=float(row["best_ask"]) if row["best_ask"] is not None else None,
            last_price=float(row["last_price"]) if row["last_price"] is not None else None,
            exchange_time=datetime.fromisoformat(str(row["exchange_time"])),
            received_at=datetime.fromisoformat(str(row["received_at"])),
            source=str(row["source"]),
        )

    def get(self, symbol: str, contract: str) -> ExecutionQuote | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_execution_quotes WHERE symbol=? AND contract=?",
                (symbol, contract),
            ).fetchone()
        return self._quote(row) if row else None

    def close(self) -> None:
        with self.lock:
            self.connection.close()
