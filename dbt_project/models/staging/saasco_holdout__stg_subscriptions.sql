-- Staging for saasco_holdout_raw.subscriptions: explicit types, names unchanged. Logic lives in
-- macros/saasco.sql so dev and holdout are built identically.
{{ config(schema='saasco_holdout_staging', alias='stg_subscriptions') }}
{{ saasco_stg_subscriptions('saasco_holdout') }}
