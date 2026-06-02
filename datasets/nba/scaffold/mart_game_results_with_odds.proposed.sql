-- PROPOSED MART: mart_game_results_with_odds
-- Rationale: Combines historical game outcomes, betting odds, and schedule metadata (season type, neutral site) to support win-probability and betting-market analysis across seasons.
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

{{ config(schema='nba_marts', alias='game_results_with_odds') }}

-- Grain: one row per game (game_id)
-- games_index is the authoritative result source (38k historical games)
-- games_schedule enriches with season_type, completed/scheduled flags, and is_neutral
-- 1:1 relationship on game_id so no fan-out risk; use LEFT JOIN to retain historical
-- games not yet in the schedule table (schedule covers only 2025-26)

WITH game_index AS (
    SELECT
        gi.game_id,
        gi.game_date,
        gi.season_year,
        gi.matchup,
        gi.home,
        gi.away,
        gi.team_id_home,
        gi.team_id_away,
        gi.team_name_home,
        gi.team_name_away,
        gi.winner,
        gi.pts_home,
        gi.pts_away,
        gi.margin,
        gi.min             AS game_minutes,
        gi.odds_home,
        gi.odds_away
    FROM {{ ref('nba__stg_games_index') }} gi
),

game_schedule AS (
    -- schedule table is 2025-26 only; bring in season_type + flags where available
    SELECT
        gs.game_id,
        gs.season_type,
        gs.completed,
        gs.scheduled,
        gs.is_neutral
    FROM {{ ref('nba__stg_games_schedule') }} gs
)

SELECT
    gi.game_id,
    gi.game_date,
    gi.season_year,
    -- season_type only populated for 2025-26 games; NULL for historical
    gs.season_type,
    gi.matchup,
    gi.home,
    gi.away,
    gi.team_id_home,
    gi.team_id_away,
    gi.team_name_home,
    gi.team_name_away,
    gi.winner,
    gi.pts_home,
    gi.pts_away,
    gi.margin,
    gi.game_minutes,
    -- odds of 0.0 indicate missing data; human should decide on NULL-coalescing
    gi.odds_home,
    gi.odds_away,
    -- implied probability from decimal odds (0 odds left as NULL)
    CASE WHEN gi.odds_home > 0 THEN 1.0 / gi.odds_home END AS implied_prob_home,
    CASE WHEN gi.odds_away > 0 THEN 1.0 / gi.odds_away END AS implied_prob_away,
    gs.completed,
    gs.scheduled,
    gs.is_neutral,
    -- derived: did the home team win?
    CASE WHEN gi.winner = gi.home THEN 1 ELSE 0 END         AS home_win
FROM game_index gi
LEFT JOIN game_schedule gs
    ON gi.game_id = gs.game_id
