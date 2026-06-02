-- Mart: the "non-ageing outliers" view. Body size predicts lifespan across the
-- animal kingdom (bigger animals live longer) via a power law — linear in
-- log-log space. We fit that line over all species with both measures, then rank
-- each species by how far its ACTUAL longevity beats (or trails) the size-implied
-- prediction. The big over-performers are the classic long-lived-for-their-size
-- species (bats, naked mole-rats, tortoises, humans). One row per species.
--
-- The fit uses DuckDB's built-in regr_slope / regr_intercept aggregates — the
-- regression itself is deterministic SQL, no library, no AI.

with base as (
    select
        hagrid, common_name, binomial, class,
        adult_weight_g,
        max_longevity_yrs,
        ln(adult_weight_g)    as ln_mass,
        ln(max_longevity_yrs) as ln_life
    from {{ ref('stg_anage') }}
    where kingdom = 'Animalia'
      and adult_weight_g > 0
      and max_longevity_yrs > 0
),

fit as (
    select
        regr_slope(ln_life, ln_mass)     as slope,
        regr_intercept(ln_life, ln_mass) as intercept,
        count(*)                          as n_fit
    from base
),

scored as (
    select
        b.*,
        f.intercept + f.slope * b.ln_mass               as predicted_ln_life,
        b.ln_life - (f.intercept + f.slope * b.ln_mass) as residual_ln
    from base b
    cross join fit f
)

select
    hagrid,
    common_name,
    binomial,
    class,
    adult_weight_g,
    max_longevity_yrs,
    round(exp(predicted_ln_life), 2)                    as predicted_longevity_yrs,
    round(max_longevity_yrs - exp(predicted_ln_life), 2) as longevity_gap_yrs,
    -- actual / size-predicted longevity. >1 = lives longer than its size implies.
    round(exp(residual_ln), 3)                          as longevity_ratio,
    round(residual_ln, 4)                               as residual_ln,
    row_number() over (order by residual_ln desc)       as overperformer_rank
from scored
order by residual_ln desc
