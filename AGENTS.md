# AGENTS.md: handoff for AI coding agents

You are continuing work on **OpenPatch**, a personal, non-commercial DIY closed-loop insulin research
project: a reusable patch-style pump, a model-predictive controller with an independent safety layer,
a virtual-patient simulator, a web app (iPhone home-screen PWA + desktop) and a CLI.

**This software doses insulin. Mistakes can cause severe hypoglycaemia or DKA.** Read the rules
below before changing anything.

## Non-negotiable rules

1. **Run the full test suite before every commit and push:** `firmware/scripts/test-host.sh`
   (Python ≥ 3.10 and a C compiler; all tests must pass). Never push red.
2. **Never weaken, skip or delete a test to make it pass.** A failing safety or parity test means
   the code is wrong until proven otherwise. Explain to the owner before changing any test's intent.
3. **The safety layer only ever reduces insulin.** `openpatch/safety.py` rules, firmware hard limits,
   the hourly cap and the safety MCU are defence in depth. Don't bypass, loosen or "temporarily
   disable" any of them.
4. **Python model and C firmware must stay identical.** `openpatch/pump/firmware.py` is the executable
   specification; `firmware/core/op_fw.c` must match it byte for byte
   (`tests/test_firmware_parity.py`). Change both together, integer arithmetic only.
5. **Never remove the closed-loop gate interlock** (`REQUIRED_GATES` in `openpatch/live.py`,
   `docs/SAFETY.md`). A real pump stays in advice mode until every gate is signed off.
6. **This repository is public.** Never commit anyone's health information, therapy settings, doses,
   keys, tokens or `~/.openpatch/` contents. Therapy numbers are entered at runtime (Settings / `op`
   shell) by the owner, from their diabetes team.
7. Ask the owner before anything irreversible or outward-facing (force-push, deleting branches,
   publishing releases, changing repo visibility).

## Status

| Area | State |
|---|---|
| Controller, safety guard, bolus calculator, simulator | Done and tested. 30-run simulation: TIR 77.5% → 84.0%, time < 70: 2.1% → 1.0% |
| Web app / PWA + CLI + HTTP API | Done: live BG, prediction, bolus, simulator, pump card, alarm banner, suspend/resume, 7-step site change, pump settings, gate checkboxes |
| Pump protocol + Python firmware model | Done (HMAC-SHA256/8, CRC-16, seq de-dup, reservoir workflow, reboot recovery) |
| C firmware core + safety MCU core | Done; parity- and unit-tested in CI (passing) |
| ATtiny1616 safety MCU build | **Passing in CI** |
| nRF52840 (Zephyr) build | **First CI run failed at configure (SDK not found, fixed in `41fb413`)**. The next run is the first real compile of `firmware/nrf52840/`: expect a few Zephyr 4.1 / devicetree / Kconfig fixes |
| Real hardware | Not built yet. Everything physical must pass the bench gates in `docs/SAFETY.md` |

## Next steps, in order

1. **Get the nRF52840 CI job green.** Read failures with `gh run view --log-failed`. Likely areas:
   `firmware/nrf52840/boards/xiao_ble.overlay` (partition overrides, disabled peripherals, ADC
   channel macros), `prj.conf` symbols (e.g. `BT_FIXED_PASSKEY`, `BT_LE_ADV_CONN_FAST_1` availability),
   FCB/settings APIs in `src/storage.c`. Keep fixes minimal and keep behaviour identical to the spec.
2. **Bump GitHub Actions** to Node-24 versions when available (checkout/cache/upload-artifact warn about Node 20).
3. **High-dose support.** Defaults (max bolus 10 U, hourly cap 12 U, max daily 80 U, safety MCU hourly
   ceiling 13 U in `firmware/attiny1616/attiny1616.ino`) are too low for insulin-resistant users. Add a
   high-dose virtual patient (80–120 U/day) to `openpatch/sim/patients.py`, verify the controller and
   limits at that scale, and document how to set higher limits consistently across app, pump and safety MCU.
4. **Selectable insulin type.** Make the action curve selectable in Settings and the simulator
   (rapid-acting peak ≈ 75 min, ultra-rapid peak ≈ 55 min).
5. **Loop host:** Raspberry Pi Zero 2 W deployment (systemd unit, Tailscale, `bleak` for BLE).
6. **Hardware bring-up** following `firmware/README.md` (provisioning, gravimetric calibration,
   occlusion threshold) and the gates G1–G5 in `docs/SAFETY.md`.

## Repo map

```
openpatch/            Python package (stdlib only, Python >= 3.10)
  config.py           profile, safety limits, validation ranges
  insulin.py carbs.py predict.py controller.py safety.py bolus.py loop.py   the algorithm
  pump/               protocol.py, firmware.py (spec), driver.py, ble.py, cfirmware.py (loads C core via ctypes)
  cgm/                Nightscout / xDrip+ / demo sources
  sim/                Hovorka virtual patients, Indian-meal scenarios, metrics, runner
  live.py server.py cli.py web/   live service, HTTP API + token auth, CLI, PWA
firmware/
  core/ safety/       portable C11 (compiled -Wall -Wextra -Wpedantic -Wconversion -Werror)
  host/               shared-library build for tests
  nrf52840/           Zephyr 4.1 app for Seeed XIAO nRF52840
  attiny1616/         megaTinyCore sketch (safety MCU)
  scripts/ docker/ build.sh   per-target builds, identical locally (Docker) and in CI
tests/                unittest suites (core, pump contract on both implementations, parity, safety MCU, server, simulation)
docs/                 HARDWARE_SPEC.md, SOFTWARE_SPEC.md, SAFETY.md
.github/workflows/firmware.yml   tests + both chip builds on every push; firmware attached as artifacts
```

## Commands

```bash
firmware/scripts/test-host.sh            # all tests (builds the C core as a host library)
bin/openpatch serve --lan                # web app on :8765; prints iPhone link + token
bin/openpatch simulate --scenario missed_bolus --patient teen_55kg
firmware/build.sh nrf52840|attiny1616    # chip builds in the same Docker images as CI
gh run list && gh run view --log-failed  # CI status and failing logs
```

## Key design decisions (don't undo without discussing)

- **MPC with carb-robust cost:** each candidate temp basal is scored against "carbs absorb as logged"
  and "only half the remaining carbs arrive". While carbs are absorbing, only negative deviation is projected.
  This removed most post-meal lows in simulation.
- **Temp basals only (30 min, auto-expiring); no automatic boluses.** Boluses are always user-confirmed.
- **iOS can't do Bluetooth from a web app**, so the loop runs on an always-on host (Mac → Pi Zero → on-pump later).
- **Pump never depends on the controller:** temp basals expire, scheduled basal is the floor, reboots
  resume basal but never an in-flight bolus (`RESTARTED` warning).
- **Safety MCU per-minute ceiling = 2× the largest legitimate burst** (bucketed windows can hold two
  consecutive per-minute bursts; see `firmware/safety/sm_core.h`).
- **Prime is never counted as insulin on board**; pump events are an undeletable log.
- **Pump key never leaves the server**; the browser only sees `key_set`.

## Conventions

- Conventional commits: `type(scope): description`. **No AI attribution trailers** (no `Co-Authored-By`,
  no "Generated with" footers).
- Match surrounding code: short `#`/`//` comments only where something is non-obvious; no doc-comment blocks.
- Python: stdlib only in `openpatch/` (portability to Pi / future ports). C: C11, no dynamic allocation in `core/`.
- Units: mg/dL for glucose, minutes for time, centi-units (0.01 U) as integers in the firmware and protocol.
