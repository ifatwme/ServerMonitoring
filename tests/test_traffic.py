import pytest

from servermon import traffic
from servermon.common import Out

T0 = "2026-11-20T10:00:00.000000000Z"
UA_BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def line(ts, ip, req, status=200, ua=UA_BROWSER, size=512):
    return f'{ts} {ip} - - [20/Nov/2026:10:00:00 +0000] "{req}" {status} {size} "-" "{ua}" "-"'


def ts(sec):
    return f"2026-11-20T10:{sec // 60:02d}:{sec % 60:02d}.000000000Z"


@pytest.mark.parametrize("req,ua,expected", [
    ("GET / HTTP/2.0", UA_BROWSER, "human"),
    ("GET /paper/1 HTTP/2.0", "Mozilla/5.0 AppleWebKit/537.36 (compatible; GPTBot/1.4)", "ai_bot"),
    ("GET / HTTP/1.1", "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)", "ai_bot"),
    ("GET / HTTP/1.1", "meta-externalagent/1.1", "ai_bot"),
    ("GET / HTTP/1.1", "Mozilla/5.0 (compatible; bingbot/2.0)", "bot"),
    ("GET / HTTP/1.1", "curl/8.5.0", "bot"),
    ("GET / HTTP/1.1", "", "bot"),
    ("GET /.env HTTP/1.1", UA_BROWSER, "scanner"),
    ("GET /wp-login.php HTTP/1.1", UA_BROWSER, "scanner"),
    ("PROPFIND / HTTP/1.1", UA_BROWSER, "scanner"),
    ("\\x16\\x03\\x01", "-", "scanner"),
    ("GET / HTTP/1.1", "server-monitor-probe/1.0", "probe"),
    ("GET /healthz HTTP/1.1", "Wget", "probe"),
])
def test_classify(req, ua, expected):
    assert traffic.classify("203.0.113.9", req, 200, ua) == expected


def test_engaged_requires_page_and_asset_from_same_ip():
    ps = traffic.new_state()
    traffic.ingest(ps, [line(ts(0), "198.51.100.1", "GET / HTTP/2.0"),          # page only: scanner-like
                        line(ts(5), "198.51.100.2", "GET / HTTP/2.0"),
                        line(ts(6), "198.51.100.2", "GET /static/app.js HTTP/2.0")])
    assert set(ps["ips"]["engaged"]) == {"198.51.100.2"}
    assert set(ps["ips"]["human"]) == {"198.51.100.1", "198.51.100.2"}


def test_assets_far_apart_are_not_engaged():
    ps = traffic.new_state()
    traffic.ingest(ps, [line("2026-11-20T10:00:00Z", "198.51.100.3", "GET / HTTP/2.0"),
                        line("2026-11-20T10:30:00Z", "198.51.100.3", "GET /a.css HTTP/2.0")])
    assert "engaged" not in ps["ips"]


def test_backfill_does_not_count_counters_and_cursor_skips_seen_lines():
    ps = traffic.new_state()
    lines = [line(ts(i), "203.0.113.1", "GET / HTTP/2.0") for i in range(3)]
    newest = traffic.ingest(ps, lines, live=False)
    assert ps["counters"] == {} and sum(b["human"] for b in ps["buckets"].values()) == 3
    traffic.ingest(ps, lines + [line(ts(10), "203.0.113.1", "GET /x HTTP/2.0", status=404)], after=newest)
    assert ps["counters"] == {"human|4xx": 1}


def test_unparseable_lines_are_skipped():
    ps = traffic.new_state()
    assert traffic.ingest(ps, ["garbage", "2026-11-20T10:00:00Z nginx: [notice] reload"]) > 0
    assert ps["buckets"] == {}


def test_windows_prune_and_export():
    ps = traffic.new_state()
    traffic.ingest(ps, [line(ts(1), "203.0.113.5", "GET / HTTP/2.0", ua="GPTBot/1.4"),
                        line(ts(2), "203.0.113.5", "GET /p HTTP/2.0", ua="GPTBot/1.4")])
    now = traffic.iso_ts(ts(30))
    assert traffic.window_totals(ps, now, 3600)["ai_bot"] == 2
    assert traffic.top_agents(ps, now)[0] == ("ai_bot", "GPTBot", 2)
    out = Out()
    traffic.export(out, "demo", ps, now)
    text = out.render()
    assert 'sm_http_requests_window{project="demo",class="ai_bot",window="24h"} 2.0' in text
    assert "# TYPE sm_http_requests_total counter" in text
    traffic.prune(ps, now + 30 * 86400)
    assert ps["buckets"] == {} and ps["ips"]["ai_bot"] == {}
