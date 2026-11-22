# Right-sizing: method

Code: `src/servermon/rightsizing.py`; inputs are queried from Prometheus by `src/servermon/analyze.py`
(7-day lookback, 5-minute steps). Output: `projctl rightsize [project] [--write DIR]`, the
`sm_rightsize_*` metrics and the **Insights** dashboard.

## Why memory and CPU are treated differently

- **Memory is incompressible.** If a container exceeds its limit it is OOM-killed. On a host
  without swap, the sum of the limits decides whether the limits protect anything: when
  Σ limits > RAM, the kernel's global OOM killer can still pick a victim from *any* container.
  The analyzer exports this as `sm_memory_limit_overcommit_ratio` (current vs. recommended).
- **CPU is compressible.** A `cpus:` cap that is too high costs nothing; one that is too low
  throttles the container (CFS) and adds latency. So the recommender never lowers CPU caps. It
  raises them only when there is evidence of harm.

## Memory recommendation

Inputs per running container: 5-minute maxima of the working set
(`max_over_time(container_memory_working_set_bytes[5m])`), memory PSI
(`container_pressure_memory_waiting_seconds_total`), OOM events (`container_oom_events_total`),
and the current limit.

1. **Target.** Exponentially decayed weighted quantile (q = 0.98) of the 5-minute peaks, with
   weight `2^(-age / 24 h)` as in VPA. Recent behaviour dominates; a spike from nine days ago has
   about 0.2 % of the weight of one from today.
2. **Floor.** The largest peak of the last 24 hours. Recent behaviour always wins.
3. **Margin.** +15 %, and at least +32 MiB. Rounded up to 16 MiB.
4. **History confidence.** With `d` < 7 days of history the upper bound is `rec · (1 + 1/d)`.
   A shrink is suggested only when this upper bound is still < 70 % of the current limit, and then
   the upper bound is what is recommended. Between 70 % and 100 % the decision is `ok` and the
   limit is kept.
5. **OOM bump.** After an OOM event in the window: at least 1.25 × the current limit (decision
   `grow`). This matters because 5-minute peaks miss short spikes. On the reference host the
   recommender caught an app that was OOM-killed at 945 MB under a 1 GB limit while its 5-minute
   peaks never exceeded 534 MB. A peak-only (KRR-style) recommender would have called it `ok`.
6. **PSI guard.** If the container stalls on memory ≥ 0.5 % of the time, it is reclaiming under
   pressure already: never recommend below the current limit.
7. **No limit.** Decision `set-limit`, with the recommendation as a starting point.

## CPU recommendation

Report the decayed p95 of usage. Recommend `cpus = max(1.5 × cap, 1.3 × p95)` only when the
container is throttled in ≥ 10 % of CFS periods **and** its CPU PSI is ≥ 5 % (tasks actually wait).
Throttling without waiting is harmless: on the reference host node-exporter is throttled in
39 % of periods with 0.2 % stall, and nothing is recommended for it.

## Output

```
projctl rightsize vector-search --write /tmp/rs
CONTAINER                 DECISION   LIMIT PEAK24h   P98 RECOMMEND  WHY
vector-search-app-1       shrink     3072M    65M    65M     160M  98th-pct peak 65 MiB, 24 h peak 65 MiB vs limit 3072 MiB …
vector-search-llm-1       shrink     2560M   774M   773M    1408M  …

# /tmp/rs/vector-search.rightsizing.yml
services:
  app:
    mem_limit: 160m    # was 3072m; shrink
```

Apply after review with `docker compose -f docker-compose.yml -f vector-search.rightsizing.yml up -d`.
Nothing is ever applied automatically.

## Differences from VPA / KRR

| | VPA | KRR | this |
|---|---|---|---|
| platform | Kubernetes | Kubernetes | Docker Compose |
| memory statistic | decayed histogram of peaks | max + buffer | decayed q98 of 5-min peaks, 24 h floor |
| OOM handling | bump | none | bump ×1.25 of the limit |
| pressure (PSI) | none | none | memory-stall guard, CPU stall + throttling for CPU |
| short history | widened bounds | none | widened upper bound gates shrinking |
| host view | none | none | Σ limits / RAM overcommit ratio |
| output | applied (modes) | report | report + compose override file |
