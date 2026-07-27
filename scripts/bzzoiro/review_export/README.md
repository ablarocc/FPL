# Bzzoiro integration review export

This directory is a **branch-only simulation** of the proposed data path. The
real Supabase ingestion project is not present in this repository, no live
Supabase tables were changed, and the canonical `data/` directory was not
modified by this generator.

`integration_preview.py` loads the captured API samples, resolves identities,
applies foreign-key and uniqueness constraints in transient SQLite tables, and
then writes the same public CSV shapes that the existing exporter would
produce. The private provider-to-canonical identity bridge exists only during
the run; it is deliberately not materialised in this Git review export.

## What reviewers can inspect

- `git_export/data/.../matches.csv` and `fixtures.csv` use the exact canonical
  match header. Each fixture file is byte-identical to its matching match file.
- A team-stat block must expose total shots, passes and accurate passes for both
  sides before it can fill canonical match statistics. Sparse placeholder-zero
  blocks are quarantined instead.
- `git_export/data/.../playermatchstats.csv` uses the exact 64-column canonical
  player-match header. Existing non-empty values always win; accepted Bzzoiro
  rows may only fill blanks or add a row with a validated canonical player and
  match. Source-specific `stats_processed` and `player_stats_processed` match
  flags are preserved unchanged because this review does not know the upstream
  pipeline's completion semantics.
- Bzzoiro-only player ratings, attempt totals, possession loss and cards live
  in a clearly named companion file. `blocked_scoring_attempt` is exposed as
  `attacking_shots_blocked`; it never populates defender `blocks`.
- Shots, lineups, incidents, momentum, minute xG and average positions remain
  separate companion datasets.
- Player availability remains a separate Bzzoiro supplemental dataset. FPL
  `status`, `news`, and playing-chance fields remain authoritative and are not
  overwritten.
- Competitions, odds snapshots and model predictions are separate supplemental
  datasets. Betting rows are exported only when the event has an exact
  canonical match identity; unrelated global rows are quarantined instead of
  being joined to an FPL Core fixture.

The friendlies mirror contains all **91** canonical rows. Only
**62** exact provider identities are eligible for
enrichment. Loose candidates, API-only fixtures, unresolved players and
unrelated betting events are counted in `audit/rejection_summary.csv`.
Player identities must be high-confidence, one-to-one mappings. Lineup names
fall back only to an exact normalized full-name match within the canonical
team; surname-only matches remain unresolved.

## Reproduce

From the repository root:

```powershell
python scripts/bzzoiro/integration_preview.py
python scripts/bzzoiro/integration_preview.py --check
```

The command is offline and deterministic. `--check` regenerates into a
temporary directory and fails if any committed review file is stale.

## Production Supabase shape (conceptual)

A production ingestion repository would keep three private identity bridges
for teams, events and players; raw event and player-stat staging tables; and
append-only odds snapshots. Accepted records would be upserted through
canonical match/player constraints, while unresolved records would remain in a
quarantine table. Those private tables and provider identifiers are not
included in this Git export.
