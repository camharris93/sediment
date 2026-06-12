"""L5 read-only guard — the security boundary of the NL->SQL agent.

The trust model rests on this: the agent may ONLY run a single read-only query
against curated warehouse relations. These tests are adversarial — they assert the
guard rejects every known way to turn "a SELECT" into a write, a multi-statement
payload, or a read of something outside the warehouse (local files / network).

If you add a capability to the guard, add the attack that motivated it here first.
"""
from __future__ import annotations

import pytest

from engine.query.dry_run import DryRunReport
from engine.query.execution import guarded_execute, is_read_only_query

# ── Payloads that MUST be refused ────────────────────────────────────────────
ATTACKS = {
    # writes / DDL
    "drop":              "DROP TABLE anage_marts.mart_longevity_by_class",
    "delete":            "DELETE FROM anage_marts.mart_longevity_by_class",
    "update":            "UPDATE anage_staging.stg_anage SET class='x'",
    "insert":            "INSERT INTO anage_marts.x VALUES (1)",
    "create":            "CREATE TABLE anage_marts.evil AS SELECT 1",
    "alter":             "ALTER TABLE anage_marts.x ADD COLUMN y INT",
    "truncate":          "TRUNCATE anage_marts.x",
    # multi-statement smuggling (DuckDB executes ;-joined statements)
    "multi_drop":        "SELECT 1; DROP TABLE anage_marts.mart_longevity_by_class",
    "multi_trailing":    "SELECT * FROM anage_marts.mart_longevity_by_class; DELETE FROM anage_marts.x",
    "multi_attach":      "ATTACH '/tmp/evil.duckdb' AS e; SELECT 1",
    # filesystem write
    "copy_to_file":      "COPY (SELECT 1) TO '/tmp/exfil.csv' (FORMAT CSV)",
    # local-file READ via table functions (read-only DB does NOT block these)
    "read_csv":          "SELECT * FROM read_csv('/etc/passwd')",
    "read_csv_auto":     "SELECT * FROM read_csv_auto('C:/Windows/win.ini')",
    "read_parquet":      "SELECT * FROM read_parquet('/etc/shadow')",
    "read_text":         "SELECT * FROM read_text('/etc/hostname')",
    "read_json":         "SELECT * FROM read_json_auto('/secret.json')",
    "glob":              "SELECT * FROM glob('/**')",
    "read_csv_nested":   "SELECT class FROM anage_marts.mart_longevity_by_class "
                         "WHERE class IN (SELECT col0 FROM read_csv('/etc/passwd'))",
    "read_csv_join":     "SELECT * FROM anage_marts.mart_longevity_by_class m "
                         "JOIN read_csv('/etc/passwd') f ON true",
    # engine-state / extension loading (network exfil surface)
    "install":           "INSTALL httpfs",
    "load":              "LOAD httpfs",
    "set_extdir":        "SET extension_directory='/tmp'",
    "pragma":            "PRAGMA database_list",
    "use":               "USE evil",
    "attach":            "ATTACH '/tmp/x.duckdb' AS x",
}

# ── Payloads that MUST pass (legitimate analytical SQL) ──────────────────────
LEGIT = {
    "simple":      "SELECT * FROM anage_marts.mart_longevity_by_class",
    "projection":  "SELECT class, n_species FROM anage_marts.mart_longevity_by_class",
    "join":        "SELECT s.species FROM anage_staging.stg_anage s "
                   "JOIN anage_marts.mart_longevity_by_class m ON s.class = m.class",
    "cte":         "WITH t AS (SELECT class FROM anage_marts.mart_longevity_by_class) SELECT * FROM t",
    "union":       "SELECT class FROM anage_marts.mart_longevity_by_class UNION SELECT class FROM anage_staging.stg_anage",
    "aggregate":   "SELECT class, COUNT(*) FROM anage_staging.stg_anage GROUP BY class HAVING COUNT(*) > 1",
    "subquery":    "SELECT * FROM anage_marts.mart_longevity_by_class WHERE n_species > (SELECT AVG(n_species) FROM anage_marts.mart_longevity_by_class)",
    "scalar_func": "SELECT upper(class), round(max_longevity, 1) FROM anage_marts.mart_longevity_by_class",
    "trailing_semicolon": "SELECT * FROM anage_marts.mart_longevity_by_class;",
}


@pytest.mark.parametrize("name", list(ATTACKS))
def test_attacks_are_refused(name):
    ok, reason = is_read_only_query(ATTACKS[name])
    assert ok is False, f"SECURITY: attack '{name}' was NOT refused"
    assert reason, "a refusal must carry a human-readable reason"


@pytest.mark.parametrize("name", list(LEGIT))
def test_legitimate_queries_pass(name):
    ok, reason = is_read_only_query(LEGIT[name])
    assert ok is True, f"false positive: legit query '{name}' was refused ({reason})"


def test_unparseable_sql_is_refused():
    ok, reason = is_read_only_query("SELECT FROM WHERE )(")
    assert ok is False and reason


def test_empty_sql_is_refused():
    ok, _ = is_read_only_query("")
    assert ok is False


# ── End-to-end: the guard refuses BEFORE touching the database ───────────────

def test_guarded_execute_runs_legit_query(warehouse):
    out = guarded_execute(
        "SELECT class, n_species FROM anage_marts.mart_longevity_by_class ORDER BY class",
        DryRunReport(ok=True), warehouse=warehouse,
    )
    assert out.ok
    assert out.provenance and out.provenance.row_count >= 1


def test_guarded_execute_refuses_local_file_read(warehouse):
    out = guarded_execute(
        "SELECT * FROM read_csv('/etc/passwd')", DryRunReport(ok=True), warehouse=warehouse,
    )
    assert not out.ok
    assert out.refusal and out.refusal.kind == "non_read_only"


def test_guarded_execute_refuses_when_dry_run_failed(warehouse):
    out = guarded_execute(
        "SELECT 1", DryRunReport(ok=False, summary="boom"), warehouse=warehouse,
    )
    assert not out.ok
    assert out.refusal and out.refusal.kind == "dry_run_not_passed"


def test_row_cap_truncates(warehouse):
    # MAX_RESULT_ROWS is large; assert the plumbing reports truncation correctly
    # by capping low through the warehouse directly is covered elsewhere — here we
    # just confirm a normal query returns untruncated.
    out = guarded_execute(
        "SELECT * FROM anage_staging.stg_anage", DryRunReport(ok=True), warehouse=warehouse,
    )
    assert out.ok and out.provenance and out.provenance.truncated is False
