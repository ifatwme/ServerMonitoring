"""Analyzer: run CPU attribution + right-sizing against Prometheus history, export the results.

    python -m servermon.analyze [--lookback 7d] [--print]

Writes $SM_STATE_DIR/textfile/server_monitor_insights.prom (sm_attribution_*, sm_rightsize_*,
sm_memory_limit_*) for node-exporter and $SM_STATE_DIR/insights.json for `projctl`.
Runs every 15 minutes from server-monitor-analyze.timer.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import attribution, burst, rightsizing
from .common import INSIGHTS_FILE, STATE_DIR, TEXTFILE_DIR, Out, atomic_write, load_json
from .prom import Prom, PromError

STEP = 300
TEXTFILE = TEXTFILE_DIR / "server_monitor_insights.prom"


def parse_duration(s: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    return int(float(s[:-1]) * units[s[-1]]) if s[-1] in units else int(s)


def by(rows, *keys) -> dict:
    return {tuple(m.get(k, "") for k in keys): pts for m, pts in rows}


def instant(prom: Prom, expr: str, key: str) -> dict[str, float]:
    return {m.get(key, ""): v for m, v in prom.query(expr)}


# ── attribution ──────────────────────────────────────────────────────────────

def run_attribution(prom: Prom, start: float, end: float) -> dict[str, attribution.Attribution]:
    cpu = by(prom.range("avg_over_time(project:cpu_cores:rate2m[5m])", start, end, STEP), "project")
    req = prom.range("sum by (project, class) (rate(sm_http_requests_total[5m]))", start, end, STEP)
    xs: dict[str, dict[str, dict[float, float]]] = {}
    for m, pts in req:
        if m.get("class") in attribution.TRAFFIC_CLASSES:
            xs.setdefault(m["project"], {})[m["class"]] = pts
    out = {}
    for proj, classes in xs.items():
        y = cpu.get((proj,))
        if not y:
            continue
        first = min(min(p) for p in classes.values() if p)        # traffic counters started here
        y = {t: v for t, v in y.items() if t >= first}
        yv, xv = attribution.align(y, classes)
        if len(yv) < 12:
            continue
        out[proj] = attribution.fit(proj, yv, xv)
    return out


def run_burst_attribution(path=None) -> dict[str, burst.BurstAttribution]:
    state = load_json(path or STATE_DIR / "bursts.json", {})
    out = {}
    for p, bursts in state.items():
        if sum(sum(b["xsum"].values()) for b in bursts) == 0:
            continue                                    # no traffic seen in any burst
        r = burst.fit_bursts(p, bursts)
        if r:
            out[p] = r
    return out


# ── right-sizing ─────────────────────────────────────────────────────────────

def run_rightsizing(prom: Prom, start: float, end: float, lookback: int) -> list[rightsizing.Recommendation]:
    sel = 'name!=""'
    keys = ("name", "project", "service")
    mem = prom.range(f"max by (name, project, service) (max_over_time(container_memory_working_set_bytes{{{sel}}}[5m]))",
                     start, end, STEP)
    cpu = by(prom.range(f"sum by (name) (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))", start, end, STEP), "name")
    mpsi = by(prom.range(f"sum by (name) (rate(container_pressure_memory_waiting_seconds_total{{{sel}}}[5m]))",
                         start, end, STEP), "name")
    cpsi = by(prom.range(f"sum by (name) (rate(container_pressure_cpu_waiting_seconds_total{{{sel}}}[5m]))",
                         start, end, STEP), "name")
    thr = by(prom.range(f"sum by (name) (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[5m]))"
                        f" / sum by (name) (rate(container_cpu_cfs_periods_total{{{sel}}}[5m]))", start, end, STEP), "name")
    oom = instant(prom, f"sum by (name) (increase(container_oom_events_total{{{sel}}}[{lookback}s]))", "name")
    mem_lim = instant(prom, "max by (container) (sm_container_memory_limit_bytes)", "container")
    cpu_lim = instant(prom, "max by (container) (sm_container_cpu_limit_cores)", "container")
    running = instant(prom, "max by (container) (sm_container_up)", "container")
    recs = []
    for m, peaks in mem:
        name, proj, svc = (m.get(k, "") for k in keys)
        if not peaks or running.get(name) != 1 or max(peaks) < end - 900:
            continue                                      # gone or stopped containers
        recs.append(rightsizing.recommend(
            proj or "other", svc or name, name, now=end, mem_peaks=peaks, cpu=cpu.get((name,), {}),
            mem_limit=mem_lim.get(name), cpu_limit=cpu_lim.get(name), mem_psi=mpsi.get((name,), {}),
            cpu_psi=cpsi.get((name,), {}), throttled=thr.get((name,), {}), oom_events=oom.get(name, 0.0)))
    return recs


# ── export ───────────────────────────────────────────────────────────────────

def export(atts: dict[str, attribution.Attribution], recs: list[rightsizing.Recommendation],
           mem_total: float | None, duration: float, now: float,
           bursts: dict[str, burst.BurstAttribution] | None = None) -> tuple[str, dict]:
    out = Out()
    bursts = bursts or {}
    for p, b in bursts.items():
        L = {"project": p, "method": "burst"}
        out.add("sm_attribution_samples", b.seconds, L, "Samples used by the attribution fit (5-min steps or 1-s burst samples)")
        out.add("sm_attribution_bursts", b.bursts, {"project": p}, "1 Hz sampling bursts behind the burst attribution")
        out.add("sm_attribution_r2", b.r2, L, "In-sample R^2 of the attribution model")
        out.add("sm_attribution_confident", 1 if b.confidence == "ok" else 0, {**L, "confidence": b.confidence},
                "1 = enough data and narrow bootstrap intervals (5m: also identifiable)")
        for comp, sh_ in b.share.items():
            C = {**L, "component": comp}
            lo, hi = b.share_ci[comp]
            out.add("sm_attribution_share", sh_, C, "Share of the project's CPU caused by each component")
            out.add("sm_attribution_share_ci_low", lo, C, "5th percentile of the share (bootstrap)")
            out.add("sm_attribution_share_ci_high", hi, C, "95th percentile of the share (bootstrap)")
            out.add("sm_attribution_cores", sh_ * b.mean_cpu, C, "Mean cores attributed to each component")
        for c, v in b.cost_per_request.items():
            if b.share_ci.get(c, (0, 0))[0] > 0.005:
                out.add("sm_attribution_cpu_ms_per_request", v * 1000, {**L, "class": c},
                        "Marginal CPU cost of one request of the class, in CPU milliseconds (only when significant)")
    for p, a in atts.items():
        L = {"project": p, "method": "5m"}
        out.add("sm_attribution_samples", a.samples, L, "Samples used by the attribution fit (5-min steps or 1-s burst samples)")
        out.add("sm_attribution_r2", a.r2, L, "In-sample R^2 of the attribution model")
        if a.r2_holdout is not None:
            out.add("sm_attribution_r2_holdout", a.r2_holdout, L, "R^2 on the chronological 30 % hold-out")
        out.add("sm_attribution_confident", 1 if a.confidence == "ok" else 0, {**L, "confidence": a.confidence},
                "1 = enough data and narrow bootstrap intervals (5m: also identifiable)")
        for c, v in a.max_hidden_share.items():
            out.add("sm_attribution_max_hidden_share", v, {"project": p, "class": c},
                    "5m model: largest share the class could have and still fit the data (identifiability)")
        for comp, s in a.share.items():
            C = {**L, "component": comp}
            out.add("sm_attribution_share", s, C, "Share of the project's CPU caused by each component")
            out.add("sm_attribution_cores", s * a.mean_cpu, C, "Mean cores attributed to each component")
            lo, hi = a.share_ci[comp]
            out.add("sm_attribution_share_ci_low", lo, C, "5th percentile of the share (bootstrap)")
            out.add("sm_attribution_share_ci_high", hi, C, "95th percentile of the share (bootstrap)")
        for c, v in a.naive_share.items():
            out.add("sm_attribution_naive_share", v, {"project": p, "component": c},
                    "Request-share allocation (what naive crawler-cost reports use), for comparison")
        for c, v in a.cost_per_request.items():
            if not a.significant(c):
                continue                          # a coefficient fitted on noise is not a cost
            out.add("sm_attribution_cpu_ms_per_request", v * 1000, {**L, "class": c},
                    "Marginal CPU cost of one request of the class, in CPU milliseconds (only when significant)")
    sum_lim = sum_rec = 0.0
    for r in recs:
        L = {"project": r.project, "service": r.service, "container": r.container}
        out.add("sm_rightsize_memory_recommended_bytes", r.mem_recommended, L, "Recommended mem_limit")
        if r.mem_limit:
            out.add("sm_rightsize_memory_limit_bytes", r.mem_limit, L, "Current mem_limit")
        out.add("sm_rightsize_memory_peak_24h_bytes", r.mem_peak_24h, L, "Largest 5-min working-set peak, last 24 h")
        out.add("sm_rightsize_memory_target_bytes", r.mem_target, L, "Decayed 98th percentile of the 5-min peaks")
        out.add("sm_rightsize_memory_psi_share", r.mem_psi_some, L, "Mean share of time stalled on memory (PSI some)")
        out.add("sm_rightsize_cpu_p95_cores", r.cpu_p95, L, "Decayed p95 CPU usage")
        out.add("sm_rightsize_cpu_throttled_share", r.cpu_throttled, L, "Share of CFS periods throttled")
        out.add("sm_rightsize_cpu_psi_share", r.cpu_psi_some, L, "Mean share of time waiting for CPU (PSI some)")
        if r.cpu_recommended:
            out.add("sm_rightsize_cpu_recommended_cores", r.cpu_recommended, L, "Recommended cpus (only when throttled)")
        out.add("sm_rightsize_history_days", r.history_days, L, "Days of history behind the recommendation")
        out.add("sm_rightsize_decision", 1, {**L, "decision": r.decision}, "shrink | grow | ok | set-limit | insufficient-data")
        sum_lim += r.mem_limit or 0
        sum_rec += r.mem_recommended
    if mem_total:
        out.add("sm_memory_limit_overcommit_ratio", sum_lim / mem_total, {"limits": "current"},
                "Sum of container memory limits / host RAM (> 1 = limits cannot all be honoured at once)")
        out.add("sm_memory_limit_overcommit_ratio", sum_rec / mem_total, {"limits": "recommended"},
                "Sum of container memory limits / host RAM (> 1 = limits cannot all be honoured at once)")
    out.add("sm_analyzer_duration_seconds", duration, None, "Analyzer run time")
    out.add("sm_analyzer_last_run_timestamp_seconds", now, None, "Analyzer last successful run")
    report = {"generated": now, "attribution": {p: a.to_dict() for p, a in atts.items()},
              "attribution_burst": {p: b.to_dict() for p, b in bursts.items()},
              "rightsizing": [r.to_dict() for r in recs], "memory_total": mem_total,
              "overcommit": {"current": sum_lim / mem_total if mem_total else None,
                             "recommended": sum_rec / mem_total if mem_total else None}}
    return out.render(), report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lookback", default="7d")
    ap.add_argument("--prometheus", default=None)
    ap.add_argument("--print", action="store_true", help="print the JSON report instead of only writing files")
    a = ap.parse_args(argv)
    t0 = time.time()
    prom = Prom(a.prometheus) if a.prometheus else Prom()
    lookback = parse_duration(a.lookback)
    end = time.time() // STEP * STEP
    start = end - lookback
    try:
        atts = run_attribution(prom, start, end)
        bursts = run_burst_attribution()
        recs = run_rightsizing(prom, start, end, lookback)
        mem_total = next(iter(v for _, v in prom.query("node_memory_MemTotal_bytes")), None)
    except PromError as e:
        print(f"prometheus unavailable: {e}", file=sys.stderr)
        return 1
    text, report = export(atts, recs, mem_total, time.time() - t0, time.time(), bursts)
    atomic_write(TEXTFILE, text)
    atomic_write(INSIGHTS_FILE, json.dumps(report, indent=1))
    if a.print:
        print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
