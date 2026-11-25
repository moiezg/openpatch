from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import SafetySettings

MODE_CLOSED = "CLOSED"          # controller rate enacted (possibly clamped)
MODE_SUSPENDED = "SUSPENDED"    # zero temp basal because of current or predicted low
MODE_FALLBACK = "FALLBACK"      # no trustworthy CGM: scheduled basal only
MODE_BLOCKED = "BLOCKED"        # pump alarm: no commands sent


@dataclass
class SafetyContext:
    bg: float | None
    cgm_age_min: float
    cgm_valid: bool
    iob: float
    scheduled_basal: float
    max_scheduled_basal: float
    target: float
    bg_slope_per5: float
    base_prediction: list[float]       # BG if scheduled basal continues
    chosen_prediction: list[float]     # BG if the proposed rate is enacted
    delivered_24h: float
    pump_alarms: list[str] = field(default_factory=list)


@dataclass
class SafetyResult:
    rate: float | None                 # None = cancel any temp, run scheduled basal
    mode: str
    reasons: list[str]


class SafetyGuard:
    # Deterministic rule layer that sits between the controller and the pump. It never
    # increases insulin beyond what the controller asked for; every rule only lowers it.

    def __init__(self, settings: SafetySettings):
        self.s = settings

    def max_rate(self, scheduled_basal: float, max_scheduled_basal: float) -> float:
        return min(self.s.max_basal_uph, 4 * scheduled_basal, 3 * max_scheduled_basal)

    def _floor(self, rate: float) -> float:
        inc = self.s.pump_increment_u
        return round(math.floor(rate / inc + 1e-9) * inc, 2)

    def apply(self, proposed_rate: float, ctx: SafetyContext) -> SafetyResult:
        s = self.s
        if ctx.pump_alarms:
            return SafetyResult(None, MODE_BLOCKED, ["pump alarm active: " + ", ".join(ctx.pump_alarms)])
        if ctx.bg is None or not ctx.cgm_valid:
            return SafetyResult(None, MODE_FALLBACK, ["CGM reading invalid -> scheduled basal"])
        if ctx.cgm_age_min > s.cgm_stale_min:
            return SafetyResult(None, MODE_FALLBACK,
                                [f"CGM stale ({ctx.cgm_age_min:.0f} min) -> scheduled basal"])

        reasons = []
        mode = MODE_CLOSED
        sched = ctx.scheduled_basal
        rate = max(0.0, proposed_rate)

        cap = self.max_rate(sched, ctx.max_scheduled_basal)
        if rate > cap:
            rate = cap
            reasons.append(f"capped at max rate {cap:.2f} U/h")

        if rate > sched:
            headroom = max(0.0, s.max_iob_u - ctx.iob)
            allowed = sched + headroom / (s.temp_duration_min / 60)
            if rate > allowed:
                rate = allowed
                reasons.append(f"IOB {ctx.iob:.2f} U near max {s.max_iob_u:.1f} U")

        if rate > sched and ctx.delivered_24h >= s.max_daily_u:
            rate = sched
            reasons.append(f"24 h total {ctx.delivered_24h:.1f} U reached daily max")

        if rate > sched and ctx.bg < ctx.target and ctx.bg_slope_per5 < 0:
            rate = sched
            reasons.append("below target and falling: no extra insulin")

        if rate > sched and ctx.chosen_prediction and min(ctx.chosen_prediction) < s.predicted_low_mgdl:
            rate = sched
            reasons.append("extra insulin would predict a low")

        window = max(1, s.predicted_low_window_min // 5)
        if ctx.base_prediction and min(ctx.base_prediction[:window]) < s.predicted_low_mgdl:
            rate = 0.0
            mode = MODE_SUSPENDED
            reasons.append(f"low predicted within {s.predicted_low_window_min} min -> suspend")

        if ctx.bg < s.low_suspend_mgdl:
            rate = 0.0
            mode = MODE_SUSPENDED
            reasons.append(f"BG {ctx.bg:.0f} below {s.low_suspend_mgdl:.0f} -> suspend")

        rate = self._floor(rate)
        if mode == MODE_CLOSED and abs(rate - sched) < s.pump_increment_u / 2:
            return SafetyResult(None, mode, reasons or ["on target: scheduled basal"])
        return SafetyResult(rate, mode, reasons)
