# Research notes: what is new here, and what it builds on

## Problem

A single VPS running several side projects (web apps, local LLMs, batch jobs) has three recurring
questions that standard Prometheus + Grafana setups do not answer:

1. **Who is the CPU actually spent on?** Real visitors, AI crawlers, other bots, scanners, or
   background work nobody asked for? Since 2025, AI crawlers send more requests to many small sites
   than people do. Studies report AI crawlers at 41 % of crawler requests and 107 per Googlebot
   request on one directory site (AppBreakup, Nov 2026), and git.kernel.org burns 14–16 cores on
   them. Whether that traffic is *expensive* on a given site is a different question.
2. **Are the memory limits right?** Docker Compose users set `mem_limit` by guesswork. On a
   swapless host, a too-high limit gives no protection and a too-low one OOM-kills the service.
3. **Which projects are idle and could be stopped?** "Idle" needs evidence (no visitors, commits,
   builds or test runs), not a guess.

## Prior art

| Area | Tools / papers | What they do | Gap for a small Compose host |
|---|---|---|---|
| Crawler cost | WebmasterID, Centinel worksheets, geodocs framework, SSD Nodes guide | allocate compute by **request share** (or cache-miss share) | ignores background load and per-request cost differences; no uncertainty |
| Per-transaction CPU demand | R-Capriccio (Zhang, Cherkasova, Smirni, Middleware 2007); regression-based workload profilers (NOMS 2022) | regress utilisation on transaction counts | built for enterprise apps with many transaction types and steady sampling; not applied to bot classes; no identifiability check |
| Energy / power attribution | Kepler (CNCF), GreenPipe (arXiv 2609.04952) | model-based per-container power from utilisation | needs RAPL or trained models; answers "watts", not "which traffic" |
| Right-sizing | Kubernetes VPA (decaying histograms, OOM bump), Robusta KRR (p95 + buffer), dockpulse (Compose p95 + headroom) | percentile-based recommendations | VPA/KRR are Kubernetes-only; dockpulse has no decay, OOM handling or PSI |
| Pressure signals | Linux PSI (4.20+); Kubernetes 1.36 made per-container PSI GA (Apr 2026) | stall time per resource | used as an alert signal, not yet as a right-sizing guard for Compose |
| Scale to zero | Sablier, DockerWakeUp, Knative | stop idle containers, wake on request | idle = "no HTTP requests"; crawlers keep sites awake; no development-activity signal |
| Anomaly baselines | Grafana "adaptive" PromQL baselines (PromCon 2024) | mean ± k·σ bands | orthogonal; not used here |

## Contributions of this repository

1. **Traffic-class CPU attribution by constrained regression.** Per project:
   `cpu(t) = b0 + Σ_c β_c · rate_c(t)` with `b0, β ≥ 0` (NNLS). This yields a **baseline**
   (CPU independent of traffic) and a **marginal cost per request** for human, AI crawler, bot and
   scanner traffic, with moving-block bootstrap intervals and a chronological hold-out. See
   [attribution.md](attribution.md).
2. **An identifiability diagnostic.** A steady (rate-limited) crawler looks like a constant, so
   regression on minute-level monitoring data cannot separate it from background work. The model
   reports how large a share each class *could be hiding* in the baseline (estimate + 2
   autocorrelation-corrected standard errors) and labels such fits `confounded` instead of
   returning a confident wrong answer. This came out of a production case: profiling contradicted
   the first model.
3. **Burst sampling for steady traffic.** Every ~15 minutes, at a randomised phase, the containers'
   cgroup v2 CPU counters are read at 1 Hz for two minutes and aligned with per-second request
   arrivals. At 1 s resolution the "flat" crawler is a point process with high variance. A
   distributed-lag NNLS regression, with its lag window chosen by cross-validation over bursts,
   recovers the per-request cost. The baseline comes from a model-free signal: the mean CPU of
   seconds the request kernel cannot reach. State is about 1 KB of normal equations per burst, and
   bursts double as bootstrap blocks. The evaluation documents a failed first design (median,
   fixed guard) next to the shipped one.
4. **PSI- and OOM-aware right-sizing for Docker Compose.** VPA's ideas (exponentially decayed
   quantiles, half-life 24 h, OOM bump, confidence widening with short history) ported to Compose.
   Kernel memory-pressure stall (PSI) is used as a guard: a limit is never shrunk while the
   container already stalls on memory. CPU caps are raised only when the container is CFS-throttled
   **and** stalling, never lowered (CPU is compressible). The output is a reviewable compose
   override file. A host-level **limit overcommit ratio** (Σ limits / RAM) shows whether the limits
   can protect the host at all.
5. **Evidence-based idle detection.** A project is a park candidate only when none of: an
   *engaged* visitor (an IP that fetched a page and its assets within 10 minutes, which filters out
   scanners with browser user agents), a commit, an uncommitted edit, an image build, or a container
   start (test runs) happened within its threshold. `projctl park/unpark` stops and restores units,
   timers and containers exactly; parked projects mute their alerts.

Everything runs on the host from systemd with the Python standard library only (no numpy).
The analysis is cheap: NNLS on ≤ 13 columns, 200 bootstrap refits in under a second.

## Limitations

- Attribution is correlational. It is sound when traffic varies independently of other CPU
  consumers; a background job triggered by traffic (e.g. a queue worker) is credited to the traffic
  class, which is usually what you want.
- The burst model assumes a request's CPU falls within ±1 s of its log line. Long requests
  (LLM inference, report generation) need more lags; this is configurable in `burst.LAGS`.
- Per-request costs are averages per class. Expensive and cheap pages of the same class are not
  separated (adding path-group classes is a natural extension).
- Right-sizing recommends; it never applies. Short history (< 7 days) widens the margin.

## Future work

- **Active identification (switchback experiments):** temporarily tighten the nginx rate limit for one
  class for a few minutes at random times and measure the CPU change. This gives causal estimates even
  for traffic that never varies.
- Path-group classes (e.g. `/paper/*` vs `/search`) from the access log.
- Wake-on-request for parked projects through the SNI edge router (Sablier-style), so parking costs
  nothing when a visitor does arrive.

## References

- Q. Zhang, L. Cherkasova, E. Smirni. *A Regression-Based Analytic Model for Dynamic Resource
  Provisioning of Multi-Tier Applications.* ICAC 2007; *R-Capriccio: A Capacity Planning and Anomaly
  Detection Tool for Enterprise Services with Live Workloads.* Middleware 2007.
- C. L. Lawson, R. J. Hanson. *Solving Least Squares Problems.* Prentice-Hall, 1974 (NNLS).
- H. R. Künsch. *The Jackknife and the Bootstrap for General Stationary Observations.* Annals of
  Statistics 17(3), 1989 (moving-block bootstrap).
- Kubernetes autoscaler: Vertical Pod Autoscaler recommender (decaying histograms, OOM bump).
- Robusta KRR: Kubernetes Resource Recommender.
- J. Weiner. *PSI: Pressure Stall Information* (Linux 4.20, 2018); Kubernetes v1.36 PSI metrics GA (2026).
- Kepler: Kubernetes-based Efficient Power Level Exporter (CNCF).
- AppBreakup. *How often AI crawlers read software pages*, Nov 2026; Pinggy, *AI crawlers now cost more
  CPU than all your real traffic*, 2026.
