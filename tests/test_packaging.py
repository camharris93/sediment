"""Packaging seams: workspace-root resolution (so an installed `sediment` can run
against a project anywhere) and the `init` scaffolder."""
from __future__ import annotations

import pytest

import engine.config as config
import run


def test_root_resolves_from_sediment_home(tmp_path, monkeypatch):
    (tmp_path / "datasets").mkdir()
    (tmp_path / "dbt_project").mkdir()
    monkeypatch.setenv("SEDIMENT_HOME", str(tmp_path))
    assert config._resolve_project_root() == tmp_path.resolve()


def test_root_walks_up_from_cwd(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "datasets").mkdir(parents=True)
    (ws / "dbt_project").mkdir()
    sub = ws / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.delenv("SEDIMENT_HOME", raising=False)
    monkeypatch.chdir(sub)
    assert config._resolve_project_root() == ws.resolve()


def test_root_falls_back_to_package_parent(tmp_path, monkeypatch):
    # A dir with no workspace markers, no SEDIMENT_HOME → the package's own parent.
    monkeypatch.delenv("SEDIMENT_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config._resolve_project_root() == config._PACKAGE_PARENT


def test_init_scaffolds_workspace(tmp_path):
    dest = tmp_path / "new_ws"
    run.t_init(str(dest))
    assert (dest / "dbt_project" / "dbt_project.yml").exists()
    assert (dest / "dbt_project" / "models" / "staging").is_dir()
    assert (dest / "dbt_project" / "models" / "marts").is_dir()
    assert (dest / "datasets" / "example" / "config.yml").exists()


def test_init_refuses_nonempty_dir(tmp_path):
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "something.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        run.t_init(str(dest))
