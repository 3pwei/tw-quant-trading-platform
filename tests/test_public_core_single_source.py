from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADOPTED_MODULES = (
    ROOT / "tw_quant/events/models.py",
    ROOT / "tw_quant/market/models.py",
    ROOT / "tw_quant/broker/capabilities.py",
    ROOT / "tw_quant/broker/identity.py",
    ROOT / "tw_quant/broker/instruments.py",
    ROOT /     ROOT / "tw_quant/execution/policy.py",
    ROOT / "tw_quant/execution/position_ledger.py",
    ROOT / "tw_quant/risk/engine.py",
)
RETIRED_MODULES = (
    ROOT / "tw_quant/events/_legacy_models.py",
    ROOT / "tw_quant/market/_legacy_models.py",
    ROOT / "tw_quant/broker/_legacy_capabilities.py",
    ROOT / "tw_quant/broker/_legacy_identity.py",
    ROOT / "tw_quant/broker/_legacy_instruments.py",
    ROOT / "tw_quant/strategy/_legacy_runtime.py",
    ROOT / "tw_quant/execution/_legacy_policy.py",
    ROOT / "tw_quant/execution/_legacy_position_ledger.py",
    ROOT / "tw_quant/risk/_legacy_engine.py",
)
RETIRED_IMPORTS = tuple(
    str(path.relative_to(ROOT).with_suffix("")).replace("/", ".")
    for path in RETIRED_MODULES
)


class PublicCoreSingleSourceTests(unittest.TestCase):
    def test_adopted_compatibility_paths_are_thin_public_core_exports(self) -> None:
        for path in ADOPTED_MODULES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn("from tw_quant_core.", source)
                self.assertIn("USING_PUBLIC_CORE = True", source)
                self.assertNotIn("TW_QUANT_CORE_ROLLBACK", source)
                self.assertNotIn("ModuleNotFoundError", source)

    def test_retired_generic_implementations_and_imports_are_absent(self) -> None:
        for path in RETIRED_MODULES:
            self.assertFalse(path.exists(), path.relative_to(ROOT))

        python_sources = {
            path: path.read_text(encoding="utf-8")
            for path in ROOT.rglob("*.py")
            if ".venv" not in path.parts
        }
        for module in RETIRED_IMPORTS:
            matches = [
                str(path.relative_to(ROOT))
                for path, source in python_sources.items()
                if module in source
            ]
            self.assertEqual(matches, [], f"stale imports for {module}")



if __name__ == "__main__":
    unittest.main()
