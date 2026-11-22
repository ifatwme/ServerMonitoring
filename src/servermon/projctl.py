"""projctl — see which projects use the server's resources and act on it.

  projctl ls                       every project: state, CPU, RAM, disk, visitors, idle time
  projctl suggest                  park candidates, degraded projects, reclaimable disk
  projctl insights [project]       who the CPU is spent on (regression attribution vs. request share)
  projctl rightsize [project] [--write DIR]   memory/CPU limit recommendations (+ compose overrides)
  projctl whatif <project>...      RAM/CPU freed and host headroom if these projects were parked
  projctl park <project> [-y]      stop its stack + timers (remembered; dashboards show it as parked)
  projctl unpark <project> [-y]    start it again exactly as it was enabled before
  projctl cleanup [-y]             remove docker build cache + images no compose file needs

Reads config/projects.toml, the collector/analyzer state ($SM_STATE_DIR) and Prometheus for live
CPU/RAM. Without -y every changing command only prints what it would do.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from .common import CONFIG, INSIGHTS_FILE, PARKED_FILE, REPO, STATE_FILE, load_json
from .prom import Prom, PromError
from .rightsizing import MiB, Recommendation, compose_override

PARKED = PARKED_FILE


def env_port() -> str:
    try:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("PROM_PORT="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "9090"


PROMETHEUS = Prom(f"http://127.0.0.1:{env_port()}", timeout=5)


def run(cmd: list[str], doit: bool, cwd: str | None = None) -> bool:
    print(("  $ " if doit else "  (dry-run) $ ") + " ".join(cmd))
    if not doit:
        return True
    r = subprocess.run(cmd, check=False, cwd=cwd)
    return r.returncode == 0


def sh(*cmd: str, cwd: str | None = None) -> str:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=cwd).stdout.strip()


def prom(q: str) -> dict[str, float]:
    try:
        return {m.get("project", ""): v for m, v in PROMETHEUS.query(q)}
    except PromError:
        return {}


def load():
    cfg = tomllib.loads(CONFIG.read_text())
    return cfg.get("projects", {}), load_json(STATE_FILE, {}), load_json(PARKED, {})


def has_compose(d: str) -> bool:
    return bool(d) and any(Path(d, f).exists() for f in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"))


def human(n: float) -> str:
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{u}" if u in ("B", "K") else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"


def ago(ts: float) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    return f"{d/86400:.1f}d" if d > 86400 else f"{d/3600:.1f}h" if d > 3600 else f"{d/60:.0f}m"


def cmd_ls(_):
    projects, st, parked = load()
    summ = st.get("summary", {})
    slow = st.get("slow", {}).get("projects", {})
    cpu = prom("project:cpu_cores:rate2m")
    cpu24 = prom("avg_over_time(project:cpu_cores:rate2m[24h])")
    mem = prom("project:memory_working_set_bytes")
    vis = prom('sm_http_unique_clients_window{class="engaged",window="7d"}')
    bots = prom('sum by (project) (sm_http_requests_window{class=~"ai_bot|bot",window="24h"})')
    print(f"{'PROJECT':22} {'STATE':10} {'CPU now':>7} {'CPU 24h':>7} {'RAM':>7} {'DISK':>7} {'VISIT7d':>7} "
          f"{'BOTS24h':>7} {'IDLE':>6}  NOTE")
    for p, c in projects.items():
        s = summ.get(p, {})
        disk = sum(v for k, v in slow.get(p, {}).items() if k.startswith("disk_"))
        state = ("parked" if p in parked else
                 "degraded" if s.get("stopped") or s.get("unhealthy") else
                 "running" if s.get("running") else "no-svc")
        note = []
        if s.get("candidate"):
            note.append("PARK CANDIDATE")
        if s.get("stopped"):
            note.append(f"{s['stopped']} container(s) stopped")
        if c.get("kind") == "infra":
            note.append("infra")
        idle = s.get("idle_days")
        print(f"{p:22} {state:10} {cpu.get(p, 0):7.2f} {cpu24.get(p, 0):7.2f} {human(mem.get(p, 0)):>7} "
              f"{human(disk):>7} {vis.get(p, 0):7.0f} {bots.get(p, 0):7.0f} "
              f"{(f'{idle}d' if idle is not None else '-'):>6}  {', '.join(note)}")
    if not cpu:
        print("\n(Prometheus not reachable: CPU/RAM columns are empty)")


def cmd_suggest(_):
    projects, st, parked = load()
    summ = st.get("summary", {})
    mem = prom("project:memory_working_set_bytes")
    cpu24 = prom("avg_over_time(project:cpu_cores:rate2m[24h])")
    print("Park candidates (running, but no engaged visitor / commit / edit / build / test run within the threshold):")
    any_ = False
    for p, s in summ.items():
        if s.get("candidate"):
            any_ = True
            sig = {k: ago(v) for k, v in s.get("signals", {}).items()}
            print(f"  - {p}: frees ~{human(mem.get(p, 0))} RAM, {cpu24.get(p, 0):.2f} CPU cores (24h avg); "
                  f"idle {s['idle_days']}d — last " + ", ".join(f"{k} {v}" for k, v in sig.items()))
            print(f"      projctl park {p} -y")
    if not any_:
        print("  none right now")
    print("\nDegraded (containers stopped / unhealthy while the project is not parked):")
    for p, s in summ.items():
        if (s.get("stopped") or s.get("unhealthy")) and p not in parked:
            print(f"  - {p}: {s.get('stopped', 0)} stopped, {s.get('unhealthy', 0)} unhealthy")
    imgs = st.get("slow", {}).get("images", [])
    unused = {i["id"]: i for i in imgs if not i["in_use"]}
    df = st.get("slow", {}).get("df", {})
    print(f"\nDisk: build cache reclaimable {human(df.get('Build Cache', {}).get('reclaimable', 0))}, "
          f"images reclaimable {human(df.get('Images', {}).get('reclaimable', 0))} "
          f"({len(unused)} images no container uses) -> projctl cleanup")


def cmd_park(a):
    projects, st, parked = load()
    p = a.project
    if p not in projects:
        sys.exit(f"unknown project {p}; known: {', '.join(projects)}")
    c = projects[p]
    if c.get("kind") == "infra" and not a.force:
        sys.exit(f"{p} is infrastructure; use --force if you really mean it")
    if p in parked:
        sys.exit(f"{p} is already parked (since {time.ctime(parked[p]['ts'])})")
    rec = {"ts": time.time(), "unit": c.get("unit"), "unit_enabled": False, "timers": {}}
    print(f"Parking {p}:")
    if c.get("unit"):
        rec["unit_enabled"] = sh("systemctl", "is-enabled", c["unit"]) == "enabled"
        ok = run(["systemctl", "disable", "--now", c["unit"]], a.yes)
    else:
        ok = True
    for t in c.get("timers", []):
        rec["timers"][t] = sh("systemctl", "is-enabled", t) == "enabled"
        run(["systemctl", "disable", "--now", t], a.yes)
    # make sure every container of the compose project is down even if the unit's ExecStop didn't do it
    if has_compose(c["dir"]):
        run(["docker", "compose", "stop"], a.yes, cwd=c["dir"])
    if a.yes and ok:
        parked[p] = rec
        PARKED.write_text(json.dumps(parked, indent=1))
        print(f"parked. Bring it back with: projctl unpark {p} -y")
    elif not a.yes:
        print("dry-run: add -y to do it")


def cmd_unpark(a):
    projects, st, parked = load()
    p = a.project
    if p not in parked:
        sys.exit(f"{p} is not parked")
    rec, c = parked[p], projects.get(p, {})
    print(f"Unparking {p}:")
    if rec.get("unit"):
        run(["systemctl", "enable" if rec.get("unit_enabled") else "start", *(["--now"] if rec.get("unit_enabled") else []),
             rec["unit"]], a.yes)
    elif c.get("dir"):
        run(["docker", "compose", "start"], a.yes, cwd=c["dir"])
    for t, en in rec.get("timers", {}).items():
        if en:
            run(["systemctl", "enable", "--now", t], a.yes)
    if a.yes:
        parked.pop(p)
        PARKED.write_text(json.dumps(parked, indent=1))
        print("unparked")
    else:
        print("dry-run: add -y to do it")


def cmd_cleanup(a):
    _, st, _ = load()
    imgs = st.get("slow", {}).get("images", [])
    unused = sorted({i["id"]: i for i in imgs if not i["in_use"]}.values(), key=lambda i: -i["size"])
    keep = set(a.keep or [])
    # never remove images a registered project's compose file pulls/builds (a parked project needs them back)
    projects, _, _ = load()
    keep.update(tomllib.loads(CONFIG.read_text()).get("settings", {}).get("keep_images", []))
    for c in projects.values():
        if has_compose(c.get("dir", "")):
            images = sh("docker", "compose", "--profile", "*", "config", "--images", cwd=c["dir"])
            keep.update(line.strip().split("@")[0] for line in images.splitlines() if line.strip())
    print("Build cache:")
    run(["docker", "builder", "prune", "-af"], a.yes)
    print("Images no container uses (layers shared with used images are not freed):")
    for i in unused:
        refs = [x["ref"] for x in imgs if x["id"] == i["id"]]
        if any(r.split(":")[0] in keep or r in keep or r.removeprefix("docker.io/") in keep for r in refs):
            print(f"  keep {', '.join(refs)}  (used by a compose file or --keep)")
            continue
        print(f"  {human(i['size']):>7}  {', '.join(refs)}  (project {i['project']}, built {ago(i['created'])} ago)")
        # untagged images ("repo:<none>") can only be removed by id
        named = [r for r in refs if not r.endswith(":<none>")]
        run(["docker", "image", "rm", *(named if len(named) == len(refs) else [i["id"]])], a.yes)
    if not a.yes:
        print("dry-run: add -y to do it; --keep <repo> to spare an image")


def _print_attr(title, r, naive=None, hidden=None):
    print(f"  {title}")
    print(f"    {'cause':10} {'CPU share':>9} {'90% CI':>11} {'by requests':>11} {'cost/request':>13}")
    for comp in ["baseline", *sorted(set(r["share"]) - {"baseline"})]:
        lo, hi = r["share_ci"][comp]
        nv = (naive or {}).get(comp)
        cost = r["cost_per_request"].get(comp)
        print(f"    {comp:10} {r['share'][comp]:9.1%} {f'{lo:.0%}-{hi:.0%}':>11} "
              f"{(f'{nv:.1%}' if nv is not None else '-'):>11} "
              f"{(f'{cost * 1000:.0f} ms' if cost and lo > 0.005 else '-'):>13}")
    for c, v in (hidden or {}).items():
        if v > 0.25 and r["share"].get(c, 0) < 0.1:
            print(f"    ! {c} traffic is too steady for 5-minute data: it could be up to {v:.0%} of the CPU")


def cmd_insights(a):
    rep = load_json(INSIGHTS_FILE, {})
    att, bur = rep.get("attribution", {}), rep.get("attribution_burst", {})
    if not att and not bur:
        sys.exit("no analysis yet: run `python3 -m servermon.analyze` (server-monitor-analyze.timer)")
    for p in sorted(set(att) | set(bur)):
        if a.project and p != a.project:
            continue
        r5, rb = att.get(p), bur.get(p)
        mean = (rb or r5)["mean_cpu"]
        print(f"{p}  (mean {mean:.3f} cores)")
        if rb:
            _print_attr(f"burst model, 1 Hz: {rb['bursts']} bursts / {rb['seconds']} s, lags {rb['lags'][0]}..{rb['lags'][1]} s, "
                        f"baseline from {rb['baseline_source']}, confidence {rb['confidence']}",
                        rb, (r5 or {}).get("naive_share"))
        if r5:
            ho = r5["r2_holdout"]
            _print_attr(f"5-minute model: {r5['samples']} samples, R2 {r5['r2']:.2f}, hold-out "
                        f"{'-' if ho is None else f'{ho:.2f}'}, confidence {r5['confidence']}",
                        r5, r5["naive_share"], r5.get("max_hidden_share"))
        print()


def _recs(project=None) -> list[Recommendation]:
    rep = load_json(INSIGHTS_FILE, {})
    return [Recommendation(**r) for r in rep.get("rightsizing", []) if not project or r["project"] == project]


def cmd_rightsize(a):
    recs = _recs(a.project)
    if not recs:
        sys.exit("no analysis yet: run `python3 -m servermon.analyze`")
    print(f"{'CONTAINER':34} {'DECISION':17} {'LIMIT':>7} {'PEAK24h':>7} {'P98':>7} {'RECOMMEND':>9}  WHY")
    for r in sorted(recs, key=lambda r: (r.project, r.service)):
        lim = f"{r.mem_limit / MiB:.0f}M" if r.mem_limit else "none"
        print(f"{r.container:34} {r.decision:17} {lim:>7} {r.mem_peak_24h / MiB:6.0f}M {r.mem_target / MiB:6.0f}M "
              f"{r.mem_recommended / MiB:8.0f}M  {'; '.join(r.reasons)}")
    rep = load_json(INSIGHTS_FILE, {})
    oc = rep.get("overcommit", {})
    if oc.get("current"):
        print(f"\nSum of memory limits / host RAM: now {oc['current']:.2f}x, recommended {oc['recommended']:.2f}x "
              "(> 1 means the limits cannot protect the host from OOM)")
    if a.write:
        out = Path(a.write)
        out.mkdir(parents=True, exist_ok=True)
        by_p: dict[str, list[Recommendation]] = {}
        for r in recs:
            by_p.setdefault(r.project, []).append(r)
        for p, rs in by_p.items():
            text = compose_override(rs)
            if text:
                (out / f"{p}.rightsizing.yml").write_text(text)
                print(f"wrote {out / f'{p}.rightsizing.yml'}")


def cmd_whatif(a):
    projects, _, _ = load()
    unknown = [p for p in a.projects if p not in projects]
    if unknown:
        sys.exit(f"unknown project(s): {', '.join(unknown)}")
    mem = prom("project:memory_working_set_bytes")
    cpu24 = prom("avg_over_time(project:cpu_cores:rate2m[24h])")
    try:
        avail = PROMETHEUS.query("node_memory_MemAvailable_bytes")[0][1]
        total = PROMETHEUS.query("node_memory_MemTotal_bytes")[0][1]
        psi = PROMETHEUS.query("rate(node_pressure_memory_waiting_seconds_total[1h])")[0][1]
    except (PromError, IndexError):
        sys.exit("Prometheus not reachable")
    freed = sum(mem.get(p, 0) for p in a.projects)
    cores = sum(cpu24.get(p, 0) for p in a.projects)
    lims = [r for r in _recs() if r.project in a.projects]
    print(f"Parking {', '.join(a.projects)}:")
    print(f"  RAM freed (working set)      {human(freed)}")
    print(f"  CPU freed (24 h average)     {cores:.2f} cores")
    print(f"  Host RAM available           {human(avail)} -> {human(avail + freed)}  "
          f"({avail / total:.0%} -> {(avail + freed) / total:.0%} of {human(total)})")
    print(f"  Memory stall now (PSI, 1 h)  {psi:.2%}")
    if lims:
        print(f"  Memory limits released       {human(sum(r.mem_limit or 0 for r in lims))}")
    print(f"\n  projctl park {' '.join(a.projects)} -y   # one at a time: projctl park <project> -y")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls").set_defaults(f=cmd_ls)
    sub.add_parser("suggest").set_defaults(f=cmd_suggest)
    s = sub.add_parser("insights")
    s.add_argument("project", nargs="?")
    s.set_defaults(f=cmd_insights)
    s = sub.add_parser("rightsize")
    s.add_argument("project", nargs="?")
    s.add_argument("--write", metavar="DIR", help="write <project>.rightsizing.yml compose overrides to DIR")
    s.set_defaults(f=cmd_rightsize)
    s = sub.add_parser("whatif")
    s.add_argument("projects", nargs="+")
    s.set_defaults(f=cmd_whatif)
    for name, f in (("park", cmd_park), ("unpark", cmd_unpark)):
        s = sub.add_parser(name)
        s.add_argument("project")
        s.add_argument("-y", "--yes", action="store_true")
        s.add_argument("--force", action="store_true")
        s.set_defaults(f=f)
    s = sub.add_parser("cleanup")
    s.add_argument("-y", "--yes", action="store_true")
    s.add_argument("--keep", action="append", help="image repo to keep (repeatable)")
    s.set_defaults(f=cmd_cleanup)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
