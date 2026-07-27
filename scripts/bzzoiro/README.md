# Bzzoiro Sports Data — evaluation

Exploratory only. Nothing here runs in the twice-daily pipeline and nothing
writes to `data/`.

## Why

We currently build `playermatchstats` from FotMob (see the `fotmob_name`
column in `teams.csv`), FPL for player/price data, and ClubElo for team
ratings. [Bzzoiro](https://sports.bzzoiro.com) is a free football API with an
OpenAPI 3 spec, 64 endpoints, and coverage claimed back to 2004. This
directory works out what — if anything — it is worth taking from it.

## Running the probe

### From GitHub Actions (easiest)

One-time setup: add a repository secret named `BZZOIRO_API_KEY` under
**Settings → Secrets and variables → Actions → New repository secret**.

Then **Actions → Bzzoiro API Probe → Run workflow**. The report is rendered
straight into the job summary, and the report plus raw JSON samples are
attached as a build artifact for 14 days.

The key has to be a *secret*, not a workflow input — input values are shown
in plaintext in the Actions UI and kept in the run metadata.

The workflow is read-only (`permissions: contents: read`) and commits
nothing. A run where no API call succeeds exits non-zero, so a bad key shows
up as a red run rather than a green empty report.

### Locally

```bash
export BZZOIRO_API_KEY=<key>
python3 scripts/bzzoiro/probe.py
```

Stdlib only, no `pip install`. Output goes to `bzzoiro_probe_out/`
(gitignored): a `REPORT.md` plus raw JSON samples.

```
--gameweek N        2025/26 gameweek to cross-check (default 10)
--skip-friendlies   skip the 2026/27 friendlies probe
--skip-last-season  skip the 2025/26 cross-check
--out DIR           output directory
```

The API key is read from the environment or `--key` and is never written to
disk. Do not commit it.

> The probe cannot run from a Claude Code web session — `sports.bzzoiro.com`
> is denied by the sandbox egress policy (403 on CONNECT). Use the Action, run
> it locally, or allowlist the host for the environment.

## What the probe answers

1. **Competition coverage** — is the Premier League there, and the FA Cup,
   EFL Cup, the three European competitions, and club friendlies? We publish
   all of them; the marketing only ever mentions leagues.
2. **Historical depth** — how far back the PL seasons go, and the `season_id`
   for 2025/26 and 2026/27.
3. **Fixture and player mapping** — what fraction of a repo gameweek pairs up,
   and whether scores agree (a cheap check that we paired the right games).
4. **Stat agreement** — mean absolute difference on the columns both sources
   carry. A wide gap on `xg`/`xa` means a different model upstream, so the two
   are not interchangeable per-column.
5. **Friendlies** — the gap worth closing. See below.
6. **Column coverage** — computed offline from the spec, so it is reported
   even with no network.

## What the spec already tells us

`PlayerStat` has 43 fields; our `playermatchstats.csv` has 64 columns. About
35 are reachable (26 direct, 9 derived). **27 are not.**

The expensive absence is **`blocks`**. FPL's defensive contribution threshold
is CBIT for defenders and CBIRT for midfielders and forwards — blocks is a
term in both — so DefCon cannot be rebuilt from this source. Also missing:
7 of our 8 goalkeeping columns (`goals_prevented`, `xgot_faced`,
`sweeper_actions`, `high_claim`, `saves_inside_box`, `gk_accurate_passes`,
`gk_accurate_long_balls`), every physical metric (`distance_covered`,
`top_speed`, sprint splits), and `xgot`, `big_chances_missed`,
`touches_opposition_box`, `final_third_passes`, `offsides`, `corners`.

So it is **not a replacement** for the FotMob-derived match stats. The case
for it rests on data we have no table for at all:

| Endpoint | Gives us |
|---|---|
| `/events/{id}/stats/` | `shotmap` — per-shot xG **and** xGOT, pitch coordinates, goalmouth placement, and a player id (`pid`) |
| | `momentum`, `xg_per_minute`, `average_positions` |
| `/events/{id}/lineups/` | confirmed XI ~1h pre-KO, or an AI-predicted XI with a confidence score |
| `/events/{id}/` | `unavailable_players`, referee card rates, weather, travel distance |
| `/players/{id}/` | DOB, height, market value, contract end, injury return date |

## The friendlies case

This is the strongest argument for a second source anywhere in the repo.
`data/2026-2027/By Tournament/Friendlies/GW0` currently holds **91 matches,
of which 5 have any player stats and 3 have team xG** — 86 matches with
nothing. The README already concedes friendly coverage "depends on what gets
published for each fixture".

If Bzzoiro covers pre-season friendlies for PL clubs, it fills a near-empty
table rather than competing with a full one, and the `blocks`/GK/physical
gaps cost nothing because we have no values there to lose. The probe measures
exactly this.

## Open questions before anything is wired in

- **Licensing.** The spec documents `websocket_plus` as *"True when Opta POEM
  pitch-level tracking is available"*, and `sr_stats` returns `ball_safe` /
  `attack` / `dangerous_attack` — Sportradar's signature counters. A free,
  unlimited API serving data derived from two commercial feeds raises a
  redistribution question, and this repo republishes onward under an open-data
  banner. Worth resolving before it reaches a CSV.
- **Error semantics.** Every endpoint in the spec documents only a `200`. No
  error schemas, no `429`, no documented rate limit or retry behaviour, and no
  `updated_at`/ETag on most resources for change detection. `probe.py` treats
  anything non-200 as retry-then-give-up.
- **ID stability.** Player and team ids are the provider's own — no FPL, Opta
  or Transfermarkt id is exposed — so mapping is name-based. `mapping.py`
  carries a hand-checked club alias table because generic normalisation
  collapses "Man City" and "Man Utd" onto the same token.

## Files

- `probe.py` — the read-only probe described above.
- `mapping.py` — field mapping, coverage constants, and the club alias table.
