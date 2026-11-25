from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CarbEntry:
    time: float
    grams: float
    absorption_min: float
    cr: float  # carb ratio in force when the meal was entered


def absorbed_fraction(entry: CarbEntry, t: float) -> float:
    # Triangular absorption rate: slow start, peak at half-time, slow tail
    x = (t - entry.time) / entry.absorption_min
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return 2 * x * x if x < 0.5 else 1 - 2 * (1 - x) ** 2


def carbs_on_board(entries: list[CarbEntry], now: float) -> float:
    return sum(e.grams * (1 - absorbed_fraction(e, now)) for e in entries if e.time <= now)


def carb_effect(entries: list[CarbEntry], t0: float, t1: float, isf: float) -> float:
    # BG rise (mg/dL) from carbs absorbed between t0 and t1
    rise = 0.0
    for e in entries:
        rise += (isf / e.cr) * e.grams * (absorbed_fraction(e, t1) - absorbed_fraction(e, t0))
    return rise
