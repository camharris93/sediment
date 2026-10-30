-- Mart (human-curated; see macros/saasco.sql for the logic and docs/investigate.md §8.2).
{{ config(schema='saasco_marts', alias='mart_subscription_lifecycle') }}
{{ saasco_mart_subscription_lifecycle('saasco') }}
