from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..auth import AccountStatus, Role, TradingMode


ShortIdentifier = Annotated[str, Field(min_length=1, max_length=80)]
EmailAddress = Annotated[str, Field(min_length=3, max_length=254)]


class StrategyParametersUpdate(BaseModel):
    parameters: dict[str, object] = Field(max_length=50)


class TradingRuntimeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_kind: Literal["atomic", "composite"] = "atomic"
    strategy_id: ShortIdentifier
    strategy_version: int | None = Field(default=None, ge=1)
    symbol: Annotated[str, Field(min_length=1, max_length=32)] = "TMF"
    contract: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    interval: Annotated[str, Field(min_length=1, max_length=16)] = "1m"
    quantity: int = Field(default=1, ge=1, le=100)
    mode: Literal["observe", "paper_auto", "live_shadow", "live_auto"] = "observe"
    execution_target_id: Annotated[str, Field(max_length=80)] | None = None


class CompositeStrategyUpdate(BaseModel):
    definition: dict[str, object] = Field(max_length=20)


class CompositeStrategyPurge(BaseModel):
    strategy_ids: list[ShortIdentifier] = Field(min_length=1, max_length=100)


class BacktestExecutionRequest(BaseModel):
    symbol: Annotated[str, Field(min_length=1, max_length=32)] = "TMF"
    strategy: ShortIdentifier
    interval: Annotated[str, Field(min_length=1, max_length=16)] = "1m"
    start: date
    end: date
    version: int | None = None


class BacktestRunPurge(BaseModel):
    run_ids: list[ShortIdentifier] = Field(default_factory=list, max_length=500)
    delete_all: bool = False
    exclude_run_ids: list[ShortIdentifier] = Field(default_factory=list, max_length=500)


class ReplayPrepareRequest(BaseModel):
    symbol: Annotated[str, Field(min_length=1, max_length=32)] = "TMF"
    trading_date: date
    session: Literal["day", "night"] = "day"
    interval: Annotated[str, Field(min_length=1, max_length=16)] = "1m"
    strategies: list[ShortIdentifier] = Field(min_length=1, max_length=3)


class ReplayCursorUpdate(BaseModel):
    cursor: int


class AdminUserCreate(BaseModel):
    email: EmailAddress
    role: Role = Role.RESEARCHER
    status: AccountStatus = AccountStatus.ACTIVE
    trading_mode: TradingMode = TradingMode.DISABLED


class AdminUserUpdate(BaseModel):
    role: Role
    status: AccountStatus
    trading_mode: TradingMode


class PaperOrderCreate(BaseModel):
    strategy_id: ShortIdentifier = "manual"
    strategy_version: int = 1
    side: Literal["buy", "sell"]
    quantity: int = 1
    stop_loss_price: float | None = None
    reduce_only: bool = False


class PaperControlRequest(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class LiveCanaryArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Annotated[str, Field(min_length=1, max_length=80)]


class LiveAutoArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["ARM LIVE AUTO - REAL MONEY"]


class ManualLiveOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["buy", "sell"]
    quantity: int = Field(default=1, ge=1, le=1)
    confirmation: Annotated[str, Field(min_length=1, max_length=120)]


class LiveCanaryActionConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["REAL ORDER"]


class LiveCanaryKillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["halt_entry", "cancel_working", "flatten"]
    reason: Annotated[str, Field(min_length=3, max_length=500)]
    confirmation: Literal["REAL ORDER"]
