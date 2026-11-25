from __future__ import annotations

import math
from dataclasses import dataclass

from .insulin import InsulinCurve
from .predict import clamp_bg

# Lows are acutely dangerous, highs are chronically harmful: lows cost more
LOW_RISK_WEIGHT = 2.0


def bg_risk(bg: float, target: float) -> float:
    # Kovatchev BG risk function, re-centred so its minimum sits at `target`
    scaled = max(bg, 20.0) * 112.5 / target
    f = 1.509 * (math.log(scaled) ** 1.084 - 5.381)
    r = 10 * f * f
    return r * LOW_RISK_WEIGHT if f < 0 else r


@dataclass
class Plan:
    rate: float
    prediction: list[float]
    cost: float


class MPCController:
    # Receding-horizon controller: try every temp basal rate the pump can deliver, hold it for
    # `hold_min`, predict BG over the horizon, pick the lowest-risk rate. Re-plans every cycle.

    def __init__(self, curve: InsulinCurve, increment: float = 0.05, horizon_min: int = 240,
                 step_min: int = 5, hold_min: int = 30, rate_penalty: float = 2.0):
        self.curve = curve
        self.increment = increment
        self.horizon_min = horizon_min
        self.step_min = step_min
        self.hold_min = hold_min
        self.rate_penalty = rate_penalty

    def unit_response(self, isf: float) -> list[float]:
        # BG change at each prediction step from running +1 U/h above schedule for hold_min
        out = []
        chunks = self.hold_min // 5
        for k in range(1, self.horizon_min // self.step_min + 1):
            t = k * self.step_min
            acted = 0.0
            for c in range(chunks):
                acted += (5 / 60) * (1 - self.curve.iob_fraction(t - (c * 5 + 2.5)))
            out.append(-isf * acted)
        return out

    def plan(self, scenarios: list[tuple[float, list[float]]], isf: float, scheduled_basal: float,
             max_rate: float, target: float) -> Plan:
        # scenarios: (weight, base prediction) pairs, e.g. carbs absorbing as expected vs. faster.
        # A rate that only looks good if the remaining carbs really arrive gets penalised.
        response = self.unit_response(isf)
        best = None
        steps = int(round(max_rate / self.increment))
        for i in range(steps + 1):
            rate = round(i * self.increment, 2)
            delta = rate - scheduled_basal
            cost = self.rate_penalty * delta * delta
            for weight, base in scenarios:
                cost += weight * sum(bg_risk(clamp_bg(b + delta * u), target) for b, u in zip(base, response))
            if best is None or cost < best.cost:
                best = Plan(rate, [clamp_bg(b + delta * u) for b, u in zip(scenarios[0][1], response)], cost)
        return best

    def prediction_for(self, base_prediction: list[float], isf: float, scheduled_basal: float,
                       rate: float) -> list[float]:
        delta = rate - scheduled_basal
        return [clamp_bg(b + delta * u) for b, u in zip(base_prediction, self.unit_response(isf))]
