-- PROPOSED MART: mart_metabolic_allometry
-- Rationale: Answers: how do metabolic rate and longevity scale with body mass across taxa — supporting allometric (log-log) regression and the 'rate-of-living' hypothesis?
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

-- mart_metabolic_allometry
-- Filtered, analysis-ready subset of species with BOTH metabolic rate AND body mass
-- (the two rarest well-paired measurements in AnAge, ~13% row coverage).
-- Intended for allometric scaling analyses (Kleiber's law, rate-of-living theory).
--
-- ASSUMPTIONS:
--   1. Both metabolic_rate_w AND body_mass_g must be non-null — this reduces
--      the dataset to ~627 rows. TODO: verify count post-filter with actual data.
--   2. Log-transformed columns are added here to simplify downstream modelling;
--      DuckDB's ln() is natural log. TODO: confirm whether log10 is preferred
--      by the analytical team (common in allometry literature).
--   3. adult_weight_g is included alongside body_mass_g because they may differ
--      (body_mass_g reflects measurement conditions; adult_weight_g is the
--      species typical adult mass). The difference is informative.
--   4. maximum_longevity_yrs is included to allow rate-of-living tests;
--      its null rows are retained so the human can decide the filter threshold.
--   5. 'questionable' data quality rows ARE included here (unlike the longevity mart)
--      because metabolic data can be valid even when longevity is uncertain.
--      TODO: confirm with domain expert.

with metabolic_subset as (
    select *
    from {{ ref('stg_anage') }}
    where metabolic_rate_w is not null
      and body_mass_g      is not null
      and metabolic_rate_w > 0
      and body_mass_g      > 0
)

select
    hagrid,
    scientific_name,
    common_name,
    kingdom,
    phylum,
    class,
    taxon_order,
    family,

    -- raw measurements
    body_mass_g,
    adult_weight_g,
    metabolic_rate_w,
    temperature_k,
    round(temperature_k - 273.15, 2)                            as temperature_c,
    maximum_longevity_yrs,
    data_quality,
    specimen_origin,

    -- log-transformed for allometric regression (natural log)
    round(ln(body_mass_g), 6)                                   as ln_body_mass_g,
    round(ln(metabolic_rate_w), 6)                              as ln_metabolic_rate_w,
    case
        when maximum_longevity_yrs is not null and maximum_longevity_yrs > 0
        then round(ln(maximum_longevity_yrs), 6)
        else null
    end                                                         as ln_maximum_longevity_yrs,

    -- mass-specific metabolic rate (W per gram) — key rate-of-living metric
    round(metabolic_rate_w / body_mass_g, 8)                    as metabolic_rate_w_per_g,

    -- lifetime energy expenditure proxy: metabolic_rate * longevity
    -- units: Watts * years — TODO: convert to Joules if needed (× 3.156e7)
    case
        when maximum_longevity_yrs is not null
        then round(metabolic_rate_w * maximum_longevity_yrs, 4)
        else null
    end                                                         as lifetime_energy_proxy_w_yrs

from metabolic_subset
order by body_mass_g asc
