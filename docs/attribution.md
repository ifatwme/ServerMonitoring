# CPU attribution: method

Code: `src/servermon/attribution.py` (5-minute model), `src/servermon/burst.py` (1 Hz model),
`src/servermon/nnls.py` (solver). Runs from `server-monitor-analyze.timer` (every 15 min) and
`server-monitor-burst.timer` (a 2-minute burst roughly every 15 min).

## 1. Traffic classes

The collector parses each project's nginx access log (`docker logs --timestamps`) and classifies
every request (`src/servermon/traffic.py`):

| class | rule (first match wins) |
|---|---|
| `probe` | this stack's blackbox probe, `/healthz` |
| `ai_bot` | GPTBot, ChatGPT-User, OAI-SearchBot, ClaudeBot, PerplexityBot, Bytespider, meta-externalagent, Amazonbot, CCBot, Applebot, … |
| `scanner` | exploit paths (`/.env`, `/wp-*`, `*.php`, `/cgi-bin`, …), odd methods, malformed request lines |
| `bot` | any other automated client (search engines, `curl`, libraries, empty user agent) |
| `human` | everything else |

`engaged` (subset of human): the IP fetched a page and an asset within 10 minutes.

## 2. The 5-minute model

For each project, with `y(t)` the cores used by its containers (cAdvisor, recording rule
`project:cpu_cores:rate2m`, averaged per 5 minutes) and `x_c(t)` the request rate of class `c`
(`rate(sm_http_requests_total[5m])`):

```
y(t) = b0 + Σ_c β_c · x_c(t) + ε(t),        b0 ≥ 0,  β_c ≥ 0
```

- **Solver.** Lawson–Hanson active-set NNLS on the normal equations. Columns are scaled to
  `[0, 1]`. Collinear columns are excluded permanently, so the solver cannot loop.
  It is tested against brute-force enumeration of all support sets and against the KKT conditions.
- **Folding.** A class whose rate barely varies (coefficient of variation < 0.05, e.g. a
  once-a-minute health check) is collinear with the intercept. It is folded into the baseline and
  listed in `folded`.
- **Shares.** Component contributions are `b0` and `β_c · mean(x_c)`. A share is a contribution
  divided by their sum (the mean fitted CPU).
- **Uncertainty.** Moving-block bootstrap (blocks of 12 samples = 1 h, 200 resamples). The per-block
  normal equations are precomputed, so each resample is a sum of small matrices plus one NNLS solve.
- **Quality.** In-sample R², and R² on a chronological 30 % hold-out scored against the
  training mean. Note that **R² ≈ 0 is the correct result when the CPU is all baseline**: there is
  nothing left to explain. Low R² alone does not mean low confidence.
- **Identifiability.** For each class, `max_hidden_share = (β_c + 2·SE_c) · mean(x_c) / mean(ŷ)`.
  Here `SE_c = σ_resid / (sd(x_c) · √(n/block))` is an OLS-style standard error with an effective
  sample size corrected for autocorrelation. If a class has a share < 10 % but could be hiding
  > 25 %, the fit is labelled `confounded`.

## 3. Why a second model: the steady-crawler case

On a production site, the 5-minute model reported Papersaurus's CPU as 100 % baseline (CI 89–100 %).
A profiler (py-spy) and per-thread CPU accounting (`/proc/<pid>/task/*/stat`) said otherwise: the
CPU was in the request worker threads rendering `/paper/…` pages, and the requests came from
crawlers. The reason: nginx rate limits and crawl-delay keep the crawler at 0.39 req/s with a
coefficient of variation of only 0.12 at 5-minute resolution. CPU and crawler rate had a
correlation of −0.002. **The cost exists, but 5-minute data cannot identify it**: a constant
crawler and a constant background loop produce the same series. The identifiability diagnostic
now flags this case (crawlers could be hiding up to 67 % of the CPU).

## 4. The burst model (1 Hz)

At 1-second resolution the same traffic is a point process: most seconds have no request, some
have one. An idle second used 0.004 cores; a second with a request about 0.27 cores. That
contrast identifies the cost.

- **Sampling.** A systemd timer fires about every 15 minutes with `RandomizedDelaySec`, so the
  phase is random and cannot lock onto cron-like jobs (aliasing). It reads
  `/sys/fs/cgroup/system.slice/docker-<id>.scope/cpu.stat` (`usage_usec`) for every project
  container once per second for 120 s (≈ 0.6 s of CPU per burst for the sampler). The project's
  access log for the same window gives arrivals per second per class.
- **Distributed-lag regression.** nginx logs a request when it finishes, so its CPU precedes the log
  line, sometimes by several seconds (thread pools, slow pages):
  ```
  y(s) = b0 + Σ_c Σ_{l ∈ window} β_{c,l} · x_c(s + l),      all coefficients ≥ 0,   cost_c = Σ_l β_{c,l}
  ```
  Each burst stores the normal equations for the widest window (−5…+2 s), for the classes present
  in that burst only (≈ 1 KB per burst). Narrower windows are column subsets, so the window
  (−1…1, −2…1, −3…1, −4…2, −5…2) is chosen per project by **5-fold cross-validation over
  contiguous groups of bursts**.
- **Hybrid baseline.** With near-periodic arrivals (rate limiting), a long CPU tail and a baseline
  are hard to tell apart even at 1 Hz: cross-validation error was flat across windows on the real
  site. The baseline therefore comes from a model-free signal when one exists: the **mean CPU of
  seconds farther from any request (any class, incl. probes) than the chosen window**, which the
  request kernel cannot reach. The request-driven remainder is split between classes by the
  regression's contributions. With fewer than 30 such seconds (dense traffic), the pure
  regression is used (`baseline_source` says which).
- **Uncertainty.** Bootstrap over whole bursts (natural blocks), recomputing both the regression
  and the quiet-second mean.
- **State.** The last 300 bursts (~3 days) in `$SM_STATE_DIR/bursts.json`.

Design history (see [evaluation.md](evaluation.md)): a fixed ±1 s window underestimated the real
site (70 %). A median-of-quiet-seconds baseline with a fixed guard matched the real site, but was
biased by 16 points on skewed backgrounds and failed completely on long tails in the semi-synthetic
benchmark. The shipped combination (mean of quiet seconds, guard = CV-chosen window, fallback)
has 3.6 % mean error there and covers the truth in 9 of 10 cases.

## 5. Reading the output

```
projctl insights cs-paper-radar
  burst model, 1 Hz: … baseline from quiet seconds, confidence ok
    cause      CPU share     90% CI by requests  cost/request
    baseline       17.7%     3%-31%           -             -
    ai_bot         82.3%    69%-97%      100.0%        294 ms
  5-minute model: … confidence confounded
    ! ai_bot traffic is too steady for 5-minute data: it could be up to 67% of the CPU
```

- `CPU share` is regression attribution. `by requests` is what request-share allocation would say.
- `cost/request` is shown only when the share's interval excludes zero.
- Grafana: **Insights** dashboard, with a model selector (`burst` / `5m`).

Validation: [evaluation.md](evaluation.md).
