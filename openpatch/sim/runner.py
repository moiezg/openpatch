from __future__ import annotations

import random
import time
from dataclasses import asdict

from ..cgm.base import BufferedCGM
from ..loop import LoopEngine
from ..pump.base import PumpError
from ..pump.driver import ProtocolPumpDriver
from ..pump.firmware import FirmwareLimits, PumpFirmware, STOP_MASK
from .hovorka import HovorkaPatient
from .metrics import glucose_metrics
from .patients import PATIENTS, derive_profile, derive_settings, patient_params
from .scenarios import build_scenario

SIM_KEY = b"openpatch-simulation-key-000000"
CGM_LAG_MIN = 8.0
RESCUE_CARBS_G = 15


def run_simulation(scenario: str = "typical_day", patient: str = "adult_70kg", days: int = 3,
                   closed_loop: bool = True, seed: int = 1, firmware: str = "python") -> dict:
    started = time.perf_counter()
    spec = build_scenario(scenario, days, seed)
    profile = derive_profile(patient)
    settings = derive_settings(profile)
    rng = random.Random(seed * 7919 + 17)

    body = HovorkaPatient(patient_params(patient), spec.initial_bg, spec.variation)
    if firmware == "c":
        from ..pump.cfirmware import CPumpFirmware as firmware_cls
    else:
        firmware_cls = PumpFirmware
    firmware = firmware_cls(SIM_KEY, profile.basal, FirmwareLimits(
        max_basal_uph=settings.max_basal_uph, max_bolus_u=settings.max_bolus_u,
        hourly_cap_u=settings.max_bolus_u + settings.max_basal_uph + 1))
    pump = ProtocolPumpDriver(firmware.handle, SIM_KEY)
    pump.set_time(0)
    cgm = BufferedCGM()
    engine = LoopEngine(profile, settings, cgm, pump, closed_loop=closed_loop, history_start=0)

    bolus_queue: list[tuple[int, float]] = []
    eat_queue: list[tuple[int, float]] = []
    for m in spec.meals:
        eat_queue.append((m.time, m.carbs))
        if m.bolus:
            bolus_queue.append((m.time + m.bolus_offset, m.announced))
    eat_queue.sort()
    bolus_queue.sort()

    interstitial = body.bg_mgdl
    noise = 0.0
    series, boluses, events = [], [], []
    alarm_since = None
    last_decision = None
    last_rescue = -10_000
    rescues = []

    for minute in range(spec.days * 1440):
        carbs_now = 0.0
        while eat_queue and eat_queue[0][0] <= minute:
            carbs_now += eat_queue.pop(0)[1]
        while bolus_queue and bolus_queue[0][0] <= minute:
            announced = bolus_queue.pop(0)[1]
            engine.add_carbs(minute, announced)
            advice = engine.advise_bolus(minute, announced)
            if advice.units > 0:
                try:
                    pump.bolus(advice.units)
                    boluses.append({"t": minute, "units": advice.units, "carbs": announced})
                except PumpError as e:
                    events.append({"t": minute, "type": "bolus_failed", "detail": str(e)})

        for start, target, duration in spec.temp_targets:
            if start == minute:
                engine.set_temp_target(minute, target, duration)

        interstitial += (body.bg_mgdl - interstitial) / CGM_LAG_MIN
        if minute % 5 == 0:
            noise = 0.7 * noise + rng.gauss(0, 3.5)
            dropped = any(a <= minute < b for a, b in spec.cgm_dropouts)
            if not dropped:
                cgm.push(minute, max(39.0, min(400.0, round(interstitial + noise))))
            last_decision = engine.run_cycle(minute)
            # A person treats a low alarm with 15 g of fast glucose, at most every 20 minutes
            if not dropped and cgm.readings(minute, 0) and cgm.readings(minute, 0)[-1].mgdl < 70 \
                    and minute - last_rescue >= 20:
                carbs_now += RESCUE_CARBS_G
                last_rescue = minute
                rescues.append({"t": minute, "carbs": RESCUE_CARBS_G})

        if spec.occlusion_at is not None and minute == spec.occlusion_at:
            firmware.inject_occlusion()
            events.append({"t": minute, "type": "occlusion_starts", "detail": "cannula blocked (not yet detected)"})
        if firmware.alarms & STOP_MASK and alarm_since is None:
            alarm_since = minute
            events.append({"t": minute, "type": "pump_alarm", "detail": "pump stopped delivery and alarmed"})
        if alarm_since is not None and minute - alarm_since >= spec.site_change_delay_min:
            firmware.replace_site()
            pump.clear_alarms()
            events.append({"t": minute, "type": "site_changed", "detail": "new infusion site, alarm cleared"})
            alarm_since = None

        rate = firmware.current_rate_uph(minute)
        units = firmware.tick(minute)
        body.step(minute, units, carbs_now)

        if minute % 5 == 0:
            readings = cgm.readings(minute, 0)
            series.append({
                "t": minute,
                "bg": round(body.bg_mgdl, 1),
                "cgm": readings[-1].mgdl if readings else None,
                "rate": round(rate, 2),
                "sched": profile.basal.at(minute),
                "iob": last_decision.iob if last_decision else 0,
                "cob": last_decision.cob if last_decision else 0,
                "mode": last_decision.mode if last_decision else "",
                "target": last_decision.target if last_decision else profile.target,
            })

    decisions = [d.to_json(with_prediction=(i % 6 == 0)) for i, d in enumerate(engine.decisions)]
    total_insulin = sum(r.units for r in pump.delivery_history(0))
    return {
        "scenario": scenario,
        "scenario_description": spec.description,
        "patient": patient,
        "patient_label": PATIENTS[patient]["label"],
        "days": spec.days,
        "mode": "closed" if closed_loop else "open",
        "seed": seed,
        "profile": profile.to_json(),
        "safety": asdict(settings),
        "metrics": glucose_metrics([s["bg"] for s in series]),
        "cgm_metrics": glucose_metrics([s["cgm"] for s in series]),
        "insulin": {
            "total_u": round(total_insulin, 2),
            "per_day_u": round(total_insulin / spec.days, 2),
            "bolus_u": round(sum(b["units"] for b in boluses), 2),
        },
        "rescue_count": len(rescues),
        "meals": [asdict(m) for m in spec.meals],
        "boluses": boluses,
        "rescue_carbs": rescues,
        "events": events,
        "series": series,
        "decisions": decisions,
        "runtime_s": round(time.perf_counter() - started, 2),
    }


def compare(scenario: str = "typical_day", patient: str = "adult_70kg", days: int = 3, seed: int = 1) -> dict:
    closed = run_simulation(scenario, patient, days, True, seed)
    opened = run_simulation(scenario, patient, days, False, seed)
    return {"closed": closed, "open": opened}
