# OpenPatch hardware specification (v0.1)

Goal: a pump you can build at home in India, with **everything reusable except what must be
sterile**. The software in this repo already speaks the pump's wire protocol, and runs the same
firmware logic as a simulation (`openpatch/pump/firmware.py`).

> Read [SAFETY.md](SAFETY.md) first. The pump must pass every bench gate with **saline** before
> it ever holds insulin.

---

## 1. Architecture: reusable drive unit + commercial sterile disposables

```
 ┌──────────── REUSABLE DRIVE UNIT (years) ─────────────┐      ┌── DISPOSABLE (2–3 days) ──┐
 │ nRF52840 main MCU ── BLE ──► controller (Mac/Pi/phone)│      │ 3 mL pump reservoir        │
 │ safety MCU (independent) ── motor power switch, buzzer│      │ (Medtronic-compatible)     │
 │ micro stepper + 1:100 gearbox + M3 lead screw         │─push─►│ infusion set + cannula     │
 │ load cell (occlusion)  · end-stop switches            │      │ (Quick-set / Mio type)     │
 │ 1S LiPo + USB-C charging · buzzer · vibration · button│      │ adhesive overpatch         │
 └───────────────────────────────────────────────────────┘      └────────────────────────────┘
```

**Why not a true tubeless pod with its own cannula?** The cannula, needle and fluid path must be
sterile and pyrogen-free. You cannot make that safely at home. So the design buys sterile parts
that are already sold in India for Medtronic pump users, and makes everything else reusable.

**"Tubeless feel"**: the drive unit clips into an adhesive cradle stuck on the body right next to
the infusion site. The shortest commercial tubing (≈46 cm) is coiled under an overpatch. Nothing
dangles, and there is no pump on a belt clip.

| Part | Reused? | Life |
|---|---|---|
| Drive unit (electronics, motor, lead screw, plunger rod, battery, enclosure) | ✅ | years (battery ~2 years) |
| Body cradle (3D printed clip) | ✅ | months |
| Reservoir, infusion set, adhesive pad | ❌ sterile/single-use | 2–3 days |

---

## 2. Mechanical: the drive

### 2.1 Delivery resolution maths
U-100 insulin: **1 U = 10 µL**, so one 0.05 U pulse = **0.5 µL**.

Measure your reservoir's inner diameter (ID) with calipers. For ID ≈ 13.5 mm the plunger area is
≈ 143 mm², so **one 0.05 U pulse = 0.0035 mm of plunger travel**.

### 2.2 Recommended drive (Option A)
| Item | Spec |
|---|---|
| Motor | 12 mm (or 10 mm) 2-phase micro stepper, 20 steps/rev |
| Gearbox | planetary 1:100 (1:150 also fine), output shaft is an **M3 × 0.5 mm lead screw** (e.g. "GM12-15BY" family) |
| Resolution | 20 × 100 = 2000 steps/rev → 0.5 mm / 2000 = **0.25 µm per step** → **~14 steps per 0.05 U** |
| Travel | ≥ 25 mm (a full 3 mL reservoir ≈ 21 mm at 13.5 mm ID) |
| Nut / plunger | anti-backlash brass nut on the screw, pushing a reusable plunger rod that engages the reservoir's plunger |
| Driver | **DRV8834** (2.5–10.8 V, works directly from 1S LiPo, STEP/DIR, sleep pin) |

**Option B (cheaper, more parts in India):** N20 DC gear motor (1:298) plus a magnetic quadrature
encoder, under closed-loop position control. It needs more firmware work and is harder to verify.
Choose Option A unless parts are unobtainable.

### 2.3 Backlash and direction rules (firmware)
- Delivery is **always in the forward direction**. Rewind only with no reservoir present (door switch open).
- After a rewind or reservoir change, run a **prime**: advance until the load cell sees plunger
  contact, then deliver a prime of 0.5–1 U (or the tubing fill volume) with the set disconnected from the body.

### 2.4 Sensors
| Sensor | Part | Purpose |
|---|---|---|
| Occlusion | 1–5 kg micro button load cell + **HX711** between nut and plunger rod | force rises when the line is blocked. Calibrate the threshold so the alarm trips at ≤ 1 U undelivered |
| Home / end of travel | 2 × micro switch or hall sensor | plunger fully retracted; reservoir empty |
| Reservoir door | micro switch | no delivery with the door open; rewind only with the door open |
| Temperature | 10 k NTC near the reservoir (plus nRF die temp) | warn at > 37 °C, since insulin degrades in Indian summers |

### 2.5 Enclosure
- 3D printed **PETG or ASA** (not PLA: it softens in a hot car). Target ≈ 60 × 42 × 20 mm.
- O-ring gasket on the shell seam, sealed USB-C, conformal coating on the PCB. Target **IPX4
  (splash)**: take it off for showers and swimming, and say so in your own operating rules.
- Reservoir bay with a viewing window to see the insulin level.

---

## 3. Electronics

| Block | Part (or equivalent) | Notes |
|---|---|---|
| Main MCU + BLE | **Seeed XIAO nRF52840** | BLE 5, built-in LiPo charger, tiny, Arduino/Zephyr support |
| Safety MCU | **ATtiny1616** (or STM32G031) | independent clock and firmware. Owns the motor power switch and the buzzer |
| Motor power switch | TPS22918 load switch | **enable comes only from the safety MCU** |
| Motor driver | DRV8834 | STEP line also routed to a safety-MCU counter input |
| Force ADC | HX711 | 10 Hz is enough |
| Battery | 1S LiPo 300–500 mAh with protection circuit | 2-week target (see budget below) |
| Alarms | piezo buzzer (safety MCU), coin vibration motor (main MCU) | an alarm still sounds if the main MCU hangs |
| UI | one tactile button: press = acknowledge, hold 5 s = suspend | plus one RGB LED |
| Programming | SWD pads + USB | **key provisioning only over USB, never over BLE** |

### 3.1 The safety MCU (defence in depth)
It works independently of the main MCU and the phone:
1. Counts STEP pulses itself and converts them to units. If the rolling **60-minute total exceeds the
   hard cap**, it cuts motor power and sounds the buzzer. (The same rule is in the firmware model as `hourly_cap_u`.)
2. Expects a heartbeat from the main MCU every ≤ 2 s. If it's missing, it cuts motor power and alarms.
3. Watches battery voltage and alarms below a safe threshold.
4. Has its own watchdog. A safety-MCU reset leaves motor power **off** until the main MCU re-handshakes.

Failure philosophy: **no delivery + loud alarm** is always preferred over uncontrolled delivery.
Stopping insulin is dangerous over hours (DKA), so every stop must alarm.

### 3.2 Power budget (estimate)
| Consumer | Daily |
|---|---|
| Motor: 40 U/day × 20 pulses × 14 steps ≈ 11 k steps × ~5 ms × ~200 mA | ~3 mAh |
| BLE connection, 5-min loop exchanges, ~0.4 mA average | ~10 mAh |
| MCUs, sensors, sleep current | ~3–5 mAh |
| **Total** | **~16–20 mAh/day** → 400 mAh ≈ 2–3 weeks. Charge it at each reservoir change |

---

## 4. Firmware (main MCU) requirements

The executable reference is `openpatch/pump/firmware.py`. The C firmware must pass the same tests
(`tests/test_pump.py`) through a hardware-in-the-loop harness.

- **States**: `PROVISION → NO_RESERVOIR → PRIMING → RUNNING ⇄ SUSPENDED → ALARM`.
- **Delivery scheduler** (every minute): scheduled basal from the profile stored in flash, temp basal
  with **automatic expiry** (max 120 min; the controller always uses 30), bolus at ≤ 1.5 U/min, in 0.05 U pulses.
- **Hard limits stored in the pump** (set by USB provisioning only): max basal, max bolus, hourly
  cap. Commands above a limit are rejected, not clamped.
- **Protocol**: `openpatch/pump/protocol.py`. HMAC-SHA256 (truncated to 8 bytes) with a 32-byte
  pre-shared key, CRC-16, sequence-number de-duplication (a BLE retry can never double a bolus),
  plus BLE LE Secure Connections pairing.
- **History**: ring buffer in flash, 48 h, per-minute basal and bolus records.
- **Power loss / brown-out**: on reboot, restore state from flash, **resume scheduled basal, raise an
  alarm**, and log the gap.
- **Boot self-test**: load-cell zero, motor coil continuity, battery, safety-MCU handshake, flash CRC.
- **If the controller disappears**: the temp basal expires and the pump runs its scheduled basal
  by itself. The pump never depends on the phone to keep you alive.

GATT: service `6f700001-…`, command characteristic `6f700002-…` (write), response `6f700003-…` (notify). See `openpatch/pump/ble.py`.

---

## 5. Bill of materials (indicative, India, late 2026)

Prices vary a lot. Sources: Robu.in, Robocraze, Evelta, ElectronicsComp, LCSC/JLCPCB (PCB + assembly).

| Item | Qty | ≈ ₹ |
|---|---|---|
| Seeed XIAO nRF52840 | 1 | 1,400–1,800 |
| Micro stepper with 1:100 planetary gearbox and M3 lead screw | 1 | 900–1,800 |
| DRV8834 driver carrier | 1 | 500–800 |
| ATtiny1616 + passives | 1 | 150–250 |
| TPS22918 load switch | 1 | 50–100 |
| Micro load cell + HX711 | 1 | 250–500 |
| 1S LiPo 400 mAh (protected) | 1 | 250–450 |
| Buzzer, vibration motor, switches, NTC, LED | — | 250–400 |
| PCB fabrication (5 boards) + stencil | — | 800–1,500 |
| PETG/ASA filament, O-rings, M2 screws, brass nut | — | 300–600 |
| **Reusable unit total** | | **≈ ₹5,000–8,000** |
| Spare drive unit (strongly recommended) | | same again |

**Disposables:** 3 mL reservoirs and infusion sets as sold for Medtronic pumps in India, plus a CGM
(Libre-class sensors). Check prices with the local distributor.

**Test equipment you need:** 0.001 g precision scale (gravimetric accuracy testing), digital calipers,
multimeter, a USB power meter. A 3D printer is useful (or use a printing service).

---

## 6. Performance targets (verified in the bench gates)

| Parameter | Target |
|---|---|
| Basal accuracy (1 U/h, 1 h window) | ±5% |
| Bolus accuracy (≥ 1 U) | ±5% |
| Smallest step | 0.05 U |
| Occlusion alarm | ≤ 1 U undelivered at 1 U/h |
| Max delivery under any single fault | ≤ hourly cap |
| Battery | ≥ 7 days, low-battery alarm ≥ 24 h before empty |
| Alarm audibility | ≥ 65 dB at 1 m, audible through a blanket |
