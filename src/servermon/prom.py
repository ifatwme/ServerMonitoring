"""Minimal Prometheus HTTP API client (stdlib only)."""
from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request

DEFAULT_URL = os.environ.get("SM_PROMETHEUS_URL", "http://127.0.0.1:9090")


class PromError(RuntimeError):
    pass


class Prom:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 30):
        self.url, self.timeout = url.rstrip("/"), timeout

    def _get(self, path: str, params: dict) -> dict:
        q = urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(f"{self.url}{path}?{q}", timeout=self.timeout) as r:
                data = json.load(r)
        except Exception as e:  # urllib raises several types; callers only care that it failed
            raise PromError(f"{path}: {e}") from e
        if data.get("status") != "success":
            raise PromError(data.get("error", "query failed"))
        return data["data"]

    def query(self, expr: str, at: float | None = None) -> list[tuple[dict, float]]:
        params = {"query": expr}
        if at is not None:
            params["time"] = at
        res = self._get("/api/v1/query", params)["result"]
        return [(r["metric"], float(r["value"][1])) for r in res]

    def range(self, expr: str, start: float, end: float, step: float) -> list[tuple[dict, dict[float, float]]]:
        """Returns [(labels, {timestamp: value})]; NaN/Inf samples are dropped."""
        res = self._get("/api/v1/query_range", {"query": expr, "start": start, "end": end, "step": step})["result"]
        out = []
        for r in res:
            pts = {}
            for t, v in r["values"]:
                f = float(v)
                if math.isfinite(f):
                    pts[float(t)] = f
            out.append((r["metric"], pts))
        return out
