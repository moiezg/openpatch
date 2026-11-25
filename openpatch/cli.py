from __future__ import annotations

import argparse
import json
import sys

ARROWS = [(-15, "⇊"), (-10, "↓"), (-5, "↘"), (5, "→"), (10, "↗"), (15, "↑")]


def trend_arrow(delta):
    if delta is None:
        return ""
    for limit, arrow in ARROWS:
        if delta < limit:
            return arrow
    return "⇈"


def _metrics_table(results: dict):
    keys = [("tir_70_180", "Time in range 70-180 %"), ("titr_70_140", "Time 70-140 %"),
            ("tbr_below_70", "Below 70 %"), ("tbr_below_54", "Below 54 %"),
            ("tar_above_180", "Above 180 %"), ("tar_above_250", "Above 250 %"),
            ("mean", "Mean mg/dL"), ("cv", "CV %"), ("gmi", "GMI (est. HbA1c) %"), ("hypo_events", "Hypo events")]
    modes = [m for m in ("open", "closed") if m in results]
    print(f"{'':28s}" + "".join(f"{m.upper() + ' LOOP':>14s}" for m in modes))
    for k, label in keys:
        print(f"{label:28s}" + "".join(f"{results[m]['metrics'][k]:>14}" for m in modes))
    print(f"{'Insulin U/day':28s}" + "".join(f"{results[m]['insulin']['per_day_u']:>14}" for m in modes))
    print(f"{'Hypo treatments (15 g)':28s}" + "".join(f"{results[m]['rescue_count']:>14}" for m in modes))


def cmd_simulate(a):
    from .sim.runner import compare, run_simulation
    if a.mode == "compare":
        res = compare(a.scenario, a.patient, a.days, a.seed)
    else:
        res = {a.mode: run_simulation(a.scenario, a.patient, a.days, a.mode == "closed", a.seed)}
    first = next(iter(res.values()))
    print(f"Scenario: {a.scenario} - {first['scenario_description']}")
    print(f"Patient : {first['patient_label']}   Days: {first['days']}   Seed: {a.seed}\n")
    _metrics_table(res)
    for mode, r in res.items():
        for ev in r["events"]:
            print(f"  [{mode}] day {ev['t'] // 1440 + 1} {ev['t'] % 1440 // 60:02d}:{ev['t'] % 60:02d}  {ev['type']}: {ev['detail']}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f)
        print(f"\nFull results written to {a.json}")


def cmd_scenarios(_a):
    from .sim.patients import PATIENTS
    from .sim.scenarios import SCENARIOS
    print("Scenarios:")
    for k, v in SCENARIOS.items():
        print(f"  {k:24s} {v}")
    print("Patients:")
    for k, v in PATIENTS.items():
        print(f"  {k:24s} {v['label']}")


def _service():
    from .live import LiveService
    return LiveService()


def cmd_status(_a):
    s = _service().status()
    for w in s["warnings"]:
        print(f"! {w}")
    if s["bg"] is None:
        print("No CGM reading.")
        return
    from .cgm.http_sources import now_minutes
    d = s["decision"] or {}
    age = now_minutes() - s["bg_time"]
    print(f"BG {s['bg']:.0f} mg/dL {trend_arrow(s['delta'])} ({s['delta']:+d}/5min, {age:.0f} min ago)"
          if s["delta"] is not None else f"BG {s['bg']:.0f} mg/dL")
    print(f"IOB {d.get('iob', 0):.2f} U   COB {d.get('cob', 0):.0f} g   Target {s['target']:.0f}   Mode {s['mode']}")
    pred = d.get("prediction") or []
    if pred:
        print(f"Predicted: 30 min {pred[5]}  1 h {pred[11]}  2 h {pred[23]}  min {min(pred)} / max {max(pred)}")
    if d.get("reasons"):
        print("Loop: " + "; ".join(d["reasons"]))


def cmd_bolus_calc(a):
    r = _service().bolus_calc(a.carbs, a.bg)
    print(f"Suggested bolus: {r['units']} U  (carbs {r['carb_units']} U, correction {r['correction_units']} U, IOB {r['iob']} U)")
    for w in r["warnings"]:
        print(f"! {w}")


def cmd_log(a):
    svc = _service()
    extra = {"duration_min": a.duration} if a.kind == "temp_target" else {}
    e = svc.log_treatment(a.kind, a.amount, a.ago, **extra)
    print(f"Logged {e['type']} {e['amount']} (id {e['id']})")


def cmd_token(a):
    svc = _service()
    print(svc.rotate_token() if a.rotate else svc.token)


def cmd_serve(a):
    from .server import serve
    serve("0.0.0.0" if a.lan else a.host, a.port)


def cmd_config(_a):
    svc = _service()
    print(f"Config file: {svc.config_path}")
    print(json.dumps(svc.settings_view(), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="openpatch", description="OpenPatch closed-loop insulin research toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the web app (desktop + iPhone)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--lan", action="store_true", help="listen on your Wi-Fi so the iPhone can connect")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("simulate", help="run the virtual-patient simulator")
    sp.add_argument("--scenario", default="typical_day")
    sp.add_argument("--patient", default="adult_70kg")
    sp.add_argument("--days", type=int, default=3)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--mode", choices=["compare", "closed", "open"], default="compare")
    sp.add_argument("--json", help="write full results to this file")
    sp.set_defaults(fn=cmd_simulate)

    sub.add_parser("scenarios", help="list scenarios and virtual patients").set_defaults(fn=cmd_scenarios)
    sub.add_parser("status", help="current BG, IOB, COB and prediction").set_defaults(fn=cmd_status)

    sp = sub.add_parser("bolus-calc", help="bolus suggestion")
    sp.add_argument("--carbs", type=float, default=0)
    sp.add_argument("--bg", type=float)
    sp.set_defaults(fn=cmd_bolus_calc)

    sp = sub.add_parser("log", help="log carbs, a pen bolus or a temp target")
    sp.add_argument("kind", choices=["carbs", "bolus", "temp_target", "temp_target_cancel"])
    sp.add_argument("amount", type=float, nargs="?", default=0)
    sp.add_argument("--ago", type=float, default=0, help="minutes ago")
    sp.add_argument("--duration", type=float, default=120, help="temp target duration (min)")
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("token", help="show (or --rotate) the web access token")
    sp.add_argument("--rotate", action="store_true")
    sp.set_defaults(fn=cmd_token)

    sub.add_parser("config", help="show the config file location and contents").set_defaults(fn=cmd_config)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
