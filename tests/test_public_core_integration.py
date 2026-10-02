from __future__ import annotations

import importlib.metadata
import os
from pathlib import PurePosixPath
import subprocess
import sys
import unittest


@unittest.skipUnless(
    True,
    "Public Core is installed only by the dedicated integration job",
)
class PublicCoreIntegrationTests(unittest.TestCase):
    def test_private_and_public_packages_coexist_at_v1(self) -> None:
        import tw_quant
        import tw_quant_core

        self.assertNotEqual(tw_quant.__name__, tw_quant_core.__name__)
        self.assertEqual(importlib.metadata.version("tw-quant-core"), "1.2.0")
        self.assertEqual(tw_quant_core.__version__, "1.2.0")

    def test_selected_runtime_contracts_are_public_core_types(self) -> None:
        from tw_quant.broker.capabilities import BrokerCapabilities as PlatformCapabilities
        from tw_quant.broker.identity import BrokerAccountRef as PlatformAccountRef
        from tw_quant.broker.instruments import BrokerInstrumentMapper as PlatformInstrumentMapper
        from tw_quant.events.models import EventMetadata as PlatformEventMetadata
        from tw_quant.market.models import KBar as PlatformKBar
        from tw_quant.market.models import TickEvent as PlatformTickEvent
        from tw_quant_core.broker import (
            BrokerAccountRef,
            BrokerCapabilities,
            BrokerInstrumentMapper,
        )
        from tw_quant_core.events import EventMetadata
        from tw_quant_core.market import KBar, TickEvent

        self.assertIs(PlatformAccountRef, BrokerAccountRef)
        self.assertIs(PlatformCapabilities, BrokerCapabilities)
        self.assertIs(PlatformInstrumentMapper, BrokerInstrumentMapper)
        self.assertIs(PlatformEventMetadata, EventMetadata)
        self.assertIs(PlatformKBar, KBar)
        self.assertIs(PlatformTickEvent, TickEvent)

    def test_retired_environment_flag_cannot_restore_legacy_contracts(self) -> None:
        env = os.environ.copy()
        env["TW_QUANT_CORE_ROLLBACK"] = "1"
        command = (
            "from tw_quant.broker.identity import BrokerAccountRef; "
            "from tw_quant.events.models import EventMetadata; "
            "from tw_quant.market.models import KBar; "
            "assert BrokerAccountRef.__module__.startswith('tw_quant_core.'); "
            "assert EventMetadata.__module__.startswith('tw_quant_core.'); "
            "assert KBar.__module__.startswith('tw_quant_core.')"
        )
        subprocess.run([sys.executable, "-c", command], env=env, check=True)

    def test_public_distribution_does_not_own_private_namespace(self) -> None:
        distribution = importlib.metadata.distribution("tw-quant-core")
        files = tuple(distribution.files or ())
        owned_paths = {PurePosixPath(str(path)).parts[0] for path in files if path.parts}

        self.assertIn("tw_quant_core", owned_paths)
        self.assertNotIn("tw_quant", owned_paths)


if __name__ == "__main__":
    unittest.main()
