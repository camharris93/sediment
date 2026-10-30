"""Build both saasco answer keys (dev + holdout) from ONE template, so their
fixture SQL is identical logic with only the variant's segments/dates swapped.

    python evals/investigations/build_answer_keys.py      # writes keys with observed: null
    python datasets/saasco/generate.py                    # fills dev observed values
    python datasets/saasco/generate.py --variant holdout  # fills holdout observed values

Edit stories/truths HERE, never in the YAML.
"""
import textwrap

VAR = {
 "dev": dict(file="saasco_answer_key.yaml", S="saasco_staging", ds="saasco", FR="EU", CH="paid_social",
             PC="2025-07-01", MIX="2025-10-01", DEC_ATTR="industry", DEC_VAL="healthcare",
             s4_kind="driver"),
 "holdout": dict(file="saasco_holdout_answer_key.yaml", S="saasco_holdout_staging", ds="saasco_holdout",
             FR="LATAM", CH="partner", PC="2025-03-01", MIX="2025-09-01", DEC_ATTR="region", DEC_VAL="APAC",
             s4_kind="absent"),
}
# Every column is alias-qualified: L3 rejects some valid unqualified CTE SQL
# (see docs/investigate.md "Findings while building").
SPANS = ("select s.account_id, min(s.start_date) as first_start, "
         "case when bool_or(s.end_date is null) then null else max(s.end_date) end as account_end "
         "from {S}.stg_subscriptions s group by s.account_id")
AM = ("with sp as (" + SPANS + "),\n"
      "m as (select cast(unnest(generate_series(date '2024-02-01', date '2026-06-01', interval 1 month)) as date) as m),\n"
      "am as (select a.company_size_band as b, a.sales_assisted as sa,\n"
      "  case when sp.account_end >= m.m and sp.account_end < m.m + interval 1 month then 1.0 else 0.0 end as c\n"
      "  from m join sp on sp.first_start < m.m and (sp.account_end is null or sp.account_end >= m.m)\n"
      "  join {S}.stg_accounts a on a.account_id = sp.account_id)\n")
def t(id, desc, sql, tol):
    return dict(id=id, desc=desc, sql=sql, tol=tol)
def build(v):
    S=v["S"]
    def q(s):
        return s.replace("{S}", S).format(**v)
    stories = []
    # S1
    stories.append(dict(id="S1", kind="driver",
      statement="Annual accounts churn at renewal, and the large 2025-Q2 annual cohort renewing in 2026-Q2 drives the Q2 2026 rise in churn.",
      mechanism="A 2025-Q2 promotion pulled in a big annual-prepay cohort with many low-usage accounts. Prepaid accounts cannot churn mid-term, so the churn is deferred to the renewal month; their usage falls 3-5 months before.",
      found_by="Churn by billing_interval x months-to-renewal (or tenure month 12/24) plus the size of the 2025-Q2 annual cohort.",
      naive="Reading the Q2 2026 spike as a product or market problem.",
      match=[ "{metric_in: [logo_churn_rate, revenue_churn_rate], dimension_any: [billing_interval], segment: {billing_interval: annual}, direction: higher}",
              "{dimension_any: ['derived:*renew*', 'derived:*months_to*', 'derived:*anniversar*'], direction: higher}",
              "{dimension_any: [cohort_month, 'derived:*cohort*'], segment: {billing_interval: annual}, direction: higher}"],
      truths=[t("S1.t1","Renewal-month churn rate of annual subscriptions due to renew in 2026-Q2.",
        q("with due as (select s.end_date, cast(unnest(generate_series(s.start_date + interval 12 month, date '2026-06-30', interval 12 month)) as date) as renewal\n"
          "  from {S}.stg_subscriptions s where s.billing_interval = 'annual')\n"
          "select avg(case when d.end_date = d.renewal then 1.0 else 0.0 end) from due d\n"
          "where d.renewal between date '2026-04-01' and date '2026-06-30' and (d.end_date is null or d.end_date >= d.renewal)"),
          "tolerance_abs: 0.03"),
        t("S1.t2","Mean monthly logo churn rate, 2026-Q2 divided by 2026-Q1.",
        q("with sp as (" + SPANS + "),\n"
          "mo as (select cast(unnest(generate_series(date '2026-01-01', date '2026-06-01', interval 1 month)) as date) as m),\n"
          "r as (select mo.m as month_start, avg(case when sp.account_end >= mo.m and sp.account_end < mo.m + interval 1 month then 1.0 else 0.0 end) as rate\n"
          "  from mo join sp on sp.first_start < mo.m and (sp.account_end is null or sp.account_end >= mo.m) group by mo.m)\n"
          "select avg(r.rate) filter (where r.month_start >= date '2026-04-01') / avg(r.rate) filter (where r.month_start < date '2026-04-01') from r"),
          "tolerance_rel: 0.10"),
        t("S1.t3","Annual subscriptions started in 2025-Q2 divided by those started in 2024-Q2.",
        q("select count(*) filter (where start_date between date '2025-04-01' and date '2025-06-30') * 1.0\n"
          "  / count(*) filter (where start_date between date '2024-04-01' and date '2024-06-30')\n"
          "from {S}.stg_subscriptions where billing_interval = 'annual'"), "tolerance_rel: 0.15")]))
    # S2
    def s2sql(cond):
        return q("with sp as (" + SPANS + "),\n"
          "fs as (select s.plan_tier, s.billing_interval, s.start_date, sp.account_end from {S}.stg_subscriptions s\n"
          "  join sp on sp.account_id = s.account_id and s.start_date = sp.first_start),\n"
          "c as (select fs.start_date >= date '{PC}' as post, avg(case when fs.account_end < fs.start_date + interval 90 day then 1.0 else 0.0 end) as r\n"
          "  from fs where fs.billing_interval = 'monthly' and " + cond + "\n"
          "  and fs.start_date >= date '{PC}' - interval 6 month and fs.start_date < date '{PC}' + interval 6 month group by 1)\n"
          "select max(c.r) filter (where c.post) / max(c.r) filter (where not c.post) from c")
    stories.append(dict(id="S2", kind="driver",
      statement="A starter price increase on {PC} (grandfathered for existing subscriptions) splits cohorts: new starter accounts after the change churn early far more than the pre-change cohort, while existing starters are unaffected.".format(**v),
      mechanism="Starter list_price_monthly steps up for subscriptions starting on/after the change date; the post-change new cohort has a much higher 90-day churn hazard.",
      found_by="Early (90-day) churn by start_date before/after the price step within plan_tier = starter, discovered through the list_price_monthly step.",
      naive="Averaging old and new starters together ('starter churn barely moved').",
      match=["{dimension_any: [start_date, list_price_monthly, cohort_month, 'derived:*price*', 'derived:*cohort*', 'derived:*period*'], segment: {plan_tier: starter}, direction: higher}",
             "{metric_in: [early_churn_rate], dimension_any: [list_price_monthly, 'derived:*price*'], direction: higher}"],
      truths=[t("S2.t1","Starter (monthly, new accounts) 90-day churn: 6 months after the price change / 6 months before.", s2sql("fs.plan_tier = 'starter'"), "tolerance_rel: 0.15"),
              t("S2.t2","Same ratio for growth + pro (the control: should be near 1).", s2sql("fs.plan_tier <> 'starter'"), "tolerance_rel: 0.15")]))
    # S3
    s3base = ("with sp as (" + SPANS + "),\n"
          "ch as (select sp.account_id, sp.account_end, a.region from sp join {S}.stg_accounts a on a.account_id = sp.account_id\n"
          "  where sp.account_end >= date '2026-01-01'),\n"
          "f as (select ch.account_id, count(i.invoice_id) as n_failed from ch left join {S}.stg_invoices i\n"
          "  on i.account_id = ch.account_id and i.status = 'failed'\n"
          "  and i.invoice_date between ch.account_end - interval 30 day and ch.account_end group by ch.account_id)\n")
    stories.append(dict(id="S3", kind="driver",
      statement="From 2026-01 a payment-failure spike in {FR} causes involuntary churn that is recorded as voluntary: most {FR} cancellations in 2026 follow 2+ failed payment attempts, yet cancel_reason says customer_request or is null.".format(**v),
      mechanism="Card-on-file failures for tenured {FR} accounts from 2026-01; failed retries end the subscription.".format(**v),
      found_by="Share of churners with failed attempts before end_date, by region and period.",
      naive="Trusting cancel_reason: '{FR} customers are choosing to leave'.".format(**v),
      match=["{{metric_in: [failed_payment_share], segment: {{region: {FR}}}, direction: higher}}".format(**v),
             "{{dimension_any: [failed_attempts_30d, failed_attempts_before_end, 'derived:*fail*', 'derived:*payment*', 'derived:*involuntary*'], segment: {{region: {FR}}}, direction: higher}}".format(**v),
             "{{dimension_all: [region, '*fail*'], segment: {{region: {FR}}}, direction: higher}}".format(**v)],
      truths=[t("S3.t1","Share of {FR} accounts churned in 2026 with >= 2 failed attempts in the 30 days before churn.".format(**v),
                q(s3base + "select avg(case when n_failed >= 2 then 1.0 else 0.0 end) from ch join f using (account_id) where region = '{FR}'"), "tolerance_abs: 0.05"),
              t("S3.t2","Same share for all other regions.",
                q(s3base + "select avg(case when n_failed >= 2 then 1.0 else 0.0 end) from ch join f using (account_id) where region <> '{FR}'"), "tolerance_abs: 0.03"),
              t("S3.t3","Of those {FR} failed-payment churners, the share whose final cancel_reason is customer_request or null.".format(**v),
                q(s3base + "select avg(case when s.cancel_reason is null or s.cancel_reason = 'customer_request' then 1.0 else 0.0 end)\n"
                  "from ch join f using (account_id) join {S}.stg_subscriptions s on s.account_id = ch.account_id and s.end_date = ch.account_end\n"
                  "where ch.region = '{FR}' and f.n_failed >= 2"), "tolerance_abs: 0.05")]))
    # S4
    s4 = dict(id="S4", kind=v["s4_kind"],
      statement=("Accounts with low activation (peak of 2 or fewer weekly active users in their first 8 weeks) churn about 3x as often in months 2-6, across all tiers and channels."
                 if v["s4_kind"]=="driver" else
                 "ABSENT on holdout: activation has no effect on churn here. A confirmed activation finding is a FALSE finding."),
      mechanism=("Low-activation flag multiplies the monthly churn hazard in tenure months 2-6." if v["s4_kind"]=="driver" else "No mechanism; low activation exists at the same rate as dev but does not change churn."),
      found_by="Churn by first-8-week activation bucket.",
      naive="None." if v["s4_kind"]=="driver" else "Carrying the dev-set activation story over.",
      match=["{dimension_any: [first_8wk_peak_active_users, 'derived:*activat*', 'derived:*first_8*', 'derived:*onboard*', 'derived:*early_usage*', 'derived:*first_week*'], direction_any: [higher, lower]}"],
      truths=[t("S4.t1","Monthly-billed new accounts (signup < 2026-01): churn in tenure months 2-6, low activation / high activation.",
        q("with sp as (" + SPANS + "),\n"
          "pk as (select sp.account_id, max(u.active_users) as peak from sp join {S}.stg_usage_weekly u\n"
          "  on u.account_id = sp.account_id and u.week_start < sp.first_start + interval 56 day group by sp.account_id),\n"
          "fs as (select sp.account_id, sp.first_start, sp.account_end, s.billing_interval from sp\n"
          "  join {S}.stg_subscriptions s on s.account_id = sp.account_id and s.start_date = sp.first_start),\n"
          "c as (select pk.peak <= 2 as low,\n"
          "  avg(case when fs.account_end >= fs.first_start + interval 1 month and fs.account_end < fs.first_start + interval 6 month then 1.0 else 0.0 end) as r\n"
          "  from fs join pk on pk.account_id = fs.account_id where fs.billing_interval = 'monthly' and fs.first_start < date '2026-01-01' group by 1)\n"
          "select max(c.r) filter (where c.low) / max(c.r) filter (where not c.low) from c"), "tolerance_rel: 0.15")])
    stories.append(s4)
    # S5
    stories.append(dict(id="S5", kind="trap",
      statement="Sales assistance REDUCES churn within every company size band, but pooled, sales-assisted accounts churn more because they are concentrated in the high-churn 1-10 band (Simpson's paradox).",
      mechanism="sales_assisted is assigned mostly to 1-10 accounts; within each band it multiplies the churn hazard by < 1.",
      found_by="Stratify sales_assisted churn by company_size_band.",
      naive="'Sales-assisted accounts churn more' is a FALSE finding.",
      match_correct=["{dimension_all: [sales_assisted, '*size_band'], segment: {sales_assisted: true}, direction: lower}"],
      match_naive=["{dimension_any: [sales_assisted], segment: {sales_assisted: true}, direction: higher}"],
      truths=[t("S5.t1","Pooled monthly logo churn rate (account-months), sales-assisted / not.", q(AM + "select avg(c) filter (where sa) / avg(c) filter (where not sa) from am"), "tolerance_rel: 0.10"),
              t("S5.t2","Number of size bands (of 4) in which sales-assisted churn is lower.", q(AM + ", by_b as (select b, avg(c) filter (where sa) as rs, avg(c) filter (where not sa) as rn from am group by b)\nselect count(*) filter (where rs < rn) from by_b"), "tolerance_abs: 0"),
              t("S5.t3","Account-month-weighted mean of the within-band ratio (sales-assisted / not).", q(AM + ", by_b as (select b, count(*) as n, avg(c) filter (where sa) as rs, avg(c) filter (where not sa) as rn from am group by b)\nselect sum(n * rs / rn) / sum(n) from by_b"), "tolerance_rel: 0.15")]))
    # S6
    s6base = ("with sp as (" + SPANS + "),\n"
              "na as (select a.acquisition_channel as ch, sp.first_start as signup, case when sp.account_end < sp.first_start + interval 90 day then 1.0 else 0.0 end as c\n"
              "  from sp join {S}.stg_accounts a on a.account_id = sp.account_id)\n")
    stories.append(dict(id="S6", kind="driver",
      statement="Channel mix shift: from {MIX} the {CH} channel grows sharply as a share of new accounts. It churns about 2x other channels at a constant planted multiplier, so the shift alone raises blended new-account churn (other stories also move channel rates).".format(**v),
      mechanism="{CH} share of signups ramps up over 3 months; each channel's churn multiplier is constant over time.".format(**v),
      found_by="Churn by channel over time, or a mix-standardized new-account churn rate.",
      naive="'New-customer quality is getting worse across the board.'",
      match=["{{dimension_any: [acquisition_channel, channel], segment: {{'*channel': {CH}}}, direction: higher}}".format(**v),
             "{{metric_in: [new_account_share], segment: {{'*channel': {CH}}}, direction: higher}}".format(**v)],
      truths=[t("S6.t1","{CH} share of new accounts, 2024-01 through the month before the shift.".format(**v),
                q(s6base + "select avg(case when na.ch = '{CH}' then 1.0 else 0.0 end) from na where na.signup < date '{MIX}'"), "tolerance_abs: 0.03"),
              t("S6.t2","{CH} share of new accounts, 2026-01..2026-06.".format(**v),
                q(s6base + "select avg(case when na.ch = '{CH}' then 1.0 else 0.0 end) from na where na.signup >= date '2026-01-01'"), "tolerance_abs: 0.03"),
              t("S6.t3","90-day churn of new accounts (signup < 2026-04): {CH} / all other channels.".format(**v),
                q(s6base + "select avg(na.c) filter (where na.ch = '{CH}') / avg(na.c) filter (where na.ch <> '{CH}') from na where na.signup < date '2026-04-01'"), "tolerance_rel: 0.15"),
              t("S6.t4","Mix effect: blended new-account 90-day churn at post-shift channel mix and PRE-shift channel rates, minus the pre-shift blended rate (absolute; > 0 means mix alone raises churn).",
                q(s6base + ", g as (select na.ch, na.signup >= date '{MIX}' as post, count(*) as n, avg(na.c) as r from na\n"
                  "  where na.signup >= date '2024-01-01' and na.signup < date '2026-04-01' group by 1, 2),\n"
                  "pre as (select g.ch, g.r from g where not g.post),\n"
                  "wt as (select g.ch, g.n * 1.0 / sum(g.n) over () as share from g where g.post)\n"
                  "select (select sum(wt.share * pre.r) from wt join pre on pre.ch = wt.ch) - (select sum(g.n * g.r) / sum(g.n) from g where not g.post)"), "tolerance_abs: 0.01")]))
    # S7
    stories.append(dict(id="S7", kind="quirk",
      statement="invoices has one row per payment ATTEMPT. Summing invoices.amount without status = 'paid' (or summing subscription prices joined to invoices) inflates revenue/MRR; worst in {FR} 2026.".format(**v),
      mechanism="Failed retries are separate rows carrying the full amount.",
      found_by="L6 fanout_risk, a skeptic fanout_or_bug reconcile, or a data quirk proposed to memory.",
      naive="Reporting inflated MRR or revenue-churn numbers.",
      match=["{quirk_tables: [invoices]}"],
      truths=[t("S7.t1","sum(amount) over all invoice rows / sum(amount) over paid rows.", q("select sum(amount) / sum(amount) filter (where status = 'paid') from {S}.stg_invoices"), "tolerance_rel: 0.03"),
              t("S7.t2","Same ratio for {FR} accounts, 2026.".format(**v),
                q("select sum(i.amount) / sum(i.amount) filter (where i.status = 'paid') from {S}.stg_invoices i join {S}.stg_accounts a on a.account_id = i.account_id\nwhere a.region = '{FR}' and i.invoice_date >= date '2026-01-01'"), "tolerance_rel: 0.05")]))
    # D1
    dq = ("with sp as (" + SPANS + "),\n"
          "q as (select a.{DEC_ATTR} = '{DEC_VAL}' as grp, case when sp.account_end <= date 'QEND' then 1.0 else 0.0 end as c\n"
          "  from sp join {S}.stg_accounts a on a.account_id = sp.account_id\n"
          "  where sp.first_start < date 'QSTART' and (sp.account_end is null or sp.account_end >= date 'QSTART'))\n")
    stories.append(dict(id="D1", kind="decoy",
      statement="{DEC_ATTR} = '{DEC_VAL}' (a small group) shows a one-quarter churn blip in 2026-Q1 from sampling luck alone.".format(**v),
      mechanism="No mechanism: the attribute is assigned after simulation, independent of churn; the RNG sub-seed was searched so the blip appears.",
      found_by="Skeptic small_n; no persistence in other quarters. Correct output: suggestive, refuted, or not reported.",
      naive="Labelling {DEC_VAL} as a confirmed churn driver.".format(**v),
      match=["{{dimension_any: [{DEC_ATTR}], segment: {{{DEC_ATTR}: {DEC_VAL}}}, direction: higher}}".format(**v)],
      truths=[t("D1.t1","2026-Q1 quarterly churn: {DEC_VAL} / everyone.".format(**v),
                q(dq.replace("QSTART","2026-01-01").replace("QEND","2026-03-31") + "select avg(c) filter (where grp) / avg(c) from q"), "tolerance_rel: 0.15"),
              t("D1.t2","2025-Q4 quarterly churn: {DEC_VAL} / everyone (no blip).".format(**v),
                q(dq.replace("QSTART","2025-10-01").replace("QEND","2025-12-31") + "select avg(c) filter (where grp) / avg(c) from q"), "tolerance_rel: 0.25"),
              t("D1.t3","{DEC_VAL} accounts active at the start of 2026-Q1.".format(**v),
                q(dq.replace("QSTART","2026-01-01").replace("QEND","2026-03-31") + "select count(*) filter (where grp) from q"), "tolerance_abs: 0")]))
    return stories

def ylist(items, ind):
    return "".join(f"{ind}- {x}\n" for x in items)
def emit(name, v):
    out = [f"""# Answer key for the {v['ds']} planted-truth dataset ({name} variant).
#
# NEVER shown to the planner, skeptic or judge (tests/test_prompt_leak.py).
# `truths[].observed` is WRITTEN BY datasets/saasco/generate.py from the data
# itself; do not edit by hand. `python evals/harness.py --investigate --truths
# --dataset {v['ds']}` re-checks every observed value against the built warehouse
# through L3-L5.
#
# Match grammar (evals/investigations/score.py — deterministic, no LLM). A
# finding's Claim matches a signature when EVERY key present holds:
#   metric_in:     claim.metric is one of these
#   dimension_any: some claim dimension matches one of these (fnmatch patterns)
#   dimension_all: every pattern matches some claim dimension
#   segment:       every key (fnmatch) has a claim segment entry with an equal value
#                  (case-insensitive; true == "true")
#   direction / direction_any: claim.direction
#   quirk_tables:  (quirks only) flagged via L6 fanout, skeptic, or a proposed quirk
# kind: driver | trap | quirk | decoy | absent
dataset: {v['ds']}
variant: {name}
stories:
"""]
    for s in build(v):
        out.append(f"  - id: {s['id']}\n    kind: {s['kind']}\n")
        for k in ("statement","mechanism","found_by","naive"):
            out.append(f"    {k}: >-\n" + textwrap.indent(textwrap.fill(s[k], 90), "      ") + "\n")
        for k in ("match","match_correct","match_naive"):
            if k in s:
                out.append(f"    {k}:\n" + ylist(s[k], "      "))
        out.append("    truths:\n")
        for tr in s["truths"]:
            out.append(f"      - id: {tr['id']}\n        description: \"{tr['desc']}\"\n        fixture_sql: |\n"
                       + textwrap.indent(tr["sql"], "          ") + "\n"
                       + f"        observed: null\n        {tr['tol']}\n")
    return "".join(out)
if __name__ == "__main__":
    from pathlib import Path
    here = Path(__file__).resolve().parent
    for name, v in VAR.items():
        (here / v["file"]).write_text(emit(name, v), encoding="utf-8")
        print(f"  wrote {v['file']} (observed: null — run generate.py to fill)")
