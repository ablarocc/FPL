# Bzzoiro Sports Data — read-only evaluation

This branch evaluates Bzzoiro and includes a **branch-only integration preview**
of how accepted rows would move through private Supabase staging into the
repository's normal public CSV shapes. It does not write to live Supabase or to
the canonical `data/` tree.

A bounded live snapshot is included at `scripts/bzzoiro/sample_data/` so
reviewers can inspect real API data without a key. The generated
`scripts/bzzoiro/review_export/` then applies identity, constraint, merge and
quarantine rules to that snapshot. Both directories are review evidence, not
production FPL Core Insights data.

## What is tested

- Exact competition selection, including UEFA rather than CAF Champions League
  and Club Friendlies rather than international friendlies.
- Premier League season depth.
- Fixture, kickoff and score agreement with a checked-in gameweek.
- Historical player identity using current squad IDs plus bounded player-detail
  fallbacks; lineup ID population is audited separately.
- Every direct and derived `playermatchstats` mapping with field population and
  response-drift reporting.
- Shot maps, xG/xGOT, coordinates, momentum, xG by minute and average positions.
- Confirmed/predicted lineups, unavailable players and incidents.
- Current player profiles and Bzzoiro availability shown separately from FPL
  `status`, `news` and chance-of-playing fields.
- Club-friendly overlap uses both opponents plus kickoff time, with one-to-one
  matching and recorded neutral-site home/away reversals; player-stat coverage
  samples completed matches only.
- Odds, bookmaker comparison, predictions and Polymarket as separate,
  time-sensitive evaluation data—not match facts or recommendations.
- Per-endpoint success, retries and latency.
- API key leakage and attempts to write artifacts under `data/`.

TV channels, social posts, generic standings and highlights are intentionally
not evaluated because they are not useful to this project.

## Run locally

The evaluator is standard-library only; no packages need installing. The API
key is accepted only through the `BZZOIRO_API_KEY` environment variable so it
does not appear in shell history or process arguments.

### Bash

```bash
export BZZOIRO_API_KEY='<your key>'
python3 -m unittest discover -s scripts/bzzoiro -p 'test_*.py' -v
python3 scripts/bzzoiro/probe.py --out bzzoiro_probe_out
```

### Windows PowerShell

```powershell
$env:BZZOIRO_API_KEY = '<your key>'
python -m unittest discover -s scripts/bzzoiro -p 'test_*.py' -v
python scripts/bzzoiro/probe.py --out bzzoiro_probe_out
Remove-Item Env:BZZOIRO_API_KEY
```

To inventory an OpenAPI file as part of the run:

```bash
python3 scripts/bzzoiro/probe.py --schema /path/to/football-schema.json
```

Useful controls:

```text
--comparison-season SEASON   repository/API season to compare (default 2025-2026)
--friendlies-season SEASON   pre-season snapshot to inspect (default 2026-2027)
--gameweek N                 comparison gameweek, 1-38 (default 10)
--sample-matches N           finished PL matches to compare (default 5)
--friendly-sample N          completed friendlies to sample (default 12)
--profile-sample N           current player profiles to sample (default 10)
--identity-detail-limit N    historical player-detail fallbacks (default 50)
--skip-last-season           skip the historical overlap comparison
--skip-friendlies            skip the friendlies evaluation
--skip-enrichment            skip shots, lineups, incidents and profiles
--skip-betting               skip odds and prediction endpoints
--strict                     return non-zero if an evaluation check fails
--out DIR                    artifact directory; data/ and arbitrary repo paths are rejected
```

A standard run currently makes roughly 120–160 logical API requests and takes
about one minute. `full` runs use more calls as coverage grows.

## Outputs

The output directory contains:

- `REPORT.md` — readable findings and a pass/warn/fail table.
- `SUMMARY.json` — structured checks and metrics.
- `RUN_MANIFEST.json` — base commit, start/end tree state, exact executing-file hashes, inputs, runtime and schema hash.
- `ENDPOINT_TELEMETRY.json` — endpoint status and latency, never the key.
- `openapi_inventory.json` — generated only when `--schema` is supplied.
- `SECRET_SCAN_OK` — written only after every artifact passes the credential scan.
- Bounded JSON evidence for competitions, seasons, compared events, player
  stats, shots, lineups, incidents, player profiles, friendlies and betting.

Each JSON file is complete and parseable. The evaluator scans all artifacts for
the API key before returning success.

## Included branch snapshot

`scripts/bzzoiro/sample_data/` is the checked full review capture. It contains
complete bounded responses for all 10 comparison fixtures and all 19 exact
completed friendlies available at capture time, plus identity maps, rejection
evidence and a mapped-fixture odds sample. It lets reviewers inspect the same
evidence without re-running the API.

To refresh it locally, run:

```bash
python3 scripts/bzzoiro/probe.py \
  --schema /path/to/football-schema.json \
  --out scripts/bzzoiro/sample_data \
  --sample-matches 10 \
  --friendly-sample 100 \
  --profile-sample 25 \
  --identity-detail-limit 200 \
  --strict
```

Review the diff carefully before sharing a refreshed snapshot. Never include
the API key, and do not copy these files into `data/`.

## Pipeline-style review export

Run the offline deterministic transformation after refreshing the snapshot:

```powershell
python scripts/bzzoiro/integration_preview.py
python scripts/bzzoiro/integration_preview.py --check
```

The generated `review_export/git_export/` mirrors the normal public exporter
schemas without modifying `data/`. Its current review result includes:

- 10 Premier League GW10 matches and 303 player-match rows;
- all 91 canonical friendlies and 270 player-match rows after 210 accepted rows
  are added;
- 250 historical and 268 friendly player-match enrichment rows;
- separate shots, momentum, minute-xG, average-position, lineup and incident
  files;
- 74 competitions, 16 provider-availability rows and three odds rows for the
  mapped Aston Villa v Real Sociedad friendly;
- a header-only mapped-predictions file because that event returned no model
  prediction; unrelated global predictions remain quarantined.

The transformation uses transient SQLite tables to exercise the intended
Supabase identity, uniqueness, foreign-key, upsert and quarantine behaviour.
No public review file adds `data_source`, `source_event_id`, `source_player_id`
or `retrieved_at`. Existing values win, FPL availability remains authoritative,
attacking blocked shots never populate defender `blocks`, and sparse match-stat
blocks with placeholder zeroes remain quarantined rather than filling canonical
facts.

`review_export/audit/` and `DIFF_SUMMARY.json` show accepted identities, merge
counts and rejection totals. The real upstream Supabase ingestion project is
not present here, so this branch demonstrates the contract and resulting export
rather than claiming a production database deployment.

## GitHub Actions

The workflow runs offline tests first, accepts bounded `quick`, `standard` and
`full` presets, validates inputs, publishes only after `SECRET_SCAN_OK`, uploads
the sanitized report, summaries, telemetry, inventory and scan marker, and
fails if any tracked file or unexpected untracked path changes. It is restricted
to this reviewed feature branch; fork testers must use their own low-scope key.

GitHub only exposes a manually dispatched workflow after that workflow exists
on the repository's default branch. Until a trusted read-only dispatcher lands
there, testers should run locally or from their own fork with their own
`BZZOIRO_API_KEY` secret. Do not use `pull_request_target` with a shared key.

A successful workflow means the evaluator completed and its required checks
passed. It does **not** mean Bzzoiro has been approved for production use.

## How to interpret the evidence

The current comparison shows excellent agreement for fixtures, scores and core
box-score fields, plus potentially valuable friendlies, shots and lineup data.
It still does not justify integration by itself:

- Provider player/team/event IDs need stability testing across repeated runs.
- `blocked_scoring_attempt` describes an attacking shot blocked and is **not**
  mapped to the defender `blocks` field without semantic validation.
- Provider availability remains separate from FPL availability.
- Betting and prediction data remains separate from observed football facts.
- Redistribution/licensing needs explicit review before API-derived data enters
  the public canonical dataset.

## Files

- `probe.py` — stable command-line entry point.
- `evaluator.py` — evaluation implementation.
- `mapping.py` — candidate field mappings and guarded team aliases.
- `test_evaluator.py` — offline evaluator and capture-contract tests.
- `integration_preview.py` — deterministic Supabase-style staging and export simulation.
- `test_integration_preview.py` — schema, safety, identity and reproducibility tests.
- `API_CAPABILITIES.md` — API capability and possible future architecture map.
- `sample_data/` — bounded live evidence for branch reviewers.
- `review_export/` — generated canonical-shaped data, companions and audit results.
