from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Instrument:
    """Provider-neutral product identity used inside the platform."""

    symbol: str
    exchange: str
    asset_class: Literal["future", "equity", "option"]


@dataclass(frozen=True)
class MarketSubscription:
    """Canonical product plus provider-specific contract selector."""

    instrument: Instrument
    contract: str


TMF = Instrument(symbol="TMF", exchange="TAIFEX", asset_class="future")
