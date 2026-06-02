-- Mart: lifespan statistics rolled up by taxonomic class.
-- Human-curated semantics: "what does longevity look like across the major
-- animal classes, and how well-sampled is each?" One row per class.

with species as (
    select *
    from {{ ref('stg_anage') }}
    where kingdom = 'Animalia'
),

by_class as (
    select
        class,
        count(*)                                              as n_species,
        count(max_longevity_yrs)                              as n_with_longevity,
        round(avg(max_longevity_yrs), 2)                      as avg_longevity_yrs,
        round(median(max_longevity_yrs), 2)                   as median_longevity_yrs,
        max(max_longevity_yrs)                                as max_longevity_yrs,
        min(max_longevity_yrs)                                as min_longevity_yrs,
        round(avg(adult_weight_g), 1)                         as avg_adult_weight_g
    from species
    group by class
)

select *
from by_class
-- Only classes with at least one longevity record carry a meaningful stat.
where n_with_longevity > 0
order by avg_longevity_yrs desc
