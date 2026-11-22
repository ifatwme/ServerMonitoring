"""Regression-based CPU attribution: who is this project's CPU actually spent on?

Request-share allocation ("AI crawlers sent 90 % of the requests, so they cost 90 % of the CPU") is
what most crawler-cost guides do. It is wrong in both directions on small hosts: a project with a
background loop burns CPU with zero traffic, and a cheap cached page and an expensive search do not
cost the same. Following the regression approach of R-Capriccio (Zhang, Cherkasova, Smirni,
Middleware 2007), which estimates per-transaction CPU demand from utilisation and transaction
counts, we fit per project and per time step t (5 min):

    cpu(t) = b0 + sum_c beta_c * rate_c(t) + e(t)        b0 >= 0, beta_c >= 0

  cpu(t)     cores used by the project's containers (cAdvisor)
  rate_c(t)  requests/s of traffic class c (human, ai_bot, bot, scanner) from the access log
  b0         baseline: cores burned regardless of traffic (workers, demo loops, idle models)
  beta_c     marginal cost of one request of class c, in core-seconds

The non-negativity constraint (NNLS) encodes physics (a request cannot give CPU back) and keeps
the model identifiable with collinear classes. Each component's share is its mean contribution
divided by the mean fitted CPU. Uncertainty comes from a moving-block bootstrap (blocks keep the
autocorrelation of the series); quality is reported in-sample and on a chronological hold-out.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field

from .nnls import nnls_normal

TRAFFIC_CLASSES = ("human", "ai_bot", "bot", "scanner")


@dataclass
class Attribution:
    project: str
    samples: int
    mean_cpu: float                                   # observed mean cores
    baseline_cores: float                             # b0
    cost_per_request: dict[str, float]                # beta_c, core-seconds per request
    mean_rate: dict[str, float]                       # requests/s
    share: dict[str, float]                           # component -> share of fitted CPU (incl. "baseline")
    share_ci: dict[str, tuple[float, float]]          # 90 % bootstrap interval
    naive_share: dict[str, float]                     # request-share allocation, for comparison
    r2: float
    r2_holdout: float | None
    folded: list[str] = field(default_factory=list)   # classes too flat to separate from the baseline
    confidence: str = "ok"
    # identifiability: the largest share each class could have and still be consistent with the
    # data (estimate + 2 standard errors, autocorrelation-corrected). Large values next to a ~0
    # estimate mean "steady traffic, can't separate it from the baseline" -> use burst sampling.
    max_hidden_share: dict[str, float] = field(default_factory=dict)

    def significant(self, component: str, floor: float = 0.005) -> bool:
        """True when the bootstrap interval of the component's share excludes ~0."""
        return self.share_ci.get(component, (0.0, 0.0))[0] > floor

    def to_dict(self) -> dict:
        d = asdict(self)
        d["share_ci"] = {k: list(v) for k, v in self.share_ci.items()}
        return d


def align(y: dict[float, float], xs: dict[str, dict[float, float]]) -> tuple[list[float], dict[str, list[float]]]:
    """Align series on y's timestamps; a missing request rate means no requests (0)."""
    ts = sorted(y)
    return [y[t] for t in ts], {c: [s.get(t, 0.0) for t in ts] for c, s in xs.items()}


def _mean(v):
    return sum(v) / len(v) if v else 0.0


def _std(v):
    m = _mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) if v else 0.0


def _design(xs: dict[str, list[float]], classes: list[str], scale: dict[str, float]) -> list[list[float]]:
    n = len(next(iter(xs.values()))) if xs else 0
    return [[1.0] + [xs[c][i] / scale[c] for c in classes] for i in range(n)]


def _normal(rows, y, idx):
    k = len(rows[0])
    AtA = [[0.0] * k for _ in range(k)]
    Atb = [0.0] * k
    for i in idx:
        r = rows[i]
        for a in range(k):
            ra = r[a]
            if ra:
                Atb[a] += ra * y[i]
                row = AtA[a]
                for b in range(k):
                    row[b] += ra * r[b]
    return AtA, Atb


def _shares(coef, classes, xs_mean, scale):
    contrib = {"baseline": coef[0]}
    for j, c in enumerate(classes, 1):
        contrib[c] = coef[j] / scale[c] * xs_mean[c]
    tot = sum(contrib.values())
    return {k: (v / tot if tot > 0 else 0.0) for k, v in contrib.items()}


def _r2(y, yhat, ref_mean=None):
    m = _mean(y) if ref_mean is None else ref_mean
    sst = sum((v - m) ** 2 for v in y)
    sse = sum((a - b) ** 2 for a, b in zip(y, yhat))
    return 1 - sse / sst if sst > 0 else 0.0


def fit(project: str, y: list[float], xs: dict[str, list[float]], *, holdout: float = 0.3,
        bootstrap: int = 200, block: int = 12, min_cv: float = 0.05, min_samples: int = 144,
        seed: int = 0) -> Attribution:
    """Fit the attribution model. `y` and every `xs[c]` must be aligned (see align())."""
    n = len(y)
    if n < 3:
        raise ValueError("need at least 3 aligned samples")
    xs_mean = {c: _mean(v) for c, v in xs.items()}
    classes, folded = [], []
    for c, v in xs.items():
        if xs_mean[c] <= 0:
            continue
        (classes if _std(v) / xs_mean[c] >= min_cv else folded).append(c)
    scale = {c: (max(xs[c]) or 1.0) for c in classes}
    rows = _design(xs, classes, scale) if classes else [[1.0] for _ in range(n)]

    coef = nnls_normal(*_normal(rows, y, range(n)))
    yhat = [sum(a * b for a, b in zip(r, coef)) for r in rows]
    share = _shares(coef, classes, xs_mean, scale)

    # chronological hold-out: train on the first part, score on the rest against the train mean
    r2_ho = None
    cut = int(n * (1 - holdout))
    if holdout and cut >= 3 and n - cut >= 3:
        c_tr = nnls_normal(*_normal(rows, y, range(cut)))
        pred = [sum(a * b for a, b in zip(rows[i], c_tr)) for i in range(cut, n)]
        r2_ho = _r2(y[cut:], pred, ref_mean=_mean(y[:cut]))

    # moving-block bootstrap of the shares
    rng = random.Random(seed)
    blocks = [list(range(s, min(s + block, n))) for s in range(0, n, block)]
    pre = [_normal(rows, y, b) for b in blocks]
    k = len(rows[0])
    draws: dict[str, list[float]] = {key: [] for key in share}
    for _ in range(bootstrap):
        AtA = [[0.0] * k for _ in range(k)]
        Atb = [0.0] * k
        for _b in range(len(blocks)):
            bA, bb = pre[rng.randrange(len(blocks))]
            for a in range(k):
                Atb[a] += bb[a]
                for b in range(k):
                    AtA[a][b] += bA[a][b]
        cb = nnls_normal(AtA, Atb)
        for key, v in _shares(cb, classes, xs_mean, scale).items():
            draws[key].append(v)
    ci = {}
    for key, vals in draws.items():
        vals.sort()
        ci[key] = (vals[int(0.05 * (len(vals) - 1))], vals[int(0.95 * (len(vals) - 1))]) if vals else (0.0, 0.0)

    # identifiability (see Attribution.max_hidden_share): OLS-style standard error of each cost with
    # an effective sample size of n / block to account for autocorrelation
    resid = [a - b for a, b in zip(y, yhat)]
    sigma = math.sqrt(sum(r * r for r in resid) / max(1, n - len(rows[0])))
    n_eff = max(1.0, n / block)
    hidden = {}
    for j, c in enumerate(classes, 1):
        sd = _std(xs[c])
        se = sigma / (sd * math.sqrt(n_eff)) if sd > 0 else float("inf")
        upper = (coef[j] / scale[c] + 2 * se) * xs_mean[c]
        hidden[c] = min(1.0, upper / max(_mean(yhat), 1e-12))
    for c in folded:
        hidden[c] = 1.0

    total_rate = sum(xs_mean.values())
    naive = {c: (xs_mean[c] / total_rate if total_rate > 0 else 0.0) for c in xs}
    r2 = _r2(y, yhat)
    confidence = "ok"
    if n < min_samples:
        confidence = "insufficient-data"
    elif any(hidden[c] > 0.25 and share.get(c, 0.0) < 0.1 for c in hidden):
        confidence = "confounded"          # steady traffic may be hiding inside the baseline
    elif (r2_ho is not None and r2_ho < -0.25) or max(hi - lo for lo, hi in ci.values()) > 0.5:
        # R^2 near 0 is expected when the CPU is almost all baseline (nothing left to explain);
        # only a clearly negative hold-out score or wide bootstrap intervals mean "don't trust it".
        confidence = "low"
    return Attribution(
        project=project, samples=n, mean_cpu=_mean(y), baseline_cores=coef[0],
        cost_per_request={c: coef[j] / scale[c] for j, c in enumerate(classes, 1)},
        mean_rate=xs_mean, share=share, share_ci=ci, naive_share=naive,
        r2=r2, r2_holdout=r2_ho, folded=folded, confidence=confidence, max_hidden_share=hidden)
