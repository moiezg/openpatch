import unittest

from openpatch.bolus import bolus_advice
from openpatch.config import ConfigError, Profile, SafetySettings, Schedule, placeholder_profile
from openpatch.insulin import Dose, InsulinCurve, insulin_on_board
from openpatch.safety import MODE_BLOCKED, MODE_CLOSED, MODE_FALLBACK, MODE_SUSPENDED, SafetyContext, SafetyGuard


class InsulinCurveTest(unittest.TestCase):
    def setUp(self):
        self.curve = InsulinCurve(75, 360)

    def test_iob_starts_full_and_ends_empty(self):
        self.assertAlmostEqual(self.curve.iob_fraction(0), 1.0)
        self.assertAlmostEqual(self.curve.iob_fraction(360), 0.0)
        self.assertLess(self.curve.iob_fraction(359), 0.01)

    def test_iob_is_monotonic(self):
        values = [self.curve.iob_fraction(t) for t in range(0, 361, 5)]
        self.assertTrue(all(a >= b for a, b in zip(values, values[1:])))

    def test_activity_integrates_to_one_and_peaks_near_peak(self):
        total = sum(self.curve.activity(t + 0.5) for t in range(360))
        self.assertAlmostEqual(total, 1.0, places=2)
        peak = max(range(360), key=self.curve.activity)
        self.assertTrue(65 <= peak <= 85)

    def test_iob_sums_doses(self):
        doses = [Dose(0, 2.0), Dose(60, 1.0)]
        self.assertAlmostEqual(insulin_on_board(doses, 60, self.curve), 2 * self.curve.iob_fraction(60) + 1.0)


class ConfigTest(unittest.TestCase):
    def test_schedule_lookup_wraps_midnight(self):
        s = Schedule([(0, 0.8), (360, 1.2)])
        self.assertEqual(s.at(100), 0.8)
        self.assertEqual(s.at(400), 1.2)
        self.assertEqual(s.at(1440 + 100), 0.8)

    def test_schedule_must_start_at_midnight(self):
        with self.assertRaises(ConfigError):
            Schedule([(60, 1.0)])

    def test_profile_rejects_dangerous_values(self):
        for bad in (dict(isf=Schedule([(0, 5)])), dict(basal=Schedule([(0, 9)])), dict(dia_min=120)):
            p = placeholder_profile()
            for k, v in bad.items():
                setattr(p, k, v)
            with self.assertRaises(ConfigError):
                p.validate()

    def test_profile_json_round_trip(self):
        p = placeholder_profile()
        self.assertEqual(Profile.from_json(p.to_json()).to_json(), p.to_json())


def ctx(**kw):
    base = dict(bg=150, cgm_age_min=3, cgm_valid=True, iob=0.0, scheduled_basal=1.0, max_scheduled_basal=1.0,
                target=110, bg_slope_per5=0, base_prediction=[150] * 48, chosen_prediction=[130] * 48,
                delivered_24h=10, pump_alarms=[])
    base.update(kw)
    return SafetyContext(**base)


class SafetyGuardTest(unittest.TestCase):
    def setUp(self):
        self.guard = SafetyGuard(SafetySettings(max_basal_uph=3.0, max_iob_u=2.0, max_daily_u=50))

    def test_stale_cgm_falls_back_to_scheduled_basal(self):
        r = self.guard.apply(2.5, ctx(cgm_age_min=15))
        self.assertIsNone(r.rate)
        self.assertEqual(r.mode, MODE_FALLBACK)

    def test_invalid_cgm_falls_back(self):
        self.assertEqual(self.guard.apply(2.5, ctx(cgm_valid=False)).mode, MODE_FALLBACK)

    def test_pump_alarm_blocks_commands(self):
        self.assertEqual(self.guard.apply(2.5, ctx(pump_alarms=["OCCLUSION"])).mode, MODE_BLOCKED)

    def test_rate_capped_at_max_basal_and_multiples(self):
        r = self.guard.apply(9.0, ctx())
        self.assertLessEqual(r.rate, 3.0)

    def test_iob_headroom_limits_extra_insulin(self):
        r = self.guard.apply(3.0, ctx(iob=1.9))
        # 0.1 U headroom over 30 min -> at most 0.2 U/h above schedule
        self.assertLessEqual(r.rate, 1.2 + 1e-9)

    def test_low_bg_suspends(self):
        r = self.guard.apply(2.0, ctx(bg=70))
        self.assertEqual(r.rate, 0.0)
        self.assertEqual(r.mode, MODE_SUSPENDED)

    def test_predicted_low_suspends(self):
        r = self.guard.apply(2.0, ctx(base_prediction=[100, 90, 80, 72, 68, 65] + [60] * 42))
        self.assertEqual(r.rate, 0.0)

    def test_falling_below_target_no_extra(self):
        r = self.guard.apply(2.0, ctx(bg=100, bg_slope_per5=-2, base_prediction=[100] * 48))
        self.assertIsNone(r.rate)

    def test_daily_max_blocks_extra(self):
        r = self.guard.apply(2.0, ctx(delivered_24h=60))
        self.assertIsNone(r.rate)

    def test_never_raises_insulin_above_proposal(self):
        for proposed in (0.0, 0.5, 1.0, 1.5, 2.5):
            r = self.guard.apply(proposed, ctx())
            rate = 1.0 if r.rate is None else r.rate
            self.assertLessEqual(rate, max(proposed, 1.0) + 1e-9)

    def test_on_target_returns_scheduled(self):
        r = self.guard.apply(1.0, ctx())
        self.assertIsNone(r.rate)
        self.assertEqual(r.mode, MODE_CLOSED)


class BolusTest(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(basal=Schedule([(0, 1.0)]), isf=Schedule([(0, 50)]), cr=Schedule([(0, 10)]),
                               target=110).validate()
        self.settings = SafetySettings(max_bolus_u=8)

    def test_carbs_plus_correction_minus_iob(self):
        a = bolus_advice(60, 210, 1.0, 0, self.profile, self.settings)
        self.assertAlmostEqual(a.units, 6 + (2 - 1))

    def test_below_target_reduces_meal_dose(self):
        a = bolus_advice(60, 85, 0, 0, self.profile, self.settings)
        self.assertAlmostEqual(a.units, 5.5)

    def test_low_blocks_bolus(self):
        self.assertEqual(bolus_advice(60, 60, 0, 0, self.profile, self.settings).units, 0)

    def test_capped_at_max(self):
        a = bolus_advice(200, 300, 0, 0, self.profile, self.settings)
        self.assertEqual(a.units, 8)
        self.assertTrue(a.warnings)


if __name__ == "__main__":
    unittest.main()
