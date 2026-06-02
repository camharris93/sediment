-- PROPOSED MART: mart_team_game_performance
-- Rationale: Joins game-level context (date, season, odds, season type) onto team boxscore stats so analysts can study team performance by home/away, season, and game environment in one wide table.
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

{{ config(schema='nba_marts', alias='team_game_performance') }}

-- Grain: one row per team per game (team_id + game_id)
-- team_boxscores is 1:many child of games_index (2 rows per game: home + away)
-- games_schedule enriches with season_type and is_neutral; LEFT JOIN to keep history
-- No aggregation needed here because team_boxscores is already at the right grain

WITH team_box AS (
    SELECT
        tb.game_id,
        tb.season_year,
        tb.team_id,
        tb.team_abbreviation,
        tb.team_name,
        tb.game_date,
        tb.is_home,
        tb.wl                  AS win,
        tb.min                 AS game_minutes,
        tb.pts,
        tb.fgm,
        tb.fga,
        tb.fg_pct,
        tb.fg3m,
        tb.fg3a,
        tb.fg3_pct,
        tb.ftm,
        tb.fta,
        tb.ft_pct,
        tb.reb,
        tb.oreb,
        tb.dreb,
        tb.ast,
        tb.tov,
        tb.stl,
        tb.blk,
        tb.pf,
        tb.plus_minus,
        tb.off_rating,
        tb.def_rating,
        tb.net_rating,
        tb.pace,
        tb.ts_pct,
        tb.efg_pct,
        tb.poss,
        tb.pie,
        -- scoring breakdown
        tb.pts_paint,
        tb.pts_fb,
        tb.pts_2nd_chance,
        tb.pts_off_tov,
        -- opponent context
        tb.opp_pts_paint,
        tb.opp_pts_fb,
        tb.opp_pts_2nd_chance,
        tb.opp_pts_off_tov
    FROM {{ ref('nba__stg_team_boxscores') }} tb
),

game_ctx AS (
    SELECT
        gi.game_id,
        gi.odds_home,
        gi.odds_away,
        gi.margin              AS game_margin,
        gi.winner
    FROM {{ ref('nba__stg_games_index') }} gi
),

sched_ctx AS (
    -- 2025-26 only; enriches season_type and neutral-site flag
    SELECT
        gs.game_id,
        gs.season_type,
        gs.is_neutral
    FROM {{ ref('nba__stg_games_schedule') }} gs
)

SELECT
    tb.game_id,
    tb.season_year,
    sc.season_type,                  -- NULL for pre-2025-26 games
    tb.game_date,
    tb.team_id,
    tb.team_abbreviation,
    tb.team_name,
    tb.is_home,
    sc.is_neutral,
    -- odds from the team's perspective (home odds if home, away odds if away)
    CASE WHEN tb.is_home = 1 THEN gc.odds_home ELSE gc.odds_away END AS team_odds,
    CASE WHEN tb.is_home = 1 THEN gc.odds_away ELSE gc.odds_home END AS opp_odds,
    tb.win,
    -- flag: did the team win as the underdog (their odds > opponent odds)?
    CASE
        WHEN tb.win = 1
             AND CASE WHEN tb.is_home = 1 THEN gc.odds_home ELSE gc.odds_away END
                 > CASE WHEN tb.is_home = 1 THEN gc.odds_away ELSE gc.odds_home END
             AND gc.odds_home > 0 AND gc.odds_away > 0
        THEN 1 ELSE 0
    END                              AS upset_win,
    tb.game_minutes,
    tb.pts,
    tb.fgm, tb.fga, tb.fg_pct,
    tb.fg3m, tb.fg3a, tb.fg3_pct,
    tb.ftm, tb.fta, tb.ft_pct,
    tb.reb, tb.oreb, tb.dreb,
    tb.ast, tb.tov, tb.stl, tb.blk, tb.pf,
    tb.plus_minus,
    tb.off_rating, tb.def_rating, tb.net_rating,
    tb.pace, tb.ts_pct, tb.efg_pct, tb.poss, tb.pie,
    tb.pts_paint, tb.pts_fb, tb.pts_2nd_chance, tb.pts_off_tov,
    tb.opp_pts_paint, tb.opp_pts_fb, tb.opp_pts_2nd_chance, tb.opp_pts_off_tov
FROM team_box tb
INNER JOIN game_ctx gc
    ON tb.game_id = gc.game_id
LEFT JOIN sched_ctx sc
    ON tb.game_id = sc.game_id
