from __future__ import annotations

from functools import lru_cache

from ..config import Profile, SafetySettings, Schedule
from .hovorka import HovorkaPatient, PatientParams

PATIENTS = {
    "adult_70kg": {"label": "Adult, 70 kg, average sensitivity", "sensitivity": 0.55, "weight": 70},
    "teen_55kg": {"label": "Teen, 55 kg, sensitive", "sensitivity": 0.85, "weight": 55},
    "resistant_95kg": {"label": "Adult, 95 kg, insulin resistant", "sensitivity": 0.35, "weight": 95},
}


def patient_params(name: str) -> PatientParams:
    spec = PATIENTS[name]
    return PatientParams().scaled(spec["sensitivity"], spec["weight"])


def _round_to(v: float, step: float) -> float:
    return round(round(v / step) * step, 2)


@lru_cache(maxsize=None)
def derive_profile(name: str) -> Profile:
    # What a well-tuned diabetes team would arrive at for this virtual person, measured the same
    # way a clinician would: fasting basal test, correction test, carb test.
    pp = patient_params(name)
    basal_u_min = HovorkaPatient(pp, 110).basal_mu_per_min / 1000

    p = HovorkaPatient(pp, 160)
    low = 160.0
    for m in range(480):
        p.step(m, basal_u_min + (1.0 if m == 0 else 0.0), 0)
        low = min(low, p.bg_mgdl)
    isf = 160 - low

    # Carb ratio titrated like in clinic: the meal bolus that returns BG to baseline 5 h after 50 g
    def bg_after_meal(bolus_u: float) -> float:
        q = HovorkaPatient(pp, 110)
        for m in range(300):
            q.step(m, basal_u_min + (bolus_u if m == 0 else 0.0), 50 if m == 0 else 0)
        return q.bg_mgdl

    lo, hi = 0.0, 30.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if bg_after_meal(mid) > 110 else (lo, mid)
    cr = 50 / ((lo + hi) / 2)

    basal = _round_to(basal_u_min * 60, 0.05)
    return Profile(
        basal=Schedule([(0, basal), (180, _round_to(basal * 1.15, 0.05)), (480, basal)]),  # dawn phenomenon
        isf=Schedule([(0, _round_to(isf, 1))]),
        cr=Schedule([(0, _round_to(cr, 0.5))]),
        target=110,
    ).validate()


def derive_settings(profile: Profile) -> SafetySettings:
    typical_meal_bolus = 60 / profile.cr.at(0)
    return SafetySettings(
        max_basal_uph=_round_to(max(1.0, 3 * profile.basal.max()), 0.05),
        max_iob_u=_round_to(typical_meal_bolus + 3 * profile.basal.max(), 0.5),
        max_bolus_u=_round_to(max(5.0, 2.5 * typical_meal_bolus), 0.5),
        max_daily_u=_round_to(max(30.0, 4 * 24 * profile.basal.max()), 1),
    ).validate()
