from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MINUTES_PER_DAY = 1440


class ConfigError(ValueError):
    pass


@dataclass
class Schedule:
    # (start minute of day, value) pairs; the first entry must start at minute 0
    entries: list[tuple[int, float]]

    def __post_init__(self):
        self.entries = sorted((int(m), float(v)) for m, v in self.entries)
        if not self.entries or self.entries[0][0] != 0:
            raise ConfigError("schedule must have an entry starting at minute 0")
        if any(not 0 <= m < MINUTES_PER_DAY for m, _ in self.entries):
            raise ConfigError("schedule minutes must be within 0..1439")

    def at(self, minute: float) -> float:
        m = minute % MINUTES_PER_DAY
        value = self.entries[0][1]
        for start, v in self.entries:
            if start > m:
                break
            value = v
        return value

    def max(self) -> float:
        return max(v for _, v in self.entries)

    def min(self) -> float:
        return min(v for _, v in self.entries)

    def to_json(self):
        return [[m, v] for m, v in self.entries]


@dataclass
class Profile:
    basal: Schedule                      # U/h
    isf: Schedule                        # mg/dL drop per 1 U
    cr: Schedule                         # grams of carbs covered by 1 U
    target: float = 110.0                # mg/dL
    insulin_peak_min: float = 75.0       # NovoRapid/Humalog ~75, Fiasp ~55
    dia_min: float = 360.0               # duration of insulin action
    carb_absorption_min: float = 180.0

    def validate(self):
        if not (0.05 <= self.basal.min() and self.basal.max() <= 5.0):
            raise ConfigError("basal rates must be within 0.05..5 U/h")
        if not (10 <= self.isf.min() and self.isf.max() <= 400):
            raise ConfigError("ISF must be within 10..400 mg/dL/U")
        if not (2 <= self.cr.min() and self.cr.max() <= 150):
            raise ConfigError("carb ratio must be within 2..150 g/U")
        if not 90 <= self.target <= 160:
            raise ConfigError("target must be within 90..160 mg/dL")
        if not 300 <= self.dia_min <= 480:
            raise ConfigError("DIA must be within 300..480 minutes (5-8 h)")
        if not 35 <= self.insulin_peak_min <= 100:
            raise ConfigError("insulin peak must be within 35..100 minutes")
        if not 60 <= self.carb_absorption_min <= 480:
            raise ConfigError("carb absorption must be within 60..480 minutes")
        return self

    def to_json(self):
        d = asdict(self)
        d["basal"], d["isf"], d["cr"] = self.basal.to_json(), self.isf.to_json(), self.cr.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Profile":
        d = dict(d)
        for key in ("basal", "isf", "cr"):
            d[key] = Schedule([tuple(e) for e in d[key]])
        return cls(**d).validate()


@dataclass
class SafetySettings:
    max_basal_uph: float = 2.0           # absolute ceiling for any temp basal
    max_iob_u: float = 3.0               # ceiling on automatic (above-basal) insulin on board
    max_bolus_u: float = 10.0
    max_daily_u: float = 80.0
    low_suspend_mgdl: float = 75.0       # current BG below this -> zero delivery
    predicted_low_mgdl: float = 70.0     # predicted BG below this within the window -> zero delivery
    predicted_low_window_min: int = 30
    cgm_stale_min: float = 12.0          # older reading -> fall back to scheduled basal
    cgm_max_jump_mgdl: float = 45.0      # larger 5-minute jump is treated as sensor noise
    temp_duration_min: int = 30          # short temps expire on their own if the controller dies
    loop_interval_min: int = 5
    pump_increment_u: float = 0.05
    max_positive_deviation_per5: float = 6.0

    def validate(self):
        if not 0.1 <= self.max_basal_uph <= 10:
            raise ConfigError("max_basal_uph must be within 0.1..10")
        if not 0 <= self.max_iob_u <= 20:
            raise ConfigError("max_iob_u must be within 0..20")
        if not 0.5 <= self.max_bolus_u <= 25:
            raise ConfigError("max_bolus_u must be within 0.5..25")
        if not 65 <= self.low_suspend_mgdl <= 100:
            raise ConfigError("low_suspend_mgdl must be within 65..100")
        if not 60 <= self.predicted_low_mgdl <= 100:
            raise ConfigError("predicted_low_mgdl must be within 60..100")
        if not 5 <= self.temp_duration_min <= 60:
            raise ConfigError("temp_duration_min must be within 5..60")
        if self.cgm_stale_min > 20:
            raise ConfigError("cgm_stale_min must not exceed 20")
        return self

    @classmethod
    def from_json(cls, d: dict) -> "SafetySettings":
        return cls(**d).validate()


@dataclass
class AppConfig:
    profile: Profile
    safety: SafetySettings = field(default_factory=SafetySettings)
    cgm: dict = field(default_factory=lambda: {"type": "demo"})

    def to_json(self):
        return {"profile": self.profile.to_json(), "safety": asdict(self.safety), "cgm": self.cgm}

    @classmethod
    def from_json(cls, d: dict) -> "AppConfig":
        return cls(
            profile=Profile.from_json(d["profile"]),
            safety=SafetySettings.from_json(d.get("safety", {})),
            cgm=d.get("cgm", {"type": "demo"}),
        )

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        return cls.from_json(json.loads(path.read_text()))

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2))


def placeholder_profile() -> Profile:
    # Deliberately generic numbers: real values must come from your own diabetes team
    return Profile(
        basal=Schedule([(0, 0.8), (240, 1.0), (480, 0.8)]),
        isf=Schedule([(0, 45)]),
        cr=Schedule([(0, 12)]),
    ).validate()
