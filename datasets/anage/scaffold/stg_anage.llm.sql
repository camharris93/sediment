-- LLM-PROPOSED staging — review against the deterministic baseline.

with source as (
    select * from {{ source('raw', 'anage') }}
),

renamed as (
    select
        -- primary identifier (zero-padded numeric code, kept as varchar)
        "HAGRID"                          as hagrid,

        -- taxonomy hierarchy
        "Kingdom"                         as kingdom,
        "Phylum"                          as phylum,
        "Class"                           as class,
        "Order"                           as taxon_order,   -- ORDER is a reserved word in SQL
        "Family"                          as family,
        "Genus"                           as genus,
        "Species"                         as species,

        -- derived natural key: binomial scientific name
        "Genus" || ' ' || "Species"       as scientific_name,

        "Common name"                     as common_name,

        -- reproductive timing (units: days)
        "Female maturity (days)"          as female_maturity_days,
        "Male maturity (days)"            as male_maturity_days,
        "Gestation/Incubation (days)"     as gestation_incubation_days,
        "Weaning (days)"                  as weaning_days,
        "Inter-litter/Interbirth interval" as interbirth_interval_days,

        -- reproductive output (dimensionless counts / rates)
        "Litter/Clutch size"              as litter_clutch_size,
        "Litters/Clutches per year"       as litters_clutches_per_year,

        -- body mass / weight (units: grams)
        "Birth weight (g)"                as birth_weight_g,
        "Weaning weight (g)"              as weaning_weight_g,
        "Adult weight (g)"                as adult_weight_g,
        "Body mass (g)"                   as body_mass_g,

        -- growth & longevity
        -- growth_rate unit: 1/days (Gompertz or von-Bertalanffy rate constant)
        "Growth rate (1/days)"            as growth_rate_per_day,
        -- maximum_longevity unit: years
        "Maximum longevity (yrs)"         as maximum_longevity_yrs,

        -- actuarial / mortality parameters
        -- imr = initial mortality rate (per year, Gompertz intercept)
        "IMR (per yr)"                    as imr_per_yr,
        -- mrdt = mortality rate doubling time (years, Gompertz slope inverse)
        "MRDT (yrs)"                      as mrdt_yrs,

        -- metabolic physiology
        -- metabolic_rate unit: Watts
        "Metabolic rate (W)"              as metabolic_rate_w,
        -- temperature unit: Kelvin (body/ambient temperature at measurement)
        "Temperature (K)"                 as temperature_k,

        -- data provenance
        "Source"                          as source_ref,
        "References"                      as references_raw,
        "Specimen origin"                 as specimen_origin,  -- wild | captivity | unknown
        "Sample size"                     as sample_size,      -- tiny | small | medium | large | huge
        "Data quality"                    as data_quality      -- questionable | low | acceptable | high

    from source

    where
        -- remove rows with no usable life-history or taxonomy signal;
        -- HAGRID is never null (profile null_rate=0) so this guards structural junk
        "HAGRID" is not null
        -- exclude records flagged as questionable quality AND with no longevity measurement,
        -- as they contribute no analytical value and inflate missingness
        and not (
            "Data quality" = 'questionable'
            and "Maximum longevity (yrs)" is null
        )
)

select * from renamed
