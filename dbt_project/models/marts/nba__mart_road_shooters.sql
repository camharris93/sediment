-- mart_road_shooters
-- Promoted from an NL->SQL chat answer. REVIEW before committing.
-- Question: which player over the last decade has the best three point shooting percentage on the road?
-- Rationale: which player over the last decade has the best three point shooting percentage on the road?
{{ config(schema='nba_marts', alias='mart_road_shooters') }}

SELECT
    player_id,
    player_name,
    SUM(fg3m) AS total_road_3pm,
    SUM(fg3a) AS total_road_3pa,
    SUM(fg3m) * 1.0 / NULLIF(SUM(fg3a), 0) AS road_fg3_pct,
    COUNT(DISTINCT game_id) AS games_played
FROM {{ ref('nba__mart_player_game_with_team_context') }}
WHERE is_home = 0
  AND season_year IN (
      '2015-16', '2016-17', '2017-18', '2018-19', '2019-20',
      '2020-21', '2021-22', '2022-23', '2023-24', '2024-25'
  )
GROUP BY player_id, player_name
HAVING SUM(fg3a) >= 100
ORDER BY road_fg3_pct DESC
LIMIT 25
