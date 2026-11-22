#!/usr/bin/env python3
"""Semi-synthetic validation of the CPU attribution model.

Ground truth for "how much CPU did the crawlers cost" does not exist in production, so we build it:
take a REAL CPU series whose load is background work (it keeps its real noise, daily pattern and
spikes), take REAL request-rate series per traffic class from a busy site (real crawler waves,
real human diurnal pattern), and add a KNOWN cost per request:

    cpu_test(t) = cpu_real(t) + sum_c cost_c * rate_c(t)

Then fit the model on cpu_test and compare the recovered shares with the injected truth, next to
the request-share allocation that most crawler-cost reports use. Repeated over a grid of costs.

    python3 experiments/semi_synthetic.py --prometheus http://127.0.0.1:9090 \\
        --cpu-project emotrack --traffic-project cs-paper-radar --lookback 2d
    python3 experiments/semi_synthetic.py --synthetic        # offline, fully reproducible
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from servermon.attribution import TRAFFIC_CLASSES, align, fit  # noqa: E402
from servermon.prom import Prom  # noqa: E402

STEP = 300
GRID = {"ai_bot": [0.0005, 0.002, 0.005, 0.02, 0.05, 0.1], "human": [0.0, 0.01, 0.05], "bot": [0.0, 0.01]}


def real_series(prom: Prom, cpu_project: str, traffic_project: str, lookback: int):
    end = time.time() // STEP * STEP
    start = end - lookback
    cpu = prom.range(f'avg_over_time(project:cpu_cores:rate2m{{project="{cpu_project}"}}[5m])', start, end, STEP)
    req = prom.range(f'sum by (class) (rate(sm_http_requests_total{{project="{traffic_project}"}}[5m]))', start, end, STEP)
    xs = {m["class"]: p for m, p in req if m.get("class") in TRAFFIC_CLASSES}
    first = min(min(p) for p in xs.values() if p)
    y = {t: v for t, v in cpu[0][1].items() if t >= first}
    return align(y, xs)


def synthetic_series(n=576, seed=11):
    rng = random.Random(seed)
    y, xs = [], {c: [] for c in ("human", "ai_bot", "bot", "scanner")}
    for i in range(n):
        y.append(max(0.0, 0.1 + 0.01 * math.sin(i / 40) + rng.gauss(0, 0.008) + (0.05 if rng.random() < 0.01 else 0)))
        xs["human"].append(max(0.0, 0.002 + 0.002 * math.sin(2 * math.pi * i / 288) + rng.gauss(0, 0.001)))
        xs["ai_bot"].append(max(0.0, (0.8 if (i // 30) % 3 == 0 else 0.25) + rng.gauss(0, 0.08)))
        xs["bot"].append(max(0.0, rng.expovariate(1 / 0.002)))
        xs["scanner"].append(max(0.0, rng.expovariate(1 / 0.003)))
    return y, xs


def run(y0, xs, bootstrap=100):
    rows = []
    classes = [c for c in GRID if c in xs]
    for costs in itertools.product(*(GRID[c] for c in classes)):
        cost = dict(zip(classes, costs))
        y = [y0[i] + sum(cost[c] * xs[c][i] for c in classes) for i in range(len(y0))]
        mean_y = sum(y) / len(y)
        truth = {c: cost[c] * sum(xs[c]) / len(xs[c]) / mean_y for c in classes}
        truth["baseline"] = 1 - sum(truth.values())
        a = fit("exp", y, xs, bootstrap=bootstrap)
        reg_err = {k: abs(a.share.get(k, 0.0) - truth[k]) for k in truth}
        naive = {c: a.naive_share.get(c, 0.0) for c in classes}
        naive["baseline"] = 0.0                     # request share has no notion of background load
        naive_err = {k: abs(naive[k] - truth[k]) for k in truth}
        # stronger heuristic: the 5th-percentile CPU is "idle floor", the rest is split by request share
        floor = sorted(y)[int(0.05 * (len(y) - 1))] / mean_y
        tot_rate = sum(sum(xs[c]) for c in xs) or 1.0
        floor_est = {c: (1 - floor) * sum(xs[c]) / tot_rate for c in classes}
        floor_est["baseline"] = floor
        floor_err = {k: abs(floor_est[k] - truth[k]) for k in truth}
        covered = {k: a.share_ci.get(k, (0, 0))[0] - 0.01 <= truth[k] <= a.share_ci.get(k, (0, 0))[1] + 0.01
                    for k in truth}
        rows.append({"cost_ms": {c: v * 1000 for c, v in cost.items()}, "truth": truth, "share": a.share, "ci": a.share_ci,
                     "reg_mae": sum(reg_err.values()) / len(reg_err), "naive_mae": sum(naive_err.values()) / len(naive_err),
                     "ai_bot_err_reg": reg_err.get("ai_bot", 0), "ai_bot_err_naive": naive_err.get("ai_bot", 0),
                     "naive_ai_bot": naive.get("ai_bot", 0.0), "floor_ai_bot": floor_est.get("ai_bot", 0.0),
                     "floor_mae": sum(floor_err.values()) / len(floor_err), "ai_bot_err_floor": floor_err.get("ai_bot", 0),
                     "coverage": sum(covered.values()) / len(covered), "r2": a.r2, "r2_holdout": a.r2_holdout})
    return rows


def summarise(rows, meta):
    def avg(k):
        return sum(r[k] for r in rows) / len(rows)

    def pct(k, q):
        v = sorted(r[k] for r in rows)
        return v[int(q * (len(v) - 1))]

    print(f"## Semi-synthetic attribution benchmark ({meta})\n")
    print(f"{len(rows)} cost scenarios; error = |estimated share - injected share|, averaged over components\n")
    print("| method | mean abs. error | p90 abs. error | AI-crawler share error (mean) |")
    print("|---|---|---|---|")
    print(f"| request-share allocation | {avg('naive_mae'):.1%} | {pct('naive_mae', .9):.1%} | {avg('ai_bot_err_naive'):.1%} |")
    print(f"| idle floor + request share | {avg('floor_mae'):.1%} | {pct('floor_mae', .9):.1%} | {avg('ai_bot_err_floor'):.1%} |")
    print(f"| NNLS regression (this) | {avg('reg_mae'):.1%} | {pct('reg_mae', .9):.1%} | {avg('ai_bot_err_reg'):.1%} |")
    print(f"\n90 % bootstrap interval covers the truth (+-1 pt) for {avg('coverage'):.0%} of components; "
          f"median hold-out R2 {pct('r2_holdout', .5):.2f}\n")
    print("| ai_bot ms/req | human ms/req | true AI-crawler share | NNLS estimate (90 % CI) "
          "| idle floor + req. share | request share |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        if "ai_bot" in r["cost_ms"] and r["cost_ms"].get("bot", 0) == 0 and r["cost_ms"].get("human", 0) == 10:
            lo, hi = r["ci"].get("ai_bot", (0, 0))
            print(f"| {r['cost_ms']['ai_bot']:.1f} | {r['cost_ms']['human']:.0f} | {r['truth']['ai_bot']:.1%} | "
                  f"{r['share'].get('ai_bot', 0):.1%} ({lo:.1%}-{hi:.1%}) | {r['floor_ai_bot']:.1%} | {r['naive_ai_bot']:.1%} |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prometheus")
    ap.add_argument("--cpu-project", default="emotrack")
    ap.add_argument("--traffic-project", default="cs-paper-radar")
    ap.add_argument("--lookback", default="2d")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--json", help="also write the raw rows here")
    a = ap.parse_args()
    if a.synthetic or not a.prometheus:
        y0, xs = synthetic_series()
        meta = "synthetic series, seed 11"
    else:
        lb = int(a.lookback[:-1]) * {"h": 3600, "d": 86400}[a.lookback[-1]]
        y0, xs = real_series(Prom(a.prometheus), a.cpu_project, a.traffic_project, lb)
        meta = f"real CPU of a background-bound project + real traffic of a crawled site, {len(y0)} x 5 min"
    rows = run(y0, xs)
    summarise(rows, meta)
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
