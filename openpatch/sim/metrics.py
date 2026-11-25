from __future__ import annotations

import statistics


def glucose_metrics(values: list[float], interval_min: int = 5) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {}
    n = len(vals)

    def pct(pred):
        return round(100.0 * sum(1 for v in vals if pred(v)) / n, 1)

    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    hypo_events = 0
    run = 0
    for v in vals:
        run = run + 1 if v < 70 else 0
        if run == 3:  # 15 consecutive minutes below 70
            hypo_events += 1
    return {
        "tir_70_180": pct(lambda v: 70 <= v <= 180),
        "titr_70_140": pct(lambda v: 70 <= v <= 140),
        "tbr_below_70": pct(lambda v: v < 70),
        "tbr_below_54": pct(lambda v: v < 54),
        "tar_above_180": pct(lambda v: v > 180),
        "tar_above_250": pct(lambda v: v > 250),
        "mean": round(mean, 1),
        "sd": round(sd, 1),
        "cv": round(100 * sd / mean, 1),
        "gmi": round(3.31 + 0.02392 * mean, 2),
        "min": round(min(vals)),
        "max": round(max(vals)),
        "hypo_events": hypo_events,
        "hours": round(n * interval_min / 60, 1),
    }
