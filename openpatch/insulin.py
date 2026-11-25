from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Dose:
    time: float   # minutes
    units: float  # negative values appear in net-of-scheduled-basal accounting


class InsulinCurve:
    # Exponential insulin action model (same family as oref0 / Loop):
    # fraction of a dose still active `age` minutes after delivery.

    def __init__(self, peak_min: float = 75.0, dia_min: float = 360.0):
        if not 0 < peak_min < dia_min / 2:
            raise ValueError("peak must be positive and less than half of DIA")
        self.peak = peak_min
        self.dia = dia_min
        tau = peak_min * (1 - peak_min / dia_min) / (1 - 2 * peak_min / dia_min)
        a = 2 * tau / dia_min
        self._tau = tau
        self._a = a
        self._s = 1 / (1 - a + (1 + a) * math.exp(-dia_min / tau))
        self._iob_table = [self._iob_exact(t) for t in range(int(dia_min) + 2)]

    def _iob_exact(self, t: float) -> float:
        if t >= self.dia:
            return 0.0
        tau, a, s, td = self._tau, self._a, self._s, self.dia
        return 1 - s * (1 - a) * ((t * t / (tau * td * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1)

    def iob_fraction(self, age: float) -> float:
        if age <= 0:
            return 1.0
        if age >= self.dia:
            return 0.0
        i = int(age)
        lo, hi = self._iob_table[i], self._iob_table[i + 1]
        return lo + (hi - lo) * (age - i)

    def activity(self, age: float) -> float:
        # fraction of the dose acting per minute
        if age <= 0 or age >= self.dia:
            return 0.0
        tau, s, td = self._tau, self._s, self.dia
        return (s / (tau * tau)) * age * (1 - age / td) * math.exp(-age / tau)


def insulin_on_board(doses: list[Dose], now: float, curve: InsulinCurve) -> float:
    return sum(d.units * curve.iob_fraction(now - d.time) for d in doses if d.time <= now)


def insulin_effect(doses: list[Dose], t0: float, t1: float, isf: float, curve: InsulinCurve) -> float:
    # BG change (mg/dL) caused by these doses between t0 and t1
    acting = 0.0
    for d in doses:
        acting += d.units * (curve.iob_fraction(t0 - d.time) - curve.iob_fraction(t1 - d.time))
    return -isf * acting
