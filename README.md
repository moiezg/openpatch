# OpenPatch

A personal, build-it-yourself closed-loop insulin system: a reusable patch-style pump, an MPC control
algorithm with an independent safety layer, a virtual-patient simulator tuned for Indian meals, and
an app you can use from **iPhone (home-screen web app), desktop browser, or the command line**.

> ⚠️ Personal research project, not a medical device, not approved by CDSCO or any regulator.
> Read [docs/SAFETY.md](docs/SAFETY.md) before connecting anything to a body.

## Quick start (no install, Python 3.10+, zero dependencies)

```bash
cd ~/Projects/openpatch
bin/openpatch serve --lan          # prints a desktop URL, an iPhone link and the access token
```

- **Desktop:** open http://127.0.0.1:8765 and paste the token.
- **iPhone:** on the same Wi-Fi, open the printed `http://<mac-ip>:8765/#token=…` link in Safari, then
  **Share → Add to Home Screen**. It opens full-screen like an app.
- **CLI:**
  ```bash
  bin/openpatch simulate --scenario missed_bolus --patient teen_55kg
  bin/openpatch scenarios
  bin/openpatch status
  bin/openpatch bolus-calc --carbs 60
  bin/openpatch log carbs 45
  bin/openpatch log temp_target 150 --duration 120     # activity mode
  ```

On first run it creates `~/.openpatch/config.json` with a **placeholder profile** and **demo CGM
data**. Both are flagged in the app until you replace them in Settings.

## What's in here
| | |
|---|---|
| [docs/HARDWARE_SPEC.md](docs/HARDWARE_SPEC.md) | reusable drive unit: mechanics, electronics, safety MCU, BOM (₹), firmware requirements |
| [docs/SOFTWARE_SPEC.md](docs/SOFTWARE_SPEC.md) | architecture, algorithm, API, deployment (Mac → Pi Zero → on-pump), CGM setup |
| [docs/SAFETY.md](docs/SAFETY.md) | hazards, mitigations, the six gates required before closed loop |
| `openpatch/` | Python package: controller, safety guard, pump protocol and firmware model, simulator, server, PWA |
| `tests/` | `python3 -m unittest discover -s tests -t .` |

## Simulation snapshot
30 runs: 3 virtual patients × 10 scenarios × 3 days.

| | Open loop | Closed loop |
|---|---|---|
| Time in range 70–180 | 77.5% | **84.0%** |
| Time below 70 | 2.1% | **1.0%** |
| Time above 250 | 4.1% | **1.8%** |
| Hypo treatments needed | 217 | **60** |

## Install as a command (optional)
```bash
python3 -m venv .venv && .venv/bin/pip install -e .     # then: .venv/bin/openpatch serve
```
