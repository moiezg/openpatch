from __future__ import annotations

from .carbs import CarbEntry, carb_effect
from .insulin import Dose, InsulinCurve, insulin_effect

DEVIATION_DECAY_MIN = 60.0
MIN_BG, MAX_BG = 39.0, 400.0


def clamp_bg(v: float) -> float:
    return max(MIN_BG, min(MAX_BG, v))


def estimate_deviation(readings, net_doses: list[Dose], carbs: list[CarbEntry], isf: float,
                       curve: InsulinCurve, window_min: float = 15.0) -> float | None:
    # How fast BG is actually moving vs what insulin + logged carbs explain, per 5 minutes.
    # Positive = unexplained rise (unlogged food, stress, illness); negative = unexplained fall (exercise).
    if not readings:
        return None
    now = readings[-1].time
    pts = [r for r in readings if now - r.time <= window_min + 0.1]
    if len(pts) < 3:
        return None
    n = len(pts)
    mt = sum(r.time for r in pts) / n
    mv = sum(r.mgdl for r in pts) / n
    den = sum((r.time - mt) ** 2 for r in pts)
    if den == 0:
        return None
    actual_slope = sum((r.time - mt) * (r.mgdl - mv) for r in pts) / den
    t0 = pts[0].time
    span = now - t0
    if span <= 0:
        return None
    modeled = insulin_effect(net_doses, t0, now, isf, curve) + carb_effect(carbs, t0, now, isf)
    return (actual_slope - modeled / span) * 5.0


def predict_bg(now: float, bg: float, net_doses: list[Dose], carbs: list[CarbEntry], isf: float,
               curve: InsulinCurve, deviation_per5: float = 0.0, horizon_min: int = 240,
               step_min: int = 5, carb_scale: float = 1.0) -> list[float]:
    # Unclamped predicted BG at now+step, now+2*step, ... now+horizon
    out = []
    dev_total = 0.0
    active_now = [(d.units, curve.iob_fraction(now - d.time), d.time) for d in net_doses]
    active_now = [a for a in active_now if a[1] > 0]
    for k in range(1, horizon_min // step_min + 1):
        t = now + k * step_min
        decay = max(0.0, 1 - (k * step_min) / DEVIATION_DECAY_MIN)
        dev_total += deviation_per5 * decay * (step_min / 5.0)
        acting = sum(u * (f_now - curve.iob_fraction(t - dt)) for u, f_now, dt in active_now)
        out.append(bg - isf * acting + carb_scale * carb_effect(carbs, now, t, isf) + dev_total)
    return out
