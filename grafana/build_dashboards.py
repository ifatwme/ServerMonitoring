#!/usr/bin/env python3
"""Generate the Grafana dashboards (grafana/dashboards/*.json) from code.

Run after editing:  python3 grafana/build_dashboards.py   (Grafana reloads them within a minute)
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


class Board:
    def __init__(self, uid, title, description, tags, variables=None, refresh="1m", time_from="now-24h"):
        self.uid, self.title, self.description = uid, title, description
        self.tags, self.vars, self.refresh, self.time_from = tags, variables or [], refresh, time_from
        self.panels, self.y, self.x, self.row_h, self.next_id = [], 0, 0, 0, 1

    def _place(self, w, h):
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title, collapsed=False):
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        self.panels.append({"type": "row", "title": title, "id": self.next_id, "collapsed": collapsed,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.next_id += 1
        self.y += 1

    def add(self, panel, w, h):
        panel["id"] = self.next_id
        self.next_id += 1
        panel["gridPos"] = self._place(w, h)
        panel.setdefault("datasource", DS)
        self.panels.append(panel)

    def json(self):
        return {
            "uid": self.uid, "title": self.title, "description": self.description, "tags": self.tags,
            "timezone": "browser", "editable": False, "graphTooltip": 1, "schemaVersion": 39,
            "refresh": self.refresh, "time": {"from": self.time_from, "to": "now"},
            "templating": {"list": self.vars}, "panels": self.panels, "annotations": {"list": []},
            "links": [{"type": "dashboards", "tags": ["server-monitor"], "asDropdown": False,
                       "title": "Dashboards", "includeVars": False, "keepTime": True}],
        }


def tgt(expr, legend="", ref="A", instant=False, table=False):
    t = {"expr": expr, "legendFormat": legend or "__auto", "refId": ref, "datasource": DS}
    if instant:
        t.update(instant=True, range=False)
    if table:
        t["format"] = "table"
    return t


def thresholds(*steps):
    """steps: (value|None, color) ..."""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


def stat(title, expr, unit="short", th=None, desc="", mappings=None, decimals=None, color_mode="value",
         legend="", text_mode="auto", reduce="lastNotNull"):
    d = {"unit": unit, "thresholds": th or thresholds((None, "green")), "mappings": mappings or []}
    if decimals is not None:
        d["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "targets": [tgt(expr, legend)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"colorMode": color_mode, "graphMode": "none", "textMode": text_mode, "justifyMode": "center",
                        "reduceOptions": {"calcs": [reduce], "fields": "", "values": False}}}


def ts(title, targets, unit="short", stack=False, desc="", fill=10, overrides=None, max_=None, min_=0, legend_table=True):
    d = {"unit": unit, "custom": {"fillOpacity": 60 if stack else fill, "lineWidth": 1, "showPoints": "never",
                                  "stacking": {"mode": "normal" if stack else "none"}, "spanNulls": 120000},
         "min": min_}
    if max_ is not None:
        d["max"] = max_
    return {"type": "timeseries", "title": title, "description": desc, "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "table" if legend_table else "list", "placement": "right",
                                   "calcs": ["mean", "max", "lastNotNull"] if legend_table else []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def bargauge(title, expr, unit="short", legend="{{project}}", th=None, desc="", max_=None, orientation="horizontal"):
    d = {"unit": unit, "thresholds": th or thresholds((None, "blue")), "min": 0}
    if max_ is not None:
        d["max"] = max_
    return {"type": "bargauge", "title": title, "description": desc,
            "targets": [tgt(expr, legend, instant=True)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"orientation": orientation, "displayMode": "gradient", "showUnfilled": True,
                        "valueMode": "color", "namePlacement": "left", "sizing": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def col(name, unit=None, th=None, mappings=None, cell=None, width=None, decimals=None, display=None):
    props = []
    if unit:
        props.append({"id": "unit", "value": unit})
    if th:
        props.append({"id": "thresholds", "value": th})
    if mappings:
        props.append({"id": "mappings", "value": mappings})
    if cell:
        props.append({"id": "custom.cellOptions", "value": {"type": cell}})
    if width:
        props.append({"id": "custom.width", "value": width})
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    if display:
        props.append({"id": "displayName", "value": display})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def table(title, queries, rename, overrides=None, desc="", sort=None, hide=None, join="merge", width=None):
    """queries: list of (refId, expr). Instant table queries merged on their common labels."""
    exclude = {"Time": True, "__name__": True, "job": True, "instance": True, **{h: True for h in (hide or [])}}
    tr = [{"id": join, "options": {}} if join == "merge" else {"id": "joinByField", "options": {"byField": join, "mode": "outer"}},
          {"id": "organize", "options": {"excludeByName": exclude, "renameByName": rename,
                                         "indexByName": {k: i for i, k in enumerate(rename)}}}]
    if sort:
        tr.append({"id": "sortBy", "options": {"sort": [{"field": sort[0], "desc": sort[1]}]}})
    return {"type": "table", "title": title, "description": desc,
            "targets": [tgt(e, ref=r, instant=True, table=True) for r, e in queries],
            "transformations": tr,
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": False, **({"width": width} if width else {})}},
                            "overrides": overrides or []},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}


UPDOWN = [{"type": "value", "options": {"1": {"text": "UP", "color": "green"}, "0": {"text": "DOWN", "color": "red"}}}]
YESNO_BAD = [{"type": "value", "options": {"1": {"text": "YES", "color": "orange"}, "0": {"text": "no", "color": "text"}}}]
PCT_TH = thresholds((None, "red"), (0.95, "orange"), (0.99, "green"))
NOTPARKED = 'unless on (project) sm_project_parked == 1'

# ═════════════════════════════════════════════════════════════════════════════
# 1. Overview
# ═════════════════════════════════════════════════════════════════════════════

def overview():
    b = Board("sm-overview", "Server overview", "Host health and every project's share of it.", ["server-monitor"])
    b.row("Host")
    b.add(stat("CPU busy", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', "percentunit",
               thresholds((None, "green"), (0.7, "orange"), (0.9, "red")), decimals=0), 3, 4)
    b.add(stat("RAM used", "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", "percentunit",
               thresholds((None, "green"), (0.8, "orange"), (0.9, "red")), decimals=0,
               desc="No swap on this host: above ~90 % the kernel starts OOM-killing."), 3, 4)
    b.add(stat("RAM available", "node_memory_MemAvailable_bytes", "bytes",
               thresholds((None, "red"), (0.8e9, "orange"), (1.5e9, "green"))), 3, 4)
    b.add(stat("Disk / used", '1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}',
               "percentunit", thresholds((None, "green"), (0.8, "orange"), (0.9, "red")), decimals=0), 3, 4)
    b.add(stat("Load / cores", 'node_load5 / scalar(count(node_cpu_seconds_total{mode="idle"}))', "percentunit",
               thresholds((None, "green"), (0.8, "orange"), (1.2, "red")), decimals=0), 3, 4)
    b.add(stat("OOM kills 24h", "increase(node_vmstat_oom_kill[24h])", "short",
               thresholds((None, "green"), (1, "red")), decimals=0), 3, 4)
    b.add(stat("Sites up", 'sum(probe_success{job="uptime"}) / count(probe_success{job="uptime"})', "percentunit",
               thresholds((None, "red"), (0.99, "green")), decimals=0), 3, 4)
    b.add(stat("Alerts firing", 'count(ALERTS{alertstate="firing",severity!="info"}) or vector(0)', "short",
               thresholds((None, "green"), (1, "red")), decimals=0), 3, 4)

    b.add(ts("CPU by project (cores)", [tgt("project:cpu_cores:rate2m", "{{project}}")], "short", stack=True,
             desc="Stacked, in cores. 'other' = containers without a compose project."), 12, 9)
    b.add(ts("Memory by project (working set)", [
        tgt("project:memory_working_set_bytes", "{{project}}"),
    ], "bytes", stack=True, desc="Working set = what the kernel can't simply drop (compare with the host RAM gauge)."), 12, 9)
    b.add(ts("Host memory", [
        tgt("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "used", "A"),
        tgt("node_memory_Cached_bytes + node_memory_Buffers_bytes", "page cache", "B"),
        tgt("node_memory_MemAvailable_bytes", "available", "C"),
    ], "bytes"), 8, 8)
    b.add(ts("Pressure stall (PSI): share of time tasks waited", [
        tgt("rate(node_pressure_cpu_waiting_seconds_total[5m])", "cpu", "A"),
        tgt("rate(node_pressure_memory_waiting_seconds_total[5m])", "memory", "B"),
        tgt("rate(node_pressure_io_waiting_seconds_total[5m])", "io", "C"),
    ], "percentunit", desc="The best 'is the box overloaded' signal: >10 % memory or io stall hurts latency."), 8, 8)
    b.add(ts("Host network + disk I/O", [
        tgt('sum(rate(node_network_receive_bytes_total{device="eth0"}[5m]))', "net in", "A"),
        tgt('sum(rate(node_network_transmit_bytes_total{device="eth0"}[5m]))', "net out", "B"),
        tgt('sum(rate(node_disk_read_bytes_total[5m]))', "disk read", "C"),
        tgt('sum(rate(node_disk_written_bytes_total[5m]))', "disk write", "D"),
    ], "Bps"), 8, 8)

    b.row("Projects")
    def M(e):
        return f"max by (project) ({e})"
    b.add(table("Projects at a glance", [
        ("A", M('probe_success{job="uptime"}')),
        ("B", M("project:probe_success:avg7d")),
        ("C", M("avg_over_time(project:cpu_cores:rate2m[24h])")),
        ("D", M("project:memory_working_set_bytes")),
        ("E", "sum by (project) (sm_project_disk_bytes)"),
        ("F", M('sm_http_unique_clients_window{class="engaged",window="7d"}')),
        ("G", 'sum by (project) (sm_http_requests_window{class=~"ai_bot|bot",window="24h"})'),
        ("H", M("sm_project_idle_seconds")),
        ("I", M('sm_project_containers{state="stopped"}')),
        ("J", M("sm_project_parked * 2 + sm_project_park_candidate")),
        ("K", M("project:tls_expiry_days")),
    ], {"project": "Project", "Value #A": "Site", "Value #B": "Uptime 7d", "Value #C": "CPU 24h avg",
        "Value #D": "RAM", "Value #E": "Disk", "Value #F": "Visitors 7d", "Value #G": "Bot req 24h",
        "Value #H": "Idle", "Value #I": "Stopped ctrs", "Value #J": "Status", "Value #K": "TLS days"},
        [col("Project", width=200), col("Site", mappings=UPDOWN, cell="color-background", width=70),
         col("Uptime 7d", "percentunit", PCT_TH, cell="color-text", decimals=2),
         col("CPU 24h avg", "short", thresholds((None, "text"), (0.25, "orange"), (1, "red")), cell="color-text", decimals=2),
         col("RAM", "bytes", thresholds((None, "text"), (5e8, "orange"), (2e9, "red")), cell="color-text"),
         col("Disk", "bytes"),
         col("Visitors 7d", decimals=0),
         col("Bot req 24h", decimals=0, th=thresholds((None, "text"), (5000, "orange")), cell="color-text"),
         col("Idle", "s", thresholds((None, "text"), (3 * 86400, "orange"), (7 * 86400, "red")), cell="color-text"),
         col("Stopped ctrs", decimals=0, th=thresholds((None, "text"), (1, "red")), cell="color-text"),
         col("Status", mappings=[{"type": "value", "options": {"0": {"text": "active", "color": "green"},
                                                                 "1": {"text": "idle: park?", "color": "orange"},
                                                                 "2": {"text": "parked", "color": "blue"},
                                                                 "3": {"text": "parked", "color": "blue"}}}],
             cell="color-text"),
         col("TLS days", decimals=0, th=thresholds((None, "red"), (14, "orange"), (30, "green")), cell="color-text")],
        desc="Visitors = distinct IPs that loaded a page AND its assets (real browsers). Idle = time since the newest "
             "of: engaged visit, commit, uncommitted edit, image build, container start (tests).",
        sort=("RAM", True), width=118), 24, 10)
    b.add(bargauge("RAM share", "project:memory_share", "percentunit", max_=1), 8, 8)
    b.add(bargauge("CPU share (24 h avg)", "avg_over_time(project:cpu_cores:rate2m[24h]) / scalar(sum(avg_over_time(project:cpu_cores:rate2m[24h])))",
                   "percentunit", max_=1), 8, 8)
    b.add(table("Firing alerts", [("A", 'ALERTS{alertstate="firing"}')],
                {"alertname": "Alert", "severity": "Severity", "project": "Project", "container": "Container",
                 "unit": "Unit", "name": "Name"},
                [col("Alert", width=170), col("Severity", width=75), col("Project", width=150),
                 col("Severity", mappings=[{"type": "value", "options": {
                    "critical": {"color": "red"}, "warning": {"color": "orange"}, "info": {"color": "blue"}}}],
                     cell="color-text")],
                hide=["alertstate", "Value", "url", "kind", "class", "window", "service", "state", "health", "image", "signal", "code"]), 8, 8)

    b.row("Uptime")
    b.add({"type": "state-timeline", "title": "Public URLs (blackbox probe every 60 s)",
           "targets": [tgt('probe_success{job="uptime"}', "{{project}}")],
           "fieldConfig": {"defaults": {"mappings": UPDOWN, "thresholds": thresholds((None, "red"), (1, "green")),
                                        "color": {"mode": "thresholds"}}, "overrides": []},
           "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                       "legend": {"showLegend": False}}}, 16, 8)
    b.add(ts("Response time (probe)", [tgt('probe_duration_seconds{job="uptime"}', "{{project}}")], "s",
             legend_table=False), 8, 8)
    return b


# ═════════════════════════════════════════════════════════════════════════════
# 2. Project detail
# ═════════════════════════════════════════════════════════════════════════════

def project():
    var = [{"name": "project", "label": "Project", "type": "query", "datasource": DS,
            "query": {"query": "label_values(sm_project_info, project)", "refId": "v"},
            "definition": "label_values(sm_project_info, project)", "refresh": 2, "current": {},
            "includeAll": False, "multi": False, "sort": 1}]
    P = 'project="$project"'
    b = Board("sm-project", "Project detail", "Everything about one project.", ["server-monitor"], var)
    b.row("Status")
    b.add(stat("Site", f'max(probe_success{{job="uptime",{P}}})', mappings=UPDOWN,
               th=thresholds((None, "red"), (1, "green")), color_mode="background"), 3, 4)
    b.add(stat("Uptime (range)", f'avg_over_time(probe_success{{job="uptime",{P}}}[$__range])', "percentunit",
               PCT_TH, decimals=2), 3, 4)
    b.add(stat("TLS days left", f'project:tls_expiry_days{{{P}}}', "none",
               thresholds((None, "red"), (14, "orange"), (30, "green")), decimals=0), 3, 4)
    b.add(stat("Containers running", f'sm_project_containers{{{P},state="running"}}', decimals=0), 3, 4)
    b.add(stat("Containers stopped", f'sm_project_containers{{{P},state="stopped"}}',
               th=thresholds((None, "green"), (1, "red")), decimals=0), 3, 4)
    b.add(stat("Idle for", f'sm_project_idle_seconds{{{P}}}', "s",
               thresholds((None, "green"), (3 * 86400, "orange"), (7 * 86400, "red"))), 3, 4)
    b.add(stat("Park candidate", f'sm_project_park_candidate{{{P}}}', mappings=YESNO_BAD,
               desc="Running but no engaged visitor, commit, edit, build or test run within the idle threshold."), 3, 4)
    b.add(stat("RAM now", f'project:memory_working_set_bytes{{{P}}}', "bytes"), 3, 4)
    b.add(table("Last activity", [("A", f'sm_project_last_activity_timestamp_seconds{{{P}}} * 1000')],
                {"signal": "Signal", "Value": "When"}, [col("When", "dateTimeFromNow")], hide=["project"],
                desc="commit / edit (uncommitted or dev-dir files) / build (newest project image) / "
                     "container_start (incl. `compose run` tests) / human_visit (engaged browser)"), 5, 9)
    b.add(table("Containers", [
        ("A", f'sm_container_info{{{P}}}'), ("B", f'sm_container_restart_count{{{P}}}'),
        ("C", f'(time() - sm_container_started_timestamp_seconds{{{P}}}) * sm_container_up{{{P}}}'),
        ("D", f'sm_container_memory_limit_bytes{{{P}}}'), ("E", f'sm_container_oom_killed{{{P}}}')],
        {"service": "Service", "state": "State", "health": "Health", "Value #B": "Restarts", "Value #C": "Up for",
         "Value #D": "Mem limit", "Value #E": "Last exit OOM", "image": "Image"},
        [col("State", mappings=[{"type": "value", "options": {"running": {"color": "green"}, "exited": {"color": "red"}}}],
             cell="color-text"),
         col("Health", mappings=[{"type": "value", "options": {"healthy": {"color": "green"}, "unhealthy": {"color": "red"},
                                                                "none": {"color": "text"}}}], cell="color-text"),
         col("Up for", "s"), col("Mem limit", "bytes"), col("Last exit OOM", mappings=YESNO_BAD)],
        hide=["project", "container", "Value #A"], join="service"), 10, 9)
    b.add(table("Jobs / units", [
        ("A", f'sm_unit_last_success{{{P}}}'), ("B", f'sm_unit_last_start_timestamp_seconds{{{P}}} * 1000'),
        ("C", f'sm_unit_last_duration_seconds{{{P}}}'), ("D", f'sm_unit_enabled{{{P}}}')],
        {"unit": "Unit", "Value #A": "Last run", "Value #B": "Started", "Value #C": "Took", "Value #D": "Enabled"},
        [col("Last run", mappings=[{"type": "value", "options": {"1": {"text": "ok", "color": "green"},
                                                                  "0": {"text": "FAILED", "color": "red"}}}], cell="color-text"),
         col("Started", "dateTimeFromNow"), col("Took", "s"),
         col("Enabled", mappings=[{"type": "value", "options": {"1": {"text": "yes"}, "0": {"text": "no"}}}])],
        hide=["project"], join="unit"), 9, 9)

    b.row("Resources")
    b.add(ts("CPU per service (cores)", [tgt(f'service:cpu_cores:rate2m{{{P}}}', "{{service}}")], "short", stack=True), 12, 8)
    b.add(ts("Memory per service (working set) vs limit", [
        tgt(f'service:memory_working_set_bytes{{{P}}}', "{{service}}", "A"),
        tgt(f'sm_container_memory_limit_bytes{{{P}}} * sm_container_up{{{P}}}', "limit {{service}}", "B"),
    ], "bytes", overrides=[{"matcher": {"id": "byFrameRefID", "options": "B"},
                            "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}},
                                           {"id": "custom.fillOpacity", "value": 0}]}]), 12, 8)
    b.add(ts("Network (bytes/s)", [
        tgt(f'project:network_receive_bytes:rate5m{{{P}}}', "in", "A"),
        tgt(f'project:network_transmit_bytes:rate5m{{{P}}}', "out", "B")], "Bps", legend_table=False), 8, 7)
    b.add(ts("Disk I/O (bytes/s)", [tgt(f'project:disk_io_bytes:rate5m{{{P}}}', "read+write")], "Bps", legend_table=False), 8, 7)
    b.add(ts("Stall per service (PSI: share of time waiting)", [
        tgt(f'sum by (service) (rate(container_pressure_cpu_waiting_seconds_total{{{P}}}[5m]))', "cpu {{service}}", "A"),
        tgt(f'sum by (service) (rate(container_pressure_memory_waiting_seconds_total{{{P}}}[5m]))', "mem {{service}}", "B"),
        tgt(f'sum by (service) (rate(container_pressure_io_waiting_seconds_total{{{P}}}[5m]))', "io {{service}}", "C"),
    ], "percentunit", legend_table=False, desc="Throttling by the cpus: limit shows up as cpu stall."), 8, 7)
    b.add(bargauge("Disk", f'sm_project_disk_bytes{{{P}}}', "bytes", "{{kind}}",
                   desc="repo = deployed dir (incl. data), dev = dev/archive dirs, images = project images "
                        "(shared layers counted per image), volumes = docker volumes"), 8, 6)
    b.add(table("Images", [("A", f'max by (image, in_use) (sm_docker_image_bytes{{{P}}})'),
                           ("B", f'max by (image, in_use) (sm_docker_image_created_timestamp_seconds{{{P}}} * 1000 * on (image) group_left (in_use) (0 * sm_docker_image_bytes{{{P}}} + 1))')],
                {"image": "Image", "in_use": "In use", "Value #A": "Size", "Value #B": "Built"},
                [col("Size", "bytes"), col("Built", "dateTimeFromNow"),
                 col("In use", mappings=[{"type": "value", "options": {"1": {"text": "yes", "color": "green"},
                                                                        "0": {"text": "unused", "color": "orange"}}}],
                     cell="color-text")],
                sort=("Size", True)), 16, 6)

    b.row("Traffic (nginx access log)")
    b.add(ts("Requests/min by client class", [
        tgt(f'sum by (class) (rate(sm_http_requests_total{{{P}}}[5m])) * 60', "{{class}}")], "short", stack=True,
        desc="human = browser-like UA that is not a known bot/scanner; ai_bot = GPTBot, ClaudeBot, ...; "
             "scanner = exploit probes (/.env, /wp-, odd methods); probe = this monitor + healthchecks"), 12, 8)
    b.add(ts("Responses/min by status", [
        tgt(f'sum by (code) (rate(sm_http_requests_total{{{P}}}[5m])) * 60', "{{code}}")], "short", stack=True), 12, 8)
    b.add(table("Who is calling (last 24 h)", [("A", f'sm_http_agent_requests_24h{{{P}}}')],
                {"agent": "Agent", "class": "Class", "Value": "Requests"}, hide=["project"],
                sort=("Requests", True)), 8, 8)
    b.add(bargauge("Requests by class (24 h)", f'sm_http_requests_window{{{P},window="24h"}}', "short", "{{class}}"), 8, 8)
    b.add(bargauge("Distinct clients (7 d)", f'sm_http_unique_clients_window{{{P},window="7d"}}', "short", "{{class}}",
                   desc="engaged = loaded a page and its assets: the best estimate of real people"), 8, 8)
    b.add(ts("Probe response time", [tgt(f'probe_duration_seconds{{job="uptime",{P}}}', "total")], "s",
             legend_table=False), 12, 6)
    b.add({"type": "state-timeline", "title": "Availability",
           "targets": [tgt(f'probe_success{{job="uptime",{P}}}', "site"),
                       tgt(f'min(sm_container_up{{{P}}})', "all containers up", "B")],
           "fieldConfig": {"defaults": {"mappings": UPDOWN, "thresholds": thresholds((None, "red"), (1, "green")),
                                        "color": {"mode": "thresholds"}}, "overrides": []},
           "options": {"showValue": "never", "mergeValues": True, "legend": {"showLegend": False}}}, 12, 6)
    return b


# ═════════════════════════════════════════════════════════════════════════════
# 3. Waste & recommendations
# ═════════════════════════════════════════════════════════════════════════════

def waste():
    b = Board("sm-waste", "Waste & recommendations", "What uses resources without giving anything back.",
              ["server-monitor"], time_from="now-7d")
    b.row("Park candidates  →  `projctl suggest` / `projctl park <project> -y`")
    b.add(table("Idle projects that still hold resources", [
        ("A", "sm_project_park_candidate == 1"),
        ("B", "project:memory_working_set_bytes and on (project) sm_project_park_candidate == 1"),
        ("C", "avg_over_time(project:cpu_cores:rate2m[24h]) and on (project) sm_project_park_candidate == 1"),
        ("D", "sm_project_idle_seconds and on (project) sm_project_park_candidate == 1"),
        ("E", 'sm_http_unique_clients_window{class="engaged",window="7d"} and on (project) sm_project_park_candidate == 1'),
    ], {"project": "Project", "Value #B": "RAM freed", "Value #C": "CPU freed (24h avg)", "Value #D": "Idle for",
        "Value #E": "Visitors 7d"},
        [col("RAM freed", "bytes"), col("CPU freed (24h avg)", decimals=2), col("Idle for", "s")],
        hide=["Value #A"], desc="Empty = nothing idle past its threshold (idle_days in config/projects.toml)."), 12, 6)
    b.add(bargauge("CPU core-seconds per real visitor (24 h)", "project:cpu_core_seconds_per_engaged_visitor:24h", "s",
                   desc="How much CPU a project burned per engaged visitor yesterday. High = background work "
                        "nobody is looking at (demo loops, crawlers, idle model servers).",
                   th=thresholds((None, "green"), (600, "orange"), (3600, "red"))), 12, 6)

    b.row("Heavy but quiet")
    LIM = 'max by (project, service, name) (label_replace(sm_container_memory_limit_bytes, "name", "$1", "container", "(.*)"))'
    b.add(table("Containers: memory held vs. CPU used (7 d)", [
        ("A", "max by (project, service, name) (service:memory_working_set_bytes)"),
        ("B", "max by (project, service, name) (avg_over_time(service:cpu_cores:rate2m[7d]))"),
        ("C", LIM),
        ("D", f"max by (project, service, name) (service:memory_working_set_bytes) / {LIM}"),
    ], {"name": "Container", "project": "Project", "Value #A": "RAM now",
        "Value #B": "CPU 7d avg", "Value #C": "Limit", "Value #D": "RAM / limit"},
        [col("Container", width=230), col("Project", width=150),
         col("RAM now", "bytes", thresholds((None, "text"), (3e8, "orange"), (1e9, "red")), cell="color-text"),
         col("CPU 7d avg", decimals=3, th=thresholds((None, "text"), (0.1, "orange"), (0.5, "red")), cell="color-text"),
         col("Limit", "bytes"),
         col("RAM / limit", "percentunit", thresholds((None, "green"), (0.8, "orange"), (0.9, "red")), cell="gauge")],
        hide=["service"], sort=("RAM now", True),
        desc="Big RAM + ~0 CPU = a loaded model or cache nobody is using. Near 100 % of limit = OOM risk."), 14, 10)
    b.add({"type": "barchart", "title": "Traffic mix, last 24 h (requests)",
           "description": "AI crawlers can dominate the CPU of dynamic sites; consider robots.txt / rate limits for them.",
           "targets": [tgt('sum by (project, class) (sm_http_requests_window{window="24h",class!="pageview"})', ref="A",
                           instant=True, table=True)],
           "transformations": [{"id": "groupingToMatrix", "options": {"rowField": "project", "columnField": "class",
                                                                       "valueField": "Value", "emptyValue": "zero"}}],
           "fieldConfig": {"defaults": {"unit": "short", "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
           "options": {"orientation": "horizontal", "stacking": "normal", "showValue": "never", "barWidth": 0.7,
                       "xTickLabelRotation": 0, "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                       "tooltip": {"mode": "multi"}}}, 10, 10)
    b.add(ts("CPU of each project vs. its real visitors", [
        tgt("project:cpu_cores:rate2m", "{{project}} cpu", "A"),
    ], "short", desc="Compare with the visitor counts on the overview; CPU that does not follow traffic is background work."), 24, 7)

    b.row("Disk")
    b.add(stat("Build cache reclaimable", 'sm_docker_reclaimable_bytes{type="build_cache"}', "bytes",
               thresholds((None, "green"), (5e9, "orange"), (15e9, "red"))), 4, 4)
    b.add(stat("Images reclaimable", 'sm_docker_reclaimable_bytes{type="images"}', "bytes",
               thresholds((None, "green"), (5e9, "orange"), (10e9, "red"))), 4, 4)
    b.add(stat("Disk free", 'node_filesystem_avail_bytes{mountpoint="/"}', "bytes",
               thresholds((None, "red"), (8e9, "orange"), (15e9, "green"))), 4, 4)
    b.add(stat("Prometheus TSDB", 'prometheus_tsdb_storage_blocks_bytes + prometheus_tsdb_wal_storage_size_bytes', "bytes"), 4, 4)
    b.add(stat("Pending apt upgrades", "sm_apt_upgradable_packages", decimals=0,
               th=thresholds((None, "green"), (10, "orange"))), 4, 4)
    b.add(stat("Reboot required", "sm_host_reboot_required", mappings=YESNO_BAD), 4, 4)
    b.add(table("Images no container uses", [("A", 'max by (image, project) (sm_docker_image_bytes{in_use="0"})'),
                                             ("B", 'max by (image, project) (sm_docker_image_created_timestamp_seconds * 1000 and on (image) sm_docker_image_bytes{in_use="0"})')],
                {"image": "Image", "project": "Project", "Value #A": "Size", "Value #B": "Built"},
                [col("Image", width=330), col("Size", "bytes"), col("Built", "dateTimeFromNow")],
                sort=("Size", True), desc="`projctl cleanup` removes them (dry-run first)."), 12, 10)
    b.add(bargauge("Disk per project", "sum by (project) (sm_project_disk_bytes)", "bytes"), 12, 10)
    b.add(ts("Root filesystem free", [tgt('node_filesystem_avail_bytes{mountpoint="/"}', "free"),
                                      tgt('predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[24h], 7*86400)', "in 7 days (trend)", "B")],
             "bytes", legend_table=False), 24, 6)
    return b


# ═════════════════════════════════════════════════════════════════════════════
# 4. Insights: CPU attribution + right-sizing (servermon.analyze / servermon.burst)
# ═════════════════════════════════════════════════════════════════════════════

COMP_COLORS = {"baseline": "#8e8e8e", "human": "green", "ai_bot": "red", "bot": "orange", "scanner": "purple"}


def insights():
    var = [{"name": "method", "label": "Attribution model", "type": "custom", "query": "burst,5m",
            "current": {"text": "burst", "value": "burst"},
            "options": [{"text": "burst", "value": "burst", "selected": True}, {"text": "5m", "value": "5m", "selected": False}]}]
    b = Board("sm-insights", "Insights: who uses the CPU, what the limits should be",
              "Regression-based CPU attribution to traffic classes and PSI/OOM-aware right-sizing.",
              ["server-monitor"], var, refresh="5m", time_from="now-7d")
    b.row("CPU attribution: what each project's CPU is actually spent on")
    color_over = [{"matcher": {"id": "byName", "options": k},
                   "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": v}}]} for k, v in COMP_COLORS.items()]
    b.add({"type": "barchart", "title": "CPU share by cause (regression)",
           "description": "baseline = CPU burned regardless of traffic (workers, demo loops, idle models). Other bars = CPU "
                          "explained by each traffic class. method=burst uses 1 Hz samples (identifies steady crawlers); "
                          "method=5m uses Prometheus history.",
           "targets": [tgt('sm_attribution_share{method="$method"}', ref="A", instant=True, table=True)],
           "transformations": [{"id": "groupingToMatrix", "options": {"rowField": "project", "columnField": "component",
                                                                       "valueField": "Value", "emptyValue": "zero"}}],
           "fieldConfig": {"defaults": {"unit": "percentunit", "max": 1, "custom": {"fillOpacity": 85, "lineWidth": 0}},
                           "overrides": color_over},
           "options": {"orientation": "horizontal", "stacking": "normal", "showValue": "never", "barWidth": 0.7,
                       "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                       "tooltip": {"mode": "multi"}}}, 12, 9)
    b.add({"type": "barchart", "title": "...what request-share allocation would claim",
           "description": "Share of requests per class. Most crawler-cost reports allocate CPU like this; it ignores background "
                          "load and assumes every request costs the same.",
           "targets": [tgt("sm_attribution_naive_share", ref="A", instant=True, table=True)],
           "transformations": [{"id": "groupingToMatrix", "options": {"rowField": "project", "columnField": "component",
                                                                       "valueField": "Value", "emptyValue": "zero"}}],
           "fieldConfig": {"defaults": {"unit": "percentunit", "max": 1, "custom": {"fillOpacity": 85, "lineWidth": 0}},
                           "overrides": color_over},
           "options": {"orientation": "horizontal", "stacking": "normal", "showValue": "never", "barWidth": 0.7,
                       "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                       "tooltip": {"mode": "multi"}}}, 12, 9)
    b.add(table("Attribution detail", [
        ("A", 'max by (project, component) (sm_attribution_share{method="$method"})'),
        ("B", 'max by (project, component) (sm_attribution_share_ci_low{method="$method"})'),
        ("C", 'max by (project, component) (sm_attribution_share_ci_high{method="$method"})'),
        ("D", 'max by (project, component) (sm_attribution_cores{method="$method"})'),
    ], {"project": "Project", "component": "Cause", "Value #A": "CPU share", "Value #B": "90% CI low",
        "Value #C": "90% CI high", "Value #D": "Cores"},
        [col("Project", width=125), col("Cause", width=75),
         col("CPU share", "percentunit", decimals=1, cell="gauge", th=thresholds((None, "blue")), width=150),
         col("90% CI low", "percentunit", decimals=0, width=85), col("90% CI high", "percentunit", decimals=0, width=85),
         col("Cores", decimals=3, width=65)], sort=("Cores", True),
        desc="Shares with moving-block / per-burst bootstrap intervals."), 10, 10)
    b.add(table("Model quality", [
        ("A", 'max by (project, method) (sm_attribution_r2)'),
        ("B", 'max by (project, method) (sm_attribution_samples)'),
        ("C", 'max by (project, method, confidence) (sm_attribution_confident)'),
    ], {"project": "Project", "method": "Model", "confidence": "Confidence", "Value #A": "R²", "Value #B": "Samples"},
        [col("Project", width=130), col("Model", width=60), col("R²", decimals=2, width=55), col("Samples", decimals=0, width=75),
         col("Confidence", mappings=[{"type": "value", "options": {
             "ok": {"color": "green"}, "confounded": {"color": "orange", "text": "confounded (steady traffic)"},
             "low": {"color": "orange"}, "insufficient-data": {"color": "text", "text": "collecting data"}}}],
             cell="color-text")],
        hide=["Value #C"], desc="confounded = the 5-min model cannot separate a steady traffic class from the baseline "
                                "(see max hidden share); use the burst model."), 8, 10)
    b.add(table("Cost per request", [("A", 'max by (project, class) (sm_attribution_cpu_ms_per_request{method="$method"})')],
                {"project": "Project", "class": "Class", "Value": "CPU ms / request"},
                [col("Project", width=120), col("Class", width=60),
                 col("CPU ms / request", decimals=1, th=thresholds((None, "text"), (50, "orange"), (200, "red")),
                     cell="color-text")], sort=("CPU ms / request", True),
                desc="Marginal CPU per request; only classes whose share interval excludes zero."), 6, 10)
    b.add(bargauge("5-min model: share a class could be hiding in the baseline", "sm_attribution_max_hidden_share",
                   "percentunit", "{{project}} · {{class}}", max_=1,
                   th=thresholds((None, "green"), (0.1, "orange"), (0.25, "red")),
                   desc="Identifiability check: estimate + 2 standard errors. Red next to a ~0 estimate = steady traffic "
                        "that only 1 Hz burst sampling can separate."), 24, 7)

    b.row("Right-sizing: memory limits from decayed peaks, OOM events and PSI")
    b.add(stat("Memory limits / host RAM (now)", 'sm_memory_limit_overcommit_ratio{limits="current"}', "x",
               thresholds((None, "green"), (1, "orange"), (1.5, "red")), decimals=2,
               desc="Sum of container memory limits / host RAM. > 1: the limits cannot all be honoured, so they do not "
                    "protect the host from OOM."), 6, 5)
    b.add(stat("... with the recommended limits", 'sm_memory_limit_overcommit_ratio{limits="recommended"}', "x",
               thresholds((None, "green"), (1, "orange"), (1.5, "red")), decimals=2), 6, 5)
    b.add(stat("Limit budget released by shrinking",
               'sum(sm_rightsize_memory_limit_bytes - on (container) group_left sm_rightsize_memory_recommended_bytes '
               'and on (container) sm_rightsize_decision{decision="shrink"})', "bytes"), 6, 5)
    b.add(stat("Containers to grow (OOM / near limit)", 'count(sm_rightsize_decision{decision="grow"}) or vector(0)',
               th=thresholds((None, "green"), (1, "red")), decimals=0), 6, 5)
    b.add(table("Recommendations  →  `projctl rightsize --write DIR` writes compose overrides", [
        ("A", 'max by (project, container, decision) (sm_rightsize_decision)'),
        ("B", 'max by (project, container) (sm_rightsize_memory_limit_bytes)'),
        ("C", 'max by (project, container) (sm_rightsize_memory_peak_24h_bytes)'),
        ("D", 'max by (project, container) (sm_rightsize_memory_target_bytes)'),
        ("E", 'max by (project, container) (sm_rightsize_memory_recommended_bytes)'),
        ("F", 'max by (project, container) (sm_rightsize_memory_psi_share)'),
        ("G", 'max by (project, container) (sm_rightsize_cpu_throttled_share)'),
        ("H", 'max by (project, container) (sm_rightsize_history_days)'),
    ], {"container": "Container", "project": "Project", "decision": "Decision", "Value #B": "Limit", "Value #C": "Peak 24h",
        "Value #D": "P98 (decayed)", "Value #E": "Recommended", "Value #F": "Mem stall", "Value #G": "CPU throttled",
        "Value #H": "History"},
        [col("Container", width=260),
         col("Decision", mappings=[{"type": "value", "options": {
             "shrink": {"color": "blue"}, "grow": {"color": "red"}, "ok": {"color": "green"},
             "set-limit": {"color": "orange"}, "insufficient-data": {"color": "text"}}}], cell="color-text"),
         col("Limit", "bytes"), col("Peak 24h", "bytes"), col("P98 (decayed)", "bytes"), col("Recommended", "bytes"),
         col("Mem stall", "percentunit", decimals=1, th=thresholds((None, "text"), (0.005, "orange")), cell="color-text"),
         col("CPU throttled", "percentunit", decimals=0, th=thresholds((None, "text"), (0.1, "orange")), cell="color-text"),
         col("History", "d", decimals=1)],
        hide=["Value #A"], sort=("Limit", True)), 24, 12)
    return b


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, fn in (("overview", overview), ("project", project), ("waste", waste), ("insights", insights)):
        (OUT / f"{name}.json").write_text(json.dumps(fn().json(), indent=1))
        print("wrote", OUT / f"{name}.json")
