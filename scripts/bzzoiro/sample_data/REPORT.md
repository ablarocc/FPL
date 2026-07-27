# Bzzoiro Sports Data — evaluation report

Generated: `2026-07-27T04:35:31.961121+00:00`
Base: `https://sports.bzzoiro.com` · repository: `FPL-Core-Insights`

## Executive summary

- **21 passed**, **8 warnings**, **0 failed**, **0 skipped**.
- 223/225 logical API requests succeeded (225 HTTP attempts).
- API calls were read-only. The evaluator wrote only the selected artifact directory; canonical `data/` and Supabase were unchanged.
- Betting and provider availability are evaluated separately from canonical football/FPL facts.

| Check | Status | Result |
|---|---|---|
| `static.player_match_coverage` | **WARN** | 23/62 merge-safe; 35 schema-comparable candidates |
| `schema.openapi_inventory` | **PASS** | 64 paths and 159 schemas parsed |
| `coverage.competitions` | **PASS** | All relevant competitions found |
| `coverage.community_shield` | **WARN** | Community Shield not listed as a distinct competition |
| `coverage.history` | **PASS** | 35 Premier League seasons; oldest starts 1992 |
| `overlap.fixture_mapping` | **PASS** | 10/10 fixtures mapped |
| `overlap.scores` | **PASS** | 10/10 mapped scores agree |
| `overlap.player_mapping` | **WARN** | 276/400 sampled player rows mapped |
| `overlap.lineup_identifiers` | **WARN** | 0/400 historical lineup rows carry player IDs |
| `overlap.response_drift` | **WARN** | 1 mapped fields absent; 6 live fields unmapped |
| `overlap.stat.goals` | **PASS** | mean absolute difference 0.0000 (threshold 0.0) |
| `overlap.stat.total_shots` | **PASS** | mean absolute difference 0.0000 (threshold 0.0) |
| `overlap.stat.minutes_played` | **PASS** | mean absolute difference 0.4384 (threshold 1.5) |
| `overlap.stat.touches` | **PASS** | mean absolute difference 0.1957 (threshold 1.5) |
| `overlap.stat.xg` | **PASS** | mean absolute difference 0.0024 (threshold 0.03) |
| `overlap.stat.xa` | **PASS** | mean absolute difference 0.0028 (threshold 0.03) |
| `enrichment.shotmap` | **PASS** | 19/19 shots carry a player ID |
| `friendlies.team_mapping` | **PASS** | 20/20 current PL teams resolved |
| `friendlies.fixture_coverage` | **WARN** | 69/91 repository friendlies matched by opponents and kickoff |
| `friendlies.player_stats` | **PASS** | 18/19 exact-mapped completed friendlies have player stats |
| `enrichment.lineups` | **PASS** | 40 player rows; status confirmed |
| `enrichment.incidents` | **PASS** | 18 incidents returned |
| `enrichment.player_profiles` | **PASS** | 17/25 profiles mapped; 25 with availability |
| `betting.global_odds` | **PASS** | 47 best-odds rows; markets 1x2 |
| `betting.predictions` | **PASS** | 20 prediction rows sampled |
| `betting.mapped_prediction` | **WARN** | Mapped fixture has no prediction; global rows are capability evidence only |
| `betting.event_markets` | **PASS** | 11 event odds fields; 4 bookmakers |
| `adoption.licensing` | **WARN** | Manual redistribution/licensing review remains required |
| `reliability.requests` | **PASS** | 223/225 requests succeeded |

## 1. OpenAPI and static column coverage

- Existing `playermatchstats.csv`: **64** columns (**2 identity** + **62 statistics**).
- Schema-comparable candidates: **26 direct** + **9 derived** = **35**.
- Current merge allowlist: **23 direct observed fields**; derived fields and unobserved `penalty_miss` remain evaluation-only.
- Still unavailable: **27** existing statistics.
- Additional player-match fields: **16**.

OpenAPI `1.0.0` describes **64 paths**, **64 operations**, and **159 schemas**.
Betting/prediction operations identified: **10**.

## 2. Competition coverage

`/api/v2/leagues/` returned **74** competitions.

| Competition | Expected region | Result |
|---|---|---|
| Premier League | England | `1` Premier League (England) |
| FA Cup | England | `39` FA Cup (England) |
| EFL Cup | England | `40` Carabao Cup (England) |
| Champions League | Europe | `7` Champions League (Europe) |
| Europa League | Europe | `8` Europa League (Europe) |
| Conference League | Europe | `83` Conference League (Europe) |
| Community Shield | England | **not found** |
| Club Friendlies | World | `79` Club Friendlies (World) |

## 3. Premier League historical depth

Premier League seasons returned: **35**.

| ID | Season | Year | Current |
|---|---|---|---|
| `1058` | Premier League 26/27 | 2026 | True |
| `337` | Premier League 25/26 | 2025 | False |
| `336` | Premier League 24/25 | 2024 | False |
| `335` | Premier League 23/24 | 2023 | False |
| `334` | Premier League 22/23 | 2022 | False |
| `333` | Premier League 21/22 | 2021 | False |
| `332` | Premier League 20/21 | 2020 | False |
| `331` | Premier League 19/20 | 2019 | False |

## 4. Existing-data cross-check (2025-2026, GW10)

Repository: **10 matches**, **303 player rows**.
API season: **380 events**.

**Fixture mapping:** 10/10.
**Score agreement:** 10/10.
**Kickoff agreement:** median delta 0.0 minutes.

**Player mapping:** 276/400 sampled API rows mapped to canonical FPL players.
Mapping uses full names first and unique surnames only as a fallback; it is diagnostic, not a production identity strategy.
Historical identity detail fallback: **45** bounded lookups. Lineup IDs populated: **0/400**.
Observed-field drift: **1 mapped source fields absent** from the sample; **6 live fields unmapped**.

| Existing column | n | Mean absolute difference | Exact |
|---|---:|---:|---:|
| `accurate_crosses` | 276 | 0.0072 | 275/276 |
| `accurate_crosses_percent` | 276 | 7.7174 | 234/276 |
| `accurate_long_balls` | 276 | 0.0326 | 274/276 |
| `accurate_long_balls_percent` | 276 | 33.9810 | 118/276 |
| `accurate_passes` | 276 | 0.1123 | 273/276 |
| `accurate_passes_percent` | 276 | 77.7447 | 9/276 |
| `aerial_duels_won` | 276 | 0.0072 | 274/276 |
| `aerial_duels_won_percent` | 276 | 31.4000 | 151/276 |
| `assists` | 276 | 0.0000 | 276/276 |
| `chances_created` | 276 | 0.0145 | 273/276 |
| `clearances` | 276 | 0.0217 | 273/276 |
| `dispossessed` | 276 | 0.4928 | 186/276 |
| `duels_lost` | 276 | 0.9928 | 132/276 |
| `duels_won` | 276 | 1.0145 | 149/276 |
| `fouls_committed` | 276 | 0.0036 | 275/276 |
| `goals` | 276 | 0.0000 | 276/276 |
| `goals_conceded` | 276 | 0.0000 | 276/276 |
| `ground_duels_won` | 276 | 1.0145 | 150/276 |
| `ground_duels_won_percent` | 276 | 44.7169 | 56/276 |
| `interceptions` | 276 | 0.0254 | 274/276 |
| `minutes_played` | 276 | 0.4384 | 270/276 |
| `recoveries` | 276 | 0.0254 | 273/276 |
| `saves` | 276 | 0.0000 | 276/276 |
| `shots_on_target` | 276 | 0.0000 | 276/276 |
| `successful_dribbles` | 276 | 0.0000 | 276/276 |
| `successful_dribbles_percent` | 276 | 20.3039 | 198/276 |
| `tackles` | 276 | 0.0109 | 275/276 |
| `tackles_won` | 276 | 0.5942 | 174/276 |
| `tackles_won_percent` | 276 | 28.3877 | 174/276 |
| `total_shots` | 276 | 0.0000 | 276/276 |
| `touches` | 276 | 0.1957 | 273/276 |
| `was_fouled` | 276 | 0.0109 | 274/276 |
| `xa` | 268 | 0.0028 | 0/268 |
| `xg` | 121 | 0.0024 | 3/121 |

## 5. Shot maps and event-level statistics

Event `91` returned **19 shots**: 19 with player IDs, 19 with xG, 9 with xGOT, 19 with pitch coordinates.

Other event-stat families present: `momentum=True`, `xg_per_minute=True`, `average_positions=True`.

## 6. Club friendlies (2026-2027)

Repository: **91 friendlies**, **5 with player stats**, **3 with xG**.

Team identity test: **20/20** resolved.

API: **104 scheduled club friendlies** involving current PL teams; **22 completed** at capture time.
Opponent-aware one-to-one repository overlap (kickoff delta <= 6h): **69/91 fixtures**; API-only: **35**; repository-only: **22**.
Loose club/date candidates excluded from coverage: **19**.
Exact-mapped completed-match player-stat coverage: **18/19** sampled.

| Event | Fixture | Date | Player rows |
|---|---|---|---:|
| `219025` | Crystal Palace v Swindon Town | 2026-07-18 | 47 |
| `219027` | Newcastle United v Darlington | 2026-07-18 | 0 |
| `219085` | Dundee FC v Everton | 2026-07-18 | 45 |
| `219087` | Northampton Town v Coventry City | 2026-07-18 | 47 |
| `219103` | Notts County v Nottingham Forest | 2026-07-18 | 45 |
| `219106` | York City v Sunderland | 2026-07-18 | 43 |
| `219152` | Manchester United v Wrexham | 2026-07-18 | 52 |
| `219241` | Walsall v Aston Villa | 2026-07-21 | 45 |
| `219263` | Nottingham Forest v Blackburn Rovers | 2026-07-22 | 48 |
| `219277` | Tottenham Hotspur v Milton Keynes Dons | 2026-07-22 | 44 |
| `219307` | Bournemouth v FC St. Pauli | 2026-07-24 | 41 |
| `219323` | Gateshead v Newcastle United | 2026-07-25 | 45 |
| `219348` | Bromley v Crystal Palace | 2026-07-25 | 45 |
| `219349` | Bolton Wanderers v Everton | 2026-07-25 | 46 |
| `221024` | Konyaspor v Hull City | 2026-07-25 | 45 |
| `219408` | FC Porto v Aston Villa | 2026-07-25 | 41 |
| `219415` | Liverpool FC v Sunderland | 2026-07-25 | 50 |
| `219416` | Leeds United v Wrexham | 2026-07-25 | 47 |
| `219417` | Auckland FC v Tottenham Hotspur | 2026-07-26 | 52 |

## 7. Lineups, availability and incidents

Event `91` lineup status: `confirmed`; **40 lineup/substitute rows**, **0 unavailable-player rows**.
Incidents: **18**, types: card, goal, injuryTime, period, substitution.
Bzzoiro availability is evaluated as a separate source and must not overwrite FPL `status`, `news`, or chance-of-playing fields.

## 8. Player profiles and separate availability source

Sample: **17/25** profiles mapped to FPL players; **25/25** expose provider availability.
The report keeps provider availability alongside FPL values for comparison; it does not combine them.

## 9. Betting and prediction data (evaluation only)

Global best-odds rows: **47**; prediction rows sampled: **20**.
Event odds fields: **11**; bookmaker comparison count: **4**; Polymarket markets present: **False**.
These are tested as time-sensitive model/market data. They are not treated as match facts and nothing is imported.

## 10. Adoption gates not settled by an API probe

- **Licensing/redistribution:** confirm that derived Opta/Sportradar-like data may legally be republished in this open repository.
- **ID stability:** rerun across time and compare provider IDs before building durable identity bridges.
- **Rate limits and change semantics:** the supplied schema does not document quotas, `429`, ETags, or stable update timestamps for all routes.
- **Production decision:** this branch evaluates evidence only; it does not modify canonical data or Supabase.

## 11. API reliability for this run

Logical requests: **225**; successful: **223**; HTTP attempts: **225**.
Median latency: **340.5 ms**; p95: **546.4 ms**; errors: **2**.
