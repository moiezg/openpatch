import random
import struct
import unittest

from openpatch.config import Schedule
from openpatch.pump.firmware import FirmwareLimits, PumpFirmware
from openpatch.pump.protocol import (
    CMD_BOLUS, CMD_CANCEL_BOLUS, CMD_CANCEL_TEMP, CMD_CLEAR_ALARMS, CMD_HISTORY, CMD_PRIME, CMD_RESUME,
    CMD_REWIND, CMD_SET_RESERVOIR, CMD_SET_TEMP, CMD_SET_TIME, CMD_STATUS, CMD_SUSPEND, encode,
)

try:
    from openpatch.pump.cfirmware import CPumpFirmware, build_library
    build_library()
    C_AVAILABLE, C_REASON = True, ""
except Exception as e:
    C_AVAILABLE, C_REASON = False, str(e)


def random_payload(rng: random.Random, cmd: int, fw_minute: int) -> bytes:
    if cmd == CMD_SET_TEMP:
        return struct.pack(">HH", rng.choice([0, 50, 120, 250, 300, 301, 900]), rng.choice([0, 4, 5, 30, 120, 121]))
    if cmd == CMD_BOLUS:
        return struct.pack(">H", rng.choice([0, 3, 5, 50, 100, 335, 1000, 1001, 5000]))
    if cmd == CMD_HISTORY:
        return struct.pack(">IB", max(0, fw_minute - rng.randint(0, 200)), rng.choice([0, 1, 5, 20, 30, 255]))
    if cmd == CMD_SET_TIME:
        return struct.pack(">I", max(0, fw_minute + rng.randint(-30, 120)))
    if cmd == CMD_SET_RESERVOIR:
        return struct.pack(">H", rng.choice([0, 999, 1000, 15000, 30000, 30001]))
    if cmd == CMD_PRIME:
        return struct.pack(">H", rng.choice([0, 3, 5, 50, 300, 305]))
    return b""


COMMANDS = [CMD_STATUS, CMD_SET_TEMP, CMD_CANCEL_TEMP, CMD_BOLUS, CMD_HISTORY, CMD_SUSPEND, CMD_RESUME,
            CMD_SET_TIME, CMD_CLEAR_ALARMS, CMD_CANCEL_BOLUS, CMD_REWIND, CMD_SET_RESERVOIR, CMD_PRIME, 0x7F]


@unittest.skipUnless(C_AVAILABLE, f"C firmware not built: {C_REASON}")
class FirmwareParityTest(unittest.TestCase):
    # The C firmware must be indistinguishable from the Python executable spec

    def run_stream(self, seed: int, steps: int = 1500):
        rng = random.Random(seed)
        key = bytes(rng.randrange(256) for _ in range(rng.choice([16, 32, 64])))
        entries = sorted({0, *rng.sample(range(30, 1440, 30), rng.randint(0, 5))})
        basal = Schedule([(m, rng.choice([0.05, 0.35, 0.8, 1.0, 1.25, 2.0])) for m in entries])
        limits = FirmwareLimits(max_basal_uph=rng.choice([2.0, 3.0]), max_bolus_u=rng.choice([5.0, 10.0]),
                                hourly_cap_u=rng.choice([3.0, 8.0, 12.0]),
                                occlusion_threshold_u=rng.choice([0.5, 1.0]))
        reservoir = rng.choice([0, 0.4, 5.0, 200.0])
        start = rng.randint(0, 3000)
        py = PumpFirmware(key, basal, limits, reservoir, start)
        c = CPumpFirmware(key, basal, limits, reservoir, start)
        minute = start
        seq = 0
        last = None
        for step in range(steps):
            action = rng.random()
            ctx = f"seed {seed} step {step}"
            if action < 0.40:
                minute += rng.choice([1, 1, 1, 2, 5])
                self.assertEqual(round(py.tick(minute) * 100), round(c.tick(minute) * 100), ctx)
            elif action < 0.88:
                cmd = rng.choice(COMMANDS)
                payload = random_payload(rng, cmd, py.minute)
                if rng.random() < 0.05:
                    payload += b"\x00"                        # wrong length
                seq = (seq + 1) & 0xFF
                frame = encode(key if rng.random() > 0.03 else b"wrong-key", seq, cmd, payload)
                r = rng.random()
                if r < 0.03 and len(frame) > 5:
                    frame = frame[:4] + bytes([frame[4] ^ 0x40]) + frame[5:]   # corrupt
                elif r < 0.05:
                    frame = frame[: rng.randint(0, len(frame) - 1)]            # truncated
                elif r < 0.10 and last is not None:
                    frame = last                                                # BLE retry
                last = frame
                self.assertEqual(py.handle(frame), c.handle(frame), f"{ctx} cmd {cmd:#x}")
            elif action < 0.92:
                py.inject_occlusion(); c.inject_occlusion()
            elif action < 0.96:
                py.replace_site(); c.replace_site()
            elif action < 0.98:
                on = rng.random() < 0.5
                py.inject_motor_fault(on); c.inject_motor_fault(on)
            else:
                py.power_cycle(); c.power_cycle()
            self.assertEqual(py.alarms, c.alarms, ctx)
            self.assertEqual(py.reservoir_cu, c.reservoir_cu, ctx)
            self.assertEqual(py.state, c.state, ctx)
        self.assertEqual(py.history, c.history, f"seed {seed} final history")

    def test_random_streams_are_byte_identical(self):
        for seed in range(60):
            self.run_stream(seed)

    def test_long_running_history_and_window(self):
        # two simulated days of continuous delivery with boluses: history pruning and the hourly window
        rng = random.Random(99)
        key = b"k" * 32
        basal = Schedule([(0, 1.0), (360, 1.5), (1080, 0.8)])
        py = PumpFirmware(key, basal, FirmwareLimits(), 250.0, 0)
        c = CPumpFirmware(key, basal, FirmwareLimits(), 250.0, 0)
        seq = 0
        for minute in range(1, 3 * 1440):
            if rng.random() < 0.01:
                seq = (seq + 1) & 0xFF
                f = encode(key, seq, CMD_BOLUS, struct.pack(">H", rng.choice([100, 300, 500])))
                self.assertEqual(py.handle(f), c.handle(f))
            self.assertEqual(py.tick(minute), c.tick(minute), minute)
        self.assertEqual(py.history, c.history)
        self.assertEqual(py.reservoir_cu, c.reservoir_cu)


    def test_closed_loop_simulation_identical_on_c_firmware(self):
        from openpatch.sim.runner import run_simulation
        for scenario in ("typical_day", "occlusion"):
            py = run_simulation(scenario, "teen_55kg", 2, True, 4, firmware="python")
            c = run_simulation(scenario, "teen_55kg", 2, True, 4, firmware="c")
            self.assertEqual([s["bg"] for s in py["series"]], [s["bg"] for s in c["series"]], scenario)
            self.assertEqual(py["events"], c["events"], scenario)


if __name__ == "__main__":
    unittest.main()
