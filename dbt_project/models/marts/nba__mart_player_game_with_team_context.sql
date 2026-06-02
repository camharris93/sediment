-- PROPOSED MART: mart_player_game_with_team_context
-- Rationale: Enriches individual player game lines with their team's game-level outcome, ratings, and game context so analysts can answer questions like 'how did star players perform in wins vs losses at different pace levels'.
-- Mart semantics are a human decision — review, correct, then move
-- into dbt_project/models/marts/ to make it real.

{{ config(schema='nba_marts', alias='player_game_with_team_context') }}

-- Grain: one row per player per game (player_id + game_id)
-- player_boxscores is 1:many child of games_index (many players per game)
-- team_boxscores is pre-aggregated to team+game grain before joining (no fan-out)
-- games_schedule adds season_type (2025-26 only)

WITH player_box AS (
    SELECT
        pb.game_id,
        pb.season_year,
        pb.game_date,
        pb.player_id,
        pb.player_name,
        pb.team_id,
        pb.team_abbreviation,
        pb.is_home,
        pb.wl                  AS win,
        pb.min                 AS minutes_played,
        pb.pts,
        pb.fgm, pb.fga, pb.fg_pct,
        pb.fg3m, pb.fg3a, pb.fg3_pct,
        pb.ftm, pb.fta, pb.ft_pct,
        pb.reb, pb.oreb, pb.dreb,
        pb.ast, pb.tov, pb.stl, pb.blk, pb.pf,
        pb.plus_minus,
        pb.usg_pct,
        pb.ts_pct,
        pb.efg_pct,
        pb.off_rating          AS player_off_rating,
        pb.def_rating          AS player_def_rating,
        pb.net_rating          AS player_net_rating,
        pb.pie                 AS player_pie,
        pb.nba_fantasy_pts,
        pb.dd2,
        pb.td3,
        pb.pts_paint, pb.pts_fb, pb.pts_2nd_chance, pb.pts_off_tov
    FROM {{ ref('nba__stg_player_boxscores') }} pb
),

-- Pre-aggregate team_boxscores to team+game grain to avoid fan-out on join
-- (player_boxscores x team_boxscores is many:many on game_id; restrict to same team)
team_box AS (
    SELECT
        tb.game_id,
        tb.team_id,
        -- team-level context to attach to each player line
        tb.pts                 AS team_pts,
        tb.off_rating          AS team_off_rating,
        tb.def_rating          AS team_def_rating,
        tb.net_rating          AS team_net_rating,
        tb.pace                AS team_pace,
        tb.poss                AS team_poss,
        tb.ts_pct              AS team_ts_pct,
        tb.efg_pct             AS team_efg_pct,
        tb.reb                 AS team_reb,
        tb.ast                 AS team_ast,
        tb.tov                 AS team_tov,
        tb.pie                 AS team_pie
    FROM {{ ref('nba__stg_team_boxscores') }} tb
    -- team_boxscores is already at team+game grain (2 rows per game); no aggregation needed
),

game_ctx AS (
    SELECT
        gi.game_id,
        gi.game_date           AS index_game_date,
        gi.season_year         AS index_season_year,
        gi.home,
        gi.away,
        gi.winner,
        gi.pts_home,
        gi.pts_away,
        gi.margin              AS game_margin,
        gi.odds_home,
        gi.odds_away,
        gi.min                 AS game_minutes
    FROM {{ ref('nba__stg_games_index') }} gi
),

sched_ctx AS (
    SELECT
        gs.game_id,
        gs.season_type,
        gs.is_neutral
    FROM {{ ref('nba__stg_games_schedule') }} gs
)

SELECT
    pb.game_id,
    pb.season_year,
    sc.season_type,              -- NULL for pre-2025-26 games
    pb.game_date,
    pb.player_id,
    pb.player_name,
    pb.team_id,
    pb.team_abbreviation,
    pb.is_home,
    sc.is_neutral,
    pb.win,
    -- game-level odds from the player's team perspective
    CASE WHEN pb.is_home = 1 THEN gc.odds_home ELSE gc.odds_away END AS team_odds,
    gc.game_margin,
    gc.game_minutes,
    -- player stats
    pb.minutes_played,
    pb.pts,
    pb.fgm, pb.fga, pb.fg_pct,
    pb.fg3m, pb.fg3a, pb.fg3_pct,
    pb.ftm, pb.fta, pb.ft_pct,
    pb.reb, pb.oreb, pb.dreb,
    pb.ast, pb.tov, pb.stl, pb.blk, pb.pf,
    pb.plus_minus,
    pb.usg_pct,
    pb.ts_pct,
    pb.efg_pct,
    pb.player_off_rating,
    pb.player_def_rating,
    pb.player_net_rating,
    pb.player_pie,
    pb.nba_fantasy_pts,
    pb.dd2, pb.td3,
    pb.pts_paint, pb.pts_fb, pb.pts_2nd_chance, pb.pts_off_tov,
    -- team-level context joined on game_id + team_id (same-team rows only; no fan-out)
    tb.team_pts,
    tb.team_off_rating,
    tb.team_def_rating,
    tb.team_net_rating,
    tb.team_pace,
    tb.team_poss,
    tb.team_ts_pct,
    tb.team_efg_pct,
    tb.team_reb,
    tb.team_ast,
    tb.team_tov,
    tb.team_pie,
    -- player's share of team scoring (handle zero team_pts defensively)
    CASE WHEN tb.team_pts > 0 THEN pb.pts * 1.0 / tb.team_pts END AS player_pts_share
FROM player_box pb
INNER JOIN game_ctx gc
    ON pb.game_id = gc.game_id
-- Join team_boxscores on BOTH game_id AND team_id to restrict to the player's own team
-- This prevents the many:many fan-out flagged in the relationships
LEFT JOIN team_box tb
    ON pb.game_id  = tb.game_id
   AND pb.team_id = tb.team_id
LEFT JOIN sched_ctx sc
    ON pb.game_id = sc.game_id
