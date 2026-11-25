from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

# Hovorka (2004) glucose-insulin model of a person with type 1 diabetes, as used in the
# Cambridge artificial-pancreas trials. Units: glucose mmol, insulin mU, time minutes.

MGDL_PER_MMOL = 18.0


@dataclass(frozen=True)
class PatientParams:
    weight_kg: float = 70.0
    ka1: float = 0.006
    ka2: float = 0.06
    ka3: float = 0.03
    sit: float = 51.2e-4     # insulin sensitivity: glucose transport
    sid: float = 8.2e-4      # insulin sensitivity: glucose disposal
    sie: float = 520e-4      # insulin sensitivity: liver glucose output
    k12: float = 0.066
    ke: float = 0.138
    vi: float = 0.12
    vg: float = 0.16
    egp0: float = 0.0161
    f01: float = 0.0097
    tmax_i: float = 55.0
    tmax_g: float = 40.0
    ag: float = 0.8

    def scaled(self, sensitivity: float = 1.0, weight_kg: float | None = None) -> "PatientParams":
        return replace(self, sit=self.sit * sensitivity, sid=self.sid * sensitivity, sie=self.sie * sensitivity,
                       weight_kg=weight_kg or self.weight_kg)


# (insulin sensitivity multiplier, liver glucose output multiplier) at a given minute
Variation = Callable[[float], tuple[float, float]]


def no_variation(_minute: float) -> tuple[float, float]:
    return 1.0, 1.0


class HovorkaPatient:
    def __init__(self, params: PatientParams, initial_bg_mgdl: float = 120.0, variation: Variation = no_variation):
        self.p = params
        self.variation = variation
        si, egp = variation(0)
        g = initial_bg_mgdl / MGDL_PER_MMOL
        self.basal_mu_per_min = self.steady_state_insulin(g, si, egp)
        u = self.basal_mu_per_min
        p, bw = params, params.weight_kg
        i = u / (p.ke * p.vi * bw)
        x1, x2, x3 = si * p.sit * i, si * p.sid * i, si * p.sie * i
        q1 = g * p.vg * bw
        self.s1 = self.s2 = u * p.tmax_i
        self.i = i
        self.x1, self.x2, self.x3 = x1, x2, x3
        self.q1 = q1
        self.q2 = x1 * q1 / (p.k12 + x2)
        self.d1 = self.d2 = 0.0

    def steady_state_insulin(self, g: float, si: float = 1.0, egp: float = 1.0) -> float:
        p, bw = self.p, self.p.weight_kg
        q1 = g * p.vg * bw
        f01c = p.f01 * bw * (1.0 if g >= 4.5 else g / 4.5)
        fr = 0.003 * (g - 9) * p.vg * bw if g >= 9 else 0.0

        def balance(i):
            x1, x2, x3 = si * p.sit * i, si * p.sid * i, si * p.sie * i
            q2 = x1 * q1 / (p.k12 + x2)
            return -f01c - fr - x1 * q1 + p.k12 * q2 + max(0.0, p.egp0 * egp * bw * (1 - x3))

        lo, hi = 0.0, 1.0 / (si * p.sie)
        for _ in range(80):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if balance(mid) > 0 else (lo, mid)
        return (lo + hi) / 2 * p.ke * p.vi * bw

    def basal_requirement_uph(self) -> float:
        return self.basal_mu_per_min * 60 / 1000

    @property
    def bg_mgdl(self) -> float:
        return self.q1 / (self.p.vg * self.p.weight_kg) * MGDL_PER_MMOL

    def step(self, minute: float, insulin_u: float, carbs_g: float, substeps: int = 4):
        # Advance one minute. insulin_u and carbs_g are amounts delivered/eaten during this minute.
        p, bw = self.p, self.p.weight_kg
        si, egp_scale = self.variation(minute)
        u = insulin_u * 1000.0
        d = carbs_g * 1000.0 / 180.0
        dt = 1.0 / substeps
        for _ in range(substeps):
            g = self.q1 / (p.vg * bw)
            ui = self.s2 / p.tmax_i
            ug = self.d2 / p.tmax_g
            f01c = p.f01 * bw * (1.0 if g >= 4.5 else g / 4.5)
            fr = 0.003 * (g - 9) * p.vg * bw if g >= 9 else 0.0
            egp = max(0.0, p.egp0 * egp_scale * bw * (1 - self.x3))

            ds1 = u - self.s1 / p.tmax_i
            ds2 = (self.s1 - self.s2) / p.tmax_i
            di = ui / (p.vi * bw) - p.ke * self.i
            dx1 = -p.ka1 * self.x1 + si * p.sit * p.ka1 * self.i
            dx2 = -p.ka2 * self.x2 + si * p.sid * p.ka2 * self.i
            dx3 = -p.ka3 * self.x3 + si * p.sie * p.ka3 * self.i
            dd1 = p.ag * d - self.d1 / p.tmax_g
            dd2 = (self.d1 - self.d2) / p.tmax_g
            dq1 = -f01c - fr - self.x1 * self.q1 + p.k12 * self.q2 + ug + egp
            dq2 = self.x1 * self.q1 - (p.k12 + self.x2) * self.q2

            self.s1 = max(0.0, self.s1 + dt * ds1)
            self.s2 = max(0.0, self.s2 + dt * ds2)
            self.i = max(0.0, self.i + dt * di)
            self.x1 = max(0.0, self.x1 + dt * dx1)
            self.x2 = max(0.0, self.x2 + dt * dx2)
            self.x3 = max(0.0, self.x3 + dt * dx3)
            self.d1 = max(0.0, self.d1 + dt * dd1)
            self.d2 = max(0.0, self.d2 + dt * dd2)
            self.q1 = max(0.1, self.q1 + dt * dq1)
            self.q2 = max(0.0, self.q2 + dt * dq2)
