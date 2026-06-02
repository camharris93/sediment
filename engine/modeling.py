"""Promote a validated NL->SQL answer into a dbt model — the "build new models
from chat" capability.

Every function that can create or change a model is guarded by `require_build_mode`:
the read path (asking questions) is always open, but turning an answer into a model
is the authoring capability and is refused in "view" mode (see engine/config.py).
Even in build mode, the unit of work is REVIEWABLE dbt SQL — we never let the chat
mutate the curated `marts` silently. Three escalating actions:

  • sandbox_build  — materialize into an isolated `_sandbox` schema to PREVIEW the
                     result (row count, columns, sanity checks). Throwaway.
  • propose_model  — write the model as `dbt_project/models/marts/<name>.sql` for a
                     human to review/commit. Does not build it.
  • materialize    — write the file AND `dbt run --select` it into `marts` (so it's
                     tested and real). The author's "ship it" button.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .config import DBT_PROJECT_DIR, WAREHOUSE_PATH, is_build_mode

MARTS_DIR = DBT_PROJECT_DIR / "models" / "marts"
SANDBOX_SCHEMA = "_sandbox"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")


class BuildModeError(PermissionError):
    """Raised when a build action is attempted in view (read-only) mode."""


def require_build_mode() -> None:
    if not is_build_mode():
        raise BuildModeError(
            "Model-building is disabled in view mode. This deployment is read-only; "
            "run the authoring app (`python run.py dashboard`) to build models."
        )


def validate_model_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise ValueError(
            "Model name must be lower snake_case, 3-48 chars, starting with a letter "
            f"(e.g. 'mart_lifespan_by_order'). Got: {name!r}"
        )
    if not name.startswith(("mart_", "int_")):
        name = "mart_" + name
    return name


# ─────────────────────────────────────────────────────────────────────────────
# Rewrite raw schema.table refs into dbt {{ ref() }} / {{ source() }}
# ─────────────────────────────────────────────────────────────────────────────

def to_dbt_refs(sql: str) -> str:
    """Turn `staging.x` / `marts.x` -> {{ ref('x') }} and `raw.x` ->
    {{ source('raw','x') }} so chat SQL becomes a committable dbt model. Handles
    both quoted and unquoted schema-qualified names."""
    def ref_unquoted(m: re.Match) -> str:
        schema, tbl = m.group(1).lower(), m.group(2)
        if schema == "raw":
            return f"{{{{ source('raw', '{tbl}') }}}}"
        return f"{{{{ ref('{tbl}') }}}}"

    def ref_quoted(m: re.Match) -> str:
        schema, tbl = m.group(1).lower(), m.group(2)
        if schema == "raw":
            return f"{{{{ source('raw', '{tbl}') }}}}"
        return f"{{{{ ref('{tbl}') }}}}"

    sql = re.sub(r'"(staging|marts|raw)"\."([A-Za-z_]\w*)"', ref_quoted, sql, flags=re.I)
    sql = re.sub(r'\b(staging|marts|raw)\.([A-Za-z_]\w*)\b', ref_unquoted, sql, flags=re.I)
    return sql


def model_text(name: str, dbt_sql: str, *, question: str = "", rationale: str = "") -> str:
    header = [
        f"-- {name}",
        "-- Promoted from an NL->SQL chat answer. REVIEW before committing.",
    ]
    if question:
        header.append(f"-- Question: {question}")
    if rationale:
        header.append(f"-- Rationale: {rationale}")
    return "\n".join(header) + "\n\n" + dbt_sql.strip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox preview
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SandboxResult:
    ok: bool
    relation: str = ""
    row_count: int = 0
    columns: list[tuple[str, str]] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def sandbox_build(name: str, raw_sql: str, *, sample_limit: int = 20) -> SandboxResult:
    """Materialize the raw (schema-qualified) SQL into `_sandbox.<name>` to preview
    what the model would produce. Isolated from `marts`; safe to re-run."""
    require_build_mode()
    name = validate_model_name(name)
    relation = f"{SANDBOX_SCHEMA}.{name}"
    body = raw_sql.strip().rstrip(";")
    con = duckdb.connect(str(WAREHOUSE_PATH))  # read-write, build mode only
    try:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {SANDBOX_SCHEMA};")
        con.execute(f'CREATE OR REPLACE TABLE {SANDBOX_SCHEMA}."{name}" AS {body};')
        (rc,) = con.execute(f'SELECT COUNT(*) FROM {SANDBOX_SCHEMA}."{name}";').fetchone()
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
            [SANDBOX_SCHEMA, name],
        ).fetchall()
        cur = con.execute(f'SELECT * FROM {SANDBOX_SCHEMA}."{name}" LIMIT {sample_limit};')
        keys = [d[0] for d in cur.description]
        sample = [dict(zip(keys, r)) for r in cur.fetchall()]
    except Exception as exc:
        return SandboxResult(ok=False, relation=relation, error=str(exc))
    finally:
        con.close()

    warnings: list[str] = []
    if rc == 0:
        warnings.append("The model produced ZERO rows — check filters before committing.")
    if rc == 1:
        warnings.append("Only one row — is this meant to be a scalar/summary mart?")
    dup_cols = [c for c, _ in cols if list(x for x, _ in cols).count(c) > 1]
    if dup_cols:
        warnings.append(f"Duplicate output column name(s): {sorted(set(dup_cols))}.")
    return SandboxResult(ok=True, relation=relation, row_count=int(rc),
                         columns=cols, sample_rows=sample, warnings=warnings)


def drop_sandbox(name: str) -> None:
    require_build_mode()
    name = validate_model_name(name)
    con = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        con.execute(f'DROP TABLE IF EXISTS {SANDBOX_SCHEMA}."{name}";')
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Propose (write file) and materialize (write + dbt run)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MaterializeResult:
    ok: bool
    model_path: str = ""
    dbt_output: str = ""
    error: str | None = None


def propose_model(name: str, raw_sql: str, *, question: str = "", rationale: str = "") -> Path:
    """Write the model file (raw refs rewritten to dbt refs) for human review. Does
    not build it. Returns the path written."""
    require_build_mode()
    name = validate_model_name(name)
    MARTS_DIR.mkdir(parents=True, exist_ok=True)
    path = MARTS_DIR / f"{name}.sql"
    path.write_text(model_text(name, to_dbt_refs(raw_sql), question=question, rationale=rationale),
                    encoding="utf-8")
    return path


def _dbt_cmd() -> list[str]:
    import sys
    exe = shutil.which("dbt")
    return [exe] if exe else [sys.executable, "-m", "dbt.cli.main"]


def materialize_model(name: str, raw_sql: str, *, question: str = "", rationale: str = "") -> MaterializeResult:
    """Write the model AND `dbt run --select` it into `marts`. The author's
    ship-it action. Cleans up the sandbox copy if present."""
    require_build_mode()
    name = validate_model_name(name)
    path = propose_model(name, raw_sql, question=question, rationale=rationale)
    env = os.environ.copy()
    env["DBT_PROFILES_DIR"] = str(DBT_PROJECT_DIR)
    proc = subprocess.run(
        _dbt_cmd() + ["run", "--select", name],
        cwd=str(DBT_PROJECT_DIR), env=env, capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return MaterializeResult(ok=False, model_path=str(path), dbt_output=out,
                                 error=f"dbt run failed (exit {proc.returncode})")
    try:
        drop_sandbox(name)  # the real table now lives in marts
    except Exception:
        pass
    return MaterializeResult(ok=True, model_path=str(path), dbt_output=out)
