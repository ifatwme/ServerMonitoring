# Evaluation

All numbers below were produced by the scripts in `experiments/` on the reference host
(4 vCPU, 8 GB RAM, no swap, 9 projects) on 2026-11-22. Commands to reproduce are at the end.
Two kinds of evidence are used:

- **Semi-synthetic ground truth.** Real CPU series of a background-bound project (real noise,
  real spikes) plus a **known** CPU cost per request, applied to real request arrivals of a crawled
  site. The truth is known by construction; the estimators only see the sum.
- **Independent measurement on production.** A profiler and per-thread CPU accounting of the
  application process, which do not use any of the estimators.

## 1. 5-minute model vs. request-share allocation

`experiments/semi_synthetic.py`: 36 cost scenarios (AI crawler 0.5–100 ms/request, human 0–50 ms,
other bots 0–10 ms), fit on 2 days of 5-minute data. Error = |estimated share − injected share|,
averaged over components (baseline + classes).

### Semi-synthetic attribution benchmark (real CPU of a background-bound project + real traffic of a crawled site, 512 x 5 min)

36 cost scenarios; error = |estimated share - injected share|, averaged over components

| method | mean abs. error | p90 abs. error | AI-crawler share error (mean) |
|---|---|---|---|
| request-share allocation | 45.8% | 49.9% | 91.6% |
| idle floor + request share | 5.5% | 7.6% | 11.1% |
| NNLS regression (this) | 0.7% | 0.8% | 1.4% |

90 % bootstrap interval covers the truth (+-1 pt) for 100% of components; median hold-out R2 0.01

| ai_bot ms/req | human ms/req | true AI-crawler share | NNLS estimate (90 % CI) | idle floor + req. share | request share |
|---|---|---|---|---|---|
| 0.5 | 10 | 0.2% | 1.7% (0.0%-6.6%) | 15.4% | 100.0% |
| 2.0 | 10 | 0.7% | 2.2% (0.0%-7.1%) | 15.3% | 100.0% |
| 5.0 | 10 | 1.7% | 3.2% (0.0%-8.0%) | 15.2% | 100.0% |
| 20.0 | 10 | 6.5% | 8.0% (4.7%-12.5%) | 14.5% | 100.0% |
| 50.0 | 10 | 14.9% | 16.2% (13.2%-20.3%) | 13.2% | 100.0% |
| 100.0 | 10 | 25.9% | 27.1% (24.5%-30.6%) | 12.5% | 100.0% |

Same benchmark with a different background project (vector-search CPU, crawled-site traffic):

| method | mean abs. error | p90 abs. error | AI-crawler share error (mean) |
|---|---|---|---|
| request-share allocation | 40.2% | 49.7% | 80.4% |
| idle floor + request share | 8.8% | 22.3% | 17.7% |
| NNLS regression (this) | 0.6% | 0.9% | 1.2% |


Fully synthetic (CI runs this on every push):

| method | mean abs. error | p90 abs. error | AI-crawler share error (mean) |
|---|---|---|---|
| request-share allocation | 44.8% | 49.7% | 88.4% |
| idle floor + request share | 5.5% | 8.8% | 10.8% |
| NNLS regression (this) | 1.7% | 1.9% | 0.7% |


**Reading.** Request-share allocation has no notion of background load and is off by ~45 points.
The stronger heuristic (5th-percentile CPU as an idle floor, the rest split by requests) is better,
but its estimate stays near 15 % whatever the true cost. It cannot tell a 1 ms page from a 100 ms
page. The regression tracks the truth within about one point, and its intervals cover the truth.
**Detection limit:** at this site's ~0.4 crawler req/s, the crawler share only becomes significant
(interval excludes 0) at about 50 ms of CPU per request (~15 % share) in the 5-minute model.

## 2. Case study: when the 5-minute model is confidently wrong

On the real site behind the traffic above (Papersaurus, an arXiv paper browser), the 5-minute
model reported **100 % baseline** (interval 89–100 %), and the crawler classes looked free.

Independent checks said otherwise:

- `py-spy` on the app process: the CPU is in the request handler for `/paper/<id>` pages
  (`render → db.stats → date_range` on every page).
- Per-thread accounting (`/proc/<pid>/task/*/stat`, 10 s): about 0.03 cores in each of the three
  request worker threads and 0.006 in the main thread, so **≥ 95 %** of the process's CPU is in
  request threads.
- 1-second resolution for 90 s: CPU 0.004 cores in seconds without a request, 0.27 cores in
  seconds with one.

Cause: nginx `limit_req` and `Crawl-delay` keep the crawlers at 0.39 req/s with a coefficient of
variation of only 0.12 at 5-minute resolution. CPU and crawler rate had a correlation of −0.002.
A constant crawler and a constant background loop produce the same 5-minute series, so the cost is
**not identifiable** from that data. Two changes followed:

1. The 5-minute model now reports `max_hidden_share` per class and labels such fits `confounded`.
   For this site it says the crawlers could be up to **67 %** of the CPU. The other projects (a demo
   loop, an idle model server, a static mirror) stay `ok`, with ≤ 4 % possibly hidden.
2. The 1 Hz burst model (below).

## 3. Burst model (1 Hz)

`experiments/burst_validation.py` recorded a 900-second trace (`experiments/data/trace-2026-11-22.json`):
the crawled site's container CPU, a background-bound project's container CPU, and per-second
request arrivals (counts only; no IPs, paths or user agents).

#### A. Real traffic, real CPU (steady, rate-limited crawler)

Mean CPU 0.146 cores at 0.40 req/s. Independent reference: per-thread CPU accounting of the app process put >= 95 % of its CPU in the request worker threads (measured separately).

| method | request-driven share | 90 % CI | per-request CPU |
|---|---|---|---|
| burst hybrid (quiet-second baseline, 101 quiet s) | 82.3% | 69%-97% | 294 ms |
| burst regression only (lags -1..1 s, chosen by CV) | 69.7% | - | - |
| regression on 60 s bins | 0.0% | - | confidence: confounded |
| request-share allocation | 100 % (ai_bot) | - | - |

#### B. Semi-synthetic: real background CPU + known cost per request on the real arrival process

CPU kernel: short (60/40 over 2 s)

| injected ms/request | true share | burst hybrid (90 % CI) | burst regression | 60 s bins | request share |
|---|---|---|---|---|---|
| 5 | 1.4% | 0.0% (0%-11%) | 0.9% | 0.0% | 100 % |
| 20 | 5.3% | 2.1% (0%-15%) | 3.6% | 0.0% | 100 % |
| 50 | 12.3% | 9.4% (0%-21%) | 10.9% | 0.0% | 100 % |
| 100 | 22.0% | 19.4% (9%-30%) | 20.8% | 0.0% | 100 % |
| 270 | 43.2% | 41.4% (34%-49%) | 42.6% | 0.0% | 100 % |

CPU kernel: long tail (4 s before the log line)

| injected ms/request | true share | burst hybrid (90 % CI) | burst regression | 60 s bins | request share |
|---|---|---|---|---|---|
| 5 | 1.4% | 0.0% (0%-10%) | 0.3% | 0.0% | 100 % |
| 20 | 5.3% | 0.0% (0%-9%) | 0.7% | 0.0% | 100 % |
| 50 | 12.3% | 0.0% (0%-7%) | 1.4% | 0.0% | 100 % |
| 100 | 22.0% | 19.6% (12%-33%) | 19.6% | 0.0% | 100 % |
| 270 | 43.2% | 40.9% (35%-49%) | 40.9% | 0.0% | 100 % |

Mean absolute error of the request-driven share over both kernels: burst hybrid 3.6%, burst regression 2.7%, 60 s bins 16.8%, request share 83.2%

**Reading.**

- On real data, the 60-second regression shows the same identifiability failure as the 5-minute
  model (0 %, `confounded`). At 1 Hz, both burst estimators see the crawler cost.
- The hybrid estimator's interval (69–97 %) spans the pure regression (70 %) and the per-thread
  measurement (≥ 95 %). The site's request-driven share is high; the exact value is uncertain, and
  the interval says so. The per-thread number covers only the server process, while the cgroup also
  pays for the container's health-check processes, so ~82 % is plausible.
- In the semi-synthetic runs both burst estimators track the injected truth (mean error 2.7 % and
  3.6 %), including a long CPU tail spread over 4 s. For the tail, cross-validation picks a wider
  lag window. Very cheap requests with long tails (≤ 50 ms spread over 4 s) are below the detection
  limit of a 15-minute trace. Production keeps ~3 days of bursts.
- **A negative result worth recording:** the first hybrid used the *median* of quiet seconds and a
  fixed ±1 s guard. It matched the real case but was badly biased on semi-synthetic data: +16
  points with a skewed background (median < mean), and 0 % with long tails (tails leak into
  "quiet" seconds). The shipped version uses the mean of quiet seconds and a guard equal to the
  cross-validated lag window, and falls back to the regression when there are fewer than 30 such
  seconds.

## 4. Right-sizing on the reference host

From `projctl rightsize` after 1.8 days of history:

- **Overcommit.** The memory limits added up to **2.47× the host RAM**; the recommendations bring
  that to 1.52×.
- **Caught OOM.** An app was OOM-killed at 945 MB under a 1 GB limit, while its 5-minute
  working-set peaks never exceeded 534 MB. Decision `grow` (1.25 × limit). A peak-only recommender
  would have said `ok`.
- **Caught OOM (2).** Grafana (5 OOM events while rendering under 640 MB): `grow`.
- **Overprovisioned.** For example a Streamlit app with a 3 GB limit and a 65 MB peak, and a vector
  DB with 1.5 GB for a 76 MB peak. With < 7 days of history the recommendation is the widened
  upper bound (160 MB, 176 MB), not the raw peak.
- **Not touched.** node-exporter is throttled in 39 % of CFS periods but stalls only 0.2 % of the
  time: no CPU change recommended.

These are recommendations; nothing was applied automatically.

## 5. Tests

`python -m pytest`: 137 tests in ~3 s, plus `promtool test rules` and a Compose smoke test in CI.

| area | what is checked |
|---|---|
| NNLS | equals unconstrained LS when feasible; clamps negative coefficients; matches brute-force support enumeration on 40 random problems; KKT conditions on 20 more; collinear columns terminate |
| 5-min attribution | recovers costs/shares on synthetic seasonal + bursty data; intervals cover the truth; regression vs. request share; pure background → baseline; flat class folded; steady traffic → `confounded`; hold-out R² tracks signal-to-noise |
| burst attribution | recovers 270 ms/request from a Bernoulli arrival process; background not attributed; long CPU tail → wider CV-chosen lag window and correct cost; dense traffic falls back to regression; cgroup reader; fake-clock sampler |
| right-sizing | decayed quantile; shrink with margin; short-history widening; recent spike floor; old spike decays; OOM bump; PSI guard; unlimited → set-limit; CPU raised only when throttled **and** stalled; compose override output |
| traffic | 13 classification cases; engaged visitor rule; backfill does not inflate counters; cursor skips seen lines; pruning; exposition format |
| analyzer | end-to-end against a fake Prometheus HTTP server; unreachable Prometheus |
| dashboards | valid panels and PromQL parentheses; no overlapping panels; committed JSON equals the generator |
| Prometheus rules | `SiteDown` waits 3 min and is muted for parked projects; per-project CPU recording rule incl. the `other` bucket; `ProjectIdle` after 1 h |

## Reproduce

```bash
python3 -m pytest -q
python3 experiments/semi_synthetic.py --synthetic
python3 experiments/semi_synthetic.py --prometheus http://127.0.0.1:9090 \
    --cpu-project <background-bound project> --traffic-project <crawled project> --lookback 2d
python3 experiments/burst_validation.py analyse experiments/data/trace-2026-11-22.json
python3 experiments/burst_validation.py record --target <project> --background <project> \
    --seconds 900 --out my-trace.json        # on your own host
```
