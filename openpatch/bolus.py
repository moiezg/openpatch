from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Profile, SafetySettings


@dataclass
class BolusAdvice:
    units: float
    carb_units: float
    correction_units: float
    iob: float
    warnings: list[str] = field(default_factory=list)


def bolus_advice(carbs_g: float, bg: float | None, iob: float, now: float, profile: Profile,
                 settings: SafetySettings) -> BolusAdvice:
    warnings = []
    cr = profile.cr.at(now)
    isf = profile.isf.at(now)
    carb_u = max(0.0, carbs_g) / cr

    if bg is None:
        correction = 0.0
        warnings.append("no current BG: carb bolus only, no correction")
    else:
        correction = (bg - profile.target) / isf
        # Active insulin already covers some of a high; below target, the negative correction trims the meal dose
        correction = max(0.0, correction - max(0.0, iob)) if correction > 0 else correction

    total = max(0.0, carb_u + correction)

    if bg is not None and bg < 70:
        total = 0.0
        warnings.append("BG below 70: treat the low first, bolus after recovery")
    if total > settings.max_bolus_u:
        warnings.append(f"capped at max bolus {settings.max_bolus_u} U")
        total = settings.max_bolus_u

    inc = settings.pump_increment_u
    total = round(math.floor(total / inc + 1e-9) * inc, 2)
    return BolusAdvice(total, round(carb_u, 2), round(correction, 2), round(iob, 2), warnings)
