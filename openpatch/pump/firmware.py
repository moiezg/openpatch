from __future__ import annotations

import bisect
import struct
from collections import deque
from dataclasses import dataclass

from ..config import Schedule
from .protocol import (
    CMD_BOLUS, CMD_CANCEL_BOLUS, CMD_CANCEL_TEMP, CMD_CLEAR_ALARMS, CMD_HISTORY, CMD_RESUME, CMD_SET_TEMP,
    CMD_SET_TIME, CMD_STATUS, CMD_SUSPEND, FLAG_SUSPENDED, FLAG_TEMP_ACTIVE, HISTORY_RECORD, STATUS_PAYLOAD,
    ST_ALARM, ST_FORMAT, ST_LIMIT, ST_OK, ST_STATE, ST_UNKNOWN, ProtocolError, decode, encode,
)

# Executable reference model of the pump firmware. The C firmware on the nRF52840 must behave
# exactly like this class; the same tests run against both (see docs/SOFTWARE_SPEC.md).

ALARM_OCCLUSION = 1 << 0
ALARM_RESERVOIR_EMPTY = 1 << 1
ALARM_HOURLY_CAP = 1 << 2
ALARM_MOTOR = 1 << 3
WARN_RESERVOIR_LOW = 1 << 8
WARN_BATTERY_LOW = 1 << 9

STOP_MASK = ALARM_OCCLUSION | ALARM_RESERVOIR_EMPTY | ALARM_HOURLY_CAP | ALARM_MOTOR

ALARM_NAMES = {
    ALARM_OCCLUSION: "OCCLUSION", ALARM_RESERVOIR_EMPTY: "RESERVOIR_EMPTY",
    ALARM_HOURLY_CAP: "HOURLY_CAP", ALARM_MOTOR: "MOTOR_FAULT",
    WARN_RESERVOIR_LOW: "RESERVOIR_LOW", WARN_BATTERY_LOW: "BATTERY_LOW",
}

KIND_BASAL, KIND_BOLUS = 0, 1
HISTORY_KEEP_MIN = 2 * 1440


@dataclass
class FirmwareLimits:
    max_basal_uph: float = 3.0
    max_bolus_u: float = 10.0
    hourly_cap_u: float = 12.0          # total delivery (basal + bolus) in any rolling 60 minutes
    max_temp_duration_min: int = 120
    pulse_u: float = 0.05
    bolus_rate_u_per_min: float = 1.5
    occlusion_threshold_u: float = 1.0  # undelivered volume before the force sensor trips


class PumpFirmware:
    def __init__(self, key: bytes, basal: Schedule, limits: FirmwareLimits | None = None,
                 reservoir_u: float = 200.0, start_minute: int = 0):
        self.key = key
        self.basal = basal
        self.limits = limits or FirmwareLimits()
        self.pulse_cu = round(self.limits.pulse_u * 100)
        self.minute = start_minute
        self.reservoir_cu = round(reservoir_u * 100)
        self.battery_pct = 100.0
        self.suspended = False
        self.temp_rate_cuph: int | None = None
        self.temp_end = 0
        self.bolus_remaining_cu = 0
        self.basal_accum_cu = 0.0
        self.alarms = 0
        self.history: list[list[int]] = []     # [minute, kind, centi-units]
        self.window: deque = deque()           # (minute, centi-units) for the rolling hourly cap
        self.occlusion_fault = False
        self.undelivered_cu = 0
        self._last = (None, None, None)        # seq, request frame, response frame

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
        try:
            status, payload = self._execute(req.cmd, req.payload)
        except struct.error:
            status, payload = ST_FORMAT, b""
        resp = encode(self.key, req.seq, req.cmd | 0x80, payload, status)
        self._last = (req.seq, frame, resp)
        return resp

    def _execute(self, cmd: int, p: bytes):
        lim = self.limits
        if cmd == CMD_STATUS:
            return ST_OK, self._status_payload()
        if cmd == CMD_HISTORY:
            since, max_n = struct.unpack(">IB", p)
            i = bisect.bisect_left(self.history, [since, -1, -1])
            recs = self.history[i: i + min(max_n, 30)]
            return ST_OK, bytes([len(recs)]) + b"".join(HISTORY_RECORD.pack(m, cu, kind) for m, kind, cu in recs)
        if cmd == CMD_SET_TIME:
            (minute,) = struct.unpack(">I", p)
            if self.history and minute < self.history[-1][0]:
                return ST_STATE, b""
            self.minute = minute
            return ST_OK, b""
        if cmd == CMD_CLEAR_ALARMS:
            if self.occlusion_fault:
                return ST_STATE, b""          # site/reservoir must be physically replaced first
            self.alarms &= ~STOP_MASK
            self.undelivered_cu = 0
            return ST_OK, b""
        if cmd == CMD_SUSPEND:
            self.suspended = True
            self.temp_rate_cuph = None
            self.bolus_remaining_cu = 0
            return ST_OK, b""
        if cmd == CMD_RESUME:
            self.suspended = False
            return ST_OK, b""
        if cmd == CMD_CANCEL_BOLUS:
            self.bolus_remaining_cu = 0
            return ST_OK, b""
        if cmd == CMD_CANCEL_TEMP:
            self.temp_rate_cuph = None
            return ST_OK, b""
        if cmd in (CMD_SET_TEMP, CMD_BOLUS):
            if self.alarms & STOP_MASK:
                return ST_ALARM, b""
            if self.suspended:
                return ST_STATE, b""
        if cmd == CMD_SET_TEMP:
            rate_cuph, duration = struct.unpack(">HH", p)
            if rate_cuph > round(lim.max_basal_uph * 100) or not 5 <= duration <= lim.max_temp_duration_min:
                return ST_LIMIT, b""
            self.temp_rate_cuph = rate_cuph
            self.temp_end = self.minute + duration
            return ST_OK, b""
        if cmd == CMD_BOLUS:
            (cu,) = struct.unpack(">H", p)
            if cu <= 0 or cu > round(lim.max_bolus_u * 100) or cu % self.pulse_cu:
                return ST_LIMIT, b""
            if self.bolus_remaining_cu > 0 or cu > self.reservoir_cu:
                return ST_STATE, b""
            self.bolus_remaining_cu = cu
            return ST_OK, b""
        return ST_UNKNOWN, b""

    def _status_payload(self) -> bytes:
        flags = (FLAG_SUSPENDED if self.suspended else 0) | (FLAG_TEMP_ACTIVE if self._temp_active() else 0)
        temp = self.temp_rate_cuph if self._temp_active() else 0
        remaining = max(0, self.temp_end - self.minute) if self._temp_active() else 0
        return STATUS_PAYLOAD.pack(
            int(self.minute), min(self.reservoir_cu, 65535), int(self.battery_pct), flags, temp, remaining,
            self.alarms & 0xFFFF, min(self._window_cu(), 65535), self.bolus_remaining_cu,
        )

    # ---------- delivery ----------

    def _temp_active(self) -> bool:
        return self.temp_rate_cuph is not None and self.minute < self.temp_end

    def current_rate_uph(self, minute: int | None = None) -> float:
        m = self.minute if minute is None else minute
        if self.suspended or self.alarms & STOP_MASK:
            return 0.0
        if self.temp_rate_cuph is not None and m < self.temp_end:
            return self.temp_rate_cuph / 100
        return self.basal.at(m)

    def _window_cu(self) -> int:
        while self.window and self.window[0][0] <= self.minute - 60:
            self.window.popleft()
        return sum(cu for _, cu in self.window)

    def _pulse(self, kind: int) -> bool:
        if self.reservoir_cu < self.pulse_cu:
            self._raise(ALARM_RESERVOIR_EMPTY)
            return False
        if self._window_cu() + self.pulse_cu > round(self.limits.hourly_cap_u * 100):
            self._raise(ALARM_HOURLY_CAP)
            return False
        self.reservoir_cu -= self.pulse_cu
        self.battery_pct = max(0.0, self.battery_pct - 0.0004)
        self.window.append((self.minute, self.pulse_cu))
        if self.history and self.history[-1][0] == self.minute and self.history[-1][1] == kind:
            self.history[-1][2] += self.pulse_cu
        else:
            self.history.append([self.minute, kind, self.pulse_cu])
        return True

    def _raise(self, alarm: int):
        self.alarms |= alarm
        if alarm & STOP_MASK:
            self.bolus_remaining_cu = 0
            self.temp_rate_cuph = None

    def tick(self, minute: int) -> float:
        # Advance one minute; returns units that actually reached the body
        self.minute = minute
        if self.temp_rate_cuph is not None and minute >= self.temp_end:
            self.temp_rate_cuph = None
        pushed = 0
        if not self.suspended and not self.alarms & STOP_MASK:
            self.basal_accum_cu += self.current_rate_uph(minute) * 100 / 60
            while self.basal_accum_cu >= self.pulse_cu - 1e-9:
                if not self._pulse(KIND_BASAL):
                    break
                self.basal_accum_cu -= self.pulse_cu
                pushed += self.pulse_cu
            budget = min(self.bolus_remaining_cu, round(self.limits.bolus_rate_u_per_min * 100))
            while budget >= self.pulse_cu and not self.alarms & STOP_MASK:
                if not self._pulse(KIND_BOLUS):
                    break
                budget -= self.pulse_cu
                self.bolus_remaining_cu -= self.pulse_cu
                pushed += self.pulse_cu

        if self.occlusion_fault and pushed:
            self.undelivered_cu += pushed
            if self.undelivered_cu >= round(self.limits.occlusion_threshold_u * 100):
                self._raise(ALARM_OCCLUSION)
            pushed = 0

        if self.reservoir_cu < 2000:
            self.alarms |= WARN_RESERVOIR_LOW
        if self.battery_pct < 15:
            self.alarms |= WARN_BATTERY_LOW
        cutoff = minute - HISTORY_KEEP_MIN
        if self.history and self.history[0][0] < cutoff:
            i = bisect.bisect_left(self.history, [cutoff, -1, -1])
            del self.history[:i]
        return pushed / 100

    # ---------- simulation hooks (physical events, not commands) ----------

    def inject_occlusion(self):
        self.occlusion_fault = True

    def replace_site(self):
        self.occlusion_fault = False
        self.undelivered_cu = 0
