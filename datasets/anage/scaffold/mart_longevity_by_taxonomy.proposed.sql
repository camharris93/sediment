-- PROPOSED MART: mart_longevity_by_taxonomy
-- Rationale: Answers: which taxonomic groups live longest, and how does longevity scale with body size and data quality across the tree of life?
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

-- mart_longevity_by_taxonomy
-- Aggregates maximum longevity and adult body size by taxonomic rank.
-- Intended for exploratory and comparative biology use cases.
--
-- ASSUMPTIONS:
--   1. We aggregate at the CLASS level as the most analytically useful rank
--      (broad enough for signal, narrow enough for meaningful comparison).
--      TODO: human to decide if Order or Family rollups are also needed.
--   2. litter_clutch_size is excluded from aggregation because extreme values
--      (e.g., 300,000,000 for some fish) would dominate averages; handle separately.
--   3. Only rows with data_quality IN ('acceptable','high') are included by default.
--      TODO: confirm with business / research team whether 'low' should be included.
--   4. adult_weight_g is used as the primary size proxy (~79% coverage at row level)
--      rather than body_mass_g (~13% coverage).

with base as (
    select *
    from {{ ref('stg_anage') }}
    where data_quality in ('acceptable', 'high')
      and maximum_longevity_yrs is not null
)

select
    kingdom,
    phylum,
    class,

    -- species counts
    count(*)                                                    as species_count,
    count(adult_weight_g)                                       as species_with_adult_weight,
    count(female_maturity_days)                                 as species_with_maturity_data,

    -- longevity statistics (years)
    round(avg(maximum_longevity_yrs), 2)                        as avg_max_longevity_yrs,
    round(median(maximum_longevity_yrs), 2)                     as median_max_longevity_yrs,
    max(maximum_longevity_yrs)                                  as max_longevity_yrs,
    min(maximum_longevity_yrs)                                  as min_longevity_yrs,

    -- body size statistics (grams)
    round(avg(adult_weight_g), 2)                               as avg_adult_weight_g,
    round(median(adult_weight_g), 2)                            as median_adult_weight_g,
    max(adult_weight_g)                                         as max_adult_weight_g,

    -- specimen context
    -- TODO: if wild vs captive longevity comparison is needed, split this rollup
    count_if(specimen_origin = 'wild')                          as wild_specimen_count,
    count_if(specimen_origin = 'captivity')                     as captive_specimen_count

from base
group by 1, 2, 3
order by kingdom, phylum, class
