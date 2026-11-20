# server-monitor

Prometheus + Grafana for every project on this server: resource use per project, uptime,
traffic (people vs. bots), activity, disk, and suggestions for what to switch off.

```
docker-compose.yml          prometheus, grafana, node-exporter, cadvisor, blackbox (all localhost-only)
prometheus/prometheus.yml   scrape config; uptime targets come from the collector (file_sd)
prometheus/rules/           recording rules (per-project CPU/RAM/net/IO, uptime) + alert rules
blackbox/blackbox.yml       HTTPS probe module
grafana/build_dashboards.py dashboards as code -> grafana/dashboards/*.json (provisioned, read-only)
collector/collect.py        host-side collector (stdlib Python, every minute) -> sm_* metrics
config/projects.toml        the project registry (local only; template: projects.example.toml)
bin/projctl                 ls / suggest / park / unpark / cleanup
deploy/systemd/             server-monitor.service, server-monitor-collect.{service,timer}
```

## What is measured

| Source | What |
|---|---|
| node-exporter | host CPU, RAM, PSI stall, disk, network, OOM kills, systemd unit states |
| cAdvisor | per container CPU, memory, network, disk I/O, PSI — labelled `project`/`service` from Compose |
| blackbox | each project's public URL every 60 s: up/down, latency, TLS expiry |
| collector (`sm_*`) | container state/health/restarts/OOM **including stopped containers**; nginx access logs split into `human` / `engaged` / `ai_bot` / `bot` / `scanner` / `probe`; top user agents; last commit, uncommitted edit, image build, container start (tests) and engaged visit → idle time and *park candidate*; result/duration of each project's systemd jobs; disk per project (repo, dev dirs, images, volumes); docker reclaimable space and unused images; failed SSH logins; pending apt upgrades; reboot-required |

*engaged visitor* = an IP that loaded a page **and** its assets within 10 minutes (real browsers do,
scanners with a browser user agent usually don't). A project is a **park candidate** when it has running
containers but none of the activity signals happened within `idle_days` (default 7) and it is not
`keep_running`/`infra`.

## Dashboards

Grafana listens on `127.0.0.1:${GRAFANA_PORT}` only. Open it through an SSH tunnel:

```bash
ssh -p <ssh-port> -L 3000:127.0.0.1:3000 root@<server>     # then http://localhost:3000
```

Login `admin` / `GF_ADMIN_PASSWORD` from `.env`. Folder *Server*:

- **Server overview** (home) — host gauges, CPU/RAM stacked by project, PSI, the projects table, firing alerts, uptime timeline.
- **Project detail** — pick a project: status, containers, jobs, last activity, CPU/RAM per service vs. limits, traffic by class, top agents, images.
- **Waste & recommendations** — park candidates, CPU per real visitor, heavy-but-quiet containers, traffic mix, unused images, build cache.

Alert rules live in Prometheus (`prometheus/rules/server.yml`) and show in Grafana → Alerting → Alert rules
(data source-managed). No notifications are sent yet.

## projctl

```bash
projctl ls                       # table of all projects
projctl suggest                  # park candidates, degraded projects, reclaimable disk
projctl park <project>           # dry-run; add -y to stop its unit + timers + containers
projctl unpark <project> -y      # restore exactly what was enabled before
projctl cleanup                  # dry-run: build cache + images no container uses; -y to delete
```

Parked projects are recorded in `/var/lib/server-monitor/parked.json`; their alerts are muted and the
dashboards show them as parked.

## Operate

```bash
systemctl status server-monitor.service server-monitor-collect.timer
docker compose ps
python3 grafana/build_dashboards.py     # after editing dashboards
python3 collector/collect.py --full     # force the slow (disk/git/image) scan now
```

Adding a project: add a `[projects.<compose-project-name>]` block to `config/projects.toml`
(and its unit names to the node-exporter `--collector.systemd.unit-include` regex).

Footprint: limits add up to ~1.4 GB, real use is ~400 MB (prometheus 512m, grafana 512m, cadvisor 256m, node 96m,
blackbox 64m; typical use is well below), TSDB capped at 90 days / 2 GB.
