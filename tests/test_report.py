from servermon import report, telegram

GB = 1024 ** 3


def data(**over):
    d = {
        "ts": 1_795_340_000.0,
        "host": {"cpu": 0.31, "cpu24": 0.25, "mem_total": 8 * GB, "mem_avail": 1 * GB, "disk_free": 8 * GB,
                 "disk_size": 75 * GB, "load5": 0.6, "uptime": 13 * 86400, "oom24": 2, "mem_psi": 0.0,
                 "reclaimable": 28 * GB, "reboot": 1, "apt": 4},
        "sites": {"web": 1.0, "api": 0.0},
        "uptime24": {"web": 0.97, "api": 0.2},
        "cpu": {"web": 0.14, "api": 0.03}, "mem": {"web": 0.5 * GB, "api": 3.7 * GB},
        "visitors": {"web": 3}, "bots": {"web": 34000},
        "summary": {"web": {"kind": "app", "running": 2, "idle_days": 0.2},
                    "api": {"kind": "app", "running": 5, "stopped": 1, "idle_days": 9.1, "candidate": True}},
        "alerts": [{"alertname": "SiteDown", "severity": "critical", "project": "api", "container": "", "unit": ""},
                   {"alertname": "RebootRequired", "severity": "info", "project": "", "container": "", "unit": ""}],
        "insights": {
            "attribution_burst": {"web": {"confidence": "ok", "share": {"baseline": 0.17, "ai_bot": 0.83},
                                          "cost_per_request": {"ai_bot": 0.293}}},
            "attribution": {"bg": {"confidence": "ok", "share": {"baseline": 0.99}, "mean_cpu": 0.11}},
            "overcommit": {"current": 2.47, "recommended": 1.52},
            "rightsizing": [{"container": "web-app-1", "decision": "grow"}]},
    }
    d.update(over)
    return d


def test_report_contains_every_section():
    text = report.build(data(), grafana_url="https://g.example/")
    assert text.startswith("🔴 <b>Server status</b>")
    for s in ("CPU 31%", "OOM kill", "reboot required", "🔴 SiteDown: api", "🔵 RebootRequired", "1/2 up · down: api",
              "flaky (24 h): web 97%", "<pre>project", "api                  down", "⏸", "83% of its CPU is ai_bot",
              "293 ms", "bg: 0.11 cores of background work", "2.47x RAM", "web-app-1", "idle, could be parked: api",
              "projctl cleanup", 'href="https://g.example/d/sm-overview"'):
        assert s in text, s
    assert len(text) < telegram.LIMIT
    assert text.count("<pre>") == text.count("</pre>")


def test_green_report_and_changes_since_last():
    d = data(alerts=[], sites={"web": 1.0}, uptime24={"web": 1.0})
    last = {"host": {"mem_used": 6.5 * GB, "disk_free": 9 * GB}, "alerts": ["SiteDown"]}
    text = report.build(d, last)
    assert text.startswith("🟢") and "none firing ✅" in text
    assert "RAM +0.5G" in text and "disk free -1.0G since last report" in text
    assert "href" not in text


def test_new_and_cleared_alerts_are_listed():
    text = report.build(data(), {"host": {}, "alerts": ["DiskAlmostFull", "SiteDown"]})
    assert "new: RebootRequired" in text and "cleared: DiskAlmostFull" in text


def test_names_are_html_escaped():
    d = data(alerts=[{"alertname": "X<y>", "severity": "warning", "project": "a&b", "container": "", "unit": ""}])
    text = report.build(d)
    assert "X&lt;y&gt;" in text and "a&amp;b" in text


def test_snapshot_roundtrip():
    s = report.snapshot(data())
    assert s["host"]["mem_used"] == 7 * GB and s["alerts"] == ["RebootRequired", "SiteDown"]


def test_legend_and_empty_memory():
    d = data(summary={"job": {"kind": "batch", "idle_days": 3}}, mem={}, cpu={})
    text = report.build(d)
    assert "job                  batch    0.00     -" in text and "⏸" not in text
