"""High-frequency ("burst") CPU attribution: make steady traffic identifiable.

Why: a rate-limited crawler arrives at an almost constant rate. Averaged over 5 minutes its rate
has nearly no variance, so a regression cannot tell "crawler work" from "background loop" — both
are a constant. (Found on a production site: the 5-minute model put 100 % on the baseline while
per-thread CPU accounting showed ~95 % of the CPU in request-handling threads.) At 1-second
resolution the same traffic is a point process — most seconds have no request, some have one or
two — and the CPU follows each arrival. That variance identifies the per-request cost.

How:
  * every ~15 minutes, at a randomised phase (no aliasing with cron-like jobs), sample each
    project's containers' cgroup v2 `cpu.stat` usage at 1 Hz for `duration` seconds;
  * count the project's requests per second and per traffic class from the access log timestamps
    (nginx logs when a request finishes, so CPU precedes the log line);
  * baseline = mean CPU of "quiet" seconds, farther from any request than the chosen lag window
    (a model-free idle level; falls back to the regression's intercept when there are too few);
  * the request-driven remainder is split between classes by a distributed-lag NNLS regression of
    cpu(t) on x_c(t+l), with the lag window chosen by K-fold cross-validation over bursts
    (falls back to the pure regression when traffic is too dense to leave quiet seconds);
  * keep only sufficient statistics per burst (X'X, X'y, y'y, sums), so state stays tiny; bursts
    are also the blocks of the bootstrap.
"""
from __future__ import annotations

import os
import random
import time
from collections import defaultdict
from dataclasses import asdict, dataclass

from .attribution import TRAFFIC_CLASSES
from .nnls import nnls_normal

MAX_LAGS = (-5, -4, -3, -2, -1, 0, 1, 2)     # stored per burst; CPU precedes the log line
# candidate lag windows, chosen per project by K-fold cross-validation over bursts: a request's CPU
# can spread over several seconds (thread pools, slow pages), and the right width is an empirical question
WINDOWS = ((-1, 1), (-2, 1), (-3, 1), (-4, 2), (-5, 2))
LAGS = (-1, 0, 1)            # default window when there is too little data to choose
MAX_BURSTS = 300             # ~3 days at one burst per 15 min
CV_FOLDS = 5
QUIET_MAX = 6                # distance (s) to the nearest request is stored up to this cap
MIN_QUIET = 30               # quiet seconds needed to trust the quiet-second baseline
CGROUP_ROOT = "/sys/fs/cgroup"


# ── sampling ─────────────────────────────────────────────────────────────────

def cgroup_usage_usec(container_id: str, root: str = CGROUP_ROOT) -> int | None:
    for rel in (f"system.slice/docker-{container_id}.scope", f"docker/{container_id}"):
        try:
            with open(os.path.join(root, rel, "cpu.stat")) as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
        except OSError:
            continue
    return None


def sample_cpu(container_ids: list[str], duration: int, read=cgroup_usage_usec, clock=time.time,
               sleep=time.sleep) -> list[tuple[float, float]]:
    """[(second_end_timestamp, cores used during that second)] summed over the containers."""
    def total():
        vals = [read(c) for c in container_ids]
        return sum(v for v in vals if v is not None)
    out = []
    t_prev, u_prev = clock(), total()
    nxt = int(t_prev) + 1.0
    for _ in range(duration):
        sleep(max(0.0, nxt - clock()))
        t, u = clock(), total()
        out.append((float(int(t)), (u - u_prev) / 1e6 / max(t - t_prev, 1e-3)))
        t_prev, u_prev = t, u
        nxt += 1.0
    return out


def per_second_counts(events: list[tuple[float, str]], seconds: list[float]) -> dict[str, dict[float, int]]:
    """events [(timestamp, class)] -> {class: {second: count}} for the given second stamps
    (the stamp is the END of the second, i.e. events in (s-1, s])."""
    wanted = set(seconds)
    out: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
    for ts, cls in events:
        s = float(int(ts) + 1) if ts != int(ts) else float(ts)
        if s in wanted:
            out[cls][s] += 1
    return out


# ── sufficient statistics ────────────────────────────────────────────────────

def feature_names(classes, lags=MAX_LAGS) -> list[str]:
    return ["1"] + [f"{c}@{lag}" for c in classes for lag in lags]


def burst_stats(cpu: list[tuple[float, float]], counts: dict[str, dict[float, int]],
                classes: tuple[str, ...] = TRAFFIC_CLASSES, lags: tuple[int, ...] = MAX_LAGS) -> dict:
    """Normal equations of one burst over the widest lag window, for the classes seen in the burst.

    Rows whose lags fall outside the burst are dropped, so every narrower window (a subset of the
    columns) is computed on exactly the same rows."""
    secs = [s for s, _ in cpu]
    y_by = dict(cpu)
    present = [c for c in classes if any(counts.get(c, {}).get(t, 0) for t in secs)]
    names = feature_names(present, lags)
    k = len(names)
    lo, hi = -min(lags), max(lags)
    AtA = [[0.0] * k for _ in range(k)]
    Atb = [0.0] * k
    yy = ysum = 0.0
    xsum = dict.fromkeys(classes, 0.0)
    n = 0
    for s in secs[lo:len(secs) - hi] if hi else secs[lo:]:
        row = [1.0]
        for c in present:
            cc = counts.get(c, {})
            row += [float(cc.get(s + lag, 0)) for lag in lags]
        y = y_by[s]
        for i in range(k):
            if row[i]:
                Atb[i] += row[i] * y
                ri = AtA[i]
                for j in range(k):
                    if row[j]:
                        ri[j] += row[i] * row[j]
        yy += y * y
        ysum += y
        for c in classes:
            xsum[c] += counts.get(c, {}).get(s, 0)
        n += 1
    # for each row second: CPU and the distance to the nearest request of ANY class (incl. probes),
    # capped at QUIET_MAX. Seconds farther from any request than the CPU kernel is wide are idle samples.
    arrivals = sorted(t for cc in counts.values() for t, v in cc.items() if v)
    quiet = []
    for t in (secs[lo:len(secs) - hi] if hi else secs[lo:]):
        d = min((abs(t - a) for a in arrivals if abs(t - a) <= QUIET_MAX), default=QUIET_MAX + 1)
        if d > 1:
            quiet.append([round(y_by[t], 6), int(min(d, QUIET_MAX + 1))])
    return {"names": names, "AtA": AtA, "Atb": Atb, "yy": yy, "ysum": ysum, "xsum": xsum, "n": n, "quiet": quiet}


# ── fit ──────────────────────────────────────────────────────────────────────

@dataclass
class BurstAttribution:
    project: str
    bursts: int
    seconds: int
    mean_cpu: float
    baseline_cores: float
    cost_per_request: dict[str, float]          # core-seconds per request (sum over the lag window)
    mean_rate: dict[str, float]
    share: dict[str, float]
    share_ci: dict[str, tuple[float, float]]
    r2: float
    lags: tuple[int, int]                       # chosen window (first, last), seconds relative to the log line
    cv_error: dict[str, float]                  # window -> cross-validated SSE per second
    baseline_source: str                        # "quiet-seconds" or "regression"
    quiet_seconds: int
    regression_share: dict[str, float]          # pure distributed-lag NNLS shares, for transparency
    confidence: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["share_ci"] = {k: list(v) for k, v in self.share_ci.items()}
        return d


def _project(b: dict, names: list[str]):
    """Sub-matrix of burst b on `names` (features missing from the burst are zero columns)."""
    idx = {n: i for i, n in enumerate(b["names"])}
    pos = [idx.get(n) for n in names]
    A = [[(b["AtA"][i][j] if i is not None and j is not None else 0.0) for j in pos] for i in pos]
    v = [(b["Atb"][i] if i is not None else 0.0) for i in pos]
    return A, v


def _add(acc, A, v, sign=1.0):
    AA, vv = acc
    for i in range(len(v)):
        vv[i] += sign * v[i]
        Ai, AAi = A[i], AA[i]
        for j in range(len(v)):
            AAi[j] += sign * Ai[j]


def _zeros(k):
    return [[0.0] * k for _ in range(k)], [0.0] * k


def _sse(coef, A, v, yy):
    k = len(coef)
    return yy - 2 * sum(coef[i] * v[i] for i in range(k)) + sum(coef[i] * A[i][j] * coef[j] for i in range(k) for j in range(k))


def _window_names(classes, window):
    return feature_names(classes, tuple(range(window[0], window[1] + 1)))


def _costs(coef, names, classes):
    cost = dict.fromkeys(classes, 0.0)
    for c, n in zip(coef, names):
        if n != "1":
            cost[n.split("@")[0]] += c
    return cost


def _shares(coef, names, classes, mean_rate):
    cost = _costs(coef, names, classes)
    contrib = {"baseline": coef[0], **{c: cost[c] * mean_rate[c] for c in classes}}
    tot = sum(contrib.values())
    return {k: (v / tot if tot > 0 else 0.0) for k, v in contrib.items()}


def _hybrid(coef, names, classes, mean_rate, mean_cpu, quiet, guard):
    """Baseline = MEAN CPU of seconds farther than `guard` from any request (the chosen lag window,
    so the kernel cannot reach them), split of the rest by the regression's contributions.
    Falls back to the pure regression when traffic is too dense to leave enough such seconds."""
    reg = _shares(coef, names, classes, mean_rate)
    idle = [v for v, d in quiet if d > guard]
    if len(idle) < MIN_QUIET or mean_cpu <= 0:
        return reg, "regression"
    base = min(1.0, sum(idle) / len(idle) / mean_cpu)
    contrib = {c: reg[c] for c in classes}
    tot = sum(contrib.values())
    if tot <= 0:                                   # regression found nothing: split by request share
        contrib = {c: mean_rate[c] for c in classes}
        tot = sum(contrib.values()) or 1.0
    out = {"baseline": base, **{c: (1 - base) * contrib[c] / tot for c in classes}}
    return out, "quiet-seconds"


def fit_bursts(project: str, bursts: list[dict], bootstrap: int = 200, seed: int = 0,
               min_bursts: int = 8, windows=WINDOWS) -> BurstAttribution | None:
    bursts = [b for b in bursts if b.get("n", 0) > 0 and "names" in b]     # skip bursts in an older format
    if not bursts:
        return None
    n = sum(b["n"] for b in bursts)
    all_classes = list(bursts[0]["xsum"])
    mean_cpu = sum(b["ysum"] for b in bursts) / n
    mean_rate = {c: sum(b["xsum"].get(c, 0.0) for b in bursts) / n for c in all_classes}
    classes = [c for c in all_classes if mean_rate[c] > 0]

    # window selection: K-fold CV over contiguous groups of bursts (needs >= 3 bursts)
    cv: dict[str, float] = {}
    best = (LAGS[0], LAGS[-1])
    if classes and len(bursts) >= 3:
        k_folds = min(CV_FOLDS, len(bursts))
        folds = [list(range(f * len(bursts) // k_folds, (f + 1) * len(bursts) // k_folds)) for f in range(k_folds)]
        for w in windows:
            names = _window_names(classes, w)
            parts = [_project(b, names) for b in bursts]
            total = _zeros(len(names))
            for A, v in parts:
                _add(total, A, v)
            err = 0.0
            for fold in folds:
                if not fold:
                    continue
                train = ([row[:] for row in total[0]], total[1][:])
                for i in fold:
                    _add(train, *parts[i], sign=-1.0)
                coef = nnls_normal(*train)
                err += sum(_sse(coef, *parts[i], bursts[i]["yy"]) for i in fold)
            cv[f"{w[0]}..{w[1]}"] = err / n
        best = min(windows, key=lambda w: cv[f"{w[0]}..{w[1]}"])

    names = _window_names(classes, best)
    parts = [_project(b, names) for b in bursts]
    total = _zeros(len(names))
    for A, v in parts:
        _add(total, A, v)
    coef = nnls_normal(*total) if classes else [mean_cpu]
    quiet = [q for b in bursts for q in b.get("quiet", [])]
    reg_share = _shares(coef, names, classes, mean_rate)
    guard = max(1, -best[0], best[1])
    share, source = _hybrid(coef, names, classes, mean_rate, mean_cpu, quiet, guard)
    yy = sum(b["yy"] for b in bursts)
    sst = yy - n * mean_cpu ** 2
    r2 = 1 - _sse(coef, *total, yy) / sst if sst > 0 else 0.0

    rng = random.Random(seed)
    draws = defaultdict(list)
    for _ in range(bootstrap if classes else 0):
        pick = [rng.randrange(len(bursts)) for _ in bursts]
        acc = _zeros(len(names))
        for i in pick:
            _add(acc, *parts[i])
        nn = sum(bursts[i]["n"] for i in pick)
        mr = {c: sum(bursts[i]["xsum"].get(c, 0.0) for i in pick) / nn for c in classes}
        mc = sum(bursts[i]["ysum"] for i in pick) / nn
        qq = [q for i in pick for q in bursts[i].get("quiet", [])]
        for key, v in _hybrid(nnls_normal(*acc), names, classes, mr, mc, qq, guard)[0].items():
            draws[key].append(v)
    ci = {key: (v, v) for key, v in share.items()}
    for key, vals in draws.items():
        vals.sort()
        ci[key] = (vals[int(0.05 * (len(vals) - 1))], vals[int(0.95 * (len(vals) - 1))])
    conf = "ok"
    if len(bursts) < min_bursts:
        conf = "insufficient-data"
    elif max(hi - lo for lo, hi in ci.values()) > 0.5:
        conf = "low"
    cost = {c: (share[c] * mean_cpu / mean_rate[c] if mean_rate[c] else 0.0) for c in classes}
    n_quiet = sum(1 for _, d in quiet if d > guard)
    return BurstAttribution(project, len(bursts), n, mean_cpu, share["baseline"] * mean_cpu, cost,
                            mean_rate, share, ci, r2, best, cv, source, n_quiet, reg_share, conf)


def keep_recent(bursts: list[dict], new: dict, limit: int = MAX_BURSTS) -> list[dict]:
    return [*bursts, new][-limit:]


# ── runner (systemd: server-monitor-burst.timer) ─────────────────────────────

def _project_containers(project: str, sh) -> list[str]:
    out = sh("docker", "ps", "-q", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}")
    return out.split()


def _events(web_container: str, since: float, sh) -> list[tuple[float, str]]:
    from .common import iso_ts
    from .traffic import LOG_RE, classify
    raw = sh("docker", "logs", "--timestamps", "--since", str(int(since) - 2), web_container, timeout=60)
    ev = []
    for line in raw.splitlines():
        ts_s, _, rest = line.partition(" ")
        ts = iso_ts(ts_s)
        m = LOG_RE.match(rest)
        if ts is None or not m:
            continue
        ip, req, status, _size, ua = m.groups()
        ev.append((ts, classify(ip, req, int(status), ua)))
    return ev


def main(argv=None) -> int:
    import argparse
    import json

    from .common import STATE_DIR, atomic_write, load_config, load_json, sh
    ap = argparse.ArgumentParser(description="Sample 1 Hz cgroup CPU + per-second requests for burst attribution")
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--jitter", type=int, default=0, help="sleep a random 0..N seconds first")
    a = ap.parse_args(argv)
    if a.jitter:
        time.sleep(random.uniform(0, a.jitter))
    _, projects = load_config()
    targets = {p: c for p, c in projects.items() if c.get("web_container")}
    ids = {p: _project_containers(p, sh) for p in targets}
    all_ids = sorted({i for v in ids.values() for i in v})
    if not all_ids:
        return 0
    # one sampling loop for every project: read all cgroups once per second
    start = time.time()
    per_container: dict[str, list[tuple[float, float]]] = {}
    clock_series = sample_cpu_multi(all_ids, a.duration)
    for cid in all_ids:
        per_container[cid] = [(t, v[cid]) for t, v in clock_series]
    path = STATE_DIR / "bursts.json"
    state = load_json(path, {})
    for p, cfg in targets.items():
        if not ids[p]:
            continue
        cpu = [(t, sum(v[c] for c in ids[p])) for t, v in clock_series]
        ev = _events(cfg["web_container"], start, sh)
        counts = per_second_counts(ev, [t for t, _ in cpu])
        st = burst_stats(cpu, counts)
        st["ts"] = start
        state[p] = keep_recent(state.get(p, []), st)
    atomic_write(path, json.dumps(state))
    return 0


def sample_cpu_multi(container_ids: list[str], duration: int, read=cgroup_usage_usec, clock=time.time,
                     sleep=time.sleep) -> list[tuple[float, dict[str, float]]]:
    """Like sample_cpu, but keeps every container separate: [(second, {id: cores})]."""
    def snap():
        return {c: read(c) or 0 for c in container_ids}
    out = []
    t_prev, u_prev = clock(), snap()
    nxt = int(t_prev) + 1.0
    for _ in range(duration):
        sleep(max(0.0, nxt - clock()))
        t, u = clock(), snap()
        dt = max(t - t_prev, 1e-3)
        out.append((float(int(t)), {c: max(0, u[c] - u_prev[c]) / 1e6 / dt for c in container_ids}))
        t_prev, u_prev = t, u
        nxt += 1.0
    return out


if __name__ == "__main__":
    import sys
    sys.exit(main())
