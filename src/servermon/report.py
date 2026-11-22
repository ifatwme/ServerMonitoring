"""Periodic server status report for Telegram (General topic).

    sm-report [--dry-run]       collect from Prometheus + collector/analyzer state and post (or print)

Run every 5 hours by server-monitor-report.timer. `collect()` gathers plain numbers; `build()`
turns them into Telegram HTML; both are pure and unit-tested. The previous report's key numbers
are kept in $SM_STATE_DIR/report.json to show what changed since then.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .common import INSIGHTS_FILE, STATE_DIR, STATE_FILE, atomic_write, load_json
from .prom import Prom, PromError
from .telegram import Bot, TelegramError, env, esc

LAST_FILE = STATE_DIR / "report.json"
GB = 1024 ** 3


def _one(prom: Prom, q: str, default=None):
    try:
        r = prom.query(q)
    except PromError:
        return default
    return r[0][1] if r else default


def _by(prom: Prom, q: str, label: str = "project") -> dict:
    try:
        return {m.get(label, ""): v for m, v in prom.query(q)}
    except PromError:
        return {}


def collect(prom: Prom) -> dict:
    alerts = []
    try:
        for m, _ in prom.query('ALERTS{alertstate="firing"}'):
            alerts.append({k: m.get(k, "") for k in ("alertname", "severity", "project", "container", "unit")})
    except PromError:
        pass
    state = load_json(STATE_FILE, {})
    insights = load_json(INSIGHTS_FILE, {})
    return {
        "ts": time.time(),
        "host": {
            "cpu": _one(prom, '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'),
            "cpu24": _one(prom, '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[24h]))'),
            "mem_total": _one(prom, "node_memory_MemTotal_bytes"),
            "mem_avail": _one(prom, "node_memory_MemAvailable_bytes"),
            "disk_free": _one(prom, 'node_filesystem_avail_bytes{mountpoint="/"}'),
            "disk_size": _one(prom, 'node_filesystem_size_bytes{mountpoint="/"}'),
            "load5": _one(prom, "node_load5"),
            "uptime": _one(prom, "time() - node_boot_time_seconds"),
            "oom24": _one(prom, "increase(node_vmstat_oom_kill[24h])", 0),
            "mem_psi": _one(prom, "rate(node_pressure_memory_waiting_seconds_total[1h])"),
            "reclaimable": _one(prom, "sum(sm_docker_reclaimable_bytes)", 0),
            "reboot": _one(prom, "sm_host_reboot_required", 0),
            "apt": _one(prom, "sm_apt_upgradable_packages", 0),
        },
        "sites": _by(prom, 'max by (project) (probe_success{job="uptime"})'),
        "uptime24": _by(prom, "project:probe_success:avg24h"),
        "cpu": _by(prom, "project:cpu_cores:rate2m"),
        "mem": _by(prom, "project:memory_working_set_bytes"),
        "visitors": _by(prom, 'sm_http_unique_clients_window{class="engaged",window="24h"}'),
        "bots": _by(prom, 'sum by (project) (sm_http_requests_window{class=~"ai_bot|bot",window="24h"})'),
        "summary": state.get("summary", {}),
        "alerts": alerts,
        "insights": insights,
    }


def _gb(b) -> str:
    return "?" if b is None else f"{b / GB:.1f}G" if b >= GB / 10 else f"{b / 1024 ** 2:.0f}M"


def _pct(v) -> str:
    return "?" if v is None else f"{v:.0%}"


def _k(n) -> str:
    return "-" if not n else f"{n / 1000:.0f}k" if n >= 10000 else f"{n:.0f}"


SEV = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


def build(d: dict, last: dict | None = None, grafana_url: str = "") -> str:
    h = d["host"]
    t = time.strftime("%d %b %Y, %H:%M UTC", time.gmtime(d["ts"]))
    used = (h["mem_total"] - h["mem_avail"]) if h.get("mem_total") and h.get("mem_avail") is not None else None
    disk_used = 1 - h["disk_free"] / h["disk_size"] if h.get("disk_size") and h.get("disk_free") is not None else None
    crit = sum(1 for a in d["alerts"] if a["severity"] == "critical")
    head = "🟢" if not d["alerts"] else "🔴" if crit else "🟠" if any(a["severity"] == "warning" for a in d["alerts"]) else "🔵"
    out = [f"{head} <b>Server status</b> · {t}", ""]
    out.append(f"<b>Host</b>  CPU {_pct(h.get('cpu'))} (24 h {_pct(h.get('cpu24'))}) · RAM {_gb(used)}/{_gb(h.get('mem_total'))}"
               f" (free {_gb(h.get('mem_avail'))}) · disk {_pct(disk_used)} (free {_gb(h.get('disk_free'))})"
               f" · load {h.get('load5') or 0:.2f} · up {(h.get('uptime') or 0) / 86400:.0f} d")
    extra = []
    if h.get("oom24"):
        extra.append(f"⚠️ {h['oom24']:.0f} OOM kill(s) in 24 h")
    if (h.get("mem_psi") or 0) > 0.01:
        extra.append(f"memory stall {h['mem_psi']:.1%}")
    if h.get("reboot"):
        extra.append("reboot required")
    if h.get("apt"):
        extra.append(f"{h['apt']:.0f} package updates")
    if last and last.get("host"):
        lh = last["host"]
        if used is not None and lh.get("mem_used") is not None:
            extra.append(f"RAM {((used - lh['mem_used']) / GB):+.1f}G")
        if h.get("disk_free") is not None and lh.get("disk_free") is not None:
            extra.append(f"disk free {((h['disk_free'] - lh['disk_free']) / GB):+.1f}G since last report")
    if extra:
        out.append("      " + " · ".join(extra))

    # alerts
    out.append("")
    if d["alerts"]:
        counts = " · ".join(f"{SEV[s]} {n} {s}" for s in ("critical", "warning", "info")
                            if (n := sum(1 for a in d["alerts"] if a["severity"] == s)))
        out.append(f"<b>Alerts</b>  {counts}")
        groups: dict[tuple, list] = {}
        for a in d["alerts"]:
            groups.setdefault((a["severity"], a["alertname"]), []).append(a["project"] or a["container"] or a["unit"])
        order = {"critical": 0, "warning": 1, "info": 2}
        for (sev, name), who in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 3), kv[0][1])):
            who = sorted({w for w in who if w})
            out.append(f"  {SEV.get(sev, '•')} {esc(name)}{': ' + esc(', '.join(who)) if who else ''}")
        new = sorted({a["alertname"] for a in d["alerts"]} - set((last or {}).get("alerts", [])))
        resolved = sorted(set((last or {}).get("alerts", [])) - {a["alertname"] for a in d["alerts"]})
        if last and (new or resolved):
            out.append("  " + " · ".join(x for x in (f"new: {', '.join(new)}" if new else "",
                                                     f"cleared: {', '.join(resolved)}" if resolved else "") if x))
    else:
        out.append("<b>Alerts</b>  none firing ✅")

    # sites
    if d["sites"]:
        down = sorted(p for p, v in d["sites"].items() if v < 1)
        line = f"<b>Sites</b>  {len(d['sites']) - len(down)}/{len(d['sites'])} up"
        if down:
            line += " · down: " + esc(", ".join(down))
        weak = sorted(p for p, v in d["uptime24"].items() if v < 0.99 and p not in down)
        if weak:
            line += " · flaky (24 h): " + esc(", ".join(f"{p} {d['uptime24'][p]:.0%}" for p in weak))
        out.append(line)

    # projects table
    rows = []
    for p, s in d["summary"].items():
        if s.get("kind") == "infra" and not d["cpu"].get(p):
            continue
        state = ("parked" if s.get("parked") else "down" if d["sites"].get(p, 1) < 1 else
                 "degraded" if s.get("stopped") or s.get("unhealthy") else "up" if s.get("running") else
                 "batch" if s.get("kind") == "batch" else "-")
        rows.append((p, state, d["cpu"].get(p, 0.0), d["mem"].get(p, 0.0), d["visitors"].get(p, 0),
                     d["bots"].get(p, 0), s.get("idle_days"), s.get("candidate")))
    if rows:
        rows.sort(key=lambda r: -r[3])
        out += ["", "<b>Projects</b> (RAM order)", "<pre>project              state     cpu   ram  ppl  bots idle"]
        for p, st, cpu, mem, vis, bots, idle, cand in rows:
            out.append(f"{p[:20]:20} {st:8} {cpu:4.2f} {(_gb(mem) if mem else '-'):>5} {vis:4.0f} {_k(bots):>5} "
                       f"{(f'{idle:.0f}d' if idle is not None else '-'):>4}{' ⏸' if cand else ''}")
        out.append("</pre>ppl = real visitors in 24 h · bots = crawler requests in 24 h"
                   + (" · ⏸ = idle, could be parked" if any(r[7] for r in rows) else ""))

    # insights
    ins, tips = d.get("insights") or {}, []
    for p, b in sorted((ins.get("attribution_burst") or {}).items()):
        if b.get("confidence") != "ok":
            continue
        top = max((c for c in b["share"] if c != "baseline"), key=lambda c: b["share"][c], default=None)
        if top and b["share"][top] >= 0.25:
            cost = b["cost_per_request"].get(top, 0) * 1000
            tips.append(f"{esc(p)}: {b['share'][top]:.0%} of its CPU is {esc(top)} traffic (~{cost:.0f} ms per request)")
    for p, a in sorted((ins.get("attribution") or {}).items()):
        if a.get("confidence") == "ok" and a["share"].get("baseline", 0) > 0.95 and a.get("mean_cpu", 0) > 0.05:
            tips.append(f"{esc(p)}: {a['mean_cpu']:.2f} cores of background work, independent of visitors")
    oc = ins.get("overcommit") or {}
    if oc.get("current") and oc["current"] > 1:
        tips.append(f"memory limits add up to {oc['current']:.2f}x RAM (recommended {oc['recommended']:.2f}x) → projctl rightsize")
    grow = [r["container"] for r in ins.get("rightsizing", []) if r["decision"] == "grow"]
    if grow:
        tips.append("raise the memory limit of " + esc(", ".join(grow)))
    cands = [p for p, s in d["summary"].items() if s.get("candidate")]
    if cands:
        tips.append("idle, could be parked: " + esc(", ".join(cands)) + " → projctl whatif / park")
    if (h.get("reclaimable") or 0) > 5 * GB:
        tips.append(f"{_gb(h['reclaimable'])} of docker disk reclaimable → projctl cleanup")
    if tips:
        out += ["", "<b>Insights</b>"] + [f"• {t}" for t in tips]
    if grafana_url:
        out += ["", f'<a href="{esc(grafana_url.rstrip("/"))}/d/sm-overview">Open the dashboards</a>']
    return "\n".join(out)


def snapshot(d: dict) -> dict:
    h = d["host"]
    used = (h["mem_total"] - h["mem_avail"]) if h.get("mem_total") and h.get("mem_avail") is not None else None
    return {"ts": d["ts"], "host": {"mem_used": used, "disk_free": h.get("disk_free")},
            "alerts": sorted({a["alertname"] for a in d["alerts"]})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--prometheus", default=None)
    a = ap.parse_args(argv)
    prom = Prom(a.prometheus) if a.prometheus else Prom()
    d = collect(prom)
    last = load_json(LAST_FILE, None)
    text = build(d, last, env("GF_ROOT_URL"))
    if a.dry_run:
        print(text)
        return 0
    try:
        Bot(env("TELEGRAM_BOT_TOKEN")).send(env("TELEGRAM_CHAT_ID"), text)
    except TelegramError as e:
        print(f"telegram: {e}", file=sys.stderr)
        return 1
    atomic_write(LAST_FILE, json.dumps(snapshot(d)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
