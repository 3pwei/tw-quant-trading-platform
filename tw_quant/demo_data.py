from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def generate_demo_bars(
    symbol: str = "2330",
    start_date: str = "2026-07-06",
    trading_days: int = 10,
    seed: int = 42,
) -> pd.DataFrame:
    """建立可重現的合成 1 分 K，只用於驗證系統流程，不代表真實行情。"""

    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start_date, periods=trading_days, tz="Asia/Taipei")
    rows: list[dict[str, object]] = []
    previous_close = 980.0

    for day_number, day in enumerate(days):
        timestamps = pd.date_range(
            day.normalize() + pd.Timedelta(hours=9),
            day.normalize() + pd.Timedelta(hours=13, minutes=30),
            freq="1min",
        )
        opening_gap = rng.normal(0, 0.002)
        price = previous_close * (1 + opening_gap)
        trend_sign = 1 if day_number % 3 == 0 else (-1 if day_number % 3 == 1 else 0)

        for minute, timestamp in enumerate(timestamps):
            open_price = price
            noise = rng.normal(0, 0.00018)
            drift = 0.0
            if 18 <= minute <= 80:
                drift = trend_sign * 0.00045
            elif 81 <= minute <= 150:
                drift = trend_sign * 0.00008
            close_price = max(1.0, open_price * (1 + noise + drift))
            wick = abs(rng.normal(0.00016, 0.00005))
            high = max(open_price, close_price) * (1 + wick)
            low = min(open_price, close_price) * (1 - wick)
            base_volume = 450 + rng.integers(0, 350)
            volume = int(base_volume * (2.4 if 18 <= minute <= 30 else 1.0))
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "open": round(open_price, 4),
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "close": round(close_price, 4),
                    "volume": volume,
                }
            )
            price = close_price
        previous_close = price
    return pd.DataFrame(rows)


def save_demo_csv(path: str | Path, **kwargs) -> pd.DataFrame:
    bars = generate_demo_bars(**kwargs)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    bars.to_csv(target, index=False)
    return bars

