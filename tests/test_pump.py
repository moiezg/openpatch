import struct
import unittest

from openpatch.config import Schedule
from openpatch.pump.base import PumpError
from openpatch.pump.driver import ProtocolPumpDriver
from openpatch.pump.firmware import FirmwareLimits, PumpFirmware
from openpatch.pump.protocol import (
    CMD_BOLUS, ST_AUTH, ST_CRC, ProtocolError, crc16_ccitt, decode, encode,
)

KEY = b"test-key-0123456789"


def make(limits=None, reservoir=200.0):
    fw = PumpFirmware(KEY, Schedule([(0, 1.2)]), limits or FirmwareLimits(), reservoir_u=reservoir)
    drv = ProtocolPumpDriver(fw.handle, KEY)
    drv.set_time(0)
    return fw, drv


def run(fw, start, end):
    return sum(fw.tick(m) for m in range(start, end))


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


class FirmwareTest(unittest.TestCase):
    def test_scheduled_basal_accuracy(self):
        fw, _ = make()
        self.assertAlmostEqual(run(fw, 0, 600), 1.2 * 10, delta=0.05)

    def test_temp_basal_expires_back_to_schedule(self):
        fw, drv = make()
        drv.set_temp_basal(0.0, 30)
        self.assertAlmostEqual(run(fw, 0, 30), 0.0)
        self.assertGreater(run(fw, 30, 90), 1.0)

    def test_bolus_delivered_in_full(self):
        fw, drv = make()
        drv.bolus(3.0)
        delivered = run(fw, 0, 10)
        self.assertAlmostEqual(delivered, 3.0 + 1.2 * 10 / 60, delta=0.06)

    def test_bolus_above_hard_limit_rejected(self):
        _, drv = make(FirmwareLimits(max_bolus_u=5))
        with self.assertRaises(PumpError):
            drv.bolus(6)

    def test_temp_above_hard_limit_rejected(self):
        _, drv = make(FirmwareLimits(max_basal_uph=2))
        with self.assertRaises(PumpError):
            drv.set_temp_basal(2.5, 30)

    def test_duplicate_frame_does_not_double_bolus(self):
        fw, _ = make()
        frame = encode(KEY, 42, CMD_BOLUS, struct.pack(">H", 200))
        fw.handle(frame)
        fw.handle(frame)   # BLE retry of the same command
        self.assertEqual(fw.bolus_remaining_cu, 200)

    def test_hourly_cap_stops_runaway_delivery(self):
        fw, drv = make(FirmwareLimits(hourly_cap_u=4.0, max_bolus_u=10))
        drv.bolus(10)
        delivered = run(fw, 0, 60)
        self.assertLessEqual(delivered, 4.0 + 1e-9)
        self.assertIn("HOURLY_CAP", drv.status().alarms)

    def test_occlusion_detected_and_blocks(self):
        fw, drv = make()
        fw.inject_occlusion()
        drv.bolus(2.0)
        self.assertEqual(run(fw, 0, 10), 0.0)
        self.assertIn("OCCLUSION", drv.status().alarms)
        with self.assertRaises(PumpError):
            drv.bolus(1.0)
        with self.assertRaises(PumpError):
            drv.clear_alarms()      # must physically replace the site first
        fw.replace_site()
        drv.clear_alarms()
        self.assertEqual(drv.status().alarms, [])

    def test_reservoir_empty_alarm(self):
        fw, drv = make(reservoir=0.5)
        run(fw, 0, 60)
        self.assertIn("RESERVOIR_EMPTY", drv.status().alarms)

    def test_history_matches_delivery(self):
        fw, drv = make()
        drv.bolus(1.0)
        total = run(fw, 0, 300)
        hist = drv.delivery_history(0)
        self.assertAlmostEqual(sum(r.units for r in hist), total, places=6)
        self.assertAlmostEqual(sum(r.units for r in hist if r.kind == "bolus"), 1.0)


if __name__ == "__main__":
    unittest.main()
