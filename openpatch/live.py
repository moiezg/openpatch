from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .cgm.http_sources import now_minutes, source_from_config
from .config import AppConfig, ConfigError, placeholder_profile
from .loop import LoopEngine
from .pump.base import DeliveryRecord, PumpDriver, PumpError, PumpStatus
from .pump.driver import ProtocolPumpDriver
from .pump.firmware import FirmwareLimits, PumpFirmware

# Every safety gate in docs/SAFETY.md must be signed off in config before the software will
# send automatic insulin commands to a real pump.
REQUIRED_GATES = ["G0_simulation", "G1_bench_accuracy", "G2_fault_injection", "G3_saline_wear",
                  "G4_advisory_2_weeks", "G5_clinician_informed"]


def home_dir() -> Path:
    return Path(os.environ.get("OPENPATCH_HOME", Path.home() / ".openpatch"))


class TreatmentLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def recent(self, now: float, minutes: float) -> list[dict]:
        return [t for t in self.all() if now - minutes <= t["time"] <= now + 1]

    def add(self, kind: str, amount: float, time_min: float, **extra) -> dict:
        entry = {"id": uuid.uuid4().hex[:12], "type": kind, "amount": amount, "time": time_min, **extra}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            entries = self.all()
            kept = [e for e in entries if e["id"] != entry_id]
            if len(kept) == len(entries):
                return False
            self.path.write_text("".join(json.dumps(e) + "\n" for e in kept))
            return True


class InjectionPump(PumpDriver):
    # No pump: insulin comes from pens. Logged boluses are the delivery history.
    def __init__(self, log: TreatmentLog):
        self.log = log

    def status(self) -> PumpStatus:
        return PumpStatus(now_minutes(), 0, 100, False, None, 0, 0, 0)

    def set_temp_basal(self, rate_uph, duration_min):
        raise PumpError("no pump connected")

    def cancel_temp_basal(self):
        raise PumpError("no pump connected")

    def bolus(self, units):
        raise PumpError("no pump connected: log the pen dose instead")

    def delivery_history(self, since: float) -> list[DeliveryRecord]:
        return [DeliveryRecord(t["time"], t["amount"], "bolus")
                for t in self.log.all() if t["type"] == "bolus" and t["time"] >= since]


class VirtualPump:
    # Firmware model ticking in real time: a dry run of the whole loop with a real CGM and no insulin
    def __init__(self, cfg: AppConfig, key: bytes):
        s = cfg.safety
        self.firmware = PumpFirmware(key, cfg.profile.basal, FirmwareLimits(
            max_basal_uph=s.max_basal_uph, max_bolus_u=s.max_bolus_u,
            hourly_cap_u=s.max_bolus_u + s.max_basal_uph + 1), start_minute=int(now_minutes()))
        self.driver = ProtocolPumpDriver(self.firmware.handle, key)
        self.driver.set_time(int(now_minutes()))
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="virtual-pump").start()

    def _run(self):
        last = int(now_minutes())
        while not self._stop.wait(5):
            now = int(now_minutes())
            while last < now:
                last += 1
                self.firmware.tick(last)

    def stop(self):
        self._stop.set()


class LiveService:
    def __init__(self, root: Path | None = None):
        self.root = root or home_dir()
        self.config_path = self.root / "config.json"
        self.treatments = TreatmentLog(self.root / "treatments.jsonl")
        self._lock = threading.RLock()
        self._loop_thread = None
        self._loop_stop = threading.Event()
        self.engine: LoopEngine | None = None
        self.virtual: VirtualPump | None = None
        self.last_error: str | None = None
        self.raw = self._load_or_create()
        self._build()

    # ---------- configuration ----------

    def _load_or_create(self) -> dict:
        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text())
        else:
            raw = AppConfig(placeholder_profile()).to_json()
            raw["configured"] = False
        raw.setdefault("server", {}).setdefault("token", secrets.token_urlsafe(18))
        raw.setdefault("pump", {"type": "none"})
        raw.setdefault("closed_loop_enabled", False)
        raw.setdefault("gates_completed", [])
        self._save(raw)
        return raw

    def _save(self, raw: dict):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.config_path)

    @property
    def token(self) -> str:
        return self.raw["server"]["token"]

    def rotate_token(self) -> str:
        with self._lock:
            self.raw["server"]["token"] = secrets.token_urlsafe(18)
            self._save(self.raw)
            return self.token

    def app_config(self) -> AppConfig:
        return AppConfig.from_json(self.raw)

    def pump_mode(self) -> str:
        return self.raw.get("pump", {}).get("type", "none")

    def closed_loop_allowed(self) -> tuple[bool, list[str]]:
        missing = [g for g in REQUIRED_GATES if g not in self.raw.get("gates_completed", [])]
        if self.pump_mode() == "virtual":
            return bool(self.raw.get("closed_loop_enabled")), []
        return bool(self.raw.get("closed_loop_enabled")) and not missing and self.pump_mode() == "ble", missing

    def settings_view(self) -> dict:
        allowed, missing = self.closed_loop_allowed()
        view = {k: v for k, v in self.raw.items() if k != "server"}
        view["closed_loop_active"] = allowed
        view["gates_missing"] = missing
        view["required_gates"] = REQUIRED_GATES
        return view

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            candidate = json.loads(json.dumps(self.raw))
            for key in ("profile", "safety", "cgm", "pump", "closed_loop_enabled", "gates_completed"):
                if key in patch:
                    candidate[key] = patch[key]
            AppConfig.from_json(candidate)   # raises ConfigError on invalid values
            if candidate.get("pump", {}).get("type") not in ("none", "virtual", "ble"):
                raise ConfigError("pump.type must be none, virtual or ble")
            if "profile" in patch:
                candidate["configured"] = True
            self.raw = candidate
            self._save(candidate)
            self._build()
            return self.settings_view()

    # ---------- engine ----------

    def _build(self):
        with self._lock:
            self._stop_loop()
            if self.virtual:
                self.virtual.stop()
                self.virtual = None
            cfg = self.app_config()
            cgm = source_from_config(cfg.cgm)
            mode = self.pump_mode()
            key = bytes.fromhex(self.raw["pump"].get("key", "")) if self.raw["pump"].get("key") else \
                self.token.encode()
            if mode == "virtual":
                self.virtual = VirtualPump(cfg, key)
                pump, baseline = self.virtual.driver, True
            elif mode == "ble":
                from .pump.ble import BleTransport
                pump, baseline = ProtocolPumpDriver(BleTransport(self.raw["pump"]["address"]), key), True
                pump.set_time(int(now_minutes()))   # the pump has no battery-backed clock
            else:
                pump, baseline = InjectionPump(self.treatments), False
            allowed, _ = self.closed_loop_allowed()
            self.engine = LoopEngine(cfg.profile, cfg.safety, cgm, pump, closed_loop=allowed,
                                     basal_baseline=baseline, history_start=now_minutes())
            if mode != "none":
                self._start_loop()

    def _sync_engine_inputs(self, now: float):
        e = self.engine
        e.carbs = []
        for t in self.treatments.recent(now, 480):
            if t["type"] == "carbs":
                e.add_carbs(t["time"], t["amount"], t.get("absorption_min"))
        e.temp_target = None
        for t in sorted(self.treatments.recent(now, 24 * 60), key=lambda x: x["time"]):
            if t["type"] == "temp_target":
                e.temp_target = (t["time"] + t["duration_min"], t["amount"], t.get("label", "activity"))
            elif t["type"] == "temp_target_cancel":
                e.temp_target = None
        if e.temp_target and e.temp_target[0] <= now:
            e.temp_target = None

    def _start_loop(self):
        self._loop_stop.clear()

        def run():
            while not self._loop_stop.is_set():
                try:
                    with self._lock:
                        now = now_minutes()
                        self._sync_engine_inputs(now)
                        self.engine.run_cycle(now)
                        self.last_error = None
                except Exception as ex:   # keep looping; the pump falls back to scheduled basal on its own
                    self.last_error = f"{type(ex).__name__}: {ex}"
                self._loop_stop.wait(self.engine.settings.loop_interval_min * 60)

        self._loop_thread = threading.Thread(target=run, daemon=True, name="loop")
        self._loop_thread.start()

    def _stop_loop(self):
        self._loop_stop.set()

    # ---------- API ----------

    def status(self) -> dict:
        with self._lock:
            now = now_minutes()
            e = self.engine
            self._sync_engine_inputs(now)
            readings, cgm_error = [], None
            try:
                readings = e.cgm.readings(now, 6 * 60)
            except Exception as ex:
                cgm_error = f"CGM source unreachable: {ex}"
            if self.pump_mode() == "none" or not e.decisions or now - e.decisions[-1].time > 6:
                # advisory snapshot (does not command anything)
                closed = e.closed_loop
                e.closed_loop = False
                try:
                    decision = e.run_cycle(now) if not cgm_error else None
                except Exception as ex:
                    decision, cgm_error = None, f"CGM source unreachable: {ex}"
                finally:
                    e.closed_loop = closed
            else:
                decision = e.decisions[-1]
            pump_status = None
            if self.pump_mode() != "none":
                try:
                    pump_status = asdict(e.pump.status())
                except PumpError as ex:
                    pump_status = {"error": str(ex)}
            latest = readings[-1] if readings else None
            delta = None
            if len(readings) >= 2:
                delta = round((readings[-1].mgdl - readings[-2].mgdl) * 5 / max(5, readings[-1].time - readings[-2].time))
            allowed, missing = self.closed_loop_allowed()
            warnings = []
            if not self.raw.get("configured", True):
                warnings.append("Using a PLACEHOLDER profile. Enter the basal, ISF and carb ratio agreed with your doctor.")
            if self.raw.get("cgm", {}).get("type") == "demo":
                warnings.append("CGM source is DEMO data, not your sensor.")
            if cgm_error:
                warnings.append(cgm_error)
            if self.last_error:
                warnings.append(f"Loop error: {self.last_error}")
            return {
                "now": now,
                "mode": "closed" if allowed else ("dry-run" if self.pump_mode() == "virtual" else "advisory"),
                "pump_mode": self.pump_mode(),
                "bg": latest.mgdl if latest else None,
                "bg_time": latest.time if latest else None,
                "delta": delta,
                "readings": [[r.time, r.mgdl] for r in readings],
                "decision": decision.to_json() if decision else None,
                "pump": pump_status,
                "target": e.target_at(now),
                "temp_target": list(e.temp_target) if e.temp_target and now < e.temp_target[0] else None,
                "treatments": sorted(self.treatments.recent(now, 24 * 60), key=lambda t: -t["time"]),
                "gates_missing": missing,
                "warnings": warnings,
                "units": "mg/dL",
            }

    def bolus_calc(self, carbs: float, bg: float | None = None) -> dict:
        with self._lock:
            now = now_minutes()
            self._sync_engine_inputs(now)
            return asdict(self.engine.advise_bolus(now, carbs, bg))

    def deliver_bolus(self, units: float, carbs: float = 0.0) -> dict:
        with self._lock:
            now = now_minutes()
            s = self.engine.settings
            if not 0 < units <= s.max_bolus_u:
                raise ValueError(f"bolus must be between 0 and {s.max_bolus_u} U")
            if self.pump_mode() == "none":
                entry = self.treatments.add("bolus", round(units, 2), now, source="pen")
            else:
                self.engine.pump.bolus(units)
                entry = self.treatments.add("bolus_request", round(units, 2), now, source="pump")
            if carbs > 0:
                self.treatments.add("carbs", carbs, now)
            return entry

    def log_treatment(self, kind: str, amount: float, minutes_ago: float = 0, **extra) -> dict:
        if kind not in ("bolus", "carbs", "temp_target", "temp_target_cancel", "note"):
            raise ValueError("unknown treatment type")
        if kind == "bolus" and self.pump_mode() != "none":
            raise ValueError("with a pump, deliver boluses through the pump")
        if kind == "carbs" and not 0 < amount <= 250:
            raise ValueError("carbs must be 1..250 g")
        if kind == "bolus" and not 0 < amount <= self.engine.settings.max_bolus_u:
            raise ValueError("bolus outside allowed range")
        if kind == "temp_target":
            if not 100 <= amount <= 200 or not 0 < extra.get("duration_min", 0) <= 1440:
                raise ValueError("temp target must be 100..200 mg/dL for up to 24 h")
        return self.treatments.add(kind, amount, now_minutes() - max(0.0, minutes_ago), **extra)

    def delete_treatment(self, entry_id: str) -> bool:
        return self.treatments.delete(entry_id)

    def decisions(self, limit: int = 288) -> list[dict]:
        return [d.to_json(with_prediction=False) for d in self.engine.decisions[-limit:]]
