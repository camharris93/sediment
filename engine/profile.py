"""Generic profiler — describe whatever landed in `raw.<table>`.

For each column: type, null rate, cardinality, min/max, top-K sample values, and
a candidate-key flag (exact distinct == row count). Emits `profile.json`, which
is (a) the human's quick read on a new dataset and (b) the SOLE input to the
AI build-time scaffolding step (engine/scaffold.py). Fully dataset-agnostic.

    python -m engine.profile anage
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from .config import WAREHOUSE_PATH, load_dataset_config

_SAMPLE_TOPK = 8

# DuckDB type families we treat as numeric / temporal for min-max + key tests.
_NUMERIC = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
            "USMALLINT", "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "DECIMAL", "REAL"}
_TEMPORAL = {"DATE", "TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS",
             "TIME", "TIMESTAMP WITH TIME ZONE"}


@dataclass
class ColumnProfile:
    name: str
    type: str
    null_count: int
    null_rate: float
    approx_distinct: int
    is_candidate_key: bool
    min_value: Any = None
    max_value: Any = None
    sample_values: list[Any] = field(default_factory=list)


@dataclass
class TableProfile:
    dataset: str
    schema: str
    table: str
    row_count: int
    column_count: int
    columns: list[ColumnProfile]
    profiled_at: str


def _base_type(t: str) -> str:
    return t.split("(")[0].strip().upper()


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def profile_table(
    schema: str, table: str, *, dataset: str = "", warehouse: Path = WAREHOUSE_PATH
) -> TableProfile:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        fq = f"{_quote(schema)}.{_quote(table)}"
        (row_count,) = con.execute(f"SELECT COUNT(*) FROM {fq};").fetchone()
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position;",
            [schema, table],
        ).fetchall()
        if not cols:
            raise ValueError(f"No such table {schema}.{table} in {warehouse}")

        col_profiles: list[ColumnProfile] = []
        for name, dtype in cols:
            q = _quote(name)
            bt = _base_type(dtype)
            # One pass for null count + approx distinct (cheap, local).
            null_count, approx_distinct = con.execute(
                f"SELECT COUNT(*) - COUNT({q}), approx_count_distinct({q}) FROM {fq};"
            ).fetchone()
            null_count = int(null_count or 0)
            approx_distinct = int(approx_distinct or 0)
            null_rate = (null_count / row_count) if row_count else 0.0
            # A column is a candidate key when it is non-null and fully distinct.
            # approx_count_distinct is a HyperLogLog estimate and undercounts
            # slightly, so a real key looks like ~0.99*row_count, not exactly
            # row_count. When a non-null column lands in that band, confirm with
            # an exact COUNT(DISTINCT) (cheap locally) before claiming a key.
            is_key = False
            if row_count and null_count == 0 and approx_distinct >= 0.9 * row_count:
                (exact,) = con.execute(
                    f"SELECT COUNT(DISTINCT {q}) FROM {fq};"
                ).fetchone()
                approx_distinct = int(exact)
                is_key = approx_distinct == row_count

            min_v = max_v = None
            if bt in _NUMERIC or bt in _TEMPORAL:
                min_v, max_v = con.execute(
                    f"SELECT MIN({q}), MAX({q}) FROM {fq};"
                ).fetchone()

            # Top-K most frequent values — shows the FORMAT of coded columns to
            # both the human reader and the downstream scaffolding LLM.
            samples: list[Any] = []
            if approx_distinct <= 10_000:  # skip on high-card free text
                rows = con.execute(
                    f"SELECT {q} AS v, COUNT(*) AS c FROM {fq} "
                    f"WHERE {q} IS NOT NULL GROUP BY 1 ORDER BY c DESC, 1 LIMIT {_SAMPLE_TOPK};"
                ).fetchall()
                samples = [_jsonable(r[0]) for r in rows]

            col_profiles.append(ColumnProfile(
                name=name, type=dtype, null_count=null_count, null_rate=round(null_rate, 4),
                approx_distinct=approx_distinct, is_candidate_key=is_key,
                min_value=_jsonable(min_v), max_value=_jsonable(max_v),
                sample_values=samples,
            ))
    finally:
        con.close()

    return TableProfile(
        dataset=dataset, schema=schema, table=table, row_count=int(row_count),
        column_count=len(col_profiles), columns=col_profiles,
        profiled_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


_KEY_NAME_RE = __import__("re").compile(r"(^|_)(id|key|code|no)($|_)|_id$|^id$", 2)


def infer_relationships(tables: list[TableProfile]) -> list[dict]:
    """Lightweight cross-table relationship inference for the profile: a shared,
    key-shaped column that is a candidate key on ONE side and present on another
    is a 1:many (parent = the unique side). Both unique → 1:1. The query agent
    re-derives this from the warehouse too; this surfaces it at profile time so the
    scaffolder can propose join marts."""
    rels: list[dict] = []
    # column_name -> [(table_name, is_key)]
    index: dict[str, list[tuple[str, bool]]] = {}
    for t in tables:
        for c in t.columns:
            if not _KEY_NAME_RE.search(c.name):
                continue
            index.setdefault(c.name.lower(), []).append((t.table, c.is_candidate_key))
    from itertools import combinations
    seen = set()
    for col, entries in index.items():
        if len(entries) < 2:
            continue
        for (ta, ka), (tb, kb) in combinations(entries, 2):
            key = tuple(sorted([ta, tb]) + [col])
            if key in seen:
                continue
            seen.add(key)
            if ka and kb:
                rels.append({"parent": ta, "child": tb, "column": col, "cardinality": "1:1"})
            elif ka:
                rels.append({"parent": ta, "child": tb, "column": col, "cardinality": "1:many"})
            elif kb:
                rels.append({"parent": tb, "child": ta, "column": col, "cardinality": "1:many"})
            else:
                p, c = sorted([ta, tb])
                rels.append({"parent": p, "child": c, "column": col, "cardinality": "many:many"})
    return rels


def profile_dataset(name: str, *, warehouse: Path = WAREHOUSE_PATH) -> dict:
    from .ingest import raw_schema
    cfg = load_dataset_config(name)
    schema = raw_schema(cfg.name)
    tables = [profile_table(schema, spec.table, dataset=cfg.name, warehouse=warehouse)
              for spec in cfg.tables]
    relationships = infer_relationships(tables) if len(tables) > 1 else []

    payload = {
        "dataset": cfg.name,
        "profiled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tables": [asdict(t) for t in tables],
        "relationships": relationships,
    }
    # Write profile.json next to the dataset (the scaffold step reads it there)
    # and a convenience copy at the repo root.
    out_paths = [cfg.dir / "profile.json", warehouse.parent / "profile.json"]
    text = json.dumps(payload, indent=2, default=str)
    for p in out_paths:
        p.write_text(text, encoding="utf-8")

    for t in tables:
        keys = [c.name for c in t.columns if c.is_candidate_key]
        print(f"  [ok] profiled raw.{t.table}: {t.row_count:,} rows x {t.column_count} cols"
              f"  (keys: {', '.join(keys) if keys else 'none'})")
    if relationships:
        print(f"    derived {len(relationships)} cross-table relationship(s):")
        for r in relationships:
            print(f"      - {r['parent']} -> {r['child']} on {r['column']} [{r['cardinality']}]")
    print(f"    -> {out_paths[0]}")
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Profile raw.<table>, emit profile.json.")
    ap.add_argument("dataset", help="dataset name (folder under datasets/)")
    args = ap.parse_args(argv)
    profile_dataset(args.dataset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
