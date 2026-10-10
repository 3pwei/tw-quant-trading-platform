"""Anonymous Demo response privacy regressions."""
from __future__ import annotations

import json
import unittest

from tw_quant.live.demo_backtest import _public_catalog_item, _redact_demo_value


class DemoBoundaryTests(unittest.TestCase):
    def test_public_demo_redacts_parameters_composition_and_diagnostics(self):
        payload = {
            'case_id': 'public-case',
            'config': {
                'initial_capital': 100000, 'commission_per_side': 10,
                'slippage_points': 1, 'contract_multiplier': 10,
                'stop_loss_pct': 0.01, 'take_profit_pct': 0.02,
            },
            'visualization': {
                'strategy': {'name': 'Public demo'},
                'parameters': [{'key': 'secret', 'value': 42}],
                'diagnostics': [{'key': 'internal', 'points': [1]}],
                'overlays': [],
            },
            'composition': {'members': [{'strategy': 'private-alpha'}]},
            'summary': {'net_profit': 1},
        }
        result = _redact_demo_value(payload)
        encoded = json.dumps(result)
        for secret in ('secret', 'internal', 'private-alpha', 'stop_loss_pct',
                       'take_profit_pct', 'members', 'composition'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(result['visualization']['parameters'], [])
        self.assertEqual(result['visualization']['diagnostics'], [])
        self.assertEqual(result['config'], {
            'initial_capital': 100000, 'commission_per_side': 10,
            'slippage_points': 1, 'contract_multiplier': 10,
        })

    def test_catalog_is_allowlisted_but_keeps_public_display_fields(self):
        item = _public_catalog_item({
            'case_id': 'case', 'name': 'Public name', 'synthetic_data': True,
            'parameters': {'secret': 1}, 'private_note': 'hidden',
        })
        self.assertEqual(item, {
            'case_id': 'case', 'name': 'Public name', 'synthetic_data': True,
        })


if __name__ == '__main__':
    unittest.main()
