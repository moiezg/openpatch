from __future__ import annotations

import hmac
import json
import mimetypes
import socket
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import ConfigError
from .live import LiveService
from .pump.base import PumpError
from .sim.patients import PATIENTS
from .sim.runner import compare, run_simulation
from .sim.scenarios import SCENARIOS

WEB_DIR = Path(__file__).parent / "web"
VERSION = "0.1.0"
MAX_BODY = 256 * 1024
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def make_handler(service: LiveService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenPatch/" + VERSION

        def log_message(self, fmt, *args):
            if "/api/" in (args[0] if args else ""):
                super().log_message(fmt, *args)

        # ---------- plumbing ----------

        def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload):
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "request too large")
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ApiError(400, "invalid JSON")

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            return bool(token) and hmac.compare_digest(token.encode(), service.token.encode())

        def _static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents or not target.is_file():
                target = WEB_DIR / "index.html"
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            cache = "no-cache" if target.suffix in (".html", ".js", ".css", ".webmanifest") else "max-age=86400"
            self._send(200, target.read_bytes(), ctype, cache)

        def _dispatch(self, method: str):
            url = urlsplit(self.path)
            path = url.path
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "method not allowed")
                return self._static(path)
            if path == "/api/ping":
                return self._json(200, {"ok": True, "version": VERSION})
            if not self._authorized():
                time.sleep(0.5)   # slow down token guessing
                raise ApiError(401, "missing or invalid access token")
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            result = self._route(method, path, query)
            self._json(200, result)

        def _route(self, method: str, path: str, query: dict):
            b = self._body() if method in ("POST", "PUT") else {}
            if method == "GET" and path == "/api/meta":
                return {"version": VERSION, "scenarios": SCENARIOS,
                        "patients": {k: v["label"] for k, v in PATIENTS.items()}}
            if method == "GET" and path == "/api/status":
                return service.status()
            if method == "GET" and path == "/api/decisions":
                return service.decisions(int(query.get("limit", 288)))
            if method == "POST" and path == "/api/bolus/calc":
                return service.bolus_calc(float(b.get("carbs", 0)), _opt_float(b.get("bg")))
            if method == "POST" and path == "/api/bolus":
                if b.get("confirm") is not True:
                    raise ApiError(400, "bolus requires confirm: true")
                return service.deliver_bolus(float(b["units"]), float(b.get("carbs", 0)))
            if method == "POST" and path == "/api/treatments":
                extra = {k: b[k] for k in ("duration_min", "label", "absorption_min", "note") if k in b}
                return service.log_treatment(b.get("type", ""), float(b.get("amount", 0)),
                                             float(b.get("minutes_ago", 0)), **extra)
            if method == "DELETE" and path.startswith("/api/treatments/"):
                if not service.delete_treatment(path.rsplit("/", 1)[-1]):
                    raise ApiError(404, "treatment not found")
                return {"deleted": True}
            if method == "GET" and path == "/api/settings":
                return service.settings_view()
            if method == "PUT" and path == "/api/settings":
                return service.update_settings(b)
            if method == "POST" and path == "/api/simulate":
                scenario = b.get("scenario", "typical_day")
                patient = b.get("patient", "adult_70kg")
                if scenario not in SCENARIOS or patient not in PATIENTS:
                    raise ApiError(400, "unknown scenario or patient")
                days = max(1, min(int(b.get("days", 3)), 7))
                seed = int(b.get("seed", 1))
                mode = b.get("mode", "compare")
                if mode == "compare":
                    return compare(scenario, patient, days, seed)
                return {mode: run_simulation(scenario, patient, days, mode == "closed", seed)}
            raise ApiError(404, "unknown endpoint")

        def _handle(self, method: str):
            try:
                self._dispatch(method)
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except (ConfigError, ValueError, KeyError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except PumpError as e:
                self._json(502, {"error": f"pump: {e}"})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": f"internal error: {type(e).__name__}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def _opt_float(v):
    return None if v in (None, "") else float(v)


def lan_addresses() -> list[str]:
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def serve(host: str = "127.0.0.1", port: int = 8765, service: LiveService | None = None):
    service = service or LiveService()
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    httpd.daemon_threads = True
    print(f"OpenPatch {VERSION} running")
    print(f"  Desktop : http://127.0.0.1:{port}/")
    if host in ("0.0.0.0", "::"):
        for a in lan_addresses():
            print(f"  iPhone  : http://{a}:{port}/#token={service.token}")
            print("            open in Safari on the same Wi-Fi, then Share > Add to Home Screen")
    else:
        print("  (add --lan to reach it from your iPhone)")
    print(f"  Token   : {service.token}")
    print(f"  Data    : {service.root}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
