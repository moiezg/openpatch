from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class PumpError(Exception):
    pass


@dataclass(frozen=True)
class DeliveryRecord:
    time: float
    units: float
    kind: str   # "basal" | "bolus" | "prime" (prime fills the tubing and never reaches the body)


@dataclass
class PumpStatus:
    minute: float
    reservoir_u: float
    battery_pct: int
    suspended: bool
    temp_rate: float | None
    temp_remaining_min: int
    delivered_last_hour: float
    bolus_remaining: float
    alarms: list[str] = field(default_factory=list)     # delivery-stopping conditions
    warnings: list[str] = field(default_factory=list)   # informational (low reservoir, low battery)
    state: str = "running"


class PumpDriver(ABC):
    @abstractmethod
    def status(self) -> PumpStatus: ...

    @abstractmethod
    def set_temp_basal(self, rate_uph: float, duration_min: int): ...

    @abstractmethod
    def cancel_temp_basal(self): ...

    @abstractmethod
    def bolus(self, units: float): ...

    @abstractmethod
    def delivery_history(self, since: float) -> list[DeliveryRecord]: ...

    def clear_alarms(self):
        raise PumpError("not supported by this pump")
