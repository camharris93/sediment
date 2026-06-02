-- LLM-PROPOSED staging — review against the deterministic baseline.

with source as (
    select * from {{ source('anage', 'anage') }}
),

renamed as (
    select
        -- primary key
        "HAGRID"                                    as hagrid,

        -- taxonomy hierarchy
        "Kingdom"                                   as kingdom,
        "Phylum"                                    as phylum,
        "Class"                                     as class,
        "Order"                                     as tax_order,   -- 'order' is a reserved word in SQL
        "Family"                                    as family,
        "Genus"                                     as genus,
        "Species"                                   as species,
        "Common name"                               as common_name,

        -- derived natural key: binomial scientific name
        "Genus" || ' ' || "Species"                 as scientific_name,

        -- reproductive timing (all in days)
        -- unit note: stored as integer days in the source
        "Female maturity (days)"                    as female_maturity_days,
        "Male maturity (days)"                      as male_maturity_days,
        "Gestation/Incubation (days)"               as gestation_incubation_days,
        "Weaning (days)"                            as weaning_days,
        "Inter-litter/Interbirth interval"           as interbirth_interval_days,

        -- reproductive output
        "Litter/Clutch size"                        as litter_clutch_size,
        "Litters/Clutches per year"                 as litters_clutches_per_year,

        -- body mass / growth (grams unless noted)
        -- unit note: all weights in grams; adult_weight_g is the most populated (~79% non-null)
        "Birth weight (g)"                          as birth_weight_g,
        "Weaning weight (g)"                        as weaning_weight_g,
        "Adult weight (g)"                          as adult_weight_g,
        "Growth rate (1/days)"                      as growth_rate_per_day,

        -- longevity
        -- unit note: stored in years (decimal)
        "Maximum longevity (yrs)"                   as maximum_longevity_yrs,

        -- actuarial / mortality parameters (~1% populated — use with caution)
        -- IMR = Initial Mortality Rate (Gompertz); MRDT = Mortality Rate Doubling Time
        "IMR (per yr)"                              as initial_mortality_rate_per_yr,
        "MRDT (yrs)"                                as mortality_rate_doubling_time_yrs,

        -- energetics (only ~14% populated)
        -- unit note: metabolic rate in Watts; body_mass_g and temperature_k are paired measurements
        "Metabolic rate (W)"                        as metabolic_rate_w,
        "Body mass (g)"                             as body_mass_for_metabolic_g,  -- distinct from adult_weight_g
        "Temperature (K)"                           as body_temperature_k,

        -- data provenance / quality
        "Specimen origin"                           as specimen_origin,
        "Sample size"                               as sample_size,
        "Data quality"                              as data_quality,
        "Source"                                    as source_ref_id,
        "References"                                as references_raw

    from source
    where
        -- remove rows with no taxonomic identity (all three are never null per profile,
        -- but guard against upstream changes)
        "HAGRID" is not null
        and "Genus"   is not null
        and "Species" is not null
        -- exclude known junk data quality flag
        and "Data quality" <> 'questionable'
        -- litter size of 300 million is biologically real (some fish) but flag
        -- extreme outliers via comment for human review:
        -- TODO: confirm whether litter_clutch_size > 1e6 rows should be excluded
        --       or kept with a flag column.  Currently retained.
)

select * from renamed
