"""Shared pytest fixtures.

Everything here is OFFLINE and KEY-FREE: a tiny throwaway DuckDB warehouse built
in a temp dir, plus a GroundingContext derived from it. No network, no Anthropic
key, no touching the repo's real warehouse.duckdb.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from engine.query.grounding import build_grounding_context
from engine.query.warehouse import DuckDBWarehouse


@pytest.fixture
def warehouse_path(tmp_path: Path) -> Path:
    """A minimal two-schema warehouse mirroring the real layout:
    <dataset>_marts.<mart> + <dataset>_staging.<stg>, so grounding/validation/
    execution have real relations to bind against."""
    path = tmp_path / "test.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE SCHEMA anage_staging;")
        con.execute("CREATE SCHEMA anage_marts;")
        con.execute(
            """
            CREATE TABLE anage_staging.stg_anage AS
            SELECT * FROM (VALUES
                (1, 'Rougheye rockfish', 'Teleostei', 495.0, 205.0),
                (2, 'Olm',               'Amphibia',   17.0, 102.0),
                (3, 'Eastern box turtle','Reptilia',  372.0, 138.0),
                (4, 'Naked mole-rat',    'Mammalia',   35.0,  31.0),
                (5, 'House mouse',       'Mammalia',   20.0,   4.0)
            ) AS t(hagrid, species, class, body_mass_g, longevity_yrs);
            """
        )
        con.execute(
            """
            CREATE TABLE anage_marts.mart_longevity_by_class AS
            SELECT class,
                   COUNT(*)            AS n_species,
                   MAX(longevity_yrs)  AS max_longevity
            FROM anage_staging.stg_anage
            GROUP BY class;
            """
        )
    finally:
        con.close()
    return path


@pytest.fixture
def warehouse(warehouse_path: Path) -> DuckDBWarehouse:
    return DuckDBWarehouse(path=warehouse_path)


@pytest.fixture
def grounding(warehouse: DuckDBWarehouse):
    """A GroundingContext over the test warehouse."""
    return build_grounding_context("anage", warehouse=warehouse)
