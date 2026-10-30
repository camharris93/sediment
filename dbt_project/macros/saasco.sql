{#
  saasco staging + marts, written ONCE and instantiated per variant
  (`saasco` dev, `saasco_holdout`). Each model file is a two-line wrapper:
      {{ config(schema='<ds>_marts', alias='mart_x') }}
      {{ saasco_mart_x('<ds>') }}
  so the dev and holdout warehouses are built by byte-identical logic — a
  requirement for the holdout comparison to mean anything.

  Staging column types MUST match STAGING_TYPES in datasets/saasco/generate.py
  (the generator computes answer-key truths on a scratch copy of staging);
  tests/test_saasco_generator.py checks this against a built warehouse.
#}

{% macro saasco_stg_accounts(ds) %}
select
    cast(account_id as integer)          as account_id,
    cast(signup_date as date)            as signup_date,
    cast(company_size_band as varchar)   as company_size_band,
    cast(industry as varchar)            as industry,
    cast(region as varchar)              as region,
    cast(acquisition_channel as varchar) as acquisition_channel,
    cast(sales_assisted as boolean)      as sales_assisted
from {{ source(ds, 'accounts') }}
{% endmacro %}

{% macro saasco_stg_subscriptions(ds) %}
select
    cast(subscription_id as integer)     as subscription_id,
    cast(account_id as integer)          as account_id,
    cast(plan_tier as varchar)           as plan_tier,
    cast(billing_interval as varchar)    as billing_interval,
    cast(list_price_monthly as double)   as list_price_monthly,
    cast(seats as integer)               as seats,
    cast(start_date as date)             as start_date,
    cast(end_date as date)               as end_date,
    cast(status as varchar)              as status,
    cast(cancel_reason as varchar)       as cancel_reason
from {{ source(ds, 'subscriptions') }}
{% endmacro %}

{% macro saasco_stg_invoices(ds) %}
-- One row per payment ATTEMPT (failed retries, then the paid/refunded row).
select
    cast(invoice_id as integer)          as invoice_id,
    cast(subscription_id as integer)     as subscription_id,
    cast(account_id as integer)          as account_id,
    cast(invoice_period as varchar)      as invoice_period,
    cast(invoice_date as date)           as invoice_date,
    cast(amount as double)               as amount,
    cast(status as varchar)              as status,
    cast(attempt_number as integer)      as attempt_number
from {{ source(ds, 'invoices') }}
{% endmacro %}

{% macro saasco_stg_usage_weekly(ds) %}
select
    cast(usage_id as integer)            as usage_id,
    cast(account_id as integer)          as account_id,
    cast(week_start as date)             as week_start,
    cast(active_users as integer)        as active_users,
    cast(key_actions as integer)         as key_actions
from {{ source(ds, 'usage_weekly') }}
{% endmacro %}

{% macro saasco_stg_support_tickets(ds) %}
select
    cast(ticket_id as integer)           as ticket_id,
    cast(account_id as integer)          as account_id,
    cast(created_at as timestamp)        as created_at,
    cast(category as varchar)            as category,
    cast(priority as varchar)            as priority,
    cast(first_response_hours as double) as first_response_hours,
    cast(csat as integer)                as csat
from {{ source(ds, 'support_tickets') }}
{% endmacro %}


{# ── Marts ─────────────────────────────────────────────────────────────────
   Account-level churn: an account is active from its first subscription start
   until its LAST subscription ends (plan changes overlap, so an account's
   subscriptions are contiguous). An account churns in month M if it was active
   on the 1st of M and is not active on the 1st of M+1, i.e. its last end_date
   falls in M. A subscription ending because of a plan change is NOT churn. #}

{% macro saasco_account_spans(ds) %}
    select
        account_id,
        min(start_date) as first_start,
        case when bool_or(end_date is null) then null else max(end_date) end as account_end
    from {{ ref(ds ~ '__stg_subscriptions') }}
    group by account_id
{% endmacro %}

{% macro saasco_mart_account_months(ds) %}
with accounts as (select * from {{ ref(ds ~ '__stg_accounts') }}),
subs as (select * from {{ ref(ds ~ '__stg_subscriptions') }}),
invoices as (select * from {{ ref(ds ~ '__stg_invoices') }}),
usage as (select * from {{ ref(ds ~ '__stg_usage_weekly') }}),
spans as ({{ saasco_account_spans(ds) }}),
months as (
    select cast(unnest(generate_series(
        (select date_trunc('month', min(start_date)) from subs),
        (select date_trunc('month', max(invoice_date)) from invoices),
        interval 1 month)) as date) as month
),
account_months as (
    select
        s.account_id,
        m.month,
        greatest(m.month, s.first_start) as ref_date,
        s.first_start,
        s.account_end
    from spans s
    join months m
      on s.first_start < m.month + interval 1 month
     and (s.account_end is null or s.account_end >= m.month)
),
-- the subscription in force on the account's first active day of the month
current_sub as (
    select am.account_id, am.month, sub.subscription_id, sub.plan_tier, sub.billing_interval
    from account_months am
    join subs sub
      on sub.account_id = am.account_id
     and sub.start_date <= am.ref_date
     and (sub.end_date is null or sub.end_date >= am.ref_date)
    qualify row_number() over (partition by am.account_id, am.month order by sub.start_date desc) = 1
),
-- MRR from the latest PAID invoice row (not attempts) of that subscription
sub_mrr as (
    select cs.account_id, cs.month,
           arg_max(i.amount, i.invoice_date)
             / case when cs.billing_interval = 'annual' then 12 else 1 end as mrr
    from current_sub cs
    join invoices i
      on i.subscription_id = cs.subscription_id
     and i.status = 'paid'
     and i.invoice_date < cs.month + interval 1 month
    group by cs.account_id, cs.month, cs.billing_interval
),
usage_m as (
    select account_id, cast(date_trunc('month', week_start) as date) as month,
           avg(active_users) as active_users_avg
    from usage group by 1, 2
),
failed_m as (
    select am.account_id, am.month, count(i.invoice_id) as failed_attempts_30d
    from account_months am
    left join invoices i
      on i.account_id = am.account_id
     and i.status = 'failed'
     and i.invoice_date >  am.month + interval 1 month - interval 31 day
     and i.invoice_date <= am.month + interval 1 month - interval 1 day
    group by 1, 2
)
select
    cast(am.account_id as varchar) || '-' || strftime(am.month, '%Y-%m')   as account_month_id,
    am.account_id,
    am.month,
    cs.plan_tier,
    cs.billing_interval,
    round(coalesce(sm.mrr, 0), 2)                                           as mrr,
    date_diff('month', cast(date_trunc('month', am.first_start) as date), am.month) + 1 as tenure_month,
    cast(date_trunc('month', am.first_start) as date)                       as cohort_month,
    coalesce(am.account_end >= am.month
             and am.account_end < am.month + interval 1 month, false)       as is_churn_month,
    round(u.active_users_avg, 2)                                            as active_users_avg,
    f.failed_attempts_30d,
    a.company_size_band                                                     as size_band,
    a.acquisition_channel                                                   as channel,
    a.region,
    a.industry,
    a.sales_assisted
from account_months am
join accounts a using (account_id)
left join current_sub cs using (account_id, month)
left join sub_mrr sm using (account_id, month)
left join usage_m u using (account_id, month)
left join failed_m f using (account_id, month)
{% endmacro %}

{% macro saasco_mart_subscription_lifecycle(ds) %}
with subs as (select * from {{ ref(ds ~ '__stg_subscriptions') }}),
invoices as (select * from {{ ref(ds ~ '__stg_invoices') }}),
usage as (select * from {{ ref(ds ~ '__stg_usage_weekly') }}),
data_end as (select max(invoice_date) as d from invoices),
next_sub as (
    select s.subscription_id,
           exists (select 1 from subs s2
                   where s2.account_id = s.account_id
                     and s2.subscription_id <> s.subscription_id
                     and s2.start_date >= s.start_date
                     and s2.start_date <= coalesce(s.end_date, s2.start_date)) as has_successor
    from subs s
),
peak as (
    select s.subscription_id, max(u.active_users) as first_8wk_peak_active_users
    from subs s
    join usage u
      on u.account_id = s.account_id
     and u.week_start >= s.start_date
     and u.week_start <  s.start_date + interval 56 day
    group by 1
),
failed as (
    select s.subscription_id, count(i.invoice_id) as failed_attempts_before_end
    from subs s
    left join invoices i
      on i.subscription_id = s.subscription_id
     and i.status = 'failed'
     and i.invoice_date >= s.end_date - interval 30 day
     and i.invoice_date <= s.end_date
    where s.end_date is not null
    group by 1
)
select
    s.subscription_id,
    s.account_id,
    s.start_date,
    s.end_date,
    date_diff('month', s.start_date, coalesce(s.end_date, (select d from data_end))) as months_active,
    s.plan_tier,
    s.billing_interval,
    s.list_price_monthly,
    case when s.end_date is null then null
         when n.has_successor then 'plan_change'
         else 'churn' end                                                   as ended_by,
    coalesce(s.end_date is not null and not n.has_successor
             and s.end_date < s.start_date + interval 90 day, false)        as churned_within_90d,
    p.first_8wk_peak_active_users,
    f.failed_attempts_before_end
from subs s
join next_sub n using (subscription_id)
left join peak p using (subscription_id)
left join failed f using (subscription_id)
{% endmacro %}

{% macro saasco_mart_monthly_kpis(ds) %}
with am as (select * from {{ ref(ds ~ '__mart_account_months') }})
select
    month,
    count(*) filter (where tenure_month > 1)                                as starting_accounts,
    count(*) filter (where tenure_month = 1)                                as new_accounts,
    count(*) filter (where tenure_month > 1 and is_churn_month)             as churned_accounts,
    round(churned_accounts / nullif(starting_accounts, 0), 4)               as logo_churn_rate,
    round(sum(mrr) filter (where tenure_month > 1), 2)                      as starting_mrr,
    round(sum(mrr) filter (where tenure_month > 1 and is_churn_month), 2)   as churned_mrr
from am
group by month
{% endmacro %}
