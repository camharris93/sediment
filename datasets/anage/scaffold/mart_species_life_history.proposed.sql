-- PROPOSED MART: mart_species_life_history
-- Rationale: Answers: what is the complete life-history profile for each species — combining maturity, reproduction, size, and longevity — for species-level comparative analysis or app-facing lookups?
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

-- mart_species_life_history
-- One row per species: a wide, analytics-ready life-history fact table.
-- Suitable as a species dimension or as input for allometric / regression analyses.
--
-- ASSUMPTIONS:
--   1. hagrid is the grain — this is a direct projection of stg_anage with
--      derived convenience columns added.
--   2. adult_weight_kg is added for human-readable reporting; grams retained too.
--   3. maturity_days uses female maturity as primary, falls back to male maturity
--      when female is missing — this is a simplification. TODO: confirm with
--      domain experts whether a single 'maturity' concept is valid or sex-specific
--      columns should always be kept separate.
--   4. annual_offspring_estimate is a rough fecundity proxy; it will be NULL
--      whenever either litter_clutch_size or litters_clutches_per_year is NULL.
--      Values > 1,000,000 should be treated with caution (r-selected fish/invertebrates).
--   5. longevity_maturity_ratio: a dimensionless 'pace-of-life' index. Requires
--      both longevity (yrs) and a maturity estimate (days → converted to yrs).
--      TODO: validate this ratio is meaningful for the taxa of interest.

with base as (
    select * from {{ ref('stg_anage') }}
)

select
    -- identifiers & taxonomy
    hagrid,
    scientific_name,
    common_name,
    kingdom,
    phylum,
    class,
    taxon_order,
    family,
    genus,
    species,

    -- data provenance
    specimen_origin,
    sample_size,
    data_quality,

    -- longevity
    maximum_longevity_yrs,

    -- maturity (days, with female-first fallback)
    female_maturity_days,
    male_maturity_days,
    coalesce(female_maturity_days, male_maturity_days)          as maturity_days_coalesced,

    -- reproductive traits
    gestation_incubation_days,
    weaning_days,
    interbirth_interval_days,
    litter_clutch_size,
    litters_clutches_per_year,
    -- fecundity proxy: expected offspring per year
    round(
        litter_clutch_size * litters_clutches_per_year, 4
    )                                                           as annual_offspring_estimate,

    -- body size (grams and kg for convenience)
    birth_weight_g,
    weaning_weight_g,
    adult_weight_g,
    round(adult_weight_g / 1000.0, 4)                          as adult_weight_kg,
    body_mass_g,

    -- growth & metabolic
    growth_rate_per_day,
    metabolic_rate_w,
    temperature_k,
    -- Celsius conversion for readability
    round(temperature_k - 273.15, 2)                           as temperature_c,

    -- Gompertz mortality parameters (sparse — ~1% coverage)
    imr_per_yr,
    mrdt_yrs,
    -- flag the sentinel value used for negligible aging
    (mrdt_yrs = 999.0)                                         as is_negligible_aging_flag,

    -- derived pace-of-life index
    -- longevity_yrs / maturity_yrs; higher = slower pace of life
    case
        when maximum_longevity_yrs is not null
         and coalesce(female_maturity_days, male_maturity_days) is not null
         and coalesce(female_maturity_days, male_maturity_days) > 0
        then round(
            maximum_longevity_yrs
            / (coalesce(female_maturity_days, male_maturity_days) / 365.25),
        3)
        else null
    end                                                         as longevity_maturity_ratio

from base
