"""End-to-end analyzer run against a fake Prometheus HTTP server."""
import json
import math
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from servermon import analyze

MiB = 1024 * 1024


def make_handler(end):
    def matrix(series):
        return {"resultType": "matrix", "result": [{"metric": m, "values": v} for m, v in series]}

    def pts(fn, start, step):
        return [[t, str(fn(t))] for t in range(int(start), int(end) + 1, int(step))]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            e = q["query"]
            if u.path.endswith("query_range"):
                s, st = float(q["start"]), float(q["step"])
                if "project:cpu_cores" in e:
                    data = matrix([({"project": "web"}, pts(lambda t: 0.05 + 0.01 * (2 + math.sin(t / 3000)), s, st))])
                elif "sm_http_requests_total" in e:
                    data = matrix([({"project": "web", "class": "human"}, pts(lambda t: 2 + math.sin(t / 3000), s, st)),
                                   ({"project": "web", "class": "ai_bot"}, pts(lambda t: 5 + math.cos(t / 777), s, st))])
                elif "working_set" in e:
                    data = matrix([({"name": "web-app-1", "project": "web", "service": "app"}, pts(lambda t: 100 * MiB, s, st))])
                else:
                    data = matrix([({"name": "web-app-1"}, pts(lambda t: 0.0, s, st))])
            else:
                if "MemTotal" in e:
                    val = [({}, 8 * 1024 * MiB)]
                elif "memory_limit" in e:
                    val = [({"container": "web-app-1"}, 1024 * MiB)]
                elif "sm_container_up" in e:
                    val = [({"container": "web-app-1"}, 1)]
                else:
                    val = []
                data = {"resultType": "vector", "result": [{"metric": m, "value": [end, str(v)]} for m, v in val]}
            body = json.dumps({"status": "success", "data": data}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
    return H


@pytest.fixture
def fake_prom(monkeypatch, tmp_path):
    import time
    end = time.time() // 300 * 300
    srv = HTTPServer(("127.0.0.1", 0), make_handler(end))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(analyze, "TEXTFILE", tmp_path / "insights.prom")
    monkeypatch.setattr(analyze, "INSIGHTS_FILE", tmp_path / "insights.json")
    yield f"http://127.0.0.1:{srv.server_port}", tmp_path
    srv.shutdown()


def test_analyzer_end_to_end(fake_prom):
    url, tmp = fake_prom
    assert analyze.main(["--prometheus", url, "--lookback", "3d"]) == 0
    report = json.loads((tmp / "insights.json").read_text())
    a = report["attribution"]["web"]
    # cpu = 0.05 + 0.01 * human_rate exactly -> human share = 0.02 / 0.07, crawler share ~ 0
    assert a["share"]["human"] == pytest.approx(0.02 / 0.07, abs=0.01) and a["share"]["ai_bot"] < 0.01
    assert a["naive_share"]["ai_bot"] > 0.6
    r = report["rightsizing"][0]
    assert r["decision"] == "shrink" and r["mem_recommended"] < 300 * MiB
    assert report["overcommit"]["current"] == pytest.approx(1 / 8)
    text = (tmp / "insights.prom").read_text()
    for fam in ("sm_attribution_share", "sm_attribution_naive_share", "sm_rightsize_decision",
                "sm_memory_limit_overcommit_ratio", "sm_analyzer_last_run_timestamp_seconds"):
        assert f"# TYPE {fam} gauge" in text


def test_analyzer_reports_unreachable_prometheus(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "TEXTFILE", tmp_path / "x.prom")
    assert analyze.main(["--prometheus", "http://127.0.0.1:9"]) == 1


def test_parse_duration():
    assert analyze.parse_duration("7d") == 604800 and analyze.parse_duration("90m") == 5400
    assert analyze.parse_duration("120") == 120
