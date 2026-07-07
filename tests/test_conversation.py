from __future__ import annotations

from types import SimpleNamespace

import duckdb

from engine.query.conversation import Session, Turn
from engine.query.grounding import ColumnInfo, GroundingContext, TableInfo


def _synthetic(name: str, sql: str, columns: list[ColumnInfo] | None = None) -> TableInfo:
    return TableInfo(
        schema="",
        name=name,
        fully_qualified=name,
        row_count=1,
        columns=columns or [ColumnInfo("player_id", "INTEGER", False)],
        is_synthetic=True,
        synthetic_sql=sql,
    )


def test_self_contained_sql_for_turn_inlines_multi_hop_deps_without_self_alias():
    """Promoting a multi-hop answer must not leave dangling hop_N_result refs.

    This mirrors the Build-tab failure mode: the final/display SQL defines its
    own turn_1_result CTE, but still reads hop_1_result from an earlier hop.
    The session also has a current-turn synthetic named turn_1_result; that alias
    represents the answer itself and must not be injected into the answer SQL.
    """
    hop_1 = _synthetic(
        "hop_1_result",
        "SELECT 1 AS player_id, 'A Player' AS player_name, 42 AS pts",
        [
            ColumnInfo("player_id", "INTEGER", False),
            ColumnInfo("player_name", "VARCHAR", False),
            ColumnInfo("pts", "INTEGER", False),
        ],
    )
    display_sql = """
WITH turn_1_result AS (
    SELECT 1 AS player_id
),
base AS (
    SELECT h.player_id, h.player_name, h.pts
    FROM hop_1_result h
    INNER JOIN turn_1_result t
        ON h.player_id = t.player_id
)
SELECT * FROM base
"""
    result = SimpleNamespace(sql=display_sql, hops=[SimpleNamespace(synthetic=hop_1)])
    # This is the current answer's own turn-level alias. If the promotion helper
    # used session.prior_synthetics() wholesale, it would inject this and create
    # a duplicate/shadowing turn_1_result CTE.
    current_turn_alias = _synthetic("turn_1_result", "SELECT 999 AS player_id")
    turn = Turn(index=1, question="multi-hop question", result=result,
                synthetic=current_turn_alias)
    session = Session(dataset="nba", turns=[turn],
                      _base_ctx=GroundingContext(schemas=[], tables=[], relationships=[]))

    promoted = session.self_contained_sql_for_turn(turn)

    assert promoted is not None
    assert "hop_1_result AS (" in promoted
    assert "SELECT 999 AS player_id" not in promoted
    assert promoted.lower().count("turn_1_result as") == 1

    rows = duckdb.connect().execute(promoted).fetchall()
    assert rows == [(1, "A Player", 42)]


def test_self_contained_sql_for_turn_inlines_prior_turn_and_hop_deps():
    prior = _synthetic(
        "turn_1_result",
        "SELECT 7 AS player_id",
        [ColumnInfo("player_id", "INTEGER", False)],
    )
    prior_turn = Turn(
        index=1,
        question="top players",
        result=SimpleNamespace(sql="SELECT 7 AS player_id", hops=[]),
        synthetic=prior,
    )
    hop_1 = _synthetic(
        "hop_1_result",
        "SELECT 7 AS player_id, 12 AS pts",
        [ColumnInfo("player_id", "INTEGER", False), ColumnInfo("pts", "INTEGER", False)],
    )
    current_sql = """
SELECT h.player_id, h.pts
FROM hop_1_result h
INNER JOIN turn_1_result t
    ON h.player_id = t.player_id
"""
    current = Turn(
        index=2,
        question="score those players",
        result=SimpleNamespace(sql=current_sql, hops=[SimpleNamespace(synthetic=hop_1)]),
        synthetic=_synthetic("turn_2_result", "SELECT 123 AS should_not_inline"),
    )
    session = Session(dataset="nba", turns=[prior_turn, current],
                      _base_ctx=GroundingContext(schemas=[], tables=[], relationships=[]))

    promoted = session.self_contained_sql_for_turn(current)

    assert promoted is not None
    assert "hop_1_result AS (" in promoted
    assert "turn_1_result AS (" in promoted
    assert "SELECT 123 AS should_not_inline" not in promoted
    assert duckdb.connect().execute(promoted).fetchall() == [(7, 12)]
