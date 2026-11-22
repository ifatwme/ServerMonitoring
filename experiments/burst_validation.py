#!/usr/bin/env python3
"""Validate burst (1 Hz) attribution against ground truth, on real traces.

    record:  python3 experiments/burst_validation.py record --target cs-paper-radar \\
                 --background emotrack --seconds 600 --out trace.json
    analyse: python3 experiments/burst_validation.py analyse trace.json

The trace holds, per second: CPU (cores) of the target project's containers, CPU of a
background-bound project's containers, and the target's request arrivals per traffic class
(no IPs, paths or user agents). Project names are replaced by "target"/"background".

Analyses
  A. Real traffic, real CPU: the burst hybrid, the pure burst regression, a 60-second-bin regression
     (what minute-level monitoring sees) and request-share allocation, next to an independent
     per-thread CPU measurement of the same app.
  B. Semi-synthetic: background CPU (real noise) + a KNOWN cost per request, using the real arrival
     process, with a short CPU kernel and a long-tailed one (CPU spread over 4 s), over a grid of costs.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from servermon import attribution, burst  # noqa: E402
from servermon.common import sh  # noqa: E402

CLASSES = attribution.TRAFFIC_CLASSES


def record(a):
    from servermon.common import load_config
    _, projects = load_config()
    tid = burst._project_containers(a.target, sh)
    bid = burst._project_containers(a.background, sh)
    start = time.time()
    series = burst.sample_cpu_multi(sorted(set(tid + bid)), a.seconds)
    ev = burst._events(projects[a.target]["web_container"], start, sh)
    secs = [t for t, _ in series]
    counts = burst.per_second_counts(ev, secs)
    trace = {"seconds": secs,
             "target_cpu": [sum(v[c] for c in tid) for _, v in series],
             "background_cpu": [sum(v[c] for c in bid) for _, v in series],
             "arrivals": {c: [counts.get(c, {}).get(s, 0) for s in secs] for c in CLASSES}}
    Path(a.out).write_text(json.dumps(trace))
    print(f"wrote {a.out}: {len(secs)} s, {sum(map(sum, trace['arrivals'].values()))} requests")


def bursts_of(cpu, arrivals, secs, size=120):
    out = []
    for i in range(0, len(secs) - size + 1, size):
        sl = slice(i, i + size)
        c = list(zip(secs[sl], cpu[sl]))
        counts = {k: dict(zip(secs[sl], v[sl])) for k, v in arrivals.items()}
        out.append(burst.burst_stats(c, counts))
    return out


def coarse(cpu, arrivals, bin_s=60):
    n = len(cpu) // bin_s
    y = [sum(cpu[i * bin_s:(i + 1) * bin_s]) / bin_s for i in range(n)]
    xs = {k: [sum(v[i * bin_s:(i + 1) * bin_s]) / bin_s for i in range(n)] for k, v in arrivals.items()}
    return attribution.fit("coarse", y, xs, bootstrap=100, block=3, min_samples=5)


def inject(arr, n_secs, cost, kernel):
    """kernel: {offset_seconds: fraction} relative to the log line, fractions sum to 1."""
    inj = [0.0] * n_secs
    for v in arr.values():
        for i, n in enumerate(v):
            if n:
                for off, frac in kernel.items():
                    if 0 <= i + off < n_secs:
                        inj[i + off] += frac * cost * n
    return inj


KERNELS = {"short (60/40 over 2 s)": {0: 0.6, -1: 0.4},
           "long tail (4 s before the log line)": {0: 0.25, -1: 0.25, -2: 0.25, -3: 0.25}}


def analyse(a):
    tr = json.loads(Path(a.trace).read_text())
    secs, arr = tr["seconds"], tr["arrivals"]
    req = {k: sum(v) for k, v in arr.items() if sum(v)}
    main_cls = max(req, key=req.get)
    total = sum(req.values())
    print(f"## Burst attribution validation ({len(secs)} s trace, {total} requests: "
          + ", ".join(f"{k} {v}" for k, v in req.items()) + ")\n")

    # A. real traffic, real CPU
    b = burst.fit_bursts("target", bursts_of(tr["target_cpu"], arr, secs), bootstrap=200, min_bursts=2)
    c = coarse(tr["target_cpu"], arr)
    mean = sum(tr["target_cpu"]) / len(secs)
    print("### A. Real traffic, real CPU (steady, rate-limited crawler)\n")
    print(f"Mean CPU {mean:.3f} cores at {total / len(secs):.2f} req/s. Independent reference: per-thread CPU accounting "
          "of the app process put >= 95 % of its CPU in the request worker threads (measured separately).\n")
    print("| method | request-driven share | 90 % CI | per-request CPU |")
    print("|---|---|---|---|")
    lo = 1 - b.share_ci["baseline"][1]
    hi = 1 - b.share_ci["baseline"][0]
    print(f"| burst hybrid (quiet-second baseline, {b.quiet_seconds} quiet s) | {1 - b.share['baseline']:.1%} "
          f"| {lo:.0%}-{hi:.0%} | "
          f"{b.cost_per_request.get(main_cls, 0) * 1000:.0f} ms |")
    reg = 1 - b.regression_share["baseline"]
    print(f"| burst regression only (lags {b.lags[0]}..{b.lags[1]} s, chosen by CV) | {reg:.1%} | - | - |")
    print(f"| regression on 60 s bins | {1 - c.share['baseline']:.1%} | - | confidence: {c.confidence} |")
    print(f"| request-share allocation | 100 % ({main_cls}) | - | - |\n")

    # B. semi-synthetic
    print("### B. Semi-synthetic: real background CPU + known cost per request on the real arrival process\n")
    errs: dict[str, list[float]] = {"burst hybrid": [], "burst regression": [], "60 s bins": [], "request share": []}
    for kname, kernel in KERNELS.items():
        print(f"CPU kernel: {kname}\n")
        print("| injected ms/request | true share | burst hybrid (90 % CI) | burst regression | 60 s bins | request share |")
        print("|---|---|---|---|---|---|")
        for ms in (5, 20, 50, 100, 270):
            inj = inject(arr, len(secs), ms / 1000, kernel)
            y = [bg + x for bg, x in zip(tr["background_cpu"], inj)]
            true = sum(inj) / sum(y)
            bb = burst.fit_bursts("s", bursts_of(y, arr, secs), bootstrap=200, min_bursts=2)
            cc = coarse(y, arr)
            est = {"burst hybrid": 1 - bb.share["baseline"], "burst regression": 1 - bb.regression_share["baseline"],
                   "60 s bins": 1 - cc.share["baseline"], "request share": 1.0}
            for k, v in est.items():
                errs[k].append(abs(v - true))
            blo, bhi = 1 - bb.share_ci["baseline"][1], 1 - bb.share_ci["baseline"][0]
            print(f"| {ms} | {true:.1%} | {est['burst hybrid']:.1%} ({blo:.0%}-{bhi:.0%}) | {est['burst regression']:.1%} | "
                  f"{est['60 s bins']:.1%} | 100 % |")
        print()
    print("Mean absolute error of the request-driven share over both kernels: "
          + ", ".join(f"{k} {sum(v) / len(v):.1%}" for k, v in errs.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--target", required=True)
    r.add_argument("--background", required=True)
    r.add_argument("--seconds", type=int, default=600)
    r.add_argument("--out", required=True)
    r.set_defaults(f=record)
    s = sub.add_parser("analyse")
    s.add_argument("trace")
    s.set_defaults(f=analyse)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
