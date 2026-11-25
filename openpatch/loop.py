from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .bolus import BolusAdvice, bolus_advice
from .carbs import CarbEntry, carbs_on_board
from .cgm.base import CGMSource, Reading
from .config import Profile, SafetySettings
from .controller import MPCController
from .insulin import Dose, InsulinCurve, insulin_on_board
from .predict import clamp_bg, estimate_deviation, predict_bg
from .pump.base import PumpDriver, PumpError
from .safety import MODE_BLOCKED, SafetyContext, SafetyGuard

MAX_DECISIONS_KEPT = 2000
# pessimistic case: only half of the remaining logged carbs still to come
FAST_CARB_SCALE = 0.5
FAST_CARB_WEIGHT = 0.5


@dataclass
class Decision:
    time: float
    bg: float | None
    cgm_age_min: float | None
    iob: float
    cob: float
    deviation_per5: float
    scheduled_basal: float
    proposed_rate: float | None
    rate: float | None              # None = scheduled basal
    target: float
    mode: str
    reasons: list[str]
    prediction: list[float] = field(default_factory=list)
    enacted: bool = False

    def to_json(self, with_prediction: bool = True):
        d = asdict(self)
        d["prediction"] = [round(p) for p in self.prediction] if with_prediction else []
        return d


def validate_reading(readings: list[Reading], max_jump: float) -> bool:
    if not readings:
        return False
    last = readings[-1]
    if not 39 <= last.mgdl <= 400:
        return False
    if len(readings) >= 2:
        prev = readings[-2]
        gap = max(5.0, last.time - prev.time)
        if abs(last.mgdl - prev.mgdl) * 5.0 / gap > max_jump:
            return False
    return True


class LoopEngine:
    def __init__(self, profile: Profile, settings: SafetySettings, cgm: CGMSource, pump: PumpDriver,
                 closed_loop: bool = True, basal_baseline: bool = True, history_start: float = 0.0):
        self.profile = profile
        self.settings = settings
        self.cgm = cgm
        self.pump = pump
        self.closed_loop = closed_loop
        # With a pump, IOB is counted net of scheduled basal. On injections (MDI) long-acting
        # insulin covers basal, so only logged boluses count.
        self.basal_baseline = basal_baseline
        self.history_start = history_start
        self.curve = InsulinCurve(profile.insulin_peak_min, profile.dia_min)
        self.controller = MPCController(self.curve, increment=settings.pump_increment_u)
        self.guard = SafetyGuard(settings)
        self.carbs: list[CarbEntry] = []
        self.decisions: list[Decision] = []
        self.temp_target: tuple[float, float, str] | None = None   # (until, mg/dL, label)

    def set_temp_target(self, now: float, target: float, duration_min: float, label: str = "activity"):
        if not 100 <= target <= 200 or not 0 < duration_min <= 24 * 60:
            raise ValueError("temp target must be 100..200 mg/dL for up to 24 h")
        self.temp_target = (now + duration_min, float(target), label)

    def cancel_temp_target(self):
        self.temp_target = None

    def target_at(self, now: float) -> float:
        if self.temp_target and now < self.temp_target[0]:
            return self.temp_target[1]
        return self.profile.target

    def add_carbs(self, time: float, grams: float, absorption_min: float | None = None):
        if grams <= 0:
            return
        self.carbs.append(CarbEntry(time, grams, absorption_min or self.profile.carb_absorption_min,
                                    self.profile.cr.at(time)))

    def _recent_carbs(self, now: float) -> list[CarbEntry]:
        self.carbs = [c for c in self.carbs if now - c.time < max(c.absorption_min, 480)]
        return [c for c in self.carbs if c.time <= now]

    def history(self, now: float):
        return [r for r in self.pump.delivery_history(now - 1440) if r.time <= now]

    def net_doses(self, now: float, history=None) -> list[Dose]:
        since = now - self.profile.dia_min
        if history is None:
            history = self.history(now)
        bins: dict[int, float] = {}
        for r in history:
            if r.time >= since:
                k = int(r.time // 5)
                bins[k] = bins.get(k, 0.0) + r.units
        if self.basal_baseline:
            t = max(since, self.history_start)
            while t < now:
                chunk = min(5.0 - t % 5, now - t)
                k = int(t // 5)
                bins[k] = bins.get(k, 0.0) - self.profile.basal.at(t) * chunk / 60
                t += chunk
        # 5-minute bins keep prediction cheap enough for a phone or a Pi Zero
        # (a bin's midpoint may lie after `now`; never date insulin into the future or IOB would miss it)
        return [Dose(min(k * 5 + 2.5, now), u) for k, u in sorted(bins.items()) if abs(u) > 1e-9]

    def iob(self, now: float) -> float:
        return insulin_on_board(self.net_doses(now), now, self.curve)

    def advise_bolus(self, now: float, carbs_g: float, bg: float | None = None) -> BolusAdvice:
        if bg is None:
            readings = self.cgm.readings(now, 15)
            if readings and validate_reading(readings, self.settings.cgm_max_jump_mgdl) \
                    and now - readings[-1].time <= self.settings.cgm_stale_min:
                bg = readings[-1].mgdl
        return bolus_advice(carbs_g, bg, self.iob(now), now, self.profile, self.settings)

    def run_cycle(self, now: float) -> Decision:
        p, s = self.profile, self.settings
        readings = self.cgm.readings(now, 30)
        latest = readings[-1] if readings else None
        bg = latest.mgdl if latest else None
        age = (now - latest.time) if latest else None
        valid = validate_reading(readings, s.cgm_max_jump_mgdl)

        history = self.history(now)
        doses = self.net_doses(now, history)
        carbs = self._recent_carbs(now)
        iob = insulin_on_board(doses, now, self.curve)
        cob = carbs_on_board(carbs, now)
        isf = p.isf.at(now)
        sched = p.basal.at(now)
        target = self.target_at(now)

        deviation = 0.0
        slope = 0.0
        base, chosen, proposed = [], [], None
        if bg is not None and valid:
            dev = estimate_deviation(readings, doses, carbs, isf, self.curve)
            if dev is not None:
                deviation = max(-15.0, min(s.max_positive_deviation_per5, dev))
            if len(readings) >= 2:
                slope = (readings[-1].mgdl - readings[-2].mgdl) * 5.0 / max(5.0, readings[-1].time - readings[-2].time)
            # While logged carbs are absorbing, a faster-than-modelled rise is usually those same
            # carbs arriving early, not new food: never project it forward as extra rise
            projected = min(deviation, 0.0) if cob > 1 else deviation
            raw = predict_bg(now, bg, doses, carbs, isf, self.curve, projected)
            fast = predict_bg(now, bg, doses, carbs, isf, self.curve, projected, carb_scale=FAST_CARB_SCALE) \
                if cob > 1 else raw
            base = [clamp_bg(v) for v in raw]
            max_rate = self.guard.max_rate(sched, p.basal.max())
            plan = self.controller.plan([(1 - FAST_CARB_WEIGHT, raw), (FAST_CARB_WEIGHT, fast)], isf, sched, max_rate, target)
            proposed = plan.rate
            chosen = self.controller.prediction_for(fast, isf, sched, plan.rate)

        status = self.pump.status()
        ctx = SafetyContext(
            bg=bg, cgm_age_min=age if age is not None else 1e9, cgm_valid=valid, iob=iob,
            scheduled_basal=sched, max_scheduled_basal=p.basal.max(), target=target,
            bg_slope_per5=slope, base_prediction=base, chosen_prediction=chosen,
            delivered_24h=sum(r.units for r in history), pump_alarms=status.alarms,
        )
        result = self.guard.apply(proposed if proposed is not None else sched, ctx)

        if base:
            final_rate = result.rate if result.rate is not None else sched
            if result.mode == MODE_BLOCKED:
                final_rate = 0.0
            prediction = self.controller.prediction_for(raw, isf, sched, final_rate)
        else:
            prediction = []

        decision = Decision(now, bg, age, round(iob, 2), round(cob, 1), round(deviation, 1), sched,
                            proposed, result.rate, target, result.mode, list(result.reasons), prediction)

        if self.closed_loop and result.mode != MODE_BLOCKED:
            try:
                if result.rate is None:
                    if status.temp_rate is not None:
                        self.pump.cancel_temp_basal()
                else:
                    self.pump.set_temp_basal(result.rate, s.temp_duration_min)
                decision.enacted = True
            except PumpError as e:
                decision.reasons.append(f"pump command failed: {e}")

        self.decisions.append(decision)
        if len(self.decisions) > MAX_DECISIONS_KEPT:
            del self.decisions[: len(self.decisions) - MAX_DECISIONS_KEPT]
        return decision
