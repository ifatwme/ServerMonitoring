"""Dashboards are generated from code: check they are valid, consistent and committed up to date."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_dashboards", ROOT / "grafana" / "build_dashboards.py")
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)
BOARDS = {"overview": bd.overview, "project": bd.project, "waste": bd.waste, "insights": bd.insights}


@pytest.mark.parametrize("name", BOARDS)
def test_dashboard_is_valid(name):
    d = BOARDS[name]().json()
    ids = [p["id"] for p in d["panels"]]
    assert len(ids) == len(set(ids)), "duplicate panel ids"
    for p in d["panels"]:
        g = p["gridPos"]
        assert g["x"] >= 0 and g["x"] + g["w"] <= 24
        for t in p.get("targets", []):
            assert t["expr"].strip() and t["expr"].count("(") == t["expr"].count(")"), (p["title"], t["expr"])
            assert t["datasource"]["uid"] == "prometheus"


@pytest.mark.parametrize("name", BOARDS)
def test_committed_json_matches_generator(name):
    committed = json.loads((ROOT / "grafana" / "dashboards" / f"{name}.json").read_text())
    assert committed == BOARDS[name]().json(), "run: python3 grafana/build_dashboards.py"


def test_panels_do_not_overlap():
    for name, fn in BOARDS.items():
        cells = set()
        for p in fn().json()["panels"]:
            g = p["gridPos"]
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    assert (x, y) not in cells, f"{name}: {p['title']} overlaps"
                    cells.add((x, y))
