from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass

from ..config import Schedule
from .protocol import (
    CMD_BOLUS, CMD_CANCEL_BOLUS, CMD_CANCEL_TEMP, CMD_CLEAR_ALARMS, CMD_HISTORY, CMD_PRIME, CMD_RESUME,
    CMD_REWIND, CMD_SET_RESERVOIR, CMD_SET_TEMP, CMD_SET_TIME, CMD_STATUS, CMD_SUSPEND, FLAG_SUSPENDED,
    FLAG_TEMP_ACTIVE, HISTORY_PAGE, HISTORY_RECORD, STATE_NO_RESERVOIR, STATE_PRIMING, STATE_RUNNING,
    STATE_SUSPENDED, STATUS_PAYLOAD, ST_ALARM, ST_FORMAT, ST_LIMIT, ST_OK, ST_STATE, ST_UNKNOWN, ProtocolError,
    decode, encode,
)

# Executable reference model of the pump firmware. firmware/core/op_fw.c implements the same
# behaviour in C; tests/test_firmware_parity.py drives both with random command streams and
# requires byte-identical responses. All arithmetic is integer so the two can match exactly.

ALARM_OCCLUSION = 1 << 0
ALARM_RESERVOIR_EMPTY = 1 << 1
ALARM_HOURLY_CAP = 1 << 2
ALARM_MOTOR = 1 << 3
WARN_RESERVOIR_LOW = 1 << 8
WARN_BATTERY_LOW = 1 << 9
WARN_RESTARTED = 1 << 10

STOP_MASK = ALARM_OCCLUSION | ALARM_RESERVOIR_EMPTY | ALARM_HOURLY_CAP | ALARM_MOTOR

ALARM_NAMES = {
    ALARM_OCCLUSION: "OCCLUSION", ALARM_RESERVOIR_EMPTY: "RESERVOIR_EMPTY",
    ALARM_HOURLY_CAP: "HOURLY_CAP", ALARM_MOTOR: "MOTOR_FAULT",
    WARN_RESERVOIR_LOW: "RESERVOIR_LOW", WARN_BATTERY_LOW: "BATTERY_LOW", WARN_RESTARTED: "RESTARTED",
}

KIND_BASAL, KIND_BOLUS, KIND_PRIME = 0, 1, 2
HISTORY_KEEP_MIN = 2 * 1440
RESERVOIR_LOW_CU = 2000
RESERVOIR_MIN_CU, RESERVOIR_MAX_CU = 1000, 30000
PRIME_MAX_CU = 300
BATTERY_LOW_PCT = 15

# simulated plant (host builds only): battery drain per pulse, in millionths of full charge
BATTERY_FULL_MICRO = 1_000_000
BATTERY_MICRO_PER_PULSE = 4


@dataclass
class FirmwareLimits:
    max_basal_uph: float = 3.0
    max_bolus_u: float = 10.0
    hourly_cap_u: float = 12.0          # total motor delivery (basal + bolus + prime) in any rolling 60 minutes
    max_temp_duration_min: int = 120
    pulse_u: float = 0.05
    bolus_rate_u_per_min: float = 1.5
    occlusion_threshold_u: float = 1.0  # line force (calibrated to undelivered volume) that trips the alarm

    def centi(self) -> tuple[int, ...]:
        # (max_basal_cuph, max_bolus_cu, hourly_cap_cu, max_temp_min, pulse_cu, bolus_rate_cu, occlusion_threshold)
        return (round(self.max_basal_uph * 100), round(self.max_bolus_u * 100), round(self.hourly_cap_u * 100),
                int(self.max_temp_duration_min), round(self.pulse_u * 100), round(self.bolus_rate_u_per_min * 100),
                round(self.occlusion_threshold_u * 100))


class PumpFirmware:
    def __init__(self, key: bytes, basal: Schedule, limits: FirmwareLimits | None = None,
                 reservoir_u: float = 200.0, start_minute: int = 0):
        self.key = key
        self.limits = limits or FirmwareLimits()
        (self.max_basal_cuph, self.max_bolus_cu, self.hourly_cap_cu, self.max_temp_min,
         self.pulse_cu, self.bolus_rate_cu, self.occlusion_threshold) = self.limits.centi()
        self.basal = [(int(m), round(v * 100)) for m, v in basal.entries]
        self.minute = start_minute
        self.reservoir_cu = round(reservoir_u * 100)
        self.state = STATE_RUNNING if self.reservoir_cu > 0 else STATE_NO_RESERVOIR
        self.temp_rate_cuph: int | None = None
        self.temp_end = 0
        self.bolus_remaining_cu = 0
        self.prime_remaining_cu = 0
        self.primed_cu = 0
        self.basal_accum = 0                   # cU·min/h: one pulse is due per pulse_cu * 60
        self.alarms = 0
        self.history: list[list[int]] = []     # [minute, kind, centi-units], sorted by minute
        self._last = (None, None, None)        # seq, request frame, response frame
        # physical plant, simulated (the C host build models it identically)
        self.battery_micro = BATTERY_FULL_MICRO
        self.line_occluded = False
        self.undelivered_cu = 0
        self.motor_fault = False

    # ---------- HAL (hardware in C; simulated plant here) ----------

    def _hal_pulse(self, kind: int) -> int | None:
        # one motor pulse; returns centi-units that reached the body, None on motor failure
        if self.motor_fault:
            return None
        self.battery_micro = max(0, self.battery_micro - BATTERY_MICRO_PER_PULSE)
        if self.line_occluded:
            self.undelivered_cu += self.pulse_cu
            return 0
        return 0 if kind == KIND_PRIME else self.pulse_cu

    def _hal_rewind(self) -> bool:
        return not self.motor_fault

    def _hal_force(self) -> int:
        return self.undelivered_cu

    def _hal_battery_pct(self) -> int:
        return self.battery_micro // 10000

    # ---------- command handling ----------

    def handle(self, frame: bytes) -> bytes:
        try:
            req = decode(self.key, frame, response=False)
        except ProtocolError as e:
            seq = frame[0] if len(frame) > 0 else 0
            cmd = frame[1] if len(frame) > 1 else 0
            return encode(self.key, seq, cmd | 0x80, b"", e.code)
        if req.seq == self._last[0] and frame == self._last[1]:
            return self._last[2]
        status, payload = self._execute(req.cmd, req.payload)
        resp = encode(self.key, req.seq, req.cmd | 0x80, payload, status)
        self._last = (req.seq, frame, resp)
        return resp

    def _execute(self, cmd: int, p: bytes):
        sizes = {CMD_STATUS: 0, CMD_HISTORY: 5, CMD_SET_TIME: 4, CMD_CLEAR_ALARMS: 0, CMD_SUSPEND: 0,
                 CMD_RESUME: 0, CMD_CANCEL_BOLUS: 0, CMD_CANCEL_TEMP: 0, CMD_SET_TEMP: 4, CMD_BOLUS: 2,
                 CMD_REWIND: 0, CMD_SET_RESERVOIR: 2, CMD_PRIME: 2}
        if cmd not in sizes:
            return ST_UNKNOWN, b""
        if len(p) != sizes[cmd]:
            return ST_FORMAT, b""
        stopped = self.alarms & STOP_MASK

        if cmd == CMD_STATUS:
            return ST_OK, self._status_payload()
        if cmd == CMD_HISTORY:
            since, max_n = struct.unpack(">IB", p)
            i = bisect.bisect_left(self.history, [since, -1, -1])
            recs = self.history[i: i + min(max_n, HISTORY_PAGE)]
            return ST_OK, bytes([len(recs)]) + b"".join(HISTORY_RECORD.pack(m, cu, k) for m, k, cu in recs)
        if cmd == CMD_SET_TIME:
            (minute,) = struct.unpack(">I", p)
            if self.history and minute < self.history[-1][0]:
                return ST_STATE, b""
            self.minute = minute
            return ST_OK, b""
        if cmd == CMD_CLEAR_ALARMS:
            if self.alarms & ALARM_OCCLUSION and self._hal_force() >= self.occlusion_threshold:
                return ST_STATE, b""          # line still pressurised: replace the site first
            self.alarms &= ~(STOP_MASK | WARN_RESTARTED)
            return ST_OK, b""
        if cmd == CMD_SUSPEND:
            if self.state == STATE_RUNNING:
                self.state = STATE_SUSPENDED
            self.temp_rate_cuph = None
            self.bolus_remaining_cu = 0
            self.prime_remaining_cu = 0
            return ST_OK, b""
        if cmd == CMD_RESUME:
            if self.state == STATE_SUSPENDED:
                self.state = STATE_RUNNING
            elif self.state == STATE_PRIMING:
                if self.primed_cu == 0 or self.prime_remaining_cu > 0:
                    return ST_STATE, b""
                self.state = STATE_RUNNING
            elif self.state == STATE_NO_RESERVOIR:
                return ST_STATE, b""
            return ST_OK, b""
        if cmd == CMD_CANCEL_BOLUS:
            self.bolus_remaining_cu = 0
            return ST_OK, b""
        if cmd == CMD_CANCEL_TEMP:
            self.temp_rate_cuph = None
            return ST_OK, b""
        if cmd == CMD_REWIND:
            if self.state == STATE_RUNNING:
                return ST_STATE, b""
            if not self._hal_rewind():
                self._raise(ALARM_MOTOR)
                return ST_STATE, b""
            self.state = STATE_NO_RESERVOIR
            self.reservoir_cu = 0
            self.primed_cu = 0
            self.prime_remaining_cu = 0
            self.basal_accum = 0
            self.alarms &= ~ALARM_RESERVOIR_EMPTY
            return ST_OK, b""
        if cmd == CMD_SET_RESERVOIR:
            (cu,) = struct.unpack(">H", p)
            if self.state != STATE_NO_RESERVOIR:
                return ST_STATE, b""
            if not RESERVOIR_MIN_CU <= cu <= RESERVOIR_MAX_CU:
                return ST_LIMIT, b""
            self.reservoir_cu = cu
            self.primed_cu = 0
            self.state = STATE_PRIMING
            return ST_OK, b""
        if cmd == CMD_PRIME:
            (cu,) = struct.unpack(">H", p)
            if stopped:
                return ST_ALARM, b""
            if self.state != STATE_PRIMING or self.prime_remaining_cu > 0:
                return ST_STATE, b""
            if cu <= 0 or cu > PRIME_MAX_CU or cu % self.pulse_cu:
                return ST_LIMIT, b""
            self.prime_remaining_cu = cu
            return ST_OK, b""
        if cmd == CMD_SET_TEMP:
            rate_cuph, duration = struct.unpack(">HH", p)
            if stopped:
                return ST_ALARM, b""
            if self.state != STATE_RUNNING:
                return ST_STATE, b""
            if rate_cuph > self.max_basal_cuph or not 5 <= duration <= self.max_temp_min:
                return ST_LIMIT, b""
            self.temp_rate_cuph = rate_cuph
            self.temp_end = self.minute + duration
            return ST_OK, b""
        # CMD_BOLUS
        (cu,) = struct.unpack(">H", p)
        if stopped:
            return ST_ALARM, b""
        if self.state != STATE_RUNNING:
            return ST_STATE, b""
        if cu <= 0 or cu > self.max_bolus_cu or cu % self.pulse_cu:
            return ST_LIMIT, b""
        if self.bolus_remaining_cu > 0 or cu > self.reservoir_cu:
            return ST_STATE, b""
        self.bolus_remaining_cu = cu
        return ST_OK, b""

    def _status_payload(self) -> bytes:
        temp_active = self._temp_active()
        flags = (FLAG_SUSPENDED if self.state == STATE_SUSPENDED else 0) | (FLAG_TEMP_ACTIVE if temp_active else 0)
        return STATUS_PAYLOAD.pack(
            self.minute & 0xFFFFFFFF, min(self.reservoir_cu, 65535), self._hal_battery_pct(), flags,
            self.temp_rate_cuph if temp_active else 0, max(0, self.temp_end - self.minute) if temp_active else 0,
            self.alarms & 0xFFFF, min(self._window_cu(), 65535), self.bolus_remaining_cu, self.state,
        )

    # ---------- delivery ----------

    def _temp_active(self) -> bool:
        return self.temp_rate_cuph is not None and self.minute < self.temp_end

    def _basal_at(self, minute: int) -> int:
        m = minute % 1440
        rate = self.basal[0][1]
        for start, r in self.basal:
            if start > m:
                break
            rate = r
        return rate

    def current_rate_uph(self, minute: int | None = None) -> float:
        m = self.minute if minute is None else minute
        if self.state != STATE_RUNNING or self.alarms & STOP_MASK:
            return 0.0
        if self.temp_rate_cuph is not None and m < self.temp_end:
            return self.temp_rate_cuph / 100
        return self._basal_at(m) / 100

    def _window_cu(self) -> int:
        total = 0
        for m, _, cu in reversed(self.history):
            if m <= self.minute - 60:
                break
            total += cu
        return total

    def _record(self, kind: int):
        last = self.history[-1] if self.history else None
        if last and last[0] == self.minute and last[1] == kind:
            last[2] += self.pulse_cu
        else:
            self.history.append([self.minute, kind, self.pulse_cu])

    def _pulse(self, kind: int) -> int | None:
        if self.reservoir_cu < self.pulse_cu:
            self._raise(ALARM_RESERVOIR_EMPTY)
            return None
        if self._window_cu() + self.pulse_cu > self.hourly_cap_cu:
            self._raise(ALARM_HOURLY_CAP)
            return None
        body = self._hal_pulse(kind)
        if body is None:
            self._raise(ALARM_MOTOR)
            return None
        self.reservoir_cu -= self.pulse_cu
        self._record(kind)
        return body

    def _raise(self, alarm: int):
        self.alarms |= alarm
        if alarm & STOP_MASK:
            self.bolus_remaining_cu = 0
            self.prime_remaining_cu = 0
            self.temp_rate_cuph = None
            self.basal_accum = 0      # never deliver a backlog after the alarm is cleared

    def tick(self, minute: int) -> float:
        # Advance to `minute` and run one minute of delivery; returns units that reached the body
        self.minute = minute
        if self.temp_rate_cuph is not None and minute >= self.temp_end:
            self.temp_rate_cuph = None
        body = 0
        pushed = 0
        due = self.pulse_cu * 60
        if not self.alarms & STOP_MASK:
            if self.state == STATE_RUNNING:
                self.basal_accum += self.temp_rate_cuph if self.temp_rate_cuph is not None else self._basal_at(minute)
                while self.basal_accum >= due:
                    r = self._pulse(KIND_BASAL)
                    if r is None:
                        break
                    self.basal_accum -= due
                    body += r
                    pushed += self.pulse_cu
                budget = min(self.bolus_remaining_cu, self.bolus_rate_cu)
                while budget >= self.pulse_cu and not self.alarms & STOP_MASK:
                    r = self._pulse(KIND_BOLUS)
                    if r is None:
                        break
                    budget -= self.pulse_cu
                    self.bolus_remaining_cu -= self.pulse_cu
                    body += r
                    pushed += self.pulse_cu
            elif self.state == STATE_PRIMING:
                budget = min(self.prime_remaining_cu, self.bolus_rate_cu)
                while budget >= self.pulse_cu and not self.alarms & STOP_MASK:
                    r = self._pulse(KIND_PRIME)
                    if r is None:
                        break
                    budget -= self.pulse_cu
                    self.prime_remaining_cu -= self.pulse_cu
                    self.primed_cu += self.pulse_cu
                    pushed += self.pulse_cu

        if pushed and self._hal_force() >= self.occlusion_threshold:
            self._raise(ALARM_OCCLUSION)

        low_res = self.state in (STATE_RUNNING, STATE_SUSPENDED) and self.reservoir_cu < RESERVOIR_LOW_CU
        self.alarms = (self.alarms | WARN_RESERVOIR_LOW) if low_res else (self.alarms & ~WARN_RESERVOIR_LOW)
        low_batt = self._hal_battery_pct() < BATTERY_LOW_PCT
        self.alarms = (self.alarms | WARN_BATTERY_LOW) if low_batt else (self.alarms & ~WARN_BATTERY_LOW)

        cutoff = minute - HISTORY_KEEP_MIN
        if self.history and self.history[0][0] < cutoff:
            del self.history[:bisect.bisect_left(self.history, [cutoff, -1, -1])]
        return body / 100

    def power_cycle(self):
        # Reboot with persisted state (reservoir, mode, alarms, history) but nothing in flight.
        # An interrupted bolus is never resumed; the controller sees RESTARTED and re-plans.
        self.temp_rate_cuph = None
        self.bolus_remaining_cu = 0
        self.prime_remaining_cu = 0
        self.basal_accum = 0
        self._last = (None, None, None)
        self.alarms |= WARN_RESTARTED

    # ---------- simulation hooks (physical events, not commands) ----------

    def inject_occlusion(self):
        self.line_occluded = True

    def replace_site(self):
        self.line_occluded = False
        self.undelivered_cu = 0

    def inject_motor_fault(self, on: bool = True):
        self.motor_fault = on
