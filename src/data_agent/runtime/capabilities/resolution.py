from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class CapabilityResolutionConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ValueResolutionTarget:
    table: str
    column: str
    period_column: str | None = None
    output_column: str | None = None


class CapabilityResolutionRegistry:
    def __init__(self, targets: dict[str, ValueResolutionTarget]) -> None:
        self._targets = dict(targets)

    def get(self, semantic_type: str) -> ValueResolutionTarget | None:
        return self._targets.get(semantic_type)

    @classmethod
    def load(cls, path: str | Path) -> CapabilityResolutionRegistry:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise CapabilityResolutionConfigError(
                f"Unable to load capability value-resolution config: {source}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise CapabilityResolutionConfigError("Capability resolution config requires version 1.")
        values = raw.get("types")
        if not isinstance(values, dict):
            raise CapabilityResolutionConfigError("Capability resolution config requires 'types'.")
        targets: dict[str, ValueResolutionTarget] = {}
        for semantic_type, value in values.items():
            if (
                not isinstance(semantic_type, str)
                or not semantic_type
                or not isinstance(value, dict)
                or not isinstance(value.get("table"), str)
                or not value["table"]
                or not isinstance(value.get("column"), str)
                or not value["column"]
            ):
                raise CapabilityResolutionConfigError(
                    f"Invalid capability resolution mapping: {semantic_type!r}."
                )
            period_column: Any = value.get("period_column")
            if period_column is not None and (
                not isinstance(period_column, str) or not period_column
            ):
                raise CapabilityResolutionConfigError(
                    f"Invalid period_column for capability resolution: {semantic_type}."
                )
            output_column: Any = value.get("output_column")
            if output_column is not None and (
                not isinstance(output_column, str) or not output_column
            ):
                raise CapabilityResolutionConfigError(
                    f"Invalid output_column for capability resolution: {semantic_type}."
                )
            targets[semantic_type] = ValueResolutionTarget(
                table=value["table"],
                column=value["column"],
                period_column=period_column,
                output_column=output_column,
            )
        return cls(targets)


__all__ = [
    "CapabilityResolutionConfigError",
    "CapabilityResolutionRegistry",
    "ValueResolutionTarget",
]
