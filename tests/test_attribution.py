import math
import random

import pytest

from servermon.attribution import align, fit


def synthetic(n=2016, baseline=0.05, costs=None, noise=0.01, seed=1):
    """A week of 5-minute samples with daily seasonality and bursty crawlers."""
    rng = random.Random(seed)
    costs = costs or {"human": 0.020, "ai_bot": 0.004}
    xs = {"human": [], "ai_bot": [], "scanner": []}
    y = []
    for i in range(n):
        day = math.sin(2 * math.pi * i / 288)
        xs["human"].append(max(0.0, 0.3 + 0.25 * day + rng.gauss(0, 0.05)))
        burst = 4.0 if (i // 36) % 5 == 0 else 0.5                  # crawler waves
        xs["ai_bot"].append(max(0.0, burst + rng.gauss(0, 0.3)))
        xs["scanner"].append(max(0.0, rng.expovariate(20)))          # cheap noise traffic
        cpu = baseline + sum(costs.get(c, 0.0) * xs[c][i] for c in xs) + rng.gauss(0, noise)
        y.append(max(0.0, cpu))
    return y, xs


def true_shares(y, xs, baseline, costs):
    contrib = {"baseline": baseline, **{c: costs.get(c, 0.0) * sum(v) / len(v) for c, v in xs.items()}}
    tot = sum(contrib.values())
    return {k: v / tot for k, v in contrib.items()}


def test_recovers_known_costs_and_shares():
    costs = {"human": 0.020, "ai_bot": 0.004}
    y, xs = synthetic(costs=costs)
    a = fit("p", y, xs, bootstrap=100)
    truth = true_shares(y, xs, 0.05, costs)
    assert a.baseline_cores == pytest.approx(0.05, abs=0.005)
    assert a.cost_per_request["human"] == pytest.approx(0.020, rel=0.1)
    assert a.cost_per_request["ai_bot"] == pytest.approx(0.004, rel=0.1)
    for k in ("baseline", "human", "ai_bot"):
        assert a.share[k] == pytest.approx(truth[k], abs=0.03)
        lo, hi = a.share_ci[k]
        assert lo - 0.02 <= truth[k] <= hi + 0.02           # interval covers the truth
    assert a.r2_holdout > 0.2 and abs(a.r2 - a.r2_holdout) < 0.1   # generalises, no overfitting
    assert a.confidence == "ok"


def test_r2_reflects_signal_to_noise():
    noisy = fit("p", *synthetic(noise=0.01), bootstrap=5)
    clean = fit("p", *synthetic(noise=0.001), bootstrap=5)
    assert clean.r2 > 0.95 and clean.r2_holdout > 0.95 and noisy.r2 < clean.r2


def test_regression_disagrees_with_naive_request_share():
    # crawlers send most requests but each is cheap; the baseline dominates the CPU
    y, xs = synthetic(baseline=0.10, costs={"human": 0.02, "ai_bot": 0.0005})
    a = fit("p", y, xs, bootstrap=50)
    assert a.naive_share["ai_bot"] > 0.7
    assert a.share["ai_bot"] < 0.1
    assert a.share["baseline"] > 0.6


def test_pure_background_load_is_all_baseline():
    rng = random.Random(3)
    n = 600
    xs = {"human": [abs(rng.gauss(0.2, 0.1)) for _ in range(n)], "ai_bot": [abs(rng.gauss(2, 1)) for _ in range(n)]}
    y = [0.11 + rng.gauss(0, 0.01) for _ in range(n)]
    a = fit("p", y, xs, bootstrap=100)
    assert a.share["baseline"] > 0.95
    assert not a.significant("ai_bot")
    assert a.confidence == "ok"                    # R^2 ~ 0 is the right answer here, not a failure


def test_flat_class_is_folded_into_baseline():
    y, xs = synthetic(n=600)
    xs["probe"] = [1 / 60] * len(y)               # constant health check: collinear with the intercept
    a = fit("p", y, xs, bootstrap=20)
    assert "probe" in a.folded
    assert "probe" not in a.share


def test_zero_traffic_class_is_ignored_and_naive_share_handles_it():
    y, xs = synthetic(n=400)
    xs["bot"] = [0.0] * len(y)
    a = fit("p", y, xs, bootstrap=10)
    assert "bot" not in a.share and a.naive_share["bot"] == 0.0


def test_short_history_is_flagged():
    y, xs = synthetic(n=60)
    assert fit("p", y, xs, bootstrap=10).confidence == "insufficient-data"


def test_bootstrap_is_deterministic_with_seed():
    y, xs = synthetic(n=500)
    assert fit("p", y, xs, bootstrap=30, seed=7).share_ci == fit("p", y, xs, bootstrap=30, seed=7).share_ci


def test_align_fills_missing_rates_with_zero():
    y = {0.0: 1.0, 300.0: 2.0, 600.0: 3.0}
    yv, xv = align(y, {"human": {300.0: 0.5}})
    assert yv == [1.0, 2.0, 3.0] and xv["human"] == [0.0, 0.5, 0.0]


def test_too_few_samples_raises():
    with pytest.raises(ValueError):
        fit("p", [1.0, 2.0], {"human": [0.1, 0.2]})


def test_to_dict_is_json_serialisable():
    import json
    y, xs = synthetic(n=300)
    json.dumps(fit("p", y, xs, bootstrap=5).to_dict())


def test_steady_traffic_is_flagged_as_confounded():
    """A rate-limited crawler (almost constant rate) whose cost is real but unidentifiable at 5 min."""
    rng = random.Random(9)
    n = 576
    xs = {"ai_bot": [0.39 + rng.gauss(0, 0.045) for _ in range(n)], "human": [abs(rng.gauss(0.002, 0.002)) for _ in range(n)]}
    # true cost 270 ms/request, but per-5-min CPU is dominated by noise from other jobs
    y = [0.27 * xs["ai_bot"][i] * (1 + rng.gauss(0, 0.3)) + (0.04 if i % 144 < 6 else 0.0) + 0.004 for i in range(n)]
    a = fit("p", y, xs, bootstrap=50)
    assert a.max_hidden_share["ai_bot"] > 0.25
    if a.share["ai_bot"] < 0.1:
        assert a.confidence == "confounded"


def test_varying_traffic_is_identifiable():
    y, xs = synthetic(n=2016)
    a = fit("p", y, xs, bootstrap=20)
    assert a.max_hidden_share["human"] < 0.6 and a.confidence == "ok"
