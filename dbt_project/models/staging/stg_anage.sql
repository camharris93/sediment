-- Staging: 1:1 with raw.anage. snake_case rename, light type tidy, a derived
-- binomial name, and junk-row filtering. NO business logic — that lives in the
-- marts. This layer is the "clean, typed, tested" contract everything reads.

with source as (
    select * from {{ source('raw', 'anage') }}
),

renamed as (
    select
        cast("HAGRID" as integer)                       as hagrid,

        -- Taxonomy. "Order" is a reserved word → taxon_order.
        "Kingdom"                                        as kingdom,
        "Phylum"                                         as phylum,
        "Class"                                          as class,
        "Order"                                          as taxon_order,
        "Family"                                         as family,
        "Genus"                                          as genus,
        "Species"                                        as species,
        "Common name"                                    as common_name,
        nullif(trim("Genus" || ' ' || "Species"), '')   as binomial,

        -- Life-history timings (days).
        "Female maturity (days)"                         as female_maturity_days,
        "Male maturity (days)"                           as male_maturity_days,
        "Gestation/Incubation (days)"                    as gestation_days,
        "Weaning (days)"                                 as weaning_days,
        "Litter/Clutch size"                             as litter_clutch_size,
        "Litters/Clutches per year"                      as litters_per_year,
        "Inter-litter/Interbirth interval"              as interbirth_interval_days,

        -- Masses (grams) and growth.
        "Birth weight (g)"                               as birth_weight_g,
        "Weaning weight (g)"                             as weaning_weight_g,
        "Adult weight (g)"                               as adult_weight_g,
        "Growth rate (1/days)"                           as growth_rate_per_day,

        -- The headline measure.
        "Maximum longevity (yrs)"                        as max_longevity_yrs,

        -- Provenance / quality.
        "Source"                                         as source_ref,
        "Specimen origin"                                as specimen_origin,
        "Sample size"                                    as sample_size,
        "Data quality"                                   as data_quality,

        -- Ageing-rate fields (mostly sparse, kept for completeness).
        "IMR (per yr)"                                   as imr_per_yr,
        "MRDT (yrs)"                                      as mrdt_yrs,
        "Metabolic rate (W)"                             as metabolic_rate_w,
        "Body mass (g)"                                  as metabolic_body_mass_g,
        "Temperature (K)"                                as temperature_k,

        "References"                                     as references_txt
    from source
)

select *
from renamed
-- Junk-row filtering: every real record is a classified animal with a HAGRID.
where hagrid is not null
  and kingdom is not null
