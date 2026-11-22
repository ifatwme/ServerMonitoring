"""PSI- and OOM-aware memory/CPU right-sizing for Docker Compose services.

Kubernetes has VPA (decaying histograms, OOM bump) and KRR (percentile + buffer); Compose users
mostly guess `mem_limit`. This module brings the VPA recommender idea to Compose and adds the
kernel's Pressure Stall Information (PSI) as a safety signal — the same signal Kubernetes 1.36
made GA per container — so a limit is never shrunk while the container is already stalling.

Memory (incompressible, the limit is what protects neighbours on a swapless host):
  peaks      5-minute maxima of the working set
  target     exponentially-decayed weighted quantile (half-life 24 h) of the peaks, q = 0.98
  floor      the largest peak of the last 24 h (recent behaviour always wins)
  margin     +15 %, and at least +32 MiB
  OOM bump   after an OOM kill or memory.max events: >= 1.25 x the limit at that time
  PSI guard  memory "some" stall >= 0.5 % of the time: never recommend below the current limit
  history    with < 7 days of history the upper bound is x (1 + 1/days) (VPA); a shrink is only
             suggested when that upper bound is < 70 % of the limit, and the upper bound is recommended
CPU (compressible: a lower cap saves nothing, a too-low cap costs latency):
  report the decayed p95 of usage; recommend a higher cap only when the container is throttled
  (CFS throttled periods >= 10 %) AND stalls on CPU (PSI) — otherwise leave the cap alone.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

MiB = 1024 * 1024
HALF_LIFE = 24 * 3600
MEM_Q = 0.98
CPU_Q = 0.95
MARGIN = 0.15
MIN_MARGIN = 32 * MiB
MIN_MEM = 32 * MiB
OOM_BUMP = 1.25
PSI_MEM_GUARD = 0.005
THROTTLE_GUARD = 0.10
PSI_CPU_GUARD = 0.05
SHRINK_BELOW = 0.70       # only suggest shrinking when the recommendation is < 70 % of the limit


@dataclass
class Recommendation:
    project: str
    service: str
    container: str
    history_days: float
    mem_limit: float | None          # current, bytes (None = unlimited)
    mem_peak_24h: float
    mem_peak_max: float
    mem_target: float                # decayed quantile of peaks
    mem_recommended: float
    mem_psi_some: float              # mean share of time stalled on memory
    oom_events: float
    cpu_limit: float | None
    cpu_p95: float
    cpu_throttled: float             # share of CFS periods throttled
    cpu_psi_some: float
    cpu_recommended: float | None    # None = keep the current cap
    decision: str                    # shrink | grow | ok | set-limit | insufficient-data
    reasons: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def decayed_quantile(points: dict[float, float], q: float, now: float, half_life: float = HALF_LIFE) -> float:
    """Weighted quantile of the values with weight 2^-(age/half_life)."""
    if not points:
        return 0.0
    items = sorted((v, 2 ** (-(now - t) / half_life)) for t, v in points.items())
    total = sum(w for _, w in items)
    acc = 0.0
    for v, w in items:
        acc += w
        if acc >= q * total:
            return v
    return items[-1][0]


def history_days(points: dict[float, float]) -> float:
    return (max(points) - min(points)) / 86400 if len(points) > 1 else 0.0


def _mean(d: dict[float, float]) -> float:
    return sum(d.values()) / len(d) if d else 0.0


def recommend(project: str, service: str, container: str, *, now: float,
              mem_peaks: dict[float, float], cpu: dict[float, float],
              mem_limit: float | None, cpu_limit: float | None,
              mem_psi: dict[float, float] | None = None, cpu_psi: dict[float, float] | None = None,
              throttled: dict[float, float] | None = None, oom_events: float = 0.0,
              min_history_days: float = 1.0) -> Recommendation:
    reasons: list[str] = []
    days = history_days(mem_peaks)
    peak_max = max(mem_peaks.values(), default=0.0)
    peak_24h = max((v for t, v in mem_peaks.items() if t >= now - 86400), default=0.0)
    target = decayed_quantile(mem_peaks, MEM_Q, now)
    base = max(target, peak_24h)
    rec = max(base * (1 + MARGIN), base + MIN_MARGIN, MIN_MEM)
    # VPA-style confidence: with little history the *upper bound* widens; shrinking is only
    # suggested when even the upper bound fits, and then the upper bound is what we recommend.
    upper = rec * (1 + 1 / max(days, 0.25)) if days < 7 else rec
    psi_m = _mean(mem_psi or {})
    psi_c = _mean(cpu_psi or {})
    thr = _mean(throttled or {})

    if oom_events > 0 and mem_limit:
        rec = max(rec, mem_limit * OOM_BUMP)
        reasons.append(f"{oom_events:.0f} OOM/limit event(s) in the window: limit bumped x{OOM_BUMP}")
    if psi_m >= PSI_MEM_GUARD and mem_limit:
        rec = max(rec, mem_limit)
        reasons.append(f"memory stall {psi_m:.1%} of the time: not shrinking below the current limit")
    upper = max(upper, rec)

    cpu_p95 = decayed_quantile(cpu, CPU_Q, now)
    cpu_rec = None
    if cpu_limit and thr >= THROTTLE_GUARD and psi_c >= PSI_CPU_GUARD:
        cpu_rec = round(max(cpu_limit * 1.5, cpu_p95 * 1.3), 2)
        reasons.append(f"throttled in {thr:.0%} of CFS periods and stalled {psi_c:.0%} on CPU: raise cpus")

    if days < min_history_days:
        decision = "insufficient-data"
        reasons.append(f"only {days:.1f} day(s) of history")
    elif mem_limit is None:
        decision = "set-limit"
        reasons.append("no memory limit: one runaway process can OOM the whole host")
    elif rec > mem_limit or cpu_rec:
        decision = "grow"
        if rec > mem_limit:
            reasons.append("recent peaks + margin exceed the current limit")
    elif upper < SHRINK_BELOW * mem_limit:
        decision = "shrink"
        rec = upper
        reasons.append(f"98th-pct peak {target / MiB:.0f} MiB, 24 h peak {peak_24h / MiB:.0f} MiB vs limit "
                       f"{mem_limit / MiB:.0f} MiB" + (f" (margin widened for {days:.1f} d of history)" if days < 7 else ""))
    else:
        decision = "ok"
        rec = mem_limit                       # within the band: keep the current limit
    rec = math.ceil(rec / (16 * MiB)) * 16 * MiB          # round up to 16 MiB
    return Recommendation(project, service, container, round(days, 2), mem_limit, peak_24h, peak_max, target,
                          float(rec), psi_m, oom_events, cpu_limit, cpu_p95, thr, psi_c, cpu_rec, decision, reasons)


def compose_override(recs: list[Recommendation]) -> str:
    """A compose override (YAML) with the recommended limits of grow/shrink/set-limit services."""
    lines = ["# Generated by server-monitor rightsizing. Review, then use with:",
             "#   docker compose -f docker-compose.yml -f <this file> up -d",
             "services:"]
    n = 0
    for r in sorted(recs, key=lambda r: r.service):
        if r.decision not in ("grow", "shrink", "set-limit"):
            continue
        n += 1
        lines.append(f"  {r.service}:")
        lines.append(f"    mem_limit: {int(r.mem_recommended // MiB)}m"
                     f"    # was {int(r.mem_limit // MiB) if r.mem_limit else 'unlimited'}{'m' if r.mem_limit else ''};"
                     f" {r.decision}")
        if r.cpu_recommended:
            lines.append(f"    cpus: {r.cpu_recommended}    # was {r.cpu_limit}")
    return "\n".join(lines) + "\n" if n else ""
