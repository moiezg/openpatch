import struct
import unittest

from openpatch.config import Schedule
from openpatch.pump.base import PumpError
from openpatch.pump.driver import ProtocolPumpDriver
from openpatch.pump.firmware import FirmwareLimits, PumpFirmware
from openpatch.pump.protocol import (
    CMD_BOLUS, CMD_SUSPEND, ST_AUTH, ST_CRC, ProtocolError, crc16_ccitt, decode, encode,
)

try:
    from openpatch.pump.cfirmware import CPumpFirmware, FirmwareBuildError, build_library
    build_library()
    C_AVAILABLE, C_REASON = True, ""
except Exception as e:  # no compiler on this machine
    CPumpFirmware, C_AVAILABLE, C_REASON = None, False, str(e)

KEY = b"test-key-0123456789"


class ProtocolTest(unittest.TestCase):
    def test_crc_known_value(self):
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)

    def test_round_trip(self):
        f = decode(KEY, encode(KEY, 7, CMD_BOLUS, b"\x01\x02"), response=False)
        self.assertEqual((f.seq, f.cmd, f.payload), (7, CMD_BOLUS, b"\x01\x02"))

    def test_corrupted_frame_rejected(self):
        frame = bytearray(encode(KEY, 1, CMD_BOLUS, b"\x00\x64"))
        frame[4] ^= 0xFF
        with self.assertRaises(ProtocolError) as e:
            decode(KEY, bytes(frame), response=False)
        self.assertEqual(e.exception.code, ST_CRC)

    def test_wrong_key_rejected(self):
        with self.assertRaises(ProtocolError) as e:
            decode(KEY, encode(b"attacker", 1, CMD_BOLUS, b"\x00\x64"), response=False)
        self.assertEqual(e.exception.code, ST_AUTH)


class FirmwareContract:
    # Behaviour every firmware implementation must have; run against the Python model and the C core
    impl = None

    def make(self, limits=None, reservoir=200.0):
        fw = self.impl(KEY, Schedule([(0, 1.2)]), limits or FirmwareLimits(), reservoir_u=reservoir)
        drv = ProtocolPumpDriver(fw.handle, KEY)
        drv.set_time(0)
        return fw, drv

    @staticmethod
    def tick_range(fw, start, end):
        return sum(fw.tick(m) for m in range(start, end))

    def test_scheduled_basal_accuracy(self):
        fw, _ = self.make()
        self.assertAlmostEqual(self.tick_range(fw, 0, 600), 1.2 * 10, delta=0.05)

    def test_temp_basal_expires_back_to_schedule(self):
        fw, drv = self.make()
        drv.set_temp_basal(0.0, 30)
        self.assertAlmostEqual(self.tick_range(fw, 0, 30), 0.0)
        self.assertGreater(self.tick_range(fw, 30, 90), 1.0)

    def test_bolus_delivered_in_full(self):
        fw, drv = self.make()
        drv.bolus(3.0)
        self.assertAlmostEqual(self.tick_range(fw, 0, 10), 3.0 + 1.2 * 10 / 60, delta=0.06)

    def test_bolus_above_hard_limit_rejected(self):
        _, drv = self.make(FirmwareLimits(max_bolus_u=5))
        with self.assertRaises(PumpError):
            drv.bolus(6)

    def test_temp_above_hard_limit_rejected(self):
        _, drv = self.make(FirmwareLimits(max_basal_uph=2))
        with self.assertRaises(PumpError):
            drv.set_temp_basal(2.5, 30)

    def test_duplicate_frame_does_not_double_bolus(self):
        fw, _ = self.make()
        frame = encode(KEY, 42, CMD_BOLUS, struct.pack(">H", 200))
        first = fw.handle(frame)
        second = fw.handle(frame)   # BLE retry of the same command
        self.assertEqual(first, second)
        self.assertEqual(fw.bolus_remaining_cu, 200)

    def test_hourly_cap_stops_runaway_delivery(self):
        fw, drv = self.make(FirmwareLimits(hourly_cap_u=4.0, max_bolus_u=10))
        drv.bolus(10)
        self.assertLessEqual(self.tick_range(fw, 0, 60), 4.0 + 1e-9)
        self.assertIn("HOURLY_CAP", drv.status().alarms)

    def test_no_backlog_after_cap_alarm_cleared(self):
        fw, drv = self.make(FirmwareLimits(hourly_cap_u=1.0))
        drv.bolus(2.0)
        self.tick_range(fw, 0, 120)
        drv.clear_alarms()
        self.assertLessEqual(self.tick_range(fw, 120, 121), 0.05 + 1e-9)

    def test_occlusion_detected_and_blocks(self):
        fw, drv = self.make()
        fw.inject_occlusion()
        drv.bolus(2.0)
        self.assertEqual(self.tick_range(fw, 0, 10), 0.0)
        self.assertIn("OCCLUSION", drv.status().alarms)
        with self.assertRaises(PumpError):
            drv.bolus(1.0)
        with self.assertRaises(PumpError):
            drv.clear_alarms()      # line still pressurised: must replace the site first
        fw.replace_site()
        drv.clear_alarms()
        self.assertEqual(drv.status().alarms, [])

    def test_motor_fault_stops_and_alarms(self):
        fw, drv = self.make()
        fw.inject_motor_fault()
        self.tick_range(fw, 0, 10)
        self.assertIn("MOTOR_FAULT", drv.status().alarms)

    def test_reservoir_empty_alarm(self):
        fw, drv = self.make(reservoir=0.5)
        self.tick_range(fw, 0, 60)
        self.assertIn("RESERVOIR_EMPTY", drv.status().alarms)

    def test_history_matches_delivery(self):
        fw, drv = self.make()
        drv.bolus(1.0)
        total = self.tick_range(fw, 0, 300)
        hist = drv.delivery_history(0)
        self.assertAlmostEqual(sum(r.units for r in hist), total, places=6)
        self.assertAlmostEqual(sum(r.units for r in hist if r.kind == "bolus"), 1.0)

    def test_reservoir_change_workflow(self):
        fw, drv = self.make()
        with self.assertRaises(PumpError):
            drv.rewind()                      # never while running
        drv.suspend()
        drv.rewind()
        self.assertEqual(drv.status().state, "no_reservoir")
        with self.assertRaises(PumpError):
            drv.bolus(1)
        with self.assertRaises(PumpError):
            drv.resume()                      # nothing to deliver from
        with self.assertRaises(PumpError):
            drv.set_reservoir(400)            # above physical capacity
        drv.set_reservoir(180)
        with self.assertRaises(PumpError):
            drv.resume()                      # must prime first
        drv.prime(1.0)
        self.assertEqual(self.tick_range(fw, 10, 12), 0.0)   # prime goes into the air, not the body
        drv.resume()
        self.assertEqual(drv.status().state, "running")
        self.assertAlmostEqual(drv.status().reservoir_u, 179.0)
        prime = [r for r in drv.delivery_history(0) if r.kind == "prime"]
        self.assertAlmostEqual(sum(r.units for r in prime), 1.0)

    def test_power_cycle_cancels_in_flight_and_flags_restart(self):
        fw, drv = self.make()
        drv.bolus(5.0)
        drv.set_temp_basal(3.0, 60)
        self.tick_range(fw, 0, 2)
        fw.power_cycle()
        st = drv.status()
        self.assertIn("RESTARTED", st.warnings)
        self.assertEqual(st.bolus_remaining, 0)
        self.assertIsNone(st.temp_rate)
        self.assertEqual(st.state, "running")       # scheduled basal continues
        self.assertGreater(self.tick_range(fw, 2, 62), 1.0)
        drv.clear_alarms()
        self.assertNotIn("RESTARTED", drv.status().warnings)

    def test_suspend_stops_everything(self):
        fw, drv = self.make()
        drv.bolus(2.0)
        fw.handle(encode(KEY, 200, CMD_SUSPEND))
        self.assertEqual(self.tick_range(fw, 0, 60), 0.0)


class PythonModelTest(FirmwareContract, unittest.TestCase):
    impl = PumpFirmware


@unittest.skipUnless(C_AVAILABLE, f"C firmware not built: {C_REASON}")
class CFirmwareTest(FirmwareContract, unittest.TestCase):
    impl = CPumpFirmware


if __name__ == "__main__":
    unittest.main()
