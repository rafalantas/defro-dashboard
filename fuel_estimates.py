"""Typed, provenance-preserving fuel estimates for ThermoFlow 2.0.

This is a contract module only: it performs no calculations, I/O, caching or
selection. The legacy engine is not switched to this contract until parity
tests establish the existing source precedence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Any


class EstimateStatus(str, Enum):
    MEASURED = "measured"
    CALIBRATED = "calibrated"
    PROVISIONAL = "provisional"
    PREDICTED = "predicted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Estimate:
    """One estimate with explicit provenance and quality; value uses `unit`."""

    source: str
    unit: str
    status: EstimateStatus
    value: float | None
    window_start: str | None = None
    window_end: str | None = None
    generated_at: str | None = None
    uncertainty_percent: float | None = None
    coverage_percent: float | None = None
    calibration_regime: str | None = None
    model_version: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("estimate source must not be empty")
        if not self.unit.strip():
            raise ValueError("estimate unit must not be empty")
        if self.value is not None and not isfinite(float(self.value)):
            raise ValueError("estimate value must be finite")
        for name in ("uncertainty_percent", "coverage_percent"):
            item = getattr(self, name)
            if item is not None and (not isfinite(float(item)) or item < 0):
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.coverage_percent is not None and self.coverage_percent > 100:
            raise ValueError("coverage_percent must not exceed 100")
        if self.status == EstimateStatus.UNAVAILABLE and self.value is not None:
            raise ValueError("unavailable estimate must not contain a value")
        if self.status != EstimateStatus.UNAVAILABLE and self.value is None:
            raise ValueError("available estimate must contain a value")
        for name in ("window_start", "window_end", "generated_at"):
            item = getattr(self, name)
            if item is not None:
                datetime.fromisoformat(item.replace("Z", "+00:00"))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation with a plain status string."""
        result = asdict(self)
        result["status"] = self.status.value
        return result
