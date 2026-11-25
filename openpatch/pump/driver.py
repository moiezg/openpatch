from __future__ import annotations

import struct
from typing import Callable

from .base import DeliveryRecord, PumpDriver, PumpError, PumpStatus
from .firmware import ALARM_NAMES, STOP_MASK
from .protocol import (
    CMD_BOLUS, CMD_CANCEL_TEMP, CMD_CLEAR_ALARMS, CMD_HISTORY, CMD_RESUME, CMD_SET_TEMP, CMD_SET_TIME,
    CMD_STATUS, CMD_SUSPEND, FLAG_SUSPENDED, FLAG_TEMP_ACTIVE, HISTORY_RECORD, STATUS_NAMES, STATUS_PAYLOAD,
    ST_OK, ProtocolError, decode, encode,
)

Transport = Callable[[bytes], bytes]


class ProtocolPumpDriver(PumpDriver):
    # One driver for every pump that speaks the OpenPatch protocol. The transport is either the
    # in-process firmware model (simulation) or a BLE characteristic write/notify (real hardware).

    def __init__(self, transport: Transport, key: bytes, retries: int = 2):
        self.transport = transport
        self.key = key
        self.retries = retries
        self._seq = 0
        self._cache: dict[tuple[int, int], int] = {}
        self._cursor = 0
        self._fetched_from: int | None = None

    def _call(self, cmd: int, payload: bytes = b"") -> bytes:
        self._seq = (self._seq + 1) & 0xFF
        frame = encode(self.key, self._seq, cmd, payload)
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                resp = decode(self.key, self.transport(frame), response=True)
            except (OSError, TimeoutError, ProtocolError) as e:
                last_error = e          # resend the identical frame: the pump de-duplicates by seq
                continue
            if resp.seq != self._seq or resp.cmd != (cmd | 0x80):
                last_error = PumpError("response does not match request")
                continue
            if resp.status != ST_OK:
                raise PumpError(STATUS_NAMES.get(resp.status, f"status {resp.status}"))
            return resp.payload
        raise PumpError(f"no valid response from pump: {last_error}")

    def set_time(self, minute: int):
        self._call(CMD_SET_TIME, struct.pack(">I", int(minute)))

    def status(self) -> PumpStatus:
        (minute, res_cu, batt, flags, temp_cu, temp_rem, alarms, hour_cu, bolus_cu) = \
            STATUS_PAYLOAD.unpack(self._call(CMD_STATUS))
        stop = [n for bit, n in ALARM_NAMES.items() if alarms & bit and bit & STOP_MASK]
        warn = [n for bit, n in ALARM_NAMES.items() if alarms & bit and not bit & STOP_MASK]
        return PumpStatus(
            minute=minute, reservoir_u=res_cu / 100, battery_pct=batt, suspended=bool(flags & FLAG_SUSPENDED),
            temp_rate=temp_cu / 100 if flags & FLAG_TEMP_ACTIVE else None, temp_remaining_min=temp_rem,
            delivered_last_hour=hour_cu / 100, bolus_remaining=bolus_cu / 100, alarms=stop, warnings=warn,
        )

    def set_temp_basal(self, rate_uph: float, duration_min: int):
        self._call(CMD_SET_TEMP, struct.pack(">HH", round(rate_uph * 100), int(duration_min)))

    def cancel_temp_basal(self):
        self._call(CMD_CANCEL_TEMP)

    def bolus(self, units: float):
        cu = round(units * 100)
        cu -= cu % 5
        if cu <= 0:
            raise PumpError("bolus below one pulse")
        self._call(CMD_BOLUS, struct.pack(">H", cu))

    def suspend(self):
        self._call(CMD_SUSPEND)

    def resume(self):
        self._call(CMD_RESUME)

    def clear_alarms(self):
        self._call(CMD_CLEAR_ALARMS)

    def delivery_history(self, since: float) -> list[DeliveryRecord]:
        # Incremental: only minutes at or after the last one seen are re-read from the pump
        if self._fetched_from is None or since < self._fetched_from:
            self._cache.clear()
            self._fetched_from = max(0, int(since))
            self._cursor = self._fetched_from
        cursor = self._cursor
        while True:
            p = self._call(CMD_HISTORY, struct.pack(">IB", cursor, 30))
            n = p[0]
            last = cursor
            for i in range(n):
                m, cu, kind = HISTORY_RECORD.unpack_from(p, 1 + i * HISTORY_RECORD.size)
                self._cache[(m, kind)] = cu
                last = max(last, m)
            if n < 30 or last == cursor:
                break
            cursor = last
        self._cursor = last
        horizon = int(since) - 60
        if horizon > self._fetched_from:
            for key in [k for k in self._cache if k[0] < horizon]:
                del self._cache[key]
            self._fetched_from = horizon
        return sorted((DeliveryRecord(float(m), cu / 100, "bolus" if kind else "basal")
                       for (m, kind), cu in self._cache.items() if m >= since), key=lambda r: r.time)
