"""Shared helpers: paths, subprocess, Prometheus text exposition, atomic file writes."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG = Path(os.environ.get("SM_CONFIG", REPO / "config" / "projects.toml"))
STATE_DIR = Path(os.environ.get("SM_STATE_DIR", "/var/lib/server-monitor"))
TEXTFILE_DIR = STATE_DIR / "textfile"
STATE_FILE = STATE_DIR / "state.json"
PARKED_FILE = STATE_DIR / "parked.json"
INSIGHTS_FILE = STATE_DIR / "insights.json"


def sh(*cmd: str, timeout: int = 60, check: bool = False) -> str:
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if check and r.returncode != 0:
        return ""
    return r.stdout


def esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


class Out:
    """Collects samples and renders Prometheus text format with HELP/TYPE once per family."""

    def __init__(self):
        self.fam: dict[str, tuple[str, str, list[str]]] = {}

    def add(self, name: str, value, labels: dict | None = None, help_: str = "", typ: str = "gauge"):
        if value is None:
            return
        f = self.fam.setdefault(name, (help_, typ, []))
        lab = ",".join(f'{k}="{esc(v)}"' for k, v in (labels or {}).items())
        f[2].append(f"{name}{{{lab}}} {float(value)!r}" if lab else f"{name} {float(value)!r}")

    def render(self) -> str:
        lines = []
        for name, (h, t, samples) in self.fam.items():
            lines += [f"# HELP {name} {h or name}", f"# TYPE {name} {t}", *samples]
        return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def iso_ts(s: str | None) -> float | None:
    if not s or s.startswith("0001-"):
        return None
    s = re.sub(r"(\.\d{6})\d*", r"\1", s.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def parse_size(s: str) -> float:
    m = re.match(r"([\d.]+)\s*([kKMGT]?i?B)", s or "")
    if not m:
        return 0.0
    mult = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
            "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    return float(m.group(1)) * mult.get(m.group(2), 1)



def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default



def load_config(path: str | Path | None = None):
    cfg = tomllib.loads(Path(path or CONFIG).read_text())
    settings = cfg.get("settings", {})
    projects = cfg.get("projects", {})
    for p in projects.values():
        p.setdefault("kind", "app")
        p.setdefault("dev_dirs", [])
        p.setdefault("timers", [])
        p.setdefault("images", [])
        p.setdefault("idle_days", settings.get("idle_days", 7))
        p.setdefault("keep_running", p["kind"] == "infra")
    return settings, projects
