"""Candidate-only CLI compatibility and fail-closed behavior tests."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from public_fixtures import InertProvider, PublicTestCase
from tw_quant.cli import main


class PublicCliTests(PublicTestCase):
    def test_no_provider_rejects_before_reading_input_or_writing_output(self):
        with patch("tw_quant.cli.load_bars") as loader, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["backtest", "--csv", "absent.csv", "--symbol", "TEST", "--contract", "TEST1", "--strategy", "scripted"])
            self.assertEqual(error.exception.code, 2)
            loader.assert_not_called()

    def test_catalog_is_empty_without_injection_and_scoped_with_provider(self):
        for provider, expected in ((None, 0), (InertProvider(), 1), (None, 0)):
            stream = io.StringIO()
            with redirect_stdout(stream): main(["catalog"], strategy_provider=provider)
            self.assertEqual(len(json.loads(stream.getvalue())["strategies"]), expected)

    def test_parameter_defaults_are_provider_owned(self):
        provider = InertProvider()
        provider.values["window"] = 42
        stream = io.StringIO()
        with redirect_stdout(stream): main(["catalog"], strategy_provider=provider)
        self.assertIn('42', stream.getvalue())

    def test_module_entrypoint_uses_public_cli_and_empty_registry(self):
        result = subprocess.run([sys.executable, "-m", "tw_quant", "catalog"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"strategies": []})

    def test_injected_hold_backtest_writes_only_runtime_results(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); csv = root / "bars.csv"; output = root / "result"
            csv.write_text("timestamp,symbol,open,high,low,close,volume\n2026-08-24T15:00:00+08:00,TEST,100,101,99,100,1\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["backtest", "--csv", str(csv), "--symbol", "TEST", "--contract", "TEST1", "--strategy", "scripted", "--output", str(output)], strategy_provider=InertProvider())
            self.assertEqual(json.loads((output / "summary.json").read_text())["trades"], 0)
            self.assertEqual({p.name for p in output.iterdir()}, {"bars.csv", "equity.csv", "trades.csv", "summary.json"})

    def test_unknown_key_and_invalid_schema_are_unavailable(self):
        for key, parameters in (("unknown", "{}"), ("scripted", '{"undeclared":1}')):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["backtest", "--csv", "absent.csv", "--symbol", "TEST", "--contract", "TEST1", "--strategy", key, "--parameters", parameters], strategy_provider=InertProvider())
            self.assertEqual(error.exception.code, 2)

    def test_demo_is_injected_and_no_builtin_case_is_available(self):
        from tw_quant.live.demo_backtest import DemoExecutionInput
        from public_fixtures import services
        provider = InertProvider()
        request = services().request("scripted", [], provider.values)
        class Demo:
            def case_execution_input(self, case_id):
                return DemoExecutionInput(case_id, "analyze", request)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["demo", "--case-id", "test-case"])
        self.assertEqual(error.exception.code, 2)
        stream = io.StringIO()
        with redirect_stdout(stream):
            main(["demo", "--case-id", "test-case"], strategy_provider=provider, demo_provider=Demo())
        self.assertEqual(json.loads(stream.getvalue())["case_id"], "test-case")
