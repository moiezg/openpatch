# OpenPatch software specification (v0.1)

## 1. Deployment: where things run

iOS rules shape the design. Safari web apps cannot use Bluetooth and cannot run in the background,
so **the loop must run on an always-on host near the pump**. The iPhone is the screen and remote.

```
 CGM (Libre/Dexcom)            controller host (runs the loop 24/7)              pump
 ─ xDrip4iOS / Juggluco ─►  Nightscout  ─►  openpatch serve  ── BLE ──►  drive unit
   (iPhone) or xDrip+                         │  (Mac now → Pi Zero 2 W)        (nRF52840)
   (Android)                                  │
                                              ├── web app / PWA: iPhone home screen, desktop browser
                                              └── CLI: `openpatch status`, `bolus-calc`, `log`, `simulate`
```

| Phase | Controller host | Pump |
|---|---|---|
| **A: now** | Mac (`openpatch serve --lan`) | none (advisory) or `virtual` (dry run) |
| **B: wearable** | Raspberry Pi Zero 2 W in a pouch, battery-powered, Wi-Fi hotspot from the phone; Tailscale for remote access | real drive unit over BLE |
| **C: self-contained** | algorithm ported to C and running **on the pump MCU**; phone or Pi only relays CGM and shows the UI | the drive unit itself |

Phase C is the most robust: if every phone and server dies, the pump still has the algorithm, plus
the scheduled basal as the floor. The Python code is written dependency-free and pure-function so
it can be ported mechanically.

**Remote access from outside home:** install Tailscale on the host and the iPhone, then run
`tailscale serve 8765`. That gives you HTTPS, which also enables the PWA's offline shell. Never
port-forward this server to the public internet.

## 2. Components (code map)

| Module | Responsibility |
|---|---|
| `openpatch/config.py` | profile (basal, ISF, CR schedules, target, insulin model), safety limits, validation ranges |
| `openpatch/insulin.py` | exponential insulin action curve (same family as oref0/Loop), IOB, insulin effect |
| `openpatch/carbs.py` | carbs-on-board, triangular absorption model |
| `openpatch/predict.py` | BG prediction (insulin + carbs + decaying deviation), deviation estimate |
| `openpatch/controller.py` | **MPC**: evaluates every deliverable temp basal (0.05 U/h grid) against a Kovatchev risk cost over 4 h; lows weighted 2× |
| `openpatch/safety.py` | deterministic **safety guard**: rules can only *reduce* insulin |
| `openpatch/bolus.py` | bolus calculator (carbs + correction − IOB, low lockout, cap) |
| `openpatch/loop.py` | the 5-minute cycle: CGM → validate → IOB/COB → predict → MPC → guard → pump; activity mode |
| `openpatch/pump/protocol.py` | wire format: HMAC-SHA256/8 + CRC-16 + seq de-duplication |
| `openpatch/pump/firmware.py` | executable reference model of the pump firmware (hard limits, hourly cap, occlusion, expiry) |
| `openpatch/pump/driver.py` | one driver for any transport (in-process firmware or BLE) |
| `openpatch/pump/ble.py` | BLE transport (`bleak`) |
| `openpatch/cgm/` | CGM sources: Nightscout, xDrip+ local web server, demo |
| `openpatch/sim/` | Hovorka virtual patients, Indian-diet scenarios, metrics, open-vs-closed runner |
| `openpatch/live.py` | live service: config, treatment log, advisory/dry-run/closed modes, gate interlock |
| `openpatch/server.py` | HTTP API + static PWA, token auth |
| `openpatch/web/` | PWA (vanilla JS, no CDN, works offline once installed over HTTPS) |
| `openpatch/cli.py` | command-line interface |

## 3. Control algorithm

Every 5 minutes:
1. **Read CGM** (last 30 min). Reject the reading if it's older than 12 min, outside 39–400, or jumped > 45 mg/dL in 5 min.
2. **Insulin on board** from the pump's own delivery history, net of scheduled basal, in 5-min bins.
3. **Deviation**: actual BG slope (regression over 15 min) minus what insulin and logged carbs
   explain. It is projected forward, decaying over 60 min. While logged carbs are absorbing, only
   *negative* deviation is projected: a faster rise is usually those carbs arriving early, not new food.
4. **Two predictions**: carbs absorb as modelled, and only half the remaining carbs arrive.
5. **MPC**: for every rate 0…max in 0.05 U/h steps (held 30 min, then scheduled basal), cost =
   ½·risk(expected) + ½·risk(fast-carbs) + small rate-change penalty. The lowest cost wins.
   This makes the loop refuse insulin that is only justified if carbs still arrive.
6. **Safety guard** (in order; each rule can only lower the rate):
   pump alarm → no commands · CGM stale/invalid → scheduled basal · cap at min(max basal,
   4× current basal, 3× max scheduled basal) · IOB headroom · 24 h max · below target and falling →
   no extra · extra insulin predicts a low → no extra · low predicted within 30 min → **zero** ·
   BG < 75 → **zero**.
7. **Enact** a 30-minute temp basal. If the controller dies, the pump reverts on its own.

Boluses are always user-initiated through the calculator. There are no automatic boluses (SMB) in v0.1.

## 4. Simulation results (`openpatch simulate`)

30 runs (3 virtual patients × 10 scenarios, 3 days, seed 2), true-BG metrics:

| Metric | Open loop (pump + boluses) | Closed loop |
|---|---|---|
| Time in range 70–180 | 77.5% | **84.0%** |
| Time below 70 | 2.1% | **1.0%** |
| Time below 54 | 0.0% | 0.0% |
| Time above 250 | 4.1% | **1.8%** |
| Hypo treatments (15 g) | 217 | **60** |

On easy days with a perfect profile the loop is slightly conservative (mean BG a few mg/dL
higher). It helps most when life is messy: missed boluses, illness, festivals, and
overestimated carbs. Exercise needs **activity mode**, which halves lows in simulation.
These are simulations of a published model, not evidence that it is safe in a human body.

## 5. Interfaces

### HTTP API (token: `Authorization: Bearer <token>`)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ping` | health (no auth) |
| GET | `/api/meta` | scenarios, patients, version |
| GET | `/api/status` | BG, trend, IOB, COB, prediction, loop decision, pump status, warnings |
| POST | `/api/bolus/calc` | `{carbs, bg?}` → advice |
| POST | `/api/bolus` | `{units, carbs, confirm: true}`: pen log (no pump) or pump bolus |
| POST | `/api/treatments` | `{type: carbs\|bolus\|temp_target\|temp_target_cancel, amount, minutes_ago, duration_min}` |
| DELETE | `/api/treatments/{id}` | remove a mistaken entry |
| GET/PUT | `/api/settings` | profile, limits, CGM source, pump, closed-loop flag, gates |
| GET | `/api/decisions` | loop decision log |
| POST | `/api/simulate` | `{scenario, patient, days, seed, mode: compare\|closed\|open}` |

### Data on disk (`~/.openpatch/`, chmod 600)
- `config.json`: settings and the access token
- `treatments.jsonl`: append-only treatment log

### Modes
| Mode | Pump setting | What happens |
|---|---|---|
| **advisory** | `none` | predictions, bolus calculator, pen-dose log; nothing is commanded |
| **dry-run** | `virtual` | the full loop runs against the firmware model in real time with your real CGM; no insulin |
| **closed** | `ble` + `closed_loop_enabled` + **all gates signed off** | automatic temp basals on the real pump |

The software refuses closed loop on real hardware until every gate in SAFETY.md is listed in
`gates_completed`. This interlock is deliberate. Don't patch it out.

## 6. Configuring a real CGM
- **iPhone + Libre:** xDrip4iOS or Juggluco uploads to a Nightscout site (self-host it or use a Nightscout
  provider). Config: `"cgm": {"type": "nightscout", "url": "https://your-site", "token": "..."}`.
- **Android + xDrip+:** enable "local web server". Config: `"cgm": {"type": "xdrip", "url": "http://<phone-ip>:17580/sgv.json"}`.

## 7. Testing
`python3 -m unittest discover -s tests -t .` runs the insulin model, config validation, every safety-guard
rule, the protocol (CRC, authentication, duplicate-bolus protection), the firmware hard limits (hourly cap,
occlusion, reservoir), the server (auth, flows, interlock), and closed-loop outcome regressions.
