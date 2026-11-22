import pytest

from servermon.rightsizing import MiB, compose_override, decayed_quantile, history_days, recommend

NOW = 1_800_000_000.0
DAY = 86400


def series(days, value, step=300, spike=None):
    pts = {NOW - i * step: value for i in range(int(days * DAY / step))}
    if spike:
        pts[NOW - spike[0]] = spike[1]
    return pts


def rec(**kw):
    base = dict(now=NOW, mem_peaks=series(10, 100 * MiB), cpu=series(10, 0.05), mem_limit=1024 * MiB, cpu_limit=1.0)
    base.update(kw)
    return recommend("proj", "svc", "proj-svc-1", **base)


def test_decayed_quantile_prefers_recent_values():
    pts = {NOW - 10 * DAY: 1000.0, NOW: 10.0}
    assert decayed_quantile(pts, 0.5, NOW) == 10.0           # the old point has ~0.1 % of the weight
    assert decayed_quantile(pts, 0.5, NOW, half_life=100 * DAY) == 10.0
    assert decayed_quantile({}, 0.9, NOW) == 0.0


def test_history_days():
    assert history_days(series(3, 1.0)) == pytest.approx(3, abs=0.01)
    assert history_days({NOW: 1.0}) == 0.0


def test_overprovisioned_service_is_shrunk_with_margin():
    r = rec()
    assert r.decision == "shrink"
    assert 100 * MiB * 1.15 <= r.mem_recommended <= 200 * MiB
    assert r.mem_recommended % (16 * MiB) == 0


def test_short_history_widens_before_shrinking():
    long_ = rec(mem_peaks=series(10, 400 * MiB))
    short = rec(mem_peaks=series(1.5, 400 * MiB))
    assert long_.decision == "shrink"
    assert short.decision == "ok"                            # upper bound 460 MiB * 1.67 > 70 % of 1 GiB
    assert short.mem_recommended == 1024 * MiB              # ok = keep the current limit


def test_recent_spike_sets_the_floor():
    r = rec(mem_peaks=series(10, 100 * MiB, spike=(3600, 600 * MiB)))
    assert r.mem_recommended >= 600 * MiB * 1.15


def test_old_spike_decays_away():
    r = rec(mem_peaks=series(10, 100 * MiB, spike=(9 * DAY, 900 * MiB)))
    assert r.decision == "shrink" and r.mem_recommended < 300 * MiB


def test_oom_event_bumps_above_the_limit():
    r = rec(mem_peaks=series(10, 500 * MiB), oom_events=1)
    assert r.decision == "grow"
    assert r.mem_recommended >= 1024 * MiB * 1.25
    assert any("OOM" in x for x in r.reasons)


def test_memory_pressure_blocks_shrinking():
    r = rec(mem_psi=series(10, 0.02))
    assert r.decision == "ok" and r.mem_recommended >= 1024 * MiB


def test_unlimited_container_gets_a_limit():
    r = rec(mem_limit=None)
    assert r.decision == "set-limit" and r.mem_recommended > 100 * MiB


def test_insufficient_history():
    assert rec(mem_peaks=series(0.2, 100 * MiB)).decision == "insufficient-data"


def test_cpu_cap_raised_only_when_throttled_and_stalling():
    assert rec(throttled=series(10, 0.3)).cpu_recommended is None            # throttled, but nobody waits
    r = rec(cpu=series(10, 0.95), throttled=series(10, 0.3), cpu_psi=series(10, 0.2))
    assert r.decision == "grow" and r.cpu_recommended >= 1.5


def test_compose_override_lists_only_actionable_services():
    shrink = rec()
    ok = rec(mem_peaks=series(10, 700 * MiB))
    text = compose_override([shrink, ok])
    assert text.startswith("# Generated") and "services:" in text
    assert text.count("mem_limit:") == 1 and "was 1024m; shrink" in text
    assert compose_override([ok]) == ""
