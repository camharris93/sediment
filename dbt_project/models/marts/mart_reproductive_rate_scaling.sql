-- mart_reproductive_rate_scaling
-- Promoted from an NL->SQL chat answer. REVIEW before committing.
-- Question: how does reporductive rate scale with longevity
-- Rationale: how does reporductive rate scale with longevity

SELECT
    hagrid,
    common_name,
    binomial,
    class,
    max_longevity_yrs,
    litters_per_year,
    litter_clutch_size,
    interbirth_interval_days,
    female_maturity_days,
    adult_weight_g
FROM {{ ref('stg_anage') }}
WHERE
    max_longevity_yrs IS NOT NULL
    AND (
        litters_per_year IS NOT NULL
        OR litter_clutch_size IS NOT NULL
        OR interbirth_interval_days IS NOT NULL
    )
ORDER BY
    max_longevity_yrs DESC
