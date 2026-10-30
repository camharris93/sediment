-- Staging for saasco_holdout_raw.accounts: explicit types, names unchanged. Logic lives in
-- macros/saasco.sql so dev and holdout are built identically.
{{ config(schema='saasco_holdout_staging', alias='stg_accounts') }}
{{ saasco_stg_accounts('saasco_holdout') }}
