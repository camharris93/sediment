-- PROPOSED MART: mart_metabolic_scaling
-- Rationale: Answers: what is the empirical relationship between body mass and basal metabolic rate across taxa (Kleiber's law validation / extension), filtered to rows where both measurements exist?
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

-- mart_metabolic_scaling
-- Purpose: provide the analysis-ready dataset for body mass vs metabolic rate
--          scaling analyses (log-log regression, Kleiber's 3/4-power law, etc.).
-- Assumption: only the ~14% of rows where BOTH metabolic_rate_w AND
--             body_mass_for_metabolic_g are non-null are included — this is by design.
-- Assumption: body_temperature_k is included as a covariate because metabolic
--             rate is temperature-dependent; endotherms cluster around 309-311 K.
-- TODO: determine whether to restrict to Animalia only for metabolic scaling;
--       Fungi/Plantae rows are retained for now with kingdom as a filter column.
-- TODO: consider excluding metabolic_rate_w < 1e-3 W as potential data entry errors.
-- NOTE: log10 transforms are applied here for convenience; analysts should
--       verify distributional assumptions before modelling.

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

        body_mass_for_metabolic_g,
        metabolic_rate_w,
        body_temperature_k,
        adult_weight_g,
        maximum_longevity_yrs,

        -- log10 transforms for allometric scaling models
        log10(body_mass_for_metabolic_g)    as log10_body_mass_g,
        log10(metabolic_rate_w)             as log10_metabolic_rate_w,

        -- mass-specific metabolic rate (W/g) — commonly reported
        round(metabolic_rate_w / body_mass_for_metabolic_g, 8)
                                            as mass_specific_metabolic_rate_w_per_g,

        -- endotherm flag: body temp >= 35 C (308.15 K) — rough heuristic
        -- TODO: validate; some reptiles in warm environments may cross threshold
        body_temperature_k >= 308.15        as is_likely_endotherm

    from {{ ref('stg_anage') }}
    where
        metabolic_rate_w            is not null
        and body_mass_for_metabolic_g is not null
        and body_mass_for_metabolic_g > 0
        and metabolic_rate_w          > 0
)

select * from base
order by kingdom, class, log10_body_mass_g
