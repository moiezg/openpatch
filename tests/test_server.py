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


if __name__ == "__main__":
    unittest.main()
