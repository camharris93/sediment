-- Staging for saasco_raw.support_tickets: explicit types, names unchanged. Logic lives in
-- macros/saasco.sql so dev and holdout are built identically.
{{ config(schema='saasco_staging', alias='stg_support_tickets') }}
{{ saasco_stg_support_tickets('saasco') }}
