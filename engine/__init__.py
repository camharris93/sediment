"""sediment engine — the dataset-agnostic core.

Everything in this package is written ONCE and works for any tabular dataset:
ingestion, profiling, AI build-time scaffolding, run-time orchestration, and the
NL→SQL query agent. What a dataset's marts *mean* stays human-curated in
`datasets/<name>/` and `dbt_project/models/marts/`.
"""

__version__ = "0.1.0"
