import argparse

from servermon import projctl


def test_cleanup_removes_untagged_images_by_id_and_keeps_compose_images(monkeypatch, tmp_path):
    imgs = [
        {"ref": "app:old", "id": "sha256:aaa", "size": 3, "created": 0, "in_use": False, "project": "app"},
        {"ref": "app:latest", "id": "sha256:bbb", "size": 2, "created": 0, "in_use": False, "project": "app"},
        {"ref": "ghcr.io/x/llama:<none>", "id": "sha256:ccc", "size": 1, "created": 0, "in_use": False,
         "project": "shared"},
        {"ref": "web:1", "id": "sha256:ddd", "size": 9, "created": 0, "in_use": True, "project": "web"},
    ]
    cfg = tmp_path / "projects.toml"
    cfg.write_text('[settings]\nkeep_images = []\n')
    monkeypatch.setattr(projctl, "CONFIG", cfg)
    monkeypatch.setattr(projctl, "load", lambda: ({"app": {"dir": str(tmp_path)}}, {"slow": {"images": imgs}}, {}))
    monkeypatch.setattr(projctl, "has_compose", lambda d: True)
    monkeypatch.setattr(projctl, "sh", lambda *c, cwd=None: "app:latest@sha256:123\n")
    ran = []
    monkeypatch.setattr(projctl, "run", lambda cmd, doit, cwd=None: ran.append(cmd) or True)
    projctl.cmd_cleanup(argparse.Namespace(keep=None, yes=True))
    removed = [c[3:] for c in ran if c[:3] == ["docker", "image", "rm"]]
    assert ["app:old"] in removed and ["sha256:ccc"] in removed
    assert not any("app:latest" in r or "web:1" in r for r in removed)
