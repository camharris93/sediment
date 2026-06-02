"""Run-time orchestration agent (PRD §5.2) — an agentic wrapper around a
DETERMINISTIC core.

It drives `load -> profile -> dbt run -> dbt test`, reads dbt's structured
artifacts, and adds judgment at the decision points only:

  • dbt test FAILS  -> pull the offending rows, explain in plain English which
                       test failed and why, and propose a fix (LLM).
  • row counts SWING -> compare against the last run's snapshot and flag drift.

The LLM never writes or regenerates the transforms — it explains and advises.
Everything still runs (and fails loudly) without a key; the key only adds the
plain-English explanation and fix proposal.

    python -m engine.orchestrate anage
    python -m engine.orchestrate anage --break   # inject a failing test to demo

The `--break` flag drops a deliberately-failing singular test in, so you can see
the agent catch and explain a real failure, then cleans it up.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb

from .config import (
    DBT_PROJECT_DIR,
    WAREHOUSE_PATH,
    has_anthropic_key,
    load_dataset_config,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_STATE = DBT_PROJECT_DIR.parent / ".cache" / "orchestrate_state.json"
_RUN_RESULTS = DBT_PROJECT_DIR / "target" / "run_results.json"
_DEMO_TEST = DBT_PROJECT_DIR / "tests" / "_demo_broken.sql"


def _dbt_cmd() -> list[str]:
    exe = shutil.which("dbt")
    return [exe] if exe else [sys.executable, "-m", "dbt.cli.main"]


def _dbt(args: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DBT_PROFILES_DIR"] = str(DBT_PROJECT_DIR)
    print(f"\n$ dbt {' '.join(args)}")
    return subprocess.run(
        _dbt_cmd() + args, cwd=str(DBT_PROJECT_DIR), env=env,
        capture_output=True, text=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# dbt artifact parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_run_results() -> list[dict[str, Any]]:
    if not _RUN_RESULTS.exists():
        return []
    data = json.loads(_RUN_RESULTS.read_text(encoding="utf-8"))
    return data.get("results", [])


def _failures(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        if r.get("status") in ("fail", "error"):
            node = r.get("unique_id", "?")
            out.append({
                "node": node,
                "status": r.get("status"),
                "failures": r.get("failures"),
                "message": (r.get("message") or "").strip(),
                "name": node.split(".")[-1],
            })
    return out


def _compiled_sql_for(node: str) -> str | None:
    """Locate the compiled SQL for a test node so we can pull example rows."""
    # node looks like test.sediment.<name>.<hash>
    name = node.split(".")[2] if len(node.split(".")) >= 3 else node
    base = DBT_PROJECT_DIR / "target" / "compiled" / "sediment"
    for p in base.rglob(f"{name}.sql"):
        return p.read_text(encoding="utf-8")
    return None


def _sample_failing_rows(node: str, limit: int = 5) -> list[dict]:
    sql = _compiled_sql_for(node)
    if not sql:
        return []
    try:
        con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        try:
            df = con.execute(f"select * from ({sql.strip().rstrip(';')}) as t limit {limit}").fetchdf()
            return df.to_dict(orient="records")
        finally:
            con.close()
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Row-count monitoring
# ─────────────────────────────────────────────────────────────────────────────

def _snapshot_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        rows = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in ('raw','staging','marts')"
        ).fetchall()
        for sch, tbl in rows:
            try:
                (n,) = con.execute(f'select count(*) from "{sch}"."{tbl}"').fetchone()
                counts[f"{sch}.{tbl}"] = int(n)
            except Exception:
                pass
    finally:
        con.close()
    return counts


def _load_state() -> dict:
    if _STATE.exists():
        try:
            return json.loads(_STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(counts: dict[str, int]) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps({"counts": counts}, indent=2), encoding="utf-8")


def _detect_swings(prev: dict[str, int], cur: dict[str, int], threshold: float = 0.20) -> list[str]:
    flags: list[str] = []
    for tbl, n in cur.items():
        if tbl not in prev:
            flags.append(f"NEW table {tbl} ({n:,} rows)")
            continue
        p = prev[tbl]
        if p == 0:
            continue
        delta = (n - p) / p
        if abs(delta) >= threshold:
            arrow = "up" if delta > 0 else "down"
            flags.append(f"{tbl}: {p:,} -> {n:,} ({delta:+.0%} {arrow})")
    for tbl in prev:
        if tbl not in cur:
            flags.append(f"DROPPED table {tbl} (was {prev[tbl]:,} rows)")
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# LLM explanation (decision point only)
# ─────────────────────────────────────────────────────────────────────────────

_EXPLAIN_SYSTEM = """\
You are the run-time ORCHESTRATION agent for a dbt + DuckDB analytics pipeline.
A `dbt test` just failed. You are given the failed test node(s), dbt's message,
and a few example offending rows. Explain — in plain English, for an analytics
engineer — WHICH test failed, WHY (what the data shows), and propose a concrete
fix (a tweak to the staging SQL, a junk-row filter, a relaxed/relocated test, or
a data-quality note). You do NOT rewrite the whole model; you advise. Be concise:
a short paragraph per failure plus a clearly-labeled "Proposed fix:" line.
"""


def _explain_failures(failures: list[dict]) -> str | None:
    if not has_anthropic_key():
        return None
    from ._llm import complete
    from .config import get_ai_settings

    blocks = []
    for f in failures:
        rows = _sample_failing_rows(f["node"])
        blocks.append(
            f"### {f['name']} ({f['status']}, {f.get('failures')} failing rows)\n"
            f"dbt message: {f['message']}\n"
            f"example offending rows: {json.dumps(rows, default=str)[:1500]}"
        )
    user = "The following dbt tests failed:\n\n" + "\n\n".join(blocks)
    try:
        text, usage = complete(
            system=_EXPLAIN_SYSTEM, user=user,
            model=get_ai_settings().model, max_tokens=1200, cache_system=False,
        )
        print(f"  [llm] model={usage.model} in={usage.input_tokens} out={usage.output_tokens}")
        return text.strip()
    except Exception as exc:
        return f"(LLM explanation unavailable: {exc})"


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrate
# ─────────────────────────────────────────────────────────────────────────────

def _inject_break() -> None:
    _DEMO_TEST.parent.mkdir(parents=True, exist_ok=True)
    _DEMO_TEST.write_text(
        "-- DEMO failing test injected by `orchestrate --break`. Auto-removed after.\n"
        "-- Asserts every species has a recorded maximum longevity. It does NOT —\n"
        "-- ~10% are null — so this test returns rows and dbt marks it failed.\n"
        "select hagrid, common_name, kingdom\n"
        "from {{ ref('stg_anage') }}\n"
        "where max_longevity_yrs is null\n",
        encoding="utf-8",
    )
    print("  [break] injected a deliberately-failing test (tests/_demo_broken.sql)")


def orchestrate(dataset: str, *, inject_break: bool = False) -> int:
    cfg = load_dataset_config(dataset)
    print(f"== Orchestrating '{cfg.name}' ==")

    # 1. Deterministic build steps.
    print("\n[1/4] load")
    subprocess.run([sys.executable, "-m", "engine.ingest", dataset], check=True)
    print("\n[2/4] profile")
    subprocess.run([sys.executable, "-m", "engine.profile", dataset], check=True)

    print("\n[3/4] dbt run")
    rr = _dbt(["run"])
    if rr.returncode != 0:
        print(rr.stdout[-2000:])
        print(rr.stderr[-1000:])
        print("\n[agent] dbt run failed before tests — see output above.")
        return rr.returncode

    if inject_break:
        _inject_break()

    print("\n[4/4] dbt test")
    try:
        tr = _dbt(["test"])
        results = _parse_run_results()
        failures = _failures(results)

        # 2. Anomaly detection (row-count swings vs last run).
        cur = _snapshot_counts()
        prev = _load_state().get("counts", {})
        swings = _detect_swings(prev, cur)
        _save_state(cur)

        print("\n" + "=" * 70)
        print("ORCHESTRATION REPORT")
        print("=" * 70)
        passed = sum(1 for r in results if r.get("status") == "pass")
        print(f"Tests: {passed} passed, {len(failures)} failed/errored "
              f"(of {len(results)} nodes).")

        if swings:
            print("\n[anomaly] row-count drift since last run:")
            for s in swings:
                print(f"  - {s}")
        else:
            print("\n[anomaly] no significant row-count drift since last run.")

        if not failures:
            print("\n[agent] All tests green. Pipeline is sound.")
            return 0

        print(f"\n[agent] {len(failures)} test(s) failed:")
        for f in failures:
            print(f"  - {f['name']}: {f.get('failures')} failing rows")

        explanation = _explain_failures(failures)
        if explanation:
            print("\n--- Plain-English explanation (agent) ---\n")
            print(explanation)
        else:
            print(
                "\n(No Anthropic key configured — showing raw failures only. Set a key "
                "for plain-English explanations and fix proposals.)"
            )
            for f in failures:
                rows = _sample_failing_rows(f["node"])
                print(f"\n  {f['name']}: {f['message']}")
                for r in rows[:3]:
                    print(f"    offending row: {r}")
        # A demo break is expected to fail; don't propagate non-zero in that case.
        return 0 if inject_break else 1
    finally:
        if inject_break and _DEMO_TEST.exists():
            _DEMO_TEST.unlink()
            print("\n  [break] removed the injected demo test.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run/monitor/explain the pipeline.")
    ap.add_argument("dataset", help="dataset name (folder under datasets/)")
    ap.add_argument("--break", dest="inject_break", action="store_true",
                    help="inject a deliberately-failing test to demo the explainer")
    args = ap.parse_args(argv)
    return orchestrate(args.dataset, inject_break=args.inject_break)


if __name__ == "__main__":
    sys.exit(main())
