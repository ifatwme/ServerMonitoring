"""Telegram delivery: Bot API client, per-project forum topics, Alertmanager config, test sends.

    sm-telegram setup        create missing topics, render the Alertmanager config, reload it
    sm-telegram render       only render the Alertmanager config (null receiver if not configured)
    sm-telegram test [project]   send a test message to General or to a project topic

Settings (.env, git-ignored): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (a forum supergroup), and
optionally GF_ROOT_URL (Grafana links). Which projects get a topic: `topic = "<name>"` in the
project's block of config/projects.toml; everything else goes to the General topic.
State: $SM_STATE_DIR/telegram.json {"chat_id": .., "topics": {project: message_thread_id}}.
Alertmanager files: $SM_STATE_DIR/alertmanager/{alertmanager.yml, telegram.tmpl, bot_token}.
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .common import REPO, STATE_DIR, atomic_write, load_config, load_json

API = "https://api.telegram.org"
TOPIC_FILE = STATE_DIR / "telegram.json"
AM_DIR = STATE_DIR / "alertmanager"
TOPIC_COLORS = (0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F)
LIMIT = 4096


class TelegramError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    try:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


class Bot:
    def __init__(self, token: str, opener=urllib.request.urlopen, sleep=time.sleep):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self._token, self._open, self._sleep = token, opener, sleep

    def call(self, method: str, **params):
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                       for k, v in params.items() if v is not None}).encode()
        for attempt in range(5):
            try:
                with self._open(urllib.request.Request(f"{API}/bot{self._token}/{method}", data=data), timeout=30) as r:
                    body = json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    body = json.load(e)
                except Exception:
                    body = {"ok": False, "error_code": e.code, "description": e.reason}
            except OSError as e:                       # network: retry, never print the URL (token)
                if attempt == 4:
                    raise TelegramError(f"{method}: network error {type(e).__name__}") from None
                self._sleep(2 ** attempt)
                continue
            if body.get("ok"):
                return body["result"]
            code = body.get("error_code")
            if code == 429:
                self._sleep(body.get("parameters", {}).get("retry_after", 5))
                continue
            if code and code >= 500 and attempt < 4:
                self._sleep(2 ** attempt)
                continue
            raise TelegramError(f"{method}: {code} {body.get('description', '')}")
        raise TelegramError(f"{method}: gave up after retries")

    def send(self, chat_id, text: str, thread_id: int | None = None, silent: bool = False) -> list[int]:
        ids = []
        for part in split(text):
            r = self.call("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML", message_thread_id=thread_id,
                          disable_notification=silent or None, link_preview_options={"is_disabled": True})
            ids.append(r["message_id"])
        return ids


def split(text: str, limit: int = LIMIT - 96) -> list[str]:
    """Split on line boundaries; keeps <pre> blocks balanced across parts."""
    if len(text) <= limit:
        return [text]
    parts, cur, in_pre = [], "", False
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur + ("</pre>" if in_pre else ""))
            cur = "<pre>" if in_pre else ""
        cur += ("\n" if cur and cur != "<pre>" else "") + line
        in_pre = (in_pre or "<pre>" in line) and "</pre>" not in line
    if cur:
        parts.append(cur)
    return parts


def esc(s) -> str:
    return html.escape(str(s), quote=False)


# ── topics ───────────────────────────────────────────────────────────────────

def topic_projects(projects: dict) -> dict[str, str]:
    """project -> topic name, from `topic = "..."` in config/projects.toml."""
    return {p: c["topic"] for p, c in projects.items() if c.get("topic")}


def _norm(name: str) -> str:
    """Topic names compared without emoji, case or spacing ("📚 Papersaurus" == "papersaurus")."""
    return " ".join("".join(ch for ch in name if ch.isalnum() or ch in " &-").split()).casefold()


def discover_topics(updates: list[dict], chat_id) -> dict[str, int]:
    """Topic name -> thread id from forum_topic_created/edited service messages (topics made by hand)."""
    found: dict[str, int] = {}
    for u in updates:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(chat_id) or not m.get("message_thread_id"):
            continue
        ev = m.get("forum_topic_created") or m.get("forum_topic_edited") or {}
        if ev.get("name"):
            found[_norm(ev["name"])] = m["message_thread_id"]
    return found


def ensure_topics(bot: Bot, chat_id, wanted: dict[str, str], state: dict, updates: list[dict] | None = None) -> dict:
    """Adopt topics created by hand (matched by name), create the rest (needs "Manage topics")."""
    topics = state.setdefault("topics", {})
    existing = discover_topics(updates or [], chat_id)
    for i, (project, name) in enumerate(wanted.items()):
        if project in topics:
            continue
        if _norm(name) in existing:
            topics[project] = existing[_norm(name)]
            continue
        r = bot.call("createForumTopic", chat_id=chat_id, name=name, icon_color=TOPIC_COLORS[i % len(TOPIC_COLORS)])
        topics[project] = r["message_thread_id"]
        bot.send(chat_id, f"📌 <b>{esc(name)}</b>\nAlerts for <code>{esc(project)}</code> arrive in this topic. "
                          "Server-wide alerts and the 5-hourly status report go to General.", thread_id=topics[project])
    state["chat_id"] = chat_id
    return state


# ── Alertmanager config ──────────────────────────────────────────────────────

TEMPLATE = r'''{{ define "sm.telegram" }}{{ if eq .Status "firing" -}}
{{ if eq .CommonLabels.severity "critical" }}🔴{{ else if eq .CommonLabels.severity "warning" }}🟠{{ else }}🔵{{ end -}}
 <b>{{ .CommonLabels.alertname }}</b>{{ if .CommonLabels.project }} · {{ .CommonLabels.project }}{{ end }}
{{- else -}}
✅ <b>Resolved: {{ .CommonLabels.alertname }}</b>{{ if .CommonLabels.project }} · {{ .CommonLabels.project }}{{ end }}
{{- end }}
{{ range .Alerts -}}
• {{ .Annotations.summary | html }}{{ if eq .Status "firing" -}}
 <i>(since {{ .StartsAt.UTC.Format "Jan 2 15:04" }} UTC)</i>
{{- else -}}
 <i>({{ .StartsAt.UTC.Format "15:04" }}–{{ .EndsAt.UTC.Format "15:04" }} UTC)</i>
{{- end }}
{{ end -}}
@@LINK@@{{ end }}
'''

LINK = ('{{ if .CommonLabels.project }}<a href="@@URL@@d/sm-project?var-project={{ .CommonLabels.project }}">Project dashboard</a>'
        '{{ else }}<a href="@@URL@@d/sm-overview">Server overview</a>{{ end }}')

NULL_CONFIG = "route:\n  receiver: none\nreceivers:\n  - name: none\n"


def _receiver(name: str, chat_id, thread: int | None, silent: bool) -> list[str]:
    lines = [f"  - name: {name}", "    telegram_configs:", "      - bot_token_file: /etc/alertmanager/bot_token",
             f"        chat_id: {int(chat_id)}"]
    if thread:
        lines.append(f"        message_thread_id: {int(thread)}")
    lines += ["        parse_mode: HTML", "        send_resolved: true",
              f"        disable_notifications: {'true' if silent else 'false'}",
              '        message: \'{{ template "sm.telegram" . }}\'']
    return lines


def render_alertmanager(chat_id, topics: dict[str, int], grafana_url: str = "") -> tuple[str, str]:
    """Returns (alertmanager.yml, telegram.tmpl). Routing: project topic if the alert's project has one,
    General otherwise; severity=info is delivered silently and repeated at most daily."""
    route = ["route:", "  receiver: general", "  group_by: [alertname, project]", "  group_wait: 30s",
             "  group_interval: 5m", "  repeat_interval: 6h", "  routes:"]
    receivers = ["receivers:", *_receiver("general", chat_id, None, False),
                 *_receiver("general-silent", chat_id, None, True)]
    for p, thread in sorted(topics.items()):
        r = f"project-{p}"
        route += [f'    - matchers: [\'project="{p}"\']', f"      receiver: {r}", "      routes:",
                  "        - matchers: ['severity=\"info\"']", f"          receiver: {r}-silent", "          repeat_interval: 24h",
                  "        - matchers: ['severity=\"critical\"']", f"          receiver: {r}", "          repeat_interval: 2h"]
        receivers += _receiver(r, chat_id, thread, False) + _receiver(f"{r}-silent", chat_id, thread, True)
    route += ["    - matchers: ['severity=\"info\"']", "      receiver: general-silent", "      repeat_interval: 24h",
              "    - matchers: ['severity=\"critical\"']", "      receiver: general", "      repeat_interval: 2h"]
    inhibit = ["inhibit_rules:",
               "  # a down site explains its stopped/unhealthy containers: don't page twice",
               "  - source_matchers: ['alertname=\"SiteDown\"']",
               "    target_matchers: ['alertname=~\"ContainerStopped|ContainerUnhealthy\"']",
               "    equal: [project]",
               "  - source_matchers: ['alertname=\"HostMemoryLow\"']",
               "    target_matchers: ['alertname=\"HostMemoryPressure\"']"]
    yml = "\n".join(["# generated by `sm-telegram render`; do not edit", "templates: [/etc/alertmanager/telegram.tmpl]",
                     *route, *receivers, *inhibit]) + "\n"
    link = LINK.replace("@@URL@@", grafana_url.rstrip("/") + "/") if grafana_url else ""
    return yml, TEMPLATE.replace("@@LINK@@", link)


def write_alertmanager(chat_id=None, topics=None, token: str = "", grafana_url: str = "", am_dir: Path = AM_DIR) -> bool:
    am_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(am_dir, 0o755)
    if chat_id and token:
        yml, tmpl = render_alertmanager(chat_id, topics or {}, grafana_url)
        tok = am_dir / "bot_token"
        tok.write_text(token)
        os.chmod(tok, 0o640)
        with contextlib.suppress(PermissionError):
            os.chown(tok, 0, 65534)                   # readable by the alertmanager container (nobody)
    else:
        yml, tmpl = NULL_CONFIG, '{{ define "sm.telegram" }}{{ end }}\n'
    atomic_write(am_dir / "alertmanager.yml", yml)
    atomic_write(am_dir / "telegram.tmpl", tmpl)
    return bool(chat_id and token)


def reload_alertmanager() -> None:
    import subprocess
    subprocess.run(["docker", "kill", "-s", "HUP", "server-monitor-alertmanager-1"], capture_output=True, check=False)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("render")
    t = sub.add_parser("test")
    t.add_argument("project", nargs="?")
    a = ap.parse_args(argv)
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    state = load_json(TOPIC_FILE, {})
    try:
        if a.cmd == "setup":
            if not (token and chat):
                sys.exit("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first")
            _, projects = load_config()
            bot = Bot(token)
            updates = bot.call("getUpdates", limit=100, allowed_updates=["message"])
            state = ensure_topics(bot, chat, topic_projects(projects), state, updates)
            atomic_write(TOPIC_FILE, json.dumps(state, indent=1))
            print(f"topics: {', '.join(f'{p}={t}' for p, t in state['topics'].items()) or 'none'}")
        if a.cmd in ("setup", "render"):
            ok = write_alertmanager(chat, state.get("topics"), token, env("GF_ROOT_URL"))
            reload_alertmanager()
            print("alertmanager config:", "telegram" if ok else "null receiver (telegram not configured)")
        if a.cmd == "test":
            thread = state.get("topics", {}).get(a.project) if a.project else None
            if a.project and not thread:
                sys.exit(f"{a.project} has no topic (topic = \"...\" in projects.toml, then `sm-telegram setup`)")
            Bot(token).send(chat, f"🧪 Test from server-monitor{f' for <b>{esc(a.project)}</b>' if a.project else ''}.",
                            thread_id=thread, silent=True)
            print("sent")
    except TelegramError as e:
        print(f"telegram: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
