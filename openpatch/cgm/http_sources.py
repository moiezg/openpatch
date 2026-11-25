from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.parse
import urllib.request

from .base import CGMSource, Reading


class EntriesCGM(CGMSource):
    # Reads the Nightscout `entries` format. xDrip+ (Android) serves it locally at
    # http://<phone-ip>:17580/sgv.json; Nightscout at <site>/api/v1/entries/sgv.json.
    # On iPhone, xDrip4iOS / Juggluco can upload a Libre/Dexcom feed to Nightscout.

    def __init__(self, url: str, api_secret: str | None = None, token: str | None = None, timeout: float = 5.0):
        self.url = url
        self.api_secret = api_secret
        self.token = token
        self.timeout = timeout

    def _fetch(self, count: int) -> list:
        parts = urllib.parse.urlsplit(self.url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        query["count"] = str(count)
        if self.token:
            query["token"] = self.token
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.api_secret:
            req.add_header("api-secret", hashlib.sha1(self.api_secret.encode()).hexdigest())
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def readings(self, now: float, lookback_min: float) -> list[Reading]:
        count = max(3, int(lookback_min / 5) + 3)
        out = []
        for e in self._fetch(count):
            if "sgv" not in e or "date" not in e:
                continue
            t = e["date"] / 60000.0
            if now - lookback_min <= t <= now + 1:
                out.append(Reading(t, float(e["sgv"])))
        return sorted(out, key=lambda r: r.time)


class DemoCGM(CGMSource):
    # Synthetic wall-clock feed so the live screen works before a real sensor is connected
    def readings(self, now: float, lookback_min: float) -> list[Reading]:
        out = []
        last = math.floor(now / 5) * 5
        t = last - lookback_min
        while t <= last:
            v = 130 + 45 * math.sin(t / 240.0) + 12 * math.sin(t / 37.0)
            out.append(Reading(t, round(v)))
            t += 5
        return [r for r in out if r.time >= now - lookback_min]


def now_minutes() -> float:
    return time.time() / 60.0


def source_from_config(cfg: dict) -> CGMSource:
    kind = cfg.get("type", "demo")
    if kind == "demo":
        return DemoCGM()
    if kind == "xdrip":
        return EntriesCGM(cfg.get("url", "http://127.0.0.1:17580/sgv.json"), api_secret=cfg.get("api_secret"))
    if kind == "nightscout":
        base = cfg["url"].rstrip("/")
        return EntriesCGM(base + "/api/v1/entries/sgv.json", api_secret=cfg.get("api_secret"), token=cfg.get("token"))
    raise ValueError(f"unknown CGM source type: {kind}")
