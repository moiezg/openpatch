# OpenPatch safety plan

This is a personal, non-commercial research build. No regulator has reviewed it. Insulin delivered at
the wrong time or in the wrong amount can cause severe hypoglycaemia, seizures or death within hours.
Stopped insulin can cause DKA within hours. The plan below is what makes the difference
between a careful experiment and a dangerous one. Every gate needs evidence you can show.

## Always, at every stage
- Carry pens with rapid **and** long-acting insulin, a glucose meter with strips, fast glucose, and
  glucagon (or nasal glucagon). Someone you live with knows how to use the glucagon.
- Your diabetes team knows you are doing this. Use their basal/ISF/CR numbers, never guesses.
- Confirm any CGM reading that disagrees with how you feel using a fingerstick.
- Keep conservative limits: max basal ≤ 2× your highest scheduled basal and a small max IOB to start.
  Raise them only after weeks of data.

## Hazards and mitigations

| Hazard | Mitigations (code / hardware) |
|---|---|
| Too much insulin from a software bug | safety guard only lowers rates; firmware hard limits reject commands; **hourly cap in firmware and an independent safety MCU**; 30-min temp expiry |
| Too much insulin from a mechanical fault (runaway motor) | safety MCU cuts motor power on step count; load cell; end stops |
| Duplicate bolus on Bluetooth retry | sequence de-duplication in the protocol (tested) |
| Someone else commanding the pump | HMAC on every frame, key provisioned by USB only, BLE secure pairing |
| Wrong CGM value drives dosing | staleness and jump checks → scheduled basal; predicted-low suspend; fingerstick before boluses |
| Blocked cannula (no insulin → DKA) | load-cell occlusion alarm ≤ 1 U; rising-BG alerts; spare sets carried |
| Controller (Mac/Pi/phone) dies | temp basal expires and the pump runs scheduled basal by itself |
| Pump power loss | brown-out recovery to scheduled basal plus alarm; low-battery alarm ≥ 24 h ahead |
| Heat-degraded insulin | reservoir temperature warning > 37 °C; change insulin more often in summer |
| Carb overestimate / exercise | robust MPC (fast-carb scenario), predicted-low suspend, activity mode |
| Wrong settings typed in | validation ranges on every field; placeholder-profile warning |

## Gates (all must be signed off before closed loop on a real pump)

| Gate | What | Pass criteria |
|---|---|---|
| **G0_simulation** | `openpatch simulate` on all scenarios with *your* profile shape | time below 70 < 4%, below 54 < 1% in every scenario |
| **G1_bench_accuracy** | saline into a beaker on a 0.001 g scale (1 mg = 0.1 U): 1 U/h for 24 h, 0.05 U steps, 1/5/10 U boluses | every 1 h window within ±5%; boluses within ±5%; repeat on 3 drive units or 3 builds |
| **G2_fault_injection** | block the line; pull the battery mid-bolus; kill BLE; kill the controller mid-temp; spam duplicate frames; send over-limit commands; force a motor runaway with the safety MCU watching | every case alarms or rejects as specified; no case delivers more than the hourly cap |
| **G3_saline_wear** | wear the drive unit with **saline** for 14 days of normal life (sleep, heat, sweat, travel) | no adhesive failure > 1 per week; no water ingress; alarms audible at night |
| **G4_advisory_2_weeks** | run `dry-run` mode (virtual pump, your real CGM) alongside your normal therapy | review every decision; no recommendation you would consider unsafe |
| **G5_clinician_informed** | show your diabetes team the plan, the limits and the data | they know, and you have an agreed fallback plan |

Record evidence under `evidence/<gate>/` (scale logs, photos, decision exports). Then add the gate
name to `gates_completed` in Settings.

## Going live (first weeks)
1. Start with the lowest limits: max basal = your highest scheduled basal × 1.5, max IOB = 1 U above your meal boluses.
2. First closed-loop nights only when someone else is at home.
3. Review the decision log every day for 2 weeks (Settings shows it; `GET /api/decisions` exports it).
4. Any unexplained low below 54 sends you back to advisory mode until you understand it.
