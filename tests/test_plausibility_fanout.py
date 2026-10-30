"""L6 fan-out detection must see the FROM table on every supported sqlglot.

Regression: sqlglot renamed the Select arg "from" -> "from_", which silently
emptied the alias map and disabled fan-out warnings entirely.
"""
from __future__ import annotations

import duckdb

from engine.query.grounding import build_grounding_context
from engine.query.plausibility import WarningKind, _resolve_alias_map, check_fanout
from engine.query.warehouse import DuckDBWarehouse


def _ctx(tmp_path):
    p = tmp_path / "fan.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE SCHEMA demo_staging")
    con.execute("CREATE TABLE demo_staging.stg_orders AS SELECT * FROM (VALUES (1, 10.0), (2, 20.0), (3, 30.0)) t(order_id, order_total)")
    con.execute("CREATE TABLE demo_staging.stg_payments AS SELECT * FROM (VALUES (1, 1, 'failed'), (2, 1, 'paid'), (3, 2, 'paid'), (4, 3, 'paid')) t(payment_id, order_id, status)")
    con.close()
    return build_grounding_context("demo", warehouse=DuckDBWarehouse(path=p))


def test_alias_map_includes_from_table():
    import sqlglot
    ast = sqlglot.parse_one("select 1 from s.a x join s.b y on x.id = y.id", dialect="duckdb")
    assert _resolve_alias_map(ast) == {"x": "s.a", "y": "s.b"}


def test_sum_of_parent_column_across_one_to_many_join_is_flagged(tmp_path):
    ctx = _ctx(tmp_path)
    sql = ("select sum(o.order_total) as revenue from demo_staging.stg_orders o "
           "join demo_staging.stg_payments p on p.order_id = o.order_id")
    ws = check_fanout(sql, ctx)
    assert ws and ws[0].kind == WarningKind.fanout_risk


def test_count_star_across_join_is_flagged_and_single_table_is_not(tmp_path):
    ctx = _ctx(tmp_path)
    joined = ("select count(*) as n from demo_staging.stg_orders o "
              "join demo_staging.stg_payments p on p.order_id = o.order_id")
    assert check_fanout(joined, ctx)
    assert check_fanout("select sum(order_total) from demo_staging.stg_orders", ctx) == []
