# OpenPatch firmware

Two chips, one shared, host-tested core.

```
firmware/
├── core/         portable C11: wire protocol, delivery engine, hard limits, history, reboot recovery
├── safety/       portable C11: the safety MCU's runaway / heartbeat / battery logic
├── host/         builds core + safety as a shared library for the Python tests (any OS)
├── nrf52840/     Zephyr app for the Seeed XIAO nRF52840: pins, motor, load cell, BLE, flash, shell
├── attiny1616/   megaTinyCore sketch for the ATtiny1616 safety MCU
├── docker/       toolchain image for the ATtiny build
├── scripts/      per-target build scripts (same ones CI runs)
└── build.sh      one command, any machine with Docker
```

## How it is verified

| What | How | Where |
|---|---|---|
| Core logic (C) | Same contract tests as the Python model, plus 60 random streams × 1,500 steps that must match the Python spec **byte for byte** (malformed frames, wrong keys, retries, faults, reboots), plus a mutation check | `tests/test_pump.py`, `tests/test_firmware_parity.py` |
| Core in the loop | Full closed-loop simulation on the C core gives the identical glucose trace | `tests/test_firmware_parity.py` |
| Safety MCU logic (C) | Boot handshake, heartbeat loss and re-arm, runaway latch in < 1 s, hourly ceiling, long-press clear, battery hysteresis | `tests/test_safety_mcu.py` |
| nRF52840 / ATtiny builds | Compile in GitHub Actions (or `firmware/build.sh`) | `.github/workflows/firmware.yml` |
| Real hardware | **Only the bench can prove it**: gravimetric accuracy, fault injection, saline wear | `docs/SAFETY.md` gates G1–G3 |

The core is compiled with `-Wall -Wextra -Wpedantic -Wconversion -Werror`.

## Building

```bash
firmware/build.sh test         # tests (python:3.12-slim + gcc)
firmware/build.sh nrf52840     # -> firmware/build/out/nrf52840/openpatch-pump.uf2 (+ .hex)
firmware/build.sh attiny1616   # -> firmware/build/out/attiny1616/attiny1616.ino.hex
```

Without Docker: `firmware/scripts/test-host.sh` needs only Python ≥ 3.10 and a C compiler.
`build-nrf52840.sh` needs west + the Zephyr SDK. `build-attiny1616.sh` needs arduino-cli with megaTinyCore.

On GitHub, every push runs all three. The firmware files are attached to the run as artifacts.

## Wiring

| Signal | XIAO nRF52840 | ATtiny1616 | Other end |
|---|---|---|---|
| STEP | D0 (P0.02) | PA1 (input) | DRV8834 STEP |
| DIR | D1 (P0.03) | | DRV8834 DIR |
| nSLEEP | D2 (P0.28) | | DRV8834 nSLEEP |
| HX711 DOUT | D3 (P0.29) | | HX711 DOUT |
| HX711 SCK | D4 (P0.04) | | HX711 SCK |
| HEARTBEAT | D5 (P0.05) | PA2 | |
| ALARM_REQ | D6 (P1.11) | PA6 | |
| MOTOR_OFF | D7 (P1.12) | PA7 (output) | |
| BUTTON | D8 (P1.13) | PA3 | push button to GND |
| LIMIT | D9 (P1.14) | | home + end switches in parallel, to GND |
| DOOR | D10 (P1.15) | | reservoir door switch, closed = GND |
| MOTOR_EN | | PA5 | TPS22918 ON (motor supply to DRV8834 VMOT) |
| VBAT | (on-board divider) | PA4 via 1:2 divider | battery + |
| BUZZER | | PB0 | NPN → active 3 V piezo buzzer |
| UPDI | | PA0 | programming adapter |

**Motor power reaches the DRV8834 only through the load switch that the ATtiny controls.** The
nRF52840 cannot overrule it.

## Flashing

- **nRF52840:** double-tap the XIAO's reset button. A `XIAO-SENSE`/`XIAO BLE` USB drive appears. Copy `openpatch-pump.uf2` onto it.
- **ATtiny1616:** use a USB-serial adapter as SerialUPDI (adapter TX + RX through a 4.7 kΩ resistor to PA0).
  `pymcuprog write -t uart -u /dev/ttyUSB0 -d attiny1616 -f attiny1616.ino.hex --erase --verify`
  Then set fuses for a 2.7 V brown-out detector (megaTinyCore "Burn bootloader" with `bodvoltage=2v6,bodmode=enabled`).

## Provisioning (USB serial shell, 115200 baud, `op` commands)

The pump does nothing until it is provisioned. Bluetooth stays off until calibration is marked done.

```
op status
op key 3f9c...e1                     # 32–128 hex chars; the same key goes into the OpenPatch app config
op limits 200 500 800                # max basal 2 U/h, max bolus 5 U, hourly cap 8 U  (centi-units)
op basal 0:80 240:100 480:80         # minute-of-day : centi-units per hour
op cal zero                          # tare the load cell with nothing on the plunger
op cal steps 1400                    # steps per 0.05 U x100, from the gravimetric test below
op cal force 120 900                 # HX711 counts per unit, occlusion threshold (see below)
op cal done                          # prints the BLE passkey; reboot
op pair                              # opens a 120 s pairing window (also open while no device is bonded)
```

Make the **ATtiny ceilings match**: edit `STEPS_PER_PULSE_X100` and `HOURLY_CEILING_PULSES` at the top of
`attiny1616/attiny1616.ino`. Set the hourly ceiling slightly *above* the pump's `hourly_cap`, so the
pump's own limit trips first and the safety MCU is the backstop. Rebuild and reflash.

### Gravimetric calibration (saline, never on the body)
1. Open the door, rewind, insert a saline-filled reservoir, `SET_RESERVOIR` → the pump is in **PRIMING**.
2. Put the infusion set outlet in a covered cup on a 0.001 g scale. Tare.
3. `op bench 200` (200 pulses = 10 U = 100 mg of saline at U-100 equivalence). Weigh.
4. `steps_per_pulse_x100 = current × 100 mg / measured mg`. Repeat until within ±2%, then run the 24 h
   accuracy test in `docs/SAFETY.md` (G1).

### Occlusion threshold
With saline flowing through an open set, read `op cal raw` during a bench run: that's normal force.
Clamp the tubing, run pulses one at a time, and record the reading when about 1 U (20 pulses) has
built up. Set `counts_per_unit` so the normal-flow force is a small number, and the threshold to the
clamped-at-1-U value. An unreadable load cell reads as maximum force, so it fails safe.

## Behaviour summary (identical to `openpatch/pump/firmware.py`)
- States: `NO_RESERVOIR → (SET_RESERVOIR) → PRIMING → (PRIME, RESUME) → RUNNING ⇄ SUSPENDED`. A
  REWIND is refused while running.
- Per minute: scheduled or temp basal in 0.05 U pulses (exact integer accumulator), bolus at
  ≤ 1.5 U/min, rolling 60-minute hard cap on all motor movement.
- Temp basals expire on their own (≤ 120 min; the controller uses 30). The pump never depends on the phone.
- Alarms that stop delivery: occlusion, reservoir empty (counter or end switch), hourly cap, motor
  fault. A stop alarm also cancels any queued bolus, prime and temp, and discards the basal backlog.
- Reboot: the snapshot (reservoir, state, alarms) and the 48 h history are restored from flash.
  Scheduled basal resumes, any in-flight bolus is **not** resumed, and `RESTARTED` is raised.
- Opening the door or holding the button for 5 s suspends delivery. A short press silences warnings for 30 min.
- If the delivery loop stalls, the heartbeat stops. The safety MCU then cuts motor power within 2 s
  and sounds the buzzer, and the watchdog resets the nRF52840 within 8 s.
