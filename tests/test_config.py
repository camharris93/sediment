"""Dataset config parsing + the dataset-name slug guard (path-traversal defence)."""
from __future__ import annotations

import pytest

import engine.config as config
from engine.config import (
    DatasetConfig,
    TableSpec,
    load_dataset_config,
    validate_dataset_name,
)


@pytest.mark.parametrize("bad", [
    "../etc", "..\\..\\windows", "a/b", "a\\b", "", "  ", ".", "/abs",
    "x" * 65, "name with spaces", "naughty;name",
])
def test_invalid_dataset_names_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_dataset_name(bad)


@pytest.mark.parametrize("good", ["anage", "nba", "my_data", "data-2", "A1"])
def test_valid_dataset_names_pass(good):
    assert validate_dataset_name(good) == good


def test_load_config_rejects_traversal_name(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASETS_DIR", tmp_path)
    with pytest.raises(ValueError):
        load_dataset_config("../secret")


def _write_cfg(datasets_dir, name, text):
    d = datasets_dir / name
    d.mkdir(parents=True)
    (d / "config.yml").write_text(text, encoding="utf-8")


def test_single_table_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASETS_DIR", tmp_path)
    _write_cfg(tmp_path, "sales", "name: sales\nsource: data/orders.csv\ntable: orders\n")
    cfg = load_dataset_config("sales")
    assert cfg.name == "sales"
    assert cfg.table == "orders"
    assert cfg.is_multi_table is False
    assert len(cfg.tables) == 1


def test_multi_table_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASETS_DIR", tmp_path)
    _write_cfg(tmp_path, "shop",
               "name: shop\ntables:\n"
               "  - {source: data/customers.csv, table: customers}\n"
               "  - {source: data/orders.csv, table: orders}\n")
    cfg = load_dataset_config("shop")
    assert cfg.is_multi_table is True
    assert [t.table for t in cfg.tables] == ["customers", "orders"]


def test_missing_config_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASETS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_dataset_config("nope")


def test_delimiter_escape_is_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASETS_DIR", tmp_path)
    _write_cfg(tmp_path, "tsv",
               'name: tsv\nsource: data/x.txt\ntable: x\ndelimiter: "\\t"\n')
    cfg = load_dataset_config("tsv")
    assert cfg.delimiter == "\t"  # the literal backslash-t became a real tab


def test_resolve_source_relative_to_dataset_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATASETS_DIR", tmp_path)
    cfg = DatasetConfig(name="x", tables=[TableSpec(source="data/f.csv", table="t")])
    assert cfg.resolve_source().as_posix().endswith("x/data/f.csv")
