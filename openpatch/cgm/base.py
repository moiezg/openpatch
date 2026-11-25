from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    time: float   # minutes (simulation minutes, or epoch minutes in live mode)
    mgdl: float


class CGMSource(ABC):
    @abstractmethod
    def readings(self, now: float, lookback_min: float) -> list[Reading]:
        ...


class BufferedCGM(CGMSource):
    # Readings pushed in by a simulator (or a test)
    def __init__(self):
        self._readings: list[Reading] = []

    def push(self, time: float, mgdl: float):
        self._readings.append(Reading(time, mgdl))
        if len(self._readings) > 600:
            del self._readings[:100]

    def readings(self, now: float, lookback_min: float) -> list[Reading]:
        return [r for r in self._readings if now - lookback_min <= r.time <= now]
