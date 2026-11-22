import random

import pytest

from servermon import burst


def point_process(seconds=600, rate=0.35, cost=0.27, idle=0.004, noise=0.003, seed=0, cls="ai_bot",
                  background=None):
    """Arrivals ~ Bernoulli per second; CPU = idle + cost spread 40/60 over the second before/of the log line."""
    rng = random.Random(seed)
    secs = [1_800_000_000.0 + i for i in range(seconds)]
    arr = [1 if rng.random() < rate else 0 for _ in secs]
    cpu = [idle + abs(rng.gauss(0, noise)) for _ in secs]
    if background:
        cpu = [c + b for c, b in zip(cpu, background)]
    for i, n in enumerate(arr):
        if n:
            cpu[i] += 0.6 * cost * n
            if i:
                cpu[i - 1] += 0.4 * cost * n
    counts = {cls: {s: n for s, n in zip(secs, arr) if n}}
    return secs, cpu, arr, counts


def bursts(secs, cpu, counts, size=120):
    out = []
    for i in range(0, len(secs) - size + 1, size):
        c = list(zip(secs[i:i + size], cpu[i:i + size]))
        out.append(burst.burst_stats(c, counts))
    return out


def test_recovers_per_request_cost_of_steady_crawler():
    secs, cpu, arr, counts = point_process(seconds=2400, rate=0.2)
    r = burst.fit_bursts("p", bursts(secs, cpu, counts), bootstrap=100)
    assert r.baseline_source == "quiet-seconds" and r.quiet_seconds >= 30
    assert r.cost_per_request["ai_bot"] == pytest.approx(0.27, rel=0.08)
    assert r.baseline_cores == pytest.approx(0.004 + 0.0024, abs=0.003)     # idle + mean |noise|
    truth = 0.27 * sum(arr) / len(arr) / (sum(cpu) / len(cpu))
    assert r.share["ai_bot"] == pytest.approx(truth, abs=0.03)
    assert r.regression_share["ai_bot"] == pytest.approx(truth, abs=0.03)
    lo, hi = r.share_ci["ai_bot"]
    assert lo <= truth <= hi
    assert r.confidence == "ok" and r.r2 > 0.8


def test_long_cpu_tail_selects_a_wider_lag_window():
    rng = random.Random(4)
    secs = [1_800_000_000.0 + i for i in range(3600)]
    arr = [1 if rng.random() < 0.08 else 0 for _ in secs]
    cpu = [0.01 + abs(rng.gauss(0, 0.002)) for _ in secs]
    for i, n in enumerate(arr):
        for off in (0, -1, -2, -3):                        # 4 s of CPU before the log line
            if n and 0 <= i + off < len(cpu):
                cpu[i + off] += 0.1 * n
    counts = {"ai_bot": {s: n for s, n in zip(secs, arr) if n}}
    r = burst.fit_bursts("p", bursts(secs, cpu, counts), bootstrap=50)
    assert r.lags[0] <= -3                                 # cross-validation found the tail
    assert r.cost_per_request["ai_bot"] == pytest.approx(0.4, rel=0.1)
    truth = 0.4 * sum(arr) / len(arr) / (sum(cpu) / len(cpu))
    assert r.share["ai_bot"] == pytest.approx(truth, abs=0.04)


def test_dense_traffic_falls_back_to_regression():
    secs, cpu, arr, counts = point_process(seconds=1200, rate=0.95)
    r = burst.fit_bursts("p", bursts(secs, cpu, counts), bootstrap=20)
    assert r.baseline_source == "regression"
    assert r.share == r.regression_share


def test_background_load_is_not_attributed_to_requests():
    rng = random.Random(5)
    bg = [0.1 + 0.02 * rng.random() for _ in range(1800)]
    secs, cpu, arr, counts = point_process(seconds=1800, cost=0.0, background=bg)
    r = burst.fit_bursts("p", bursts(secs, cpu, counts), bootstrap=100)
    assert r.share["baseline"] > 0.97
    assert r.share_ci["ai_bot"][1] < 0.05


def test_few_bursts_are_flagged():
    secs, cpu, _, counts = point_process(seconds=360)
    assert burst.fit_bursts("p", bursts(secs, cpu, counts)).confidence == "insufficient-data"


def test_classes_without_requests_are_dropped():
    secs, cpu, _, counts = point_process(seconds=1200)
    r = burst.fit_bursts("p", bursts(secs, cpu, counts), bootstrap=10)
    assert set(r.share) == {"baseline", "ai_bot"} and "human" not in r.cost_per_request


def test_burst_stats_drops_edge_rows_keeps_present_classes_and_quiet_seconds():
    secs = [float(t) for t in range(100, 130)]
    cpu = list(zip(secs, [0.1] * 30))
    st = burst.burst_stats(cpu, {"human": {110.0: 2, 112.0: 1}, "probe": {120.0: 1}})
    assert st["n"] == 30 - 5 - 2                                    # MAX_LAGS = -5..+2
    assert st["names"] == burst.feature_names(["human"])           # only classes seen in the burst
    assert st["xsum"]["human"] == 3 and st["xsum"]["bot"] == 0
    assert len(st["AtA"]) == 1 + len(burst.MAX_LAGS) and st["AtA"][0][0] == st["n"]
    dist = {int(round(v * 10)): d for v, d in st["quiet"]}         # all CPU values equal; check distances
    assert all(d > 1 for _, d in st["quiet"]) and dist


def test_per_second_counts_uses_end_of_second_stamps():
    c = burst.per_second_counts([(100.2, "human"), (100.9, "human"), (101.0, "bot"), (500.5, "bot")], [101.0, 102.0])
    assert c["human"][101.0] == 2 and c["bot"][101.0] == 1 and 501.0 not in c["bot"]


def test_sample_cpu_with_fake_clock():
    usage = {"a": 0, "b": 0}
    clock = {"t": 1000.5}

    def read(cid):
        return usage[cid]

    def sleep(dt):
        clock["t"] += dt
        usage["a"] += int(250_000 * dt)        # 0.25 cores
        usage["b"] += int(500_000 * dt)        # 0.5 cores

    out = burst.sample_cpu(["a", "b"], 5, read=read, clock=lambda: clock["t"], sleep=sleep)
    assert len(out) == 5 and all(v == pytest.approx(0.75, rel=0.01) for _, v in out[1:])
    multi = burst.sample_cpu_multi(["a", "b"], 3, read=read, clock=lambda: clock["t"], sleep=sleep)
    assert multi[-1][1]["b"] == pytest.approx(0.5, rel=0.01)


def test_cgroup_reader(tmp_path):
    d = tmp_path / "system.slice" / "docker-abc.scope"
    d.mkdir(parents=True)
    (d / "cpu.stat").write_text("usage_usec 12345\nuser_usec 1\n")
    assert burst.cgroup_usage_usec("abc", root=str(tmp_path)) == 12345
    assert burst.cgroup_usage_usec("missing", root=str(tmp_path)) is None


def test_keep_recent_bounds_state():
    assert len(burst.keep_recent([{"n": i} for i in range(10)], {"n": 99}, limit=5)) == 5


def test_to_dict_roundtrip():
    import json
    secs, cpu, _, counts = point_process(seconds=1200)
    json.dumps(burst.fit_bursts("p", bursts(secs, cpu, counts), bootstrap=5).to_dict())
