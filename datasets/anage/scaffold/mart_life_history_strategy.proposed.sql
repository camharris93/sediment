-- PROPOSED MART: mart_life_history_strategy
-- Rationale: Answers: how do reproductive pace (maturity age, litter size, litters/year) and longevity co-vary across species — enabling fast-vs-slow life history strategy analysis?
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

-- mart_life_history_strategy
-- Purpose: one row per species with the key life-history trade-off variables
--          needed to place species on the 'fast-slow' life history continuum.
-- Assumption: we take female_maturity_days when available, falling back to
--             male_maturity_days, as the primary 'age at first reproduction' proxy.
--             TODO: verify this fallback logic with domain expert.
-- Assumption: annual_offspring_estimate is a rough fecundity proxy;
--             it will be null whenever either component is null.
-- Assumption: only data_quality IN ('acceptable','high') rows are included
--             to keep this mart suitable for quantitative modelling.
--             TODO: human should confirm quality threshold.

with base as (
    select
        hagrid,
        scientific_name,
        common_name,
        kingdom,
        phylum,
        class,
        tax_order,
        family,
        specimen_origin,
        data_quality,
        sample_size,

        -- age at first reproduction proxy (days)
        coalesce(
            female_maturity_days,
            male_maturity_days
        )                                                       as age_at_maturity_days,
        female_maturity_days is not null
            and male_maturity_days is not null                 as has_both_sexes_maturity,

        gestation_incubation_days,
        weaning_days,
        litter_clutch_size,
        litters_clutches_per_year,
        interbirth_interval_days,

        -- crude annual fecundity estimate
        -- TODO: validate against published fecundity figures for a few species
        round(litter_clutch_size * litters_clutches_per_year, 3)
                                                                as annual_offspring_estimate,

        maximum_longevity_yrs,
        adult_weight_g,

        -- dimensionless pace-of-life ratio: longevity relative to maturity
        -- higher = longer reproductive lifespan relative to age at first repro
        case
            when female_maturity_days > 0
             and maximum_longevity_yrs is not null
            then round(
                (maximum_longevity_yrs * 365.25) / female_maturity_days
            , 3)
        end                                                     as longevity_to_maturity_ratio

    from {{ ref('stg_anage') }}
    where data_quality in ('acceptable', 'high')
)

select * from base
order by class, tax_order, scientific_name
