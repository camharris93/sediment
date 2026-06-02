-- PROPOSED MART: mart_longevity_by_taxonomy
-- Rationale: Answers: which taxonomic groups live longest, and how does maximum longevity vary by class, order, and specimen origin?
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

-- mart_longevity_by_taxonomy
-- Purpose: aggregate maximum longevity statistics by taxonomic rank and specimen origin.
-- Assumption: 'maximum_longevity_yrs' is the canonical ageing metric in AnAge.
-- Assumption: we group at the CLASS + TAX_ORDER level; add FAMILY if needed downstream.
-- TODO: decide whether to include specimen_origin = 'unknown' in headline figures
--       (currently included — human should verify this is acceptable).
-- TODO: confirm whether data_quality = 'low' records should be excluded here;
--       they pass the staging filter but may inflate/deflate longevity statistics.

with base as (
    select
        kingdom,
        phylum,
        class,
        tax_order,
        specimen_origin,
        data_quality,
        maximum_longevity_yrs,
        adult_weight_g
    from {{ ref('stg_anage') }}
    where maximum_longevity_yrs is not null
),

aggregated as (
    select
        kingdom,
        phylum,
        class,
        tax_order,
        specimen_origin,

        -- species count with longevity data
        count(*)                                             as species_count,

        -- longevity statistics (years)
        round(avg(maximum_longevity_yrs), 2)                as avg_max_longevity_yrs,
        round(median(maximum_longevity_yrs), 2)             as median_max_longevity_yrs,
        round(min(maximum_longevity_yrs), 2)                as min_max_longevity_yrs,
        round(max(maximum_longevity_yrs), 2)                as max_max_longevity_yrs,
        round(stddev(maximum_longevity_yrs), 2)             as stddev_max_longevity_yrs,

        -- body size context (log-scale analyses common in comparative biology)
        round(avg(log10(adult_weight_g)), 4)                as avg_log10_adult_weight_g,
        count(adult_weight_g)                               as species_with_weight_count

    from base
    group by all
)

select * from aggregated
order by kingdom, phylum, class, tax_order, specimen_origin
