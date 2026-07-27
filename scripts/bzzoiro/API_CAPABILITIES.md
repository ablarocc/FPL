# Bzzoiro API capability map

This evaluation inventory is derived from `football-schema.json` (OpenAPI 3.0.3,
API version 1.0.0) and selected live v2 responses. It describes possible
future value only; this branch does not integrate any endpoint into production data.

## Potential value if the evaluation later passes

### Tier 1 — enrich existing FPL Core Insights tables

- Event identity, scores, status, round, season, venue, referee and attendance.
- Team match statistics, xG, momentum, average positions and shot maps.
- Per-player match statistics and heatmaps.
- Confirmed or predicted lineups, formations and unavailable players.
- Incidents: goals, cards and substitutions.

If Bzzoiro is eventually adopted, provider IDs need private bridge tables rather
than replacing FPL or FotMob identifiers. The review preview exercises those
constraints in transient SQLite only; it creates no persistent or live tables.

Of the existing 64 `playermatchstats` columns, two are identity columns
(`player_id`, `match_id`); the remaining 62 split into 35 schema-comparable
candidates and 27 currently unavailable statistics. Only 23 observed direct
fields are on the current merge allowlist; all derived fields remain review-only.

### Tier 2 — new football tables

- `bzzoiro_events`
- `bzzoiro_event_shots`
- `bzzoiro_event_momentum`
- `bzzoiro_event_average_positions`
- `bzzoiro_lineups`
- `bzzoiro_unavailable_players`
- `bzzoiro_incidents`
- `bzzoiro_players`
- `bzzoiro_player_team_history`
- `bzzoiro_transfers`
- `bzzoiro_managers`
- `bzzoiro_referees`
- `bzzoiro_venues`

### Tier 3 — betting and model data (keep separate from football facts)

- Bookmaker directory.
- Event odds and side-by-side bookmaker comparison.
- Best upcoming odds.
- Decimal odds, implied probability, movement, previous price, line,
  outcome, maximum-quote flag and update time.
- Model predictions for match result, BTTS, totals, corners, draw-no-bet,
  expected goals and most-likely score.
- Recommendation fields such as favourite, winner and totals.
- Polymarket event markets, goalscorer markets and exact-score markets.

Store odds as timestamped snapshots. Never merge a changing price into the
canonical match row, and never treat model predictions as observed results.

## Other API families

The schema contains 64 paths and 159 schemas:

- Events (14): lists, live matches, detail, H2H, stats, player stats,
  lineups, incidents, metadata, odds, odds comparison, Polymarket,
  predictions, broadcasts and social items.
- Players (8): list/detail, career, national team, per-match stats,
  transfers and social items.
- Leagues (10): seasons, current season, standings, Best XI, leaderboards
  and venues.
- Managers (5): detail, career, matches, tactical profile and social items.
- Odds/bookmakers/predictions (6).
- Referees (3), venues (3), TV channels/broadcasts (5), social (2),
  teams (5), transfers (1), and World Cup 2026 squads (2).

## Important live/schema differences

- Season responses use `seasons`, not `results`.
- Event player-stat responses use `player_stats`.
- Player-stat rows currently expose flat `player_id` and `team_id`; the
  documented nested player object is not present.
- Shot-map rows currently expose `player_id`, not `pid`.
- League objects expose `country`; relying on absent `country_code` caused
  UEFA Champions League to be confused with CAF Champions League.
- Club Friendlies and International Friendly Games are distinct leagues.

The evaluator uses tolerant envelope extraction and preserves bounded samples so
future response drift is visible. `blocked_scoring_attempt` is deliberately not
mapped to defender `blocks`; its semantics must be validated first.

## Future production identity strategy

Create bridge tables for provider IDs:

- `team_identity(fpl_team_code, fotmob_team_id, bzzoiro_team_id, season, confidence)`
- `player_identity(fpl_player_id, player_code, bzzoiro_player_id, season, confidence)`
- `event_identity(match_id, fotmob_event_id, bzzoiro_event_id, confidence)`

Match teams by exact aliases and season, events by both teams plus kickoff
time (recording neutral-site orientation reversals), and players by team,
normalized full name, date of birth and position. Canonical match and player
joins use `teams.code`; `teams.id` is not the match foreign key.
Surname-only matching is suitable for diagnostics only.
