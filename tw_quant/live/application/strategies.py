from __future__ import annotations

from ...strategy_registry import (
    get_strategy_services,
    supported_strategies,
    default_composite_definition,
    generate_composite_signals,
    new_composite_id,
    strategy_catalog,
    validate_composite_definition,
    validate_composite_dependencies,
    validate_strategy_parameters,
)
from ..storage import (
    MarketRepository,
    StrategyRepository,
    StrategyNameConflictError,
    StrategyPurgeError,
    StrategyReferencedError,
)
from .errors import (
    InvalidInputError,
    ResourceConflictError,
    ResourceGoneError,
    ResourceNotFoundError,
)


class StrategyApplicationService:
    """Owner-scoped strategy use cases, independent from FastAPI."""

    def __init__(
        self,
        repository: StrategyRepository,
        market_repository: MarketRepository,
        symbol: str,
    ):
        self.repository = repository
        self.market_repository = market_repository
        self.symbol = symbol

    def catalog(self, owner_id: str) -> dict[str, object]:
        return {
            "strategies": strategy_catalog(
                self.repository.strategy_parameters(owner_id)
            )
        }

    def update_parameters(
        self, strategy: str, raw_parameters: dict[str, object], owner_id: str
    ) -> dict[str, object]:
        key = strategy.lower()
        if key not in supported_strategies():
            raise ResourceNotFoundError("unsupported strategy")
        try:
            parameters = validate_strategy_parameters(key, raw_parameters)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        version = self.repository.save_strategy_parameters(
            key,
            parameters,
            owner_id,
            get_strategy_services().identity_snapshot(),
        )
        result = next(
            item
            for item in strategy_catalog(
                self.repository.strategy_parameters(owner_id)
            )
            if item["key"] == key
        )
        if version is not None:
            result["runtime_data_version"] = version["version"]
            result["strategy_lineage"] = version["lineage"]
        return result

    def composites(self, owner_id: str) -> dict[str, object]:
        active = self.repository.composite_strategies(owner_id)
        return {
            "template": default_composite_definition(),
            "strategies": active,
            "archived_strategies": (
                self.repository.archived_composite_strategies(owner_id)
            ),
            "reference_strategies": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "versions": [
                        {
                            "version": version["version"],
                            "name": version["name"],
                        }
                        for version in self.repository.composite_strategy_versions(
                            str(item["id"]), owner_id
                        )
                    ],
                }
                for item in active
            ],
        }

    def composite_versions(
        self, strategy_id: str, owner_id: str
    ) -> dict[str, object]:
        versions = self.repository.composite_strategy_versions(
            strategy_id, owner_id
        )
        if not versions:
            raise ResourceNotFoundError("找不到組合策略")
        return {
            "id": strategy_id,
            "archived": self.repository.composite_strategy_archived(
                strategy_id, owner_id
            ),
            "versions": versions,
        }

    def composite(
        self, strategy_id: str, version: int | None, owner_id: str
    ) -> dict[str, object]:
        item = self.repository.composite_strategy(
            strategy_id, version, owner_id
        )
        if item is None:
            raise ResourceNotFoundError("找不到組合策略版本")
        return item

    def composite_signals(
        self,
        strategy_id: str,
        version: int | None,
        symbol: str,
        limit: int,
        owner_id: str,
    ) -> dict[str, object]:
        self._require_symbol(symbol)
        item = self.composite(strategy_id, version, owner_id)
        signals, trace = generate_composite_signals(
            self.market_repository.latest(self.symbol, limit),
            item["definition"],
        )
        return {
            "id": item["id"],
            "version": item["version"],
            "name": item["name"],
            "signals": signals,
            "trace": trace,
        }

    def create_composite(
        self, raw: dict[str, object], owner_id: str
    ) -> dict[str, object]:
        strategy_id = new_composite_id()
        definition = self._validated_definition(raw, owner_id, strategy_id)
        try:
            return self.repository.save_composite_strategy(
                strategy_id,
                definition,
                owner_id,
                get_strategy_services().identity_snapshot(),
            )
        except StrategyNameConflictError as exc:
            raise ResourceConflictError(str(exc)) from exc

    def update_composite(
        self, strategy_id: str, raw: dict[str, object], owner_id: str
    ) -> dict[str, object]:
        current = self.repository.composite_strategy(
            strategy_id, owner_user_id=owner_id
        )
        if current is None:
            raise ResourceNotFoundError("找不到組合策略")
        if self.repository.composite_strategy_archived(strategy_id, owner_id):
            raise ResourceGoneError("組合策略已封存")
        candidate_name = str(raw.get("name", "")).strip()
        target_id = (
            new_composite_id()
            if candidate_name != current["name"]
            else strategy_id
        )
        definition = self._validated_definition(raw, owner_id, target_id)
        try:
            saved = self.repository.save_composite_strategy(
                target_id,
                definition,
                owner_id,
                get_strategy_services().identity_snapshot(),
            )
        except StrategyNameConflictError as exc:
            raise ResourceConflictError(str(exc)) from exc
        if target_id != strategy_id:
            saved["created_from_strategy_id"] = strategy_id
        return saved

    def archive_composite(
        self, strategy_id: str, owner_id: str
    ) -> dict[str, object]:
        try:
            return self.repository.archive_composite_strategy(
                strategy_id, owner_id
            )
        except ValueError as exc:
            raise ResourceNotFoundError(str(exc)) from exc

    def purge_composites(
        self, strategy_ids: list[str], owner_id: str
    ) -> dict[str, object]:
        try:
            return self.repository.purge_archived_composite_strategies(
                strategy_ids, owner_id
            )
        except (StrategyReferencedError, StrategyPurgeError) as exc:
            raise ResourceConflictError(str(exc)) from exc

    def _validated_definition(
        self, raw: dict[str, object], owner_id: str, strategy_id: str
    ) -> dict[str, object]:
        def resolve_child(
            child_id: str, child_version: int
        ) -> dict[str, object] | None:
            child = self.repository.composite_strategy(
                child_id, child_version, owner_user_id=owner_id
            )
            if child is None:
                return None
            if self.repository.composite_strategy_archived(
                child_id, owner_id
            ):
                raise ValueError(
                    f"封存策略不可加入新的組合："
                    f"{child['name']} v{child_version}"
                )
            return child

        try:
            candidate = dict(raw)
            name = str(candidate.get("name", "")).strip()
            if not name or len(name) > 80:
                raise ValueError("composite name is required and must be at most 80 characters")
            current = self.repository.composite_strategy(strategy_id, owner_user_id=owner_id)
            candidate.update(name=name, composite_id=strategy_id, composite_version=(int(current["version"]) + 1 if current else 1))
            definition = validate_composite_definition(
                candidate,
                self.repository.strategy_parameters(owner_id),
                composite_resolver=resolve_child,
            )
            validate_composite_dependencies(definition, strategy_id)
            return definition
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc

    def _require_symbol(self, symbol: str) -> None:
        if symbol.upper() != self.symbol:
            raise ResourceNotFoundError("unsupported symbol")
