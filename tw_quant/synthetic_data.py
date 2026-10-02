"""Repository-owned deterministic test ticks; no exchange/user/runtime data.

This arithmetic sequence is test data, not a strategy or market sample. The
caller owns the output path/lifetime. No CSV payload is distributed with source.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path


def write_ticks(path: Path, *, count: int = 120, symbol: str = "TMF", contract: str = "TMFR1") -> Path:
    if not 1 <= count <= 10000:
        raise ValueError("synthetic tick count outside test bounds")
    anchor = datetime(2000, 1, 3, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("exchange_time", "symbol", "contract", "price", "volume", "sequence"), lineterminator="\n")
        writer.writeheader()
        for index in range(count):
            writer.writerow({"exchange_time": (anchor + timedelta(seconds=10*index)).isoformat(),
                             "symbol": symbol, "contract": contract,
                             "price": 100 + index % 5, "volume": 1, "sequence": index})
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_ticks(args.output)


if __name__ == "__main__":
    main()
