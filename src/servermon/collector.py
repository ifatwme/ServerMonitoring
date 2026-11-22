#!/usr/bin/env python3
"""server-monitor collector: host + project facts that cAdvisor/node_exporter can't see.

Runs every minute (server-monitor-collect.timer) on the host, stdlib only. Writes
  $SM_STATE_DIR/textfile/server_monitor.prom   read by node_exporter's textfile collector
  $SM_STATE_DIR/targets/blackbox.json          file_sd targets for the uptime probes
  $SM_STATE_DIR/state.json                     incremental state + summary used by `projctl`

Metric families (all prefixed sm_):
  containers  state / health / restarts / OOM / limits (also for STOPPED containers)
  traffic     nginx access logs per project, split into human / ai_bot / bot / scanner / probe
  activity    last commit, last file edit, last image build, last container start (tests),
              last human visit -> idle time and park candidates
  units       last result + duration of each project's systemd services/timers
  disk        per-project repo / dev dir / image bytes, docker reclaimable space
  host        ssh auth failures, pending apt upgrades, reboot required
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import traffic
from .common import (
    PARKED_FILE,
    STATE_DIR,
    STATE_FILE,
    TEXTFILE_DIR,
    Out,
    atomic_write,
    iso_ts,
    load_config,
    load_json,
    parse_size,
    sh,
)

TEXTFILE = TEXTFILE_DIR / "server_monitor.prom"
TARGETS = STATE_DIR / "targets" / "blackbox.json"

SLOW_EVERY = 600        # disk / git / image scans
HOURLY_EVERY = 3600     # apt
BUCKET = traffic.BUCKET
NOW = time.time()


# ── config / state ───────────────────────────────────────────────────────────

# ── containers ───────────────────────────────────────────────────────────────

def containers():
    ids = sh("docker", "ps", "-aq").split()
    if not ids:
        return []
    try:
        return json.loads(sh("docker", "inspect", *ids) or "[]")
    except json.JSONDecodeError:
        return []


def project_of_container(c, projects) -> str:
    lab = c["Config"].get("Labels") or {}
    p = lab.get("com.docker.compose.project")
    if p:
        return p
    name = c["Name"].lstrip("/")
    for pn in projects:
        if name.startswith(pn):
            return pn
    return "unassigned"


def collect_containers(out: Out, projects, cons):
    per_project = defaultdict(lambda: Counter())
    for c in cons:
        lab = c["Config"].get("Labels") or {}
        if lab.get("com.docker.compose.oneoff") == "True":
            continue  # `compose run` jobs/tests: counted as activity, not as services
        st = c["State"]
        name = c["Name"].lstrip("/")
        proj = project_of_container(c, projects)
        svc = lab.get("com.docker.compose.service", name)
        L = {"project": proj, "service": svc, "container": name}
        health = (st.get("Health") or {}).get("Status", "none")
        running = st.get("Running", False)
        out.add("sm_container_up", 1 if running else 0, L, "1 if the container is running (also exported for stopped containers)")
        out.add("sm_container_info", 1, {**L, "state": st.get("Status", "?"), "health": health,
                                          "image": c["Config"].get("Image", "")}, "Container metadata")
        out.add("sm_container_healthy", {"healthy": 1, "unhealthy": 0, "starting": 0.5}.get(health, -1) if running else 0,
                L, "1 healthy, 0 unhealthy/stopped, 0.5 starting, -1 no healthcheck")
        out.add("sm_container_restart_count", c.get("RestartCount", 0), L, "Docker restart count")
        out.add("sm_container_oom_killed", 1 if st.get("OOMKilled") else 0, L, "Last exit was an OOM kill")
        out.add("sm_container_exit_code", st.get("ExitCode", 0), L, "Last exit code")
        out.add("sm_container_started_timestamp_seconds", iso_ts(st.get("StartedAt")), L, "Last start time")
        if not running:
            out.add("sm_container_finished_timestamp_seconds", iso_ts(st.get("FinishedAt")), L, "Stop time of a stopped container")
        hc = c["HostConfig"]
        if hc.get("Memory"):
            out.add("sm_container_memory_limit_bytes", hc["Memory"], L, "Memory limit (mem_limit)")
        if hc.get("NanoCpus"):
            out.add("sm_container_cpu_limit_cores", hc["NanoCpus"] / 1e9, L, "CPU limit (cpus)")
        per_project[proj]["running" if running else "stopped"] += 1
        if running and health == "unhealthy":
            per_project[proj]["unhealthy"] += 1
    return per_project


def container_activity(state, projects, cons):
    """Last container start per project, from docker events (catches `compose run --rm` tests)."""
    since = int(state.get("events_since", NOW - 7 * 86400))
    until = int(NOW)
    last = state.setdefault("last_container_start", {})
    raw = sh("docker", "events", "--since", str(since), "--until", str(until),
             "--filter", "type=container", "--filter", "event=start", "--format", "{{json .}}", timeout=30)
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        a = ev.get("Actor", {}).get("Attributes", {})
        proj = a.get("com.docker.compose.project") or ""
        if not proj:  # plain `docker run`: attribute by container name only (images are shared as tool runners)
            name = a.get("name", "")
            proj = next((p for p in projects if name.startswith(p)), "")
        if proj:
            last[proj] = max(last.get(proj, 0), ev.get("time", 0))
    state["events_since"] = until
    return last


# ── traffic (nginx access logs) ──────────────────────────────────────────────

def collect_traffic(out: Out, state, projects):
    tr = state.setdefault("traffic", {})
    for proj, cfg in projects.items():
        cname = cfg.get("web_container")
        if not cname:
            continue
        ps = tr.setdefault(proj, traffic.new_state())
        ps.setdefault("seen", {})
        cid = sh("docker", "inspect", "-f", "{{.Id}}", cname, check=True).strip()
        if cid:
            cur = ps["cursor"].get(cid)
            since = str(int(cur)) if cur else str(int(NOW - 8 * 86400))
            raw = sh("docker", "logs", "--timestamps", "--since", since, cname, timeout=60)
            newest = traffic.ingest(ps, raw.splitlines(), after=cur, live=cur is not None)
            ps["cursor"] = {cid: newest or NOW}
        traffic.prune(ps, NOW)
        traffic.export(out, proj, ps, NOW)


# ── activity (git, files, images) ────────────────────────────────────────────

def newest_mtime(path: str, max_depth: int = 4) -> float:
    """Newest mtime of regular files under path, skipping heavy/generated dirs."""
    skip = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__", "data", "cache", ".cache",
            "models", "out", "site", "_site", "state", "logs", "tls", "certs", "acme", ".next"}
    best = 0.0
    base = Path(path)
    if not base.exists():
        return 0.0
    for dirpath, dirnames, filenames in os.walk(base):
        depth = len(Path(dirpath).relative_to(base).parts)
        dirnames[:] = [d for d in dirnames if d not in skip and depth < max_depth]
        for f in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, f))
            except OSError:
                continue
            best = max(best, st.st_mtime)
    return best


def repo_activity(d: str) -> tuple[float, float]:
    """(last commit time, newest uncommitted edit) for a git repo."""
    if not Path(d, ".git").exists():
        return 0.0, newest_mtime(d)
    commit = float(sh("git", "-C", d, "log", "-1", "--format=%ct").strip() or 0)
    edit = 0.0
    files = sh("git", "-C", d, "ls-files", "-m", "-o", "--exclude-standard", "-z").split("\0")
    for f in files[:5000]:
        if not f:
            continue
        with contextlib.suppress(OSError):
            edit = max(edit, os.lstat(os.path.join(d, f)).st_mtime)
    return commit, edit


def du_bytes(path: str) -> float:
    if not Path(path).exists():
        return 0.0
    o = sh("du", "-sxb", path, timeout=120)
    return float(o.split()[0]) if o.strip() else 0.0


def image_inventory(cons):
    used = {c["Image"] for c in cons}  # image IDs referenced by any container
    rows = []
    lines = sh("docker", "image", "ls", "--format", "{{.ID}}\t{{.Repository}}:{{.Tag}}").splitlines()
    ids = sorted({l.split("\t")[0] for l in lines if l})
    meta = {}
    if ids:
        for l in sh("docker", "image", "inspect", "--format", "{{.Id}}\t{{.Size}}\t{{.Created}}", *ids).splitlines():
            iid, size, created = l.split("\t")
            meta[iid] = (float(size), iso_ts(created) or 0)
    for l in lines:
        short, ref = l.split("\t")
        full = next((k for k in meta if k.split(":")[-1].startswith(short)), None)
        if not full:
            continue
        size, created = meta[full]
        rows.append({"ref": ref, "id": full, "size": size, "created": created, "in_use": full in used})
    return rows


def image_project(ref: str, projects) -> str:
    best, blen = "shared", 0
    for p, cfg in projects.items():
        for pre in cfg["images"]:
            if ref.startswith(pre) and len(pre) > blen:
                best, blen = p, len(pre)
    return best


def collect_slow(state, projects, cons):
    slow = {"projects": {}, "images": [], "df": {}, "ts": NOW}
    imgs = image_inventory(cons)
    for im in imgs:
        im["project"] = image_project(im["ref"], projects)
    slow["images"] = imgs
    for proj, cfg in projects.items():
        commit, edit = repo_activity(cfg["dir"])
        dev_edit = max([newest_mtime(d) for d in cfg["dev_dirs"]] or [0])
        mine = [i for i in imgs if i["project"] == proj]
        slow["projects"][proj] = {
            "last_commit": commit,
            "last_edit": max(edit, dev_edit),
            "last_build": max([i["created"] for i in mine] or [0]),
            "disk_repo": du_bytes(cfg["dir"]),
            "disk_dev": sum(du_bytes(d) for d in cfg["dev_dirs"]),
            "disk_images": sum(i["size"] for i in {i["id"]: i for i in mine}.values()),
        }
    for l in sh("docker", "system", "df", "--format", "{{json .}}", timeout=120).splitlines():
        try:
            r = json.loads(l)
        except json.JSONDecodeError:
            continue
        slow["df"][r["Type"]] = {"size": parse_size(r["Size"]), "reclaimable": parse_size(r["Reclaimable"]),
                                 "count": float(r.get("TotalCount") or 0), "active": float(r.get("Active") or 0)}
    vols = defaultdict(float)
    vjs = sh("docker", "system", "df", "-v", "--format", "{{json .Volumes}}", timeout=120).strip()
    try:
        for v in json.loads(vjs or "[]"):
            p = (v.get("Labels") or "")
            m = re.search(r"com\.docker\.compose\.project=([^,]+)", p)
            vols[m.group(1) if m else "shared"] += parse_size(v.get("Size", "0B"))
    except json.JSONDecodeError:
        pass
    for p, b in vols.items():
        slow["projects"].setdefault(p, {})["disk_volumes"] = b
    state["slow"] = slow


# ── systemd units ────────────────────────────────────────────────────────────

def collect_units(out: Out, projects):
    for proj, cfg in projects.items():
        units = ([cfg["unit"]] if cfg.get("unit") else []) + [t.replace(".timer", ".service") for t in cfg["timers"]]
        for u in units:
            props = dict(l.split("=", 1) for l in sh(
                "systemctl", "show", u, "--timestamp=unix", "-p",
                "Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp,ActiveState,UnitFileState,LoadState"
            ).splitlines() if "=" in l)
            if props.get("LoadState") != "loaded":
                continue
            L = {"project": proj, "unit": u}
            start = float(props.get("ExecMainStartTimestamp", "@0").lstrip("@") or 0)
            end = float(props.get("ExecMainExitTimestamp", "@0").lstrip("@") or 0)
            ok = props.get("Result") == "success" and props.get("ExecMainStatus", "0") == "0"
            out.add("sm_unit_last_success", 1 if ok else 0, L, "1 if the unit's last run succeeded")
            out.add("sm_unit_active", 1 if props.get("ActiveState") == "active" else 0, L, "Unit is active")
            out.add("sm_unit_enabled", 1 if props.get("UnitFileState") == "enabled" else 0, L, "Unit is enabled")
            if start:
                out.add("sm_unit_last_start_timestamp_seconds", start, L, "Last start of the unit's main process")
            if start and end >= start:
                out.add("sm_unit_last_duration_seconds", end - start, L, "Duration of the last run")
        for t in cfg["timers"]:
            en = sh("systemctl", "is-enabled", t).strip()
            out.add("sm_timer_enabled", 1 if en == "enabled" else 0, {"project": proj, "unit": t}, "Timer is enabled")


# ── host ─────────────────────────────────────────────────────────────────────

def collect_host(out: Out, state):
    h = state.setdefault("host", {"ssh_fail": 0, "ssh_since": NOW - 60})
    raw = sh("journalctl", "-q", "--no-pager", "-o", "cat", "--since", f"@{int(h['ssh_since'])}",
             "--until", f"@{int(NOW)}", "-t", "sshd", "-t", "sshd-session", timeout=30)
    h["ssh_fail"] += len(re.findall(r"Invalid user|Failed password|Failed publickey|authentication failure|"
                                    r"Connection closed by authenticating user|maximum authentication attempts",
                                    raw))
    h["ssh_since"] = NOW
    out.add("sm_ssh_auth_failures_total", h["ssh_fail"], None, "Failed SSH authentications (sshd journal)", "counter")
    out.add("sm_host_reboot_required", 1 if Path("/var/run/reboot-required").exists() else 0, None,
            "1 if the kernel/libc update needs a reboot")
    if NOW - h.get("apt_ts", 0) > HOURLY_EVERY:
        o = sh("apt", "list", "--upgradable", timeout=60)
        h["apt_upgradable"] = sum(1 for l in o.splitlines() if "/" in l and "upgradable" in l)
        h["apt_security"] = sum(1 for l in o.splitlines() if "-security" in l)
        h["apt_ts"] = NOW
    out.add("sm_apt_upgradable_packages", h.get("apt_upgradable", 0), None, "Packages with pending upgrades")
    out.add("sm_apt_security_upgrades", h.get("apt_security", 0), None, "Pending security upgrades")


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="run the slow disk/git/image scan now")
    args = ap.parse_args(argv)
    t0 = time.time()
    settings, projects = load_config()
    state = load_json(STATE_FILE, {})
    parked = load_json(PARKED_FILE, {})
    out = Out()
    cons = containers()

    per_project = collect_containers(out, projects, cons)
    starts = container_activity(state, projects, cons)
    collect_traffic(out, state, projects)
    if NOW - state.get("slow", {}).get("ts", 0) > SLOW_EVERY or args.full:
        collect_slow(state, projects, cons)
    collect_units(out, projects)
    collect_host(out, state)

    slow = state.get("slow", {"projects": {}, "images": [], "df": {}})
    summary = {}
    for proj, cfg in projects.items():
        L = {"project": proj}
        sp = slow["projects"].get(proj, {})
        tr = state.get("traffic", {}).get(proj, {})
        last_human = tr.get("last", {}).get("engaged", 0)
        signals = {
            "commit": sp.get("last_commit", 0),
            "edit": sp.get("last_edit", 0),
            "build": sp.get("last_build", 0),
            "container_start": starts.get(proj, 0),
            "human_visit": last_human,
        }
        last = max(signals.values())
        idle = NOW - last if last else 30 * 86400
        pc = per_project.get(proj, Counter())
        is_parked = proj in parked
        candidate = (not cfg["keep_running"] and not is_parked and pc["running"] > 0
                     and idle > cfg["idle_days"] * 86400)
        out.add("sm_project_info", 1, {**L, "kind": cfg["kind"], "has_url": "1" if cfg.get("url") else "0",
                                       "unit": cfg.get("unit", "")}, "Project registry")
        for sig, ts in signals.items():
            if ts:
                out.add("sm_project_last_activity_timestamp_seconds", ts, {**L, "signal": sig},
                        "Last activity per signal: commit, edit, build, container_start (tests), human_visit")
        out.add("sm_project_idle_seconds", idle, L, "Seconds since the newest activity signal")
        out.add("sm_project_idle_threshold_seconds", cfg["idle_days"] * 86400, L, "Idle time after which parking is suggested")
        out.add("sm_project_park_candidate", 1 if candidate else 0, L, "1 = running but idle past its threshold")
        out.add("sm_project_parked", 1 if is_parked else 0, L, "1 = deliberately stopped with `projctl park`")
        out.add("sm_project_keep_running", 1 if cfg["keep_running"] else 0, L, "1 = never suggested for parking")
        for st in ("running", "stopped", "unhealthy"):
            out.add("sm_project_containers", pc[st], {**L, "state": st}, "Service containers per state")
        for k in ("repo", "dev", "images", "volumes"):
            if f"disk_{k}" in sp:
                out.add("sm_project_disk_bytes", sp[f"disk_{k}"], {**L, "kind": k},
                        "Disk used per project: repo dir, dev dirs, docker images, docker volumes")
        summary[proj] = {"kind": cfg["kind"], "running": pc["running"], "stopped": pc["stopped"],
                         "unhealthy": pc["unhealthy"], "parked": is_parked, "idle_days": round(idle / 86400, 1),
                         "candidate": candidate, "signals": signals,
                         "pageviews_7d": sum(d.get("pageview", 0) for b, d in tr.get("buckets", {}).items()
                                             if float(b) + BUCKET > NOW - 7 * 86400),
                         "bot_7d": sum(d.get("bot", 0) + d.get("ai_bot", 0) for b, d in tr.get("buckets", {}).items()
                                       if float(b) + BUCKET > NOW - 7 * 86400),
                         "unit": cfg.get("unit", ""), "timers": cfg["timers"], "dir": cfg["dir"]}
    for im in slow.get("images", []):
        out.add("sm_docker_image_bytes", im["size"], {"image": im["ref"], "project": im["project"],
                                                      "in_use": "1" if im["in_use"] else "0"},
                "Image size (layers shared between images are counted in each)")
        out.add("sm_docker_image_created_timestamp_seconds", im["created"], {"image": im["ref"], "project": im["project"]},
                "Image build time")
    for typ, d in slow.get("df", {}).items():
        t = typ.lower().replace(" ", "_")
        out.add("sm_docker_disk_bytes", d["size"], {"type": t}, "docker system df: size")
        out.add("sm_docker_reclaimable_bytes", d["reclaimable"], {"type": t}, "docker system df: reclaimable")
        out.add("sm_docker_objects", d["count"], {"type": t}, "docker system df: object count")
    out.add("sm_collector_duration_seconds", time.time() - t0, None, "Collector run time")
    out.add("sm_collector_last_run_timestamp_seconds", NOW, None, "Collector last successful run")

    targets = [{"targets": [cfg["url"]], "labels": {"project": p, "kind": cfg["kind"]}}
               for p, cfg in projects.items() if cfg.get("url")]
    atomic_write(TARGETS, json.dumps(targets, indent=1))
    atomic_write(TEXTFILE, out.render())
    state["summary"] = summary
    state["summary_ts"] = NOW
    atomic_write(STATE_FILE, json.dumps(state))


if __name__ == "__main__":
    main()
