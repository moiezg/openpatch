import unittest

from openpatch.sim.hovorka import HovorkaPatient
from openpatch.sim.patients import patient_params
from openpatch.sim.runner import compare, run_simulation


class PatientModelTest(unittest.TestCase):
    def test_steady_state_holds_without_food(self):
        p = HovorkaPatient(patient_params("adult_70kg"), 110)
        basal = p.basal_mu_per_min / 1000
        for m in range(600):
            p.step(m, basal, 0)
        self.assertAlmostEqual(p.bg_mgdl, 110, delta=1)


class ClosedLoopOutcomeTest(unittest.TestCase):
    # Regression guard on the algorithm: these must keep holding after any controller change

    def test_typical_day_closed_loop_beats_open_loop_safely(self):
        closed_tir, open_tir, closed_rescues, open_rescues = [], [], 0, 0
        for patient in ("adult_70kg", "teen_55kg", "resistant_95kg"):
            r = compare("typical_day", patient, 2, 3)
            c, o = r["closed"]["metrics"], r["open"]["metrics"]
            closed_tir.append(c["tir_70_180"])
            open_tir.append(o["tir_70_180"])
            closed_rescues += r["closed"]["rescue_count"]
            open_rescues += r["open"]["rescue_count"]
            # per person: safety targets from the international consensus on time in range
            self.assertLess(c["tbr_below_70"], 4.0, patient)
            self.assertLess(c["tbr_below_54"], 1.0, patient)
            # on an easy day with a perfect profile the loop may be slightly conservative, never much worse
            self.assertGreaterEqual(c["tir_70_180"], o["tir_70_180"] - 5, patient)
        self.assertLessEqual(closed_rescues, open_rescues)

    def test_hard_days_closed_loop_clearly_better(self):
        closed, opened = 0.0, 0.0
        for scenario in ("missed_bolus", "illness", "festival", "carb_overestimate"):
            r = compare(scenario, "teen_55kg", 3, 2)
            closed += r["closed"]["metrics"]["tir_70_180"]
            opened += r["open"]["metrics"]["tir_70_180"]
            self.assertLess(r["closed"]["metrics"]["tbr_below_70"], 4.0, scenario)
        self.assertGreater(closed / 4, opened / 4 + 5)

    def test_missed_bolus_is_rescued(self):
        r = compare("missed_bolus", "adult_70kg", 3, 1)
        self.assertLess(r["closed"]["metrics"]["tar_above_180"], r["open"]["metrics"]["tar_above_180"])
        self.assertGreater(r["closed"]["metrics"]["tir_70_180"], r["open"]["metrics"]["tir_70_180"])

    def test_occlusion_is_detected_and_alarmed(self):
        r = run_simulation("occlusion", "adult_70kg", 2, True, 1)
        kinds = [e["type"] for e in r["events"]]
        self.assertIn("pump_alarm", kinds)
        self.assertIn("site_changed", kinds)

    def test_sensor_dropout_falls_back(self):
        r = run_simulation("sensor_dropout", "adult_70kg", 2, True, 1)
        modes = {d["mode"] for d in r["decisions"]}
        self.assertIn("FALLBACK", modes)
        self.assertLess(r["metrics"]["tbr_below_54"], 1.0)


if __name__ == "__main__":
    unittest.main()
