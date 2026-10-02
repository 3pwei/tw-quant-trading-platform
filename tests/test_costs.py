import unittest

from tw_quant_core.config import CostConfig
from tw_quant_core.costs import TaiwanStockCostModel
from tw_quant_core.futures_costs import FuturesCostConfig


class CostModelTests(unittest.TestCase):
    def test_sell_side_includes_tax(self):
        model = TaiwanStockCostModel(
            CostConfig(slippage_bps=0, round_cost_to_ntd=True)
        )
        buy = model.order_cost(100, 1_000, "buy")
        sell = model.order_cost(100, 1_000, "sell")
        self.assertEqual(buy.commission, 142)
        self.assertEqual(buy.tax, 0)
        self.assertEqual(sell.commission, 142)
        self.assertEqual(sell.tax, 150)

    def test_minimum_commission(self):
        model = TaiwanStockCostModel(CostConfig(slippage_bps=0))
        self.assertEqual(model.order_cost(10, 1, "buy").commission, 20)

    def test_futures_cost_configuration_rejects_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "multiplier"):
            FuturesCostConfig(multiplier=0)
        with self.assertRaisesRegex(ValueError, "slippage_points"):
            FuturesCostConfig(slippage_points=-1)
        with self.assertRaisesRegex(ValueError, "price and contracts"):
            FuturesCostConfig().side_cost(0, 1)


if __name__ == "__main__":
    unittest.main()
