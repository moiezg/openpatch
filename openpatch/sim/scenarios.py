from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class Meal:
    time: int                 # minute from simulation start
    carbs: float              # actually eaten
    announced: float          # entered into the calculator (0 = not entered)
    bolus: bool = True
    bolus_offset: int = 0     # minutes after eating (negative = pre-bolus)
    label: str = ""


@dataclass
class ScenarioSpec:
    name: str
    description: str
    days: int
    meals: list[Meal]
    si_events: list[tuple[int, int, float]] = field(default_factory=list)   # start, end, multiplier
    cgm_dropouts: list[tuple[int, int]] = field(default_factory=list)
    temp_targets: list[tuple[int, float, int]] = field(default_factory=list)  # start, mg/dL, minutes
    occlusion_at: int | None = None
    site_change_delay_min: int = 45
    daily_si: list[float] = field(default_factory=list)
    initial_bg: float = 130.0

    def variation(self, minute: float) -> tuple[float, float]:
        day = int(minute // 1440)
        si = self.daily_si[day] if day < len(self.daily_si) else 1.0
        for start, end, mult in self.si_events:
            if start <= minute < end:
                si *= mult
        # dawn phenomenon: liver output rises ~25% between 03:00 and 08:00
        tod = minute % 1440
        egp = 1.0
        if 180 <= tod < 480:
            egp += 0.25 * math.sin(math.pi * (tod - 180) / 300)
        return si, egp


INDIAN_DAY = [
    (8 * 60, 50, "breakfast (poha / idli)"),
    (13 * 60 + 30, 75, "lunch (rice, dal, sabzi)"),
    (17 * 60 + 30, 25, "chai + biscuits"),
    (21 * 60, 65, "dinner (roti, sabzi)"),
]

SCENARIOS = {
    "typical_day": "Four Indian meals, carb estimates off by up to ±15%, occasional late bolus",
    "missed_bolus": "Dinner on day 2 eaten without a bolus or carb entry",
    "carb_overestimate": "Breakfast on day 2 entered at 1.6x what was actually eaten",
    "fasting": "Ramadan-style day: sehri before dawn, iftar and dinner after sunset",
    "festival": "Diwali-style: very large lunch, sweets underestimated",
    "exercise": "45-minute evening walk raises insulin sensitivity for ~3 hours (no activity mode)",
    "exercise_activity_mode": "Same walk, with activity mode (target 150) set 60 min before",
    "illness": "Fever on day 2: insulin sensitivity drops 40% all day",
    "sensor_dropout": "CGM signal lost for 2 h overnight and 1.5 h in the afternoon",
    "occlusion": "Cannula blocks at 02:00 on day 2; site replaced 45 min after the alarm",
}


def build_scenario(name: str, days: int = 3, seed: int = 1) -> ScenarioSpec:
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario: {name}")
    days = max(1, min(days, 14))
    rng = random.Random(seed)
    special_day = min(1, days - 1)
    spec = ScenarioSpec(name, SCENARIOS[name], days, [],
                        daily_si=[max(0.75, min(1.25, rng.gauss(1.0, 0.08))) for _ in range(days)])

    for day in range(days):
        base = day * 1440
        if name == "fasting" and day == special_day:
            spec.meals += [Meal(base + 270, 60, 60, label="sehri"),
                           Meal(base + 18 * 60 + 45, 90, 90, label="iftar"),
                           Meal(base + 21 * 60 + 30, 40, 40, label="dinner")]
            continue
        for tod, carbs, label in INDIAN_DAY:
            actual = round(carbs * rng.uniform(0.85, 1.15))
            offset = rng.choice([0, 0, 0, 0, 0, 0, -10, -10, 20, 20])
            meal = Meal(base + tod + rng.randint(-20, 20), actual, carbs, True, offset, label)
            if day == special_day:
                if name == "missed_bolus" and label.startswith("dinner"):
                    meal.bolus, meal.announced = False, 0
                if name == "carb_overestimate" and label.startswith("breakfast"):
                    meal.announced = round(actual * 1.6)
                if name == "festival":
                    if label.startswith("lunch"):
                        meal.carbs, meal.announced = 120, 110
                    if label.startswith("chai"):
                        meal.carbs, meal.announced, meal.label = 60, 30, "mithai (underestimated)"
            spec.meals.append(meal)

    sd = special_day * 1440
    if name in ("exercise", "exercise_activity_mode"):
        for day in range(days):
            spec.si_events.append((day * 1440 + 18 * 60 + 30, day * 1440 + 21 * 60 + 30, 1.6))
            if name == "exercise_activity_mode":
                spec.temp_targets.append((day * 1440 + 17 * 60 + 30, 150, 240))
    if name == "illness":
        spec.si_events.append((sd, sd + 1440, 0.6))
    if name == "sensor_dropout":
        spec.cgm_dropouts += [(sd + 60, sd + 180), (sd + 15 * 60, sd + 16 * 60 + 30)]
    if name == "occlusion":
        spec.occlusion_at = sd + 120
    spec.meals.sort(key=lambda m: m.time)
    return spec
