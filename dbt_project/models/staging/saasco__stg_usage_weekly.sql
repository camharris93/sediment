-- Staging for saasco_raw.usage_weekly: explicit types, names unchanged. Logic lives in
-- macros/saasco.sql so dev and holdout are built identically.
{{ config(schema='saasco_staging', alias='stg_usage_weekly') }}
{{ saasco_stg_usage_weekly('saasco') }}
