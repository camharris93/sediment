"""Dataset registry — maps each dataset to the schemas/tables it owns.

With per-dataset schema namespacing, the mapping is EXACT: a dataset's marts are
exactly the tables in `<dataset>_marts`, its staging models in `<dataset>_staging`,
its raw tables in `<dataset>_raw`. No manifest/dependency parsing needed, and two
datasets can share a table name without colliding (different schemas).
"""
from __future__ import annotations

import re

import duckdb

from .config import DATASETS_DIR, DBT_PROJECT_DIR, WAREHOUSE_PATH, load_dataset_config

MARTS_DIR = DBT_PROJECT_DIR / "models" / "marts"


def raw_schema(name: str) -> str:
    return f"{name}_raw"


def staging_schema(name: str) -> str:
    return f"{name}_staging"


def marts_schema(name: str) -> str:
    return f"{name}_marts"


def list_datasets() -> list[str]:
    """Every dataset folder with a config.yml (anage first if present)."""
    names = sorted(p.parent.name for p in DATASETS_DIR.glob("*/config.yml"))
    return sorted(names, key=lambda n: (n != "anage", n))


def dataset_tables(name: str) -> list[str]:
    try:
        return [t.table for t in load_dataset_config(name).tables]
    except Exception:
        return []


def _tables_in_schema(schema: str) -> list[str]:
    if not WAREHOUSE_PATH.exists():
        return []
    try:
        con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        try:
            rows = con.execute(
                "select table_name from information_schema.tables "
                "where table_schema = ? order by table_name", [schema]).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()
    except Exception:
        return []


def _marts_from_files(name: str) -> list[str]:
    """Fallback when the warehouse isn't built yet: a mart belongs to the dataset
    if its model declares config(schema='<dataset>_marts'). Returns the clean
    relation name (the alias), not the dataset-prefixed model node name."""
    target = marts_schema(name)
    out = []
    for f in MARTS_DIR.glob("*.sql"):
        txt = f.read_text(encoding="utf-8")
        if not re.search(rf"schema\s*=\s*['\"]{re.escape(target)}['\"]", txt):
            continue
        alias = re.search(r"alias\s*=\s*['\"]([\w]+)['\"]", txt)
        out.append(alias.group(1) if alias else f.stem.split("__", 1)[-1])
    return sorted(out)


def dataset_models(name: str) -> dict[str, list[str]]:
    """Return {'staging': [...], 'marts': [...]} owned by the dataset, read from
    its schemas in the warehouse (falling back to model files if not built)."""
    staging = _tables_in_schema(staging_schema(name))
    marts = _tables_in_schema(marts_schema(name))
    if not marts and not staging:
        marts = _marts_from_files(name)
        staging = [f"stg_{t}" for t in dataset_tables(name)]
    return {"staging": staging, "marts": marts}


def dataset_table_names(name: str) -> set[str]:
    m = dataset_models(name)
    return set(m["staging"]) | set(m["marts"])
