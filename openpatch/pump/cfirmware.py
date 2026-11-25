from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Schedule
from .firmware import FirmwareLimits

# Loads firmware/core (the C code that runs on the pump) as a host shared library, so the same
# tests and simulations can run against the real firmware logic instead of the Python model.

ROOT = Path(__file__).resolve().parents[2]
FW_DIR = ROOT / "firmware"
SOURCES = ["core/op_protocol.c", "core/op_fw.c", "safety/sm_core.c", "host/op_host.c"]
CFLAGS = ["-std=c11", "-O2", "-fPIC", "-shared", "-fvisibility=hidden", "-Wall", "-Wextra", "-Wpedantic",
          "-Werror", "-Wshadow", "-Wstrict-prototypes", "-Wconversion", "-Wno-sign-conversion"]

_lib = None


class FirmwareBuildError(RuntimeError):
    pass


def library_path() -> Path:
    ext = {"darwin": ".dylib", "win32": ".dll"}.get(sys.platform, ".so")
    return Path(os.environ.get("OPENPATCH_FW_LIB", FW_DIR / "build" / "host" / f"libopenpatch_fw{ext}"))


def build_library(force: bool = False) -> Path:
    out = library_path()
    srcs = [FW_DIR / s for s in SOURCES]
    headers = list((FW_DIR / "core").glob("*.h")) + list((FW_DIR / "safety").glob("*.h"))
    if out.exists() and not force and all(out.stat().st_mtime >= p.stat().st_mtime for p in srcs + headers):
        return out
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        raise FirmwareBuildError("no C compiler found (set CC)")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [cc, *CFLAGS, f"-I{FW_DIR / 'core'}", f"-I{FW_DIR / 'safety'}", "-o", str(out), *map(str, srcs)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise FirmwareBuildError(res.stderr.strip() or "compiler failed")
    return out


def load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    lib = ctypes.CDLL(str(build_library()))
    vp, u8p, u16p, u32p = ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint16), \
        ctypes.POINTER(ctypes.c_uint32)
    sig = {
        "oph_new": (vp, [u8p, ctypes.c_int, u16p, u16p, ctypes.c_int, u16p, ctypes.c_uint16, ctypes.c_uint32]),
        "oph_free": (None, [vp]),
        "oph_handle": (ctypes.c_int, [vp, u8p, ctypes.c_int, u8p, ctypes.c_int]),
        "oph_tick": (ctypes.c_int, [vp, ctypes.c_uint32]),
        "oph_local_command": (ctypes.c_int, [vp, ctypes.c_uint8, u8p, ctypes.c_int]),
        "oph_inject_occlusion": (None, [vp]),
        "oph_replace_site": (None, [vp]),
        "oph_inject_motor_fault": (None, [vp, ctypes.c_int]),
        "oph_power_cycle": (None, [vp]),
        "oph_current_rate_cuph": (ctypes.c_int, [vp, ctypes.c_uint32]),
        "oph_get": (ctypes.c_uint32, [vp, ctypes.c_int]),
        "oph_history": (ctypes.c_int, [vp, u32p, u16p, u8p, ctypes.c_int]),
        "oph_sha256": (None, [u8p, ctypes.c_int, u8p]),
        "smh_size": (ctypes.c_int, []),
        "smh_init": (None, [vp, ctypes.c_uint32, ctypes.c_uint32] + [ctypes.c_uint16] * 7),
        "smh_tick": (None, [vp, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint8, ctypes.c_uint16,
                            ctypes.c_uint8, ctypes.c_uint8, u8p]),
    }
    for name, (res, args) in sig.items():
        fn = getattr(lib, name)
        fn.restype = res
        fn.argtypes = args
    _lib = lib
    return lib


def _buf(data: bytes):
    return (ctypes.c_uint8 * max(1, len(data))).from_buffer_copy(data or b"\0")


F_ALARMS, F_BOLUS_REMAINING, F_RESERVOIR, F_STATE, F_MINUTE, F_BATTERY, F_PRIMED, F_HIST_COUNT = range(8)


class CPumpFirmware:
    # Same interface as openpatch.pump.firmware.PumpFirmware, backed by firmware/core in C

    def __init__(self, key: bytes, basal: Schedule, limits: FirmwareLimits | None = None,
                 reservoir_u: float = 200.0, start_minute: int = 0):
        self._lib = load()
        self.limits = limits or FirmwareLimits()
        starts = [int(m) for m, _ in basal.entries]
        rates = [round(v * 100) for _, v in basal.entries]
        lim = self.limits.centi()
        h = self._lib.oph_new(_buf(key), len(key), (ctypes.c_uint16 * len(starts))(*starts),
                              (ctypes.c_uint16 * len(rates))(*rates), len(starts), (ctypes.c_uint16 * 7)(*lim),
                              round(reservoir_u * 100), start_minute)
        if not h:
            raise ValueError("firmware rejected the configuration")
        self._h = ctypes.c_void_p(h)

    def __del__(self):
        h = getattr(self, "_h", None)
        if h:
            self._lib.oph_free(h)
            self._h = None

    def handle(self, frame: bytes) -> bytes:
        out = (ctypes.c_uint8 * 300)()
        n = self._lib.oph_handle(self._h, _buf(frame), len(frame), out, len(out))
        return bytes(out[:n])

    def tick(self, minute: int) -> float:
        return self._lib.oph_tick(self._h, minute) / 100

    def local_command(self, cmd: int, payload: bytes = b"") -> int:
        return self._lib.oph_local_command(self._h, cmd, _buf(payload), len(payload))

    def current_rate_uph(self, minute: int | None = None) -> float:
        m = self.minute if minute is None else minute
        return self._lib.oph_current_rate_cuph(self._h, m) / 100

    def inject_occlusion(self):
        self._lib.oph_inject_occlusion(self._h)

    def replace_site(self):
        self._lib.oph_replace_site(self._h)

    def inject_motor_fault(self, on: bool = True):
        self._lib.oph_inject_motor_fault(self._h, 1 if on else 0)

    def power_cycle(self):
        self._lib.oph_power_cycle(self._h)

    def _get(self, field: int) -> int:
        return self._lib.oph_get(self._h, field)

    alarms = property(lambda self: self._get(F_ALARMS))
    bolus_remaining_cu = property(lambda self: self._get(F_BOLUS_REMAINING))
    reservoir_cu = property(lambda self: self._get(F_RESERVOIR))
    state = property(lambda self: self._get(F_STATE))
    minute = property(lambda self: self._get(F_MINUTE))
    battery_micro = property(lambda self: self._get(F_BATTERY))
    primed_cu = property(lambda self: self._get(F_PRIMED))

    @property
    def history(self) -> list[list[int]]:
        n = self._get(F_HIST_COUNT)
        mins, cus, kinds = (ctypes.c_uint32 * max(n, 1))(), (ctypes.c_uint16 * max(n, 1))(), (ctypes.c_uint8 * max(n, 1))()
        n = self._lib.oph_history(self._h, mins, cus, kinds, n)
        return [[mins[i], kinds[i], cus[i]] for i in range(n)]


def sha256(data: bytes) -> bytes:
    out = (ctypes.c_uint8 * 32)()
    load().oph_sha256(_buf(data), len(data), out)
    return bytes(out)


class SafetyMCU:
    # Host wrapper around firmware/safety/sm_core.c (the ATtiny1616 logic)

    def __init__(self, max_steps_per_min: int, max_steps_per_hour: int, hb_timeout_ms: int = 2000,
                 rearm_ms: int = 3000, clear_hold_ms: int = 5000, boot_grace_ms: int = 10000,
                 vbat_warn_mv: int = 3500, vbat_cutoff_mv: int = 3300, vbat_hyst_mv: int = 150):
        self._lib = load()
        self._buf = ctypes.create_string_buffer(self._lib.smh_size())
        self._lib.smh_init(self._buf, max_steps_per_min, max_steps_per_hour, hb_timeout_ms, rearm_ms,
                           clear_hold_ms, boot_grace_ms, vbat_warn_mv, vbat_cutoff_mv, vbat_hyst_mv)

    def tick(self, elapsed_ms: int = 10, steps: int = 0, heartbeat: bool = False, vbat_mv: int = 3900,
             button: bool = False, alarm_request: bool = False) -> tuple[int, int, int]:
        out = (ctypes.c_uint8 * 3)()
        self._lib.smh_tick(self._buf, elapsed_ms, steps, 1 if heartbeat else 0, vbat_mv, 1 if button else 0,
                           1 if alarm_request else 0, out)
        return out[0], out[1], out[2]
