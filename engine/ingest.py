"""Generic ingestion — land ANY tabular file into `raw.<table>`, untouched.

Dataset-agnostic by design (PRD §4.1). Infers types via DuckDB's auto readers,
preserves the source columns verbatim (no rename, no cast — that is staging's
job), and lands the result in the `raw` schema of the single warehouse file.

    python -m engine.ingest anage
    python -m engine.ingest --file path/to/any.csv --table my_table

The only thing that is NOT generic is *where the file comes from* and *what it's
called* — that lives in the dataset's config.yml.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import duckdb

from .config import WAREHOUSE_PATH, DatasetConfig, load_dataset_config


# Map an extension to the DuckDB auto-reader table function. `.txt` is treated as
# delimited text (AnAge ships a .txt that is really TSV); the delimiter hint from
# config.yml is passed through when present.
def _reader_sql(path: Path, delimiter: str | None) -> str:
    p = str(path).replace("\\", "/")
    ext = path.suffix.lower()
    if ext == ".parquet":
        return f"read_parquet('{p}')"
    if ext in (".json", ".ndjson"):
        return f"read_json_auto('{p}')"
    # Everything else -> delimited text. read_csv with auto-detect handles types,
    # headers, and quoting; we only override the delimiter when config gives one.
    opts = ["header=true", "auto_detect=true", "sample_size=-1", "all_varchar=false"]
    if delimiter:
        opts.append(f"delim='{delimiter}'")
    return f"read_csv('{p}', {', '.join(opts)})"


def _maybe_extract_from_zip(src: Path, member: str | None) -> Path:
    """If the resolved source is a .zip, extract the named member (or the first
    data-looking file)."""
    if src.suffix.lower() != ".zip":
        return src
    with zipfile.ZipFile(src) as zf:
        names = zf.namelist()
        if member is None:
            # Pick the first data-looking member deterministically.
            data_members = [n for n in names if n.lower().endswith((".csv", ".tsv", ".txt", ".parquet", ".json"))]
            if not data_members:
                raise ValueError(f"{src} contains no recognizable data file: {names}")
            member = sorted(data_members)[0]
        out = src.parent / Path(member).name
        out.write_bytes(zf.read(member))
        return out


def ingest_file(
    file_path: Path,
    table: str,
    *,
    schema: str = "raw",
    delimiter: str | None = None,
    warehouse: Path = WAREHOUSE_PATH,
) -> int:
    """Land one file into `<schema>.<table>`. Returns the row count. Replaces any
    existing table of that name so re-runs are idempotent. The schema is the
    dataset's raw landing schema (`<dataset>_raw`); the generic `--file` mode
    defaults to `raw`."""
    if not file_path.exists():
        raise FileNotFoundError(f"Source file not found: {file_path}")

    reader = _reader_sql(file_path, delimiter)
    con = duckdb.connect(str(warehouse))
    try:
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}";')
        con.execute(f'CREATE OR REPLACE TABLE "{schema}"."{table}" AS SELECT * FROM {reader};')
        (n,) = con.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}";').fetchone()
        (ncols,) = con.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=? AND table_name=?;",
            [schema, table],
        ).fetchone()
    finally:
        con.close()
    print(f"  [ok] {schema}.{table}: {n:,} rows x {ncols} columns  <-  {file_path.name}")
    return int(n)


def raw_schema(dataset: str) -> str:
    """The per-dataset raw landing schema. Namespacing by dataset keeps two
    datasets' same-named tables from colliding in the one warehouse file."""
    return f"{dataset}_raw"


def ingest_dataset(name: str, *, warehouse: Path = WAREHOUSE_PATH) -> int:
    """Ingest every table the dataset declares into `<name>_raw`. Returns total rows."""
    cfg = load_dataset_config(name)
    schema = raw_schema(cfg.name)
    label = f"'{cfg.name}'" + (f" ({len(cfg.tables)} tables)" if cfg.is_multi_table else "")
    print(f"Ingesting dataset {label} -> schema {schema}")
    total = 0
    for spec in cfg.tables:
        src = cfg.resolve_source(spec)
        src = _maybe_extract_from_zip(src, spec.raw_glob)
        total += ingest_file(src, spec.table, schema=schema, delimiter=spec.delimiter, warehouse=warehouse)
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Land any tabular file into raw.<table>.")
    ap.add_argument("dataset", nargs="?", help="dataset name (folder under datasets/)")
    ap.add_argument("--file", help="ingest an arbitrary file instead of a dataset")
    ap.add_argument("--table", help="raw table name (required with --file)")
    ap.add_argument("--delimiter", help="override delimiter, e.g. '\\t'")
    args = ap.parse_args(argv)

    if args.file:
        if not args.table:
            ap.error("--file requires --table")
        delim = args.delimiter.encode().decode("unicode_escape") if args.delimiter else None
        ingest_file(Path(args.file), args.table, delimiter=delim)
        return 0
    if not args.dataset:
        ap.error("provide a dataset name, or --file with --table")
    ingest_dataset(args.dataset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
