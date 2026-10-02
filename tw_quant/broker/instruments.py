"""Stable Private import path and fail-closed Production instrument mapper."""

from __future__ import annotations

from dataclasses import dataclass

from tw_quant_core.broker import BrokerInstrumentMapper


@dataclass(frozen=True)
class CanonicalInstrument:
    symbol: str
    contract: str

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.contract.strip():
            raise ValueError("canonical symbol and contract are required")


class LockedInstrumentMapper:
    """Fail-closed mapper used until a production adapter supplies one."""

    def to_broker_contract(self, instrument: CanonicalInstrument) -> str:
        raise RuntimeError("production instrument mapping is not implemented")

    def to_canonical_instrument(
        self, broker_contract: str
    ) -> CanonicalInstrument:
        raise RuntimeError("production instrument mapping is not implemented")


USING_PUBLIC_CORE = True

__all__ = [
    "BrokerInstrumentMapper", "CanonicalInstrument", "LockedInstrumentMapper",
    "USING_PUBLIC_CORE",
]
