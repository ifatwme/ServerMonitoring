# Telegram notifications

Alerts and a periodic status report go to one Telegram **forum group** (a supergroup with Topics):

| Where | What |
|---|---|
| one topic per project | that project's alerts (site down, container stopped/unhealthy/restarting, OOM kill, near memory limit, failed job, idle, crawler load), plus a "resolved" message |
| General | host-wide alerts (memory, CPU, disk, SSH brute force, reboot required, collector stale), alerts of projects without a topic, and the status report every 5 hours |

Severity decides how loud a message is: `critical` and `warning` notify; `info` (idle project,
AI-crawler load, reboot required) arrives silently and repeats at most once a day. Critical alerts
repeat every 2 h while they fire, warnings every 6 h.

## Pieces

```
Prometheus rules ──▶ Alertmanager ──telegram_configs──▶ topic of alert's `project` label, else General
collector/analyzer state + PromQL ──▶ sm-report (server-monitor-report.timer, every 5 h) ──▶ General
```

- `src/servermon/telegram.py`, `bin/sm-telegram`:
  - Bot API client (retries 429 / 5xx; error messages never contain the token);
  - `sm-telegram setup` creates any missing project topics (`createForumTopic`) and stores their
    `message_thread_id`s in `$SM_STATE_DIR/telegram.json`;
  - renders the Alertmanager config: one receiver (+ a silent twin) per topic, routing by the
    `project` label, inhibition (a `SiteDown` hides that project's `ContainerStopped|ContainerUnhealthy`).
    The bot token is written to a 0640 file mounted read-only into Alertmanager, never into the YAML.
    Without Telegram settings it renders a null receiver, so the stack runs the same either way.
- `src/servermon/report.py`, `bin/sm-report [--dry-run]`: host CPU / RAM / disk / OOM kills / memory
  stall, firing alerts grouped by severity with what is new or cleared since the last report,
  site uptime, a per-project table (state, CPU, RAM, real visitors and crawler requests in 24 h,
  idle days, park candidates) and the analyzer's findings (where CPU goes, memory overcommit,
  limits to raise, reclaimable disk).

## Setup

1. Create a group, turn on **Topics** (group settings), add the bot as admin with **Manage topics**.
2. Send any message in the group, then read the chat id (starts with `-100`):
   ```bash
   curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | grep -o '"chat":{"id":-[0-9]*'
   ```
   An invite link can't be resolved to an id through the Bot API.
3. Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` into `.env` (git-ignored), and `topic = "Name"`
   into the projects that should get their own topic in `config/projects.toml`.
4. `bin/sm-telegram setup` (creates topics, writes and reloads the Alertmanager config), then
   `bin/sm-telegram test <project>` and `bin/sm-report --dry-run`.

Adding a project later: add `topic = "..."` and run `sm-telegram setup` again; existing topics are kept.
Deleting a topic in Telegram: remove its entry from `telegram.json` and re-run setup.
