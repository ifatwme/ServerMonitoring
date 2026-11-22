import json

from servermon.common import Out, atomic_write, iso_ts, load_config, load_json, parse_size


def test_render_keeps_full_timestamp_precision():
    out = Out()
    out.add("x_ts", 1795190862.8651817, None, "a timestamp")
    assert "x_ts 1795190862.8651817" in out.render()          # %.6g would lose ~1000 s


def test_render_groups_family_and_escapes_labels():
    out = Out()
    out.add("m", 1, {"a": 'quo"te\\n'}, "help", "counter")
    out.add("m", 2, {"a": "line\nbreak"})
    out.add("skip", None)
    text = out.render()
    assert text.count("# TYPE m counter") == 1 and "skip" not in text
    assert 'a="quo\\"te\\\\n"' in text and 'a="line break"' in text


def test_iso_ts_handles_nanoseconds_and_zero_time():
    assert iso_ts("2026-11-20T10:00:00.123456789Z") == iso_ts("2026-11-20T10:00:00.123456Z")
    assert iso_ts("0001-01-01T00:00:00Z") is None and iso_ts("nope") is None


def test_parse_size():
    assert parse_size("1.5GB") == 1.5e9 and parse_size("2MiB") == 2 * 1024**2 and parse_size("0B") == 0


def test_atomic_write_and_load_json(tmp_path):
    p = tmp_path / "a" / "b.json"
    atomic_write(p, json.dumps({"k": 1}))
    assert load_json(p, None) == {"k": 1} and load_json(tmp_path / "missing", 5) == 5


def test_example_config_loads_with_defaults(tmp_path):
    from pathlib import Path
    ex = Path(__file__).resolve().parents[1] / "config" / "projects.example.toml"
    settings, projects = load_config(ex)
    assert settings["idle_days"] == 7
    assert projects["edge"]["keep_running"] is True and projects["example-app"]["dev_dirs"]
