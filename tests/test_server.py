import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from openpatch.live import LiveService
from openpatch.server import make_handler


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service = LiveService(Path(cls.tmp.name))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.service))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=True):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + self.service.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_static_app_served_without_token(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"OpenPatch", r.read())

    def test_api_requires_token(self):
        status, _ = self.call("GET", "/api/status", token=False)
        self.assertEqual(status, 401)

    def test_status_with_demo_cgm(self):
        status, s = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIsNotNone(s["bg"])
        self.assertEqual(s["mode"], "advisory")
        self.assertTrue(any("PLACEHOLDER" in w for w in s["warnings"]))

    def test_bolus_calc_and_pen_log_flow(self):
        status, a = self.call("POST", "/api/bolus/calc", {"carbs": 48, "bg": 110})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(a["units"], 4.0)
        status, _ = self.call("POST", "/api/bolus", {"units": a["units"], "carbs": 48})
        self.assertEqual(status, 400)   # missing confirm
        status, entry = self.call("POST", "/api/bolus", {"units": a["units"], "carbs": 48, "confirm": True})
        self.assertEqual(status, 200)
        status, s = self.call("GET", "/api/status")
        self.assertGreater(s["decision"]["iob"], 3.5)
        self.assertGreater(s["decision"]["cob"], 40)
        self.call("DELETE", "/api/treatments/" + entry["id"])

    def test_invalid_settings_rejected(self):
        status, settings = self.call("GET", "/api/settings")
        profile = settings["profile"]
        profile["isf"] = [[0, 3]]
        status, err = self.call("PUT", "/api/settings", {"profile": profile})
        self.assertEqual(status, 400)
        self.assertIn("ISF", err["error"])

    def test_closed_loop_refused_without_gates(self):
        status, s = self.call("PUT", "/api/settings", {"closed_loop_enabled": True})
        self.assertEqual(status, 200)
        self.assertFalse(s["closed_loop_active"])
        self.assertTrue(s["gates_missing"])
        self.call("PUT", "/api/settings", {"closed_loop_enabled": False})

    def test_simulate_endpoint(self):
        status, r = self.call("POST", "/api/simulate", {"scenario": "typical_day", "days": 1, "mode": "closed"})
        self.assertEqual(status, 200)
        self.assertIn("tir_70_180", r["closed"]["metrics"])


class PumpControlTest(unittest.TestCase):
    # Site change and pump controls end to end: HTTP API -> live service -> driver -> firmware model

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = LiveService(Path(self.tmp.name))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.call = lambda *a, **k: ServerTest.call(self, *a, **k)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.service.virtual:
            self.service.virtual.stop()
        self.tmp.cleanup()

    def use_virtual_pump(self, key="ab" * 16):
        status, view = self.call("PUT", "/api/settings", {"pump": {"type": "virtual", "key": key}})
        self.assertEqual(status, 200, view)
        self.service.virtual.stop()          # drive time by hand instead of the wall clock
        return view

    def advance(self, minutes):
        fw = self.service.virtual.firmware
        for _ in range(minutes):
            fw.tick(fw.minute + 1)

    def test_no_pump_endpoints_refuse(self):
        status, err = self.call("GET", "/api/pump")
        self.assertEqual(status, 400)
        self.assertIn("no pump", err["error"])

    def test_key_is_never_returned(self):
        view = self.use_virtual_pump()
        self.assertTrue(view["pump"]["key_set"])
        self.assertNotIn("key", view["pump"])
        status, view = self.call("PUT", "/api/settings", {"pump": {"type": "virtual", "key": ""}})
        self.assertEqual(status, 200)
        self.assertEqual(self.service.raw["pump"]["key"], "ab" * 16)    # blank keeps the stored key

    def test_bad_pump_settings_rejected_and_previous_kept(self):
        self.use_virtual_pump()
        status, err = self.call("PUT", "/api/settings", {"pump": {"type": "ble", "key": "ab" * 16}})
        self.assertEqual(status, 400)
        self.assertIn("address", err["error"])
        status, err = self.call("PUT", "/api/settings", {"pump": {"type": "virtual", "key": "xyz"}})
        self.assertEqual(status, 400)
        self.assertEqual(self.service.pump_mode(), "virtual")

    def test_full_site_change(self):
        self.use_virtual_pump()
        status, p = self.call("GET", "/api/pump")
        self.assertEqual(p["state"], "running")
        status, err = self.call("POST", "/api/pump/rewind")
        self.assertEqual(status, 409)                         # never while running
        self.assertEqual(self.call("POST", "/api/pump/suspend")[1]["state"], "suspended")
        self.assertEqual(self.call("POST", "/api/pump/rewind")[1]["state"], "no_reservoir")
        status, err = self.call("POST", "/api/pump/set-reservoir", {"units": 500})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("POST", "/api/pump/set-reservoir", {"units": 180})[1]["state"], "priming")
        self.assertEqual(self.call("POST", "/api/pump/resume")[0], 409)   # must prime first
        for _ in range(4):                                    # tubing fill in 3 U steps
            self.assertEqual(self.call("POST", "/api/pump/prime", {"units": 3})[0], 200)
            self.advance(3)
        p = self.call("GET", "/api/pump")[1]
        self.assertAlmostEqual(p["primed_u"], 12.0)
        self.assertAlmostEqual(p["reservoir_u"], 168.0)
        self.assertEqual(self.call("POST", "/api/pump/resume")[1]["state"], "running")
        p = self.call("POST", "/api/pump/site-change-done")[1]
        self.assertIsNotNone(p["last_site_change"])
        status, s = self.call("GET", "/api/status")
        self.assertEqual(s["decision"]["iob"], 0)             # primed insulin never counts as on board

    def test_occlusion_alarm_and_clear(self):
        self.use_virtual_pump()
        fw = self.service.virtual.firmware
        fw.inject_occlusion()
        self.advance(120)
        p = self.call("GET", "/api/pump")[1]
        self.assertIn("OCCLUSION", p["alarms"])
        status, s = self.call("GET", "/api/status")
        self.assertEqual(s["decision"]["mode"], "BLOCKED")
        self.assertEqual(self.call("POST", "/api/pump/clear-alarms")[0], 409)   # line still blocked
        fw.replace_site()
        self.assertEqual(self.call("POST", "/api/pump/clear-alarms")[1]["alarms"], [])

    def test_loop_never_commands_suspended_pump(self):
        self.use_virtual_pump()
        self.call("POST", "/api/pump/suspend")
        status, s = self.call("GET", "/api/status")
        self.assertEqual(s["decision"]["mode"], "BLOCKED")
        self.assertIn("pump suspended", " ".join(s["decision"]["reasons"]))


if __name__ == "__main__":
    unittest.main()
