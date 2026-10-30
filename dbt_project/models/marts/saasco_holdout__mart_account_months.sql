-- Mart (human-curated; see macros/saasco.sql for the logic and docs/investigate.md §8.2).
{{ config(schema='saasco_holdout_marts', alias='mart_account_months') }}
{{ saasco_mart_account_months('saasco_holdout') }}
