import unittest

import pandas as pd

from tw_quant_core.data import BarDataError, validate_bars


def valid_frame():
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-02 09:00", periods=2, freq="1min", tz="Asia/Taipei"),
            "symbol": ["2330", "2330"],
            "open": [100.0, 100.5],
            "high": [101.0, 101.0],
            "low": [99.5, 100.0],
            "close": [100.5, 100.8],
            "volume": [1000, 1200],
        }
    )


class DataValidationTests(unittest.TestCase):
    def test_valid_bars(self):
        validate_bars(valid_frame())

    def test_rejects_invalid_high(self):
        bars = valid_frame()
        bars.loc[0, "high"] = 99.0
        with self.assertRaises(BarDataError):
            validate_bars(bars)


if __name__ == "__main__":
    unittest.main()

