-- Mart (human-curated; see macros/saasco.sql for the logic and docs/investigate.md §8.2).
{{ config(schema='saasco_marts', alias='mart_monthly_kpis') }}
{{ saasco_mart_monthly_kpis('saasco') }}
