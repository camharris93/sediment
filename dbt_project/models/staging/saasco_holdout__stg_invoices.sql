-- Staging for saasco_holdout_raw.invoices: explicit types, names unchanged. Logic lives in
-- macros/saasco.sql so dev and holdout are built identically.
{{ config(schema='saasco_holdout_staging', alias='stg_invoices') }}
{{ saasco_stg_invoices('saasco_holdout') }}
