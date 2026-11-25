from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

# Wire protocol between controller and pump (identical over BLE and in simulation).
# Request : seq u8 | cmd u8 | len u8 | payload | hmac8 | crc16
# Response: seq u8 | cmd|0x80 u8 | status u8 | len u8 | payload | hmac8 | crc16
# The HMAC (SHA-256, truncated to 8 bytes, pre-shared key) stops any other BLE device from
# commanding insulin. The seq byte makes retries idempotent: a repeated frame returns the cached
# response instead of delivering a second bolus.

CMD_STATUS = 0x01
CMD_SET_TEMP = 0x02
CMD_CANCEL_TEMP = 0x03
CMD_BOLUS = 0x04
CMD_HISTORY = 0x05
CMD_SUSPEND = 0x06
CMD_RESUME = 0x07
CMD_SET_TIME = 0x08
CMD_CLEAR_ALARMS = 0x09
CMD_CANCEL_BOLUS = 0x0A

ST_OK = 0x00
ST_CRC = 0x01
ST_AUTH = 0x02
ST_LIMIT = 0x03
ST_STATE = 0x04
ST_UNKNOWN = 0x05
ST_ALARM = 0x06
ST_FORMAT = 0x07

STATUS_NAMES = {ST_OK: "ok", ST_CRC: "crc error", ST_AUTH: "authentication failed",
                ST_LIMIT: "exceeds pump hard limit", ST_STATE: "invalid in current state",
                ST_UNKNOWN: "unknown command", ST_ALARM: "pump alarm active", ST_FORMAT: "malformed payload"}

TAG_LEN = 8
HISTORY_RECORD = struct.Struct(">IHB")       # minute, centi-units, kind (0 basal, 1 bolus)
STATUS_PAYLOAD = struct.Struct(">IHBBHHHHH")  # minute, reservoir cU, battery %, flags, temp cU/h,
                                              # temp remaining min, alarms bitmask, last-hour cU, bolus remaining cU
FLAG_SUSPENDED = 0x01
FLAG_TEMP_ACTIVE = 0x02


class ProtocolError(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message or STATUS_NAMES.get(code, "protocol error"))
        self.code = code


@dataclass
class Frame:
    seq: int
    cmd: int
    payload: bytes
    status: int | None = None


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _tag(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, body, hashlib.sha256).digest()[:TAG_LEN]


def encode(key: bytes, seq: int, cmd: int, payload: bytes = b"", status: int | None = None) -> bytes:
    if len(payload) > 255:
        raise ValueError("payload too large")
    head = bytes([seq & 0xFF, cmd & 0xFF]) + (bytes([status]) if status is not None else b"") + bytes([len(payload)])
    body = head + payload
    signed = body + _tag(key, body)
    return signed + struct.pack(">H", crc16_ccitt(signed))


def decode(key: bytes, frame: bytes, response: bool) -> Frame:
    head_len = 4 if response else 3
    if len(frame) < head_len + TAG_LEN + 2:
        raise ProtocolError(ST_FORMAT, "frame too short")
    if crc16_ccitt(frame[:-2]) != struct.unpack(">H", frame[-2:])[0]:
        raise ProtocolError(ST_CRC)
    length = frame[head_len - 1]
    if len(frame) != head_len + length + TAG_LEN + 2:
        raise ProtocolError(ST_FORMAT, "length mismatch")
    body = frame[: head_len + length]
    if not hmac.compare_digest(_tag(key, body), frame[head_len + length: head_len + length + TAG_LEN]):
        raise ProtocolError(ST_AUTH)
    return Frame(seq=frame[0], cmd=frame[1], payload=body[head_len:], status=frame[2] if response else None)
