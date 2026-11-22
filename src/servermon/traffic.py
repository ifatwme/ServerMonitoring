"""Access-log traffic model: classify requests and keep hourly, per-class aggregates.

Classes
  human    browser-like user agent that is not a known bot and not requesting exploit paths
  ai_bot   LLM training / answer-engine crawlers (GPTBot, ClaudeBot, Meta, Amazonbot, ...)
  bot      every other automated client (search engines, libraries, uptime checkers)
  scanner  exploit probes: /.env, /wp-login.php, odd methods, malformed request lines
  probe    this stack's own blackbox probe and health checks

"engaged" is a subset of human: an IP that fetched a page AND one of its assets within
ENGAGED_WINDOW seconds. Real browsers do that; scanners spoofing a browser user agent rarely do.
The state is a plain JSON-serialisable dict so the collector can persist it between runs.
"""
from __future__ import annotations

import re
from collections import Counter

from .common import iso_ts

BUCKET = 3600                 # aggregate per hour
KEEP_SECONDS = 8 * 86400      # hourly buckets kept for 8 days
AGENT_KEEP_SECONDS = 2 * 86400
ENGAGED_WINDOW = 600
WINDOWS = (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400))

LOG_RE = re.compile(r'^(\S+) \S+ \S+ \[[^\]]+\] "([^"]*)" (\d{3}) (\d+|-) "[^"]*" "([^"]*)"')
AI_BOT = re.compile(r"GPTBot|ChatGPT|OAI-SearchBot|ClaudeBot|Claude-|anthropic|PerplexityBot|Bytespider|"
                    r"meta-externalagent|Amazonbot|CCBot|Google-Extended|Applebot|cohere|Diffbot|"
                    r"YouBot|Timpibot|ImagesiftBot|PetalBot|DuckAssistBot", re.I)
BOT = re.compile(r"bot|crawl|spider|slurp|fetch|python|curl|wget|go-http|httpx|scrapy|axios|node|java/|"
                 r"headless|libwww|okhttp|facebookexternalhit|preview|monitor|zgrab|masscan|nmap|scan|"
                 r"censys|expanse|^-?$", re.I)
SCAN_PATH = re.compile(r"\.(php|asp|aspx|jsp|cgi|env|git|svn|bak|sql|ini|yml|yaml|xml)(\?|$|/)|/wp-|/\.|"
                       r"phpmyadmin|/admin|/cgi-bin|/boaform|/HNAP|/redcap|/vendor/|/actuator|/solr|"
                       r"/owa/|/remote/|/geoserver|/console", re.I)
PROBE = re.compile(r"server-monitor|Blackbox|kube-probe", re.I)
ASSET = re.compile(r"\.(js|css|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|map|mp4|webm|json|txt)(\?|$)", re.I)
CLASSES = ("human", "ai_bot", "bot", "scanner", "probe")
AGENT_NAME = re.compile(r"([A-Za-z][\w.-]*(?:bot|Bot|crawler|spider|agent|Agent))", re.I)


def classify(ip: str, req: str, status: int, ua: str) -> str:
    parts = req.split()
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else req
    if PROBE.search(ua) or path.startswith("/healthz") or ip.startswith("127.") and "curl" in ua.lower():
        return "probe"
    if AI_BOT.search(ua):
        return "ai_bot"
    if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS") or SCAN_PATH.search(path) \
            or len(parts) < 2:
        return "scanner"
    if BOT.search(ua):
        return "bot"
    return "human"


def agent_label(ua: str, cls: str) -> str:
    if cls == "human":
        return "browser"
    m = AGENT_NAME.search(ua) or re.match(r"([\w.-]+)/", ua)
    return (m.group(1) if m else (ua[:24] or "empty"))[:40]



def new_state() -> dict:
    return {"counters": {}, "bytes": {}, "last": {}, "buckets": {}, "ips": {}, "agents": {}, "seen": {}, "cursor": {}}


def ingest(ps: dict, lines, after: float | None = None, live: bool = True) -> float:
    """Feed `docker logs --timestamps` lines (``<RFC3339> <nginx combined line>``) into state `ps`.

    Lines at or before `after` are skipped (already seen). `live=False` is used for the first
    backfill: windows/last-seen are filled, but monotonic counters are not, so Prometheus does
    not see a fake burst. Returns the newest timestamp seen.
    """
    newest = after or 0.0
    for line in lines:
        ts_s, _, rest = line.partition(" ")
        ts = iso_ts(ts_s)
        if ts is None or (after and ts <= after):
            continue
        newest = max(newest, ts)
        m = LOG_RE.match(rest)
        if not m:
            continue
        ip, req, status_s, size, ua = m.groups()
        status = int(status_s)
        cls = classify(ip, req, status, ua)
        b = str(int(ts // BUCKET * BUCKET))
        bk = ps["buckets"].setdefault(b, {})
        bk[cls] = bk.get(cls, 0) + 1
        parts = req.split()
        path = parts[1] if len(parts) > 1 else ""
        if cls == "human" and status < 400:
            kind = "asset" if ASSET.search(path) or path.startswith("/_stcore") else "page"
            if kind == "page":
                bk["pageview"] = bk.get("pageview", 0) + 1
            seen = ps["seen"].setdefault(ip, {})
            seen[kind] = ts
            other = seen.get("asset" if kind == "page" else "page", 0)
            if other and abs(ts - other) < ENGAGED_WINDOW:
                ps["ips"].setdefault("engaged", {})[ip] = ts
                ps["last"]["engaged"] = max(ps["last"].get("engaged", 0), ts)
        ps["last"][cls] = max(ps["last"].get(cls, 0), ts)
        ips = ps["ips"].setdefault(cls, {})
        ips[ip] = max(ips.get(ip, 0), ts)
        ag = ps["agents"].setdefault(b, {})
        an = f"{cls}|{agent_label(ua, cls)}"
        ag[an] = ag.get(an, 0) + 1
        if live:
            k = f"{cls}|{status // 100}xx"
            ps["counters"][k] = ps["counters"].get(k, 0) + 1
            ps["bytes"][cls] = ps["bytes"].get(cls, 0) + (int(size) if size.isdigit() else 0)
    return newest


def prune(ps: dict, now: float) -> None:
    ps["buckets"] = {k: v for k, v in ps["buckets"].items() if float(k) >= now - KEEP_SECONDS}
    ps["agents"] = {k: v for k, v in ps["agents"].items() if float(k) >= now - AGENT_KEEP_SECONDS}
    ps["seen"] = {ip: v for ip, v in ps.get("seen", {}).items() if max(v.values()) >= now - 3600}
    for cls in list(ps["ips"]):
        ps["ips"][cls] = {ip: t for ip, t in ps["ips"][cls].items() if t >= now - 7 * 86400}


def window_totals(ps: dict, now: float, seconds: int) -> Counter:
    tot: Counter = Counter()
    for b, d in ps["buckets"].items():
        if float(b) + BUCKET > now - seconds:
            tot.update(d)
    return tot


def unique_clients(ps: dict, cls: str, now: float, seconds: int) -> int:
    return sum(1 for t in ps["ips"].get(cls, {}).values() if t >= now - seconds)


def top_agents(ps: dict, now: float, n: int = 12) -> list[tuple[str, str, int]]:
    agents: Counter = Counter()
    for b, d in ps["agents"].items():
        if float(b) + BUCKET > now - 86400:
            agents.update(d)
    return [(*an.split("|", 1), c) for an, c in agents.most_common(n)]


def export(out, project: str, ps: dict, now: float) -> None:
    """Write the sm_http_* families for one project into a common.Out."""
    L = {"project": project}
    for k, v in ps["counters"].items():
        cls, code = k.split("|")
        out.add("sm_http_requests_total", v, {**L, "class": cls, "code": code},
                "Requests seen in the nginx access log since the collector started", "counter")
    for cls, v in ps["bytes"].items():
        out.add("sm_http_response_bytes_total", v, {**L, "class": cls}, "Response bytes by client class", "counter")
    for cls, t in ps["last"].items():
        out.add("sm_http_last_request_timestamp_seconds", t, {**L, "class": cls},
                "Time of the last request per client class")
    for window, secs in WINDOWS:
        tot = window_totals(ps, now, secs)
        for cls in (*CLASSES, "pageview"):
            out.add("sm_http_requests_window", tot.get(cls, 0), {**L, "class": cls, "window": window},
                    "Requests in the trailing window (hour buckets); class=pageview = human non-asset 2xx/3xx")
        for cls in (*CLASSES, "engaged"):
            out.add("sm_http_unique_clients_window", unique_clients(ps, cls, now, secs),
                    {**L, "class": cls, "window": window},
                    "Distinct client IPs in the trailing window; class=engaged = browser that loaded a page and its assets")
    for cls, name, n in top_agents(ps, now):
        out.add("sm_http_agent_requests_24h", n, {**L, "class": cls, "agent": name},
                "Top user agents (last 24 h) per project")
