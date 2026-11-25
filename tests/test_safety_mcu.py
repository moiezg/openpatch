import unittest

try:
    from openpatch.pump.cfirmware import SafetyMCU, build_library
    build_library()
    C_AVAILABLE, C_REASON = True, ""
except Exception as e:
    C_AVAILABLE, C_REASON = False, str(e)

STEPS_PER_PULSE = 14
MAX_PER_MIN = 2 * 32 * STEPS_PER_PULSE  # 2x (1.5 U/min burst + margin): see sm_core.h
MAX_PER_HOUR = 260 * STEPS_PER_PULSE    # 13 U/h hard ceiling
SM_FAULT_WAITING, SM_FAULT_HEARTBEAT, SM_FAULT_RATE, SM_FAULT_HOURLY, SM_FAULT_BATTERY, SM_FAULT_BATTERY_LOW = \
    1, 2, 4, 8, 16, 32
BUZZER_OFF, BUZZER_CONTINUOUS, BUZZER_BEEP, BUZZER_CHIRP = 0, 1, 2, 3


@unittest.skipUnless(C_AVAILABLE, f"C firmware not built: {C_REASON}")
class SafetyMCUTest(unittest.TestCase):
    def setUp(self):
        self.sm = SafetyMCU(MAX_PER_MIN, MAX_PER_HOUR)

    def run_ms(self, ms, **kw):
        out = None
        hb = kw.pop("heartbeat_every", 250)
        for t in range(0, ms, 10):
            beat = hb is not None and t % hb == 0
            out = self.sm.tick(10, heartbeat=beat, **kw)
        return out

    def armed(self):
        return self.run_ms(3500)

    def test_motor_stays_off_until_heartbeat_handshake(self):
        enable, buzzer, faults = self.run_ms(2000)
        self.assertEqual(enable, 0)
        self.assertEqual(buzzer, BUZZER_OFF)               # silent during boot
        self.assertTrue(faults & SM_FAULT_WAITING)
        enable, _, _ = self.run_ms(1500)
        self.assertEqual(enable, 1)

    def test_no_heartbeat_after_boot_grace_alarms(self):
        enable, buzzer, faults = self.run_ms(11000, heartbeat_every=None)
        self.assertEqual(enable, 0)
        self.assertEqual(buzzer, BUZZER_CONTINUOUS)
        self.assertTrue(faults & SM_FAULT_HEARTBEAT)

    def test_heartbeat_loss_cuts_power_then_rearms(self):
        self.armed()
        enable, buzzer, faults = self.run_ms(2100, heartbeat_every=None)
        self.assertEqual(enable, 0)
        self.assertEqual(buzzer, BUZZER_CONTINUOUS)
        self.assertTrue(faults & SM_FAULT_HEARTBEAT)
        enable, buzzer, _ = self.run_ms(3500)               # main MCU rebooted and is beating again
        self.assertEqual(enable, 1)
        self.assertEqual(buzzer, BUZZER_OFF)

    def test_runaway_motor_latched_within_a_minute(self):
        self.armed()
        out = None
        for t in range(0, 60000, 10):                      # 1000 steps/s: a runaway driver
            out = self.sm.tick(10, steps=10, heartbeat=(t % 250 == 0))
            if out[0] == 0:
                break
        self.assertEqual(out[0], 0)
        self.assertLess(t, 1000)                            # caught in under a second
        self.assertTrue(out[2] & SM_FAULT_RATE)
        enable, buzzer, _ = self.run_ms(10000)              # heartbeat alone does not clear it
        self.assertEqual(enable, 0)
        self.assertEqual(buzzer, BUZZER_CONTINUOUS)

    def test_latched_fault_needs_deliberate_long_press(self):
        self.armed()
        self.sm.tick(10, steps=MAX_PER_MIN + 1, heartbeat=True)
        self.assertEqual(self.run_ms(3000, button=True)[0], 0)     # 3 s press is not enough
        self.run_ms(100)
        enable, buzzer, faults = self.run_ms(5100, button=True)
        self.assertEqual(enable, 1)
        self.assertFalse(faults & (SM_FAULT_RATE | SM_FAULT_HOURLY))

    def test_normal_bolus_rate_is_allowed(self):
        self.armed()
        for t in range(0, 120000, 10):                     # 1.5 U/min for two minutes
            out = self.sm.tick(10, steps=(30 * STEPS_PER_PULSE if t % 60000 == 0 else 0), heartbeat=(t % 250 == 0))
        self.assertEqual(out[0], 1)

    def test_hourly_ceiling(self):
        self.armed()
        steps_per_min = 25 * STEPS_PER_PULSE               # below the per-minute ceiling, above 13 U/h overall
        out = None
        for minute in range(60):
            for t in range(0, 60000, 10):
                out = self.sm.tick(10, steps=(steps_per_min if t == 0 else 0), heartbeat=(t % 250 == 0))
            if out[0] == 0:
                break
        self.assertEqual(out[0], 0)
        self.assertTrue(out[2] & SM_FAULT_HOURLY)
        self.assertLess(minute, 12)

    def test_battery_cutoff_with_hysteresis(self):
        self.armed()
        self.assertEqual(self.run_ms(100, vbat_mv=3450)[1], BUZZER_CHIRP)     # low warning, motor still on
        enable, buzzer, faults = self.run_ms(100, vbat_mv=3250)
        self.assertEqual(enable, 0)
        self.assertTrue(faults & SM_FAULT_BATTERY)
        self.assertEqual(self.run_ms(100, vbat_mv=3350)[0], 0)                 # inside hysteresis band
        self.assertEqual(self.run_ms(100, vbat_mv=3500)[0], 1)

    def test_main_mcu_alarm_request_beeps(self):
        self.armed()
        self.assertEqual(self.run_ms(100, alarm_request=True)[1], BUZZER_BEEP)


if __name__ == "__main__":
    unittest.main()
