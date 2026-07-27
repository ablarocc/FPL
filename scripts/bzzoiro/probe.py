#!/usr/bin/env python3
"""Read-only probe of the Bzzoiro Sports Data API against this repo's data.

Answers the questions that decide whether the source is worth wiring in:

  1. Which competitions are covered? (Premier League, domestic cups, Europe,
     club friendlies)
  2. How far back do seasons go — is 2025/26 retrievable?
  3. Do their fixtures and players map onto ours, and at what hit rate?
  4. Where their stats overlap ours, do the numbers agree?
  5. Do they cover the pre-season friendlies our own pipeline mostly misses?

Nothing is written to the repo's data/ tree. Output is a report plus raw
JSON samples in --out (gitignored), for eyeballing before anything is built.

Usage:
    export BZZOIRO_API_KEY=...
    python3 scripts/bzzoiro/probe.py

    # or narrow it down
    python3 scripts/bzzoiro/probe.py --skip-friendlies --gameweek 12

Stdlib only — no pip install required.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mapping  # noqa: E402

BASE_URL = "https://sports.bzzoiro.com"
REPO_ROOT = Path(__file__).resolve().parents[2]

# Competitions we care about, and the loose patterns that identify them in
# whatever naming convention the provider happens to use.
COMPETITIONS_OF_INTEREST = {
    "Premier League": r"premier league",
    "FA Cup": r"\bfa cup\b",
    "EFL Cup": r"(efl|league|carabao) cup",
    "Champions League": r"champions league",
    "Europa League": r"europa league",
    "Conference League": r"conference league",
    "Community Shield": r"community shield",
    "Friendlies": r"friendl",
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Api:
    def __init__(self, key: str, base: str = BASE_URL, delay: float = 0.15):
        self.key = key
        self.base = base.rstrip("/")
        self.delay = delay
        self.calls = 0
        self.ok_calls = 0          # calls that actually returned JSON
        self.dead = False          # circuit breaker: host unreachable
        self.transport_fails = 0
        self.ctx = ssl.create_default_context()
        bundle = os.environ.get("SSL_CERT_FILE") or "/root/.ccr/ca-bundle.crt"
        if os.path.exists(bundle):
            try:
                self.ctx.load_verify_locations(bundle)
            except Exception:
                pass

    def get(self, path: str, retries: int = 3, **params):
        """GET a path. Returns parsed JSON, or None on a handled failure.

        The spec documents only 200 responses, so error semantics are
        unknown — treat anything else as retryable-then-give-up and record it.
        """
        if self.dead:
            return None
        qs = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base}{path}"
        if qs:
            url += "?" + urllib.parse.urlencode(qs)

        for attempt in range(retries):
            req = urllib.request.Request(url, headers={
                "Authorization": f"Token {self.key}",
                "Accept": "application/json",
                "User-Agent": "FPL-Core-Insights-probe/1.0",
            })
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=45, context=self.ctx) as r:
                    self.transport_fails = 0
                    self.ok_calls += 1
                    time.sleep(self.delay)
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                if e.code in (401, 403):
                    warn(f"HTTP {e.code} on {path} — auth or policy denial. {body}")
                    return None
                if e.code == 404:
                    return None
                warn(f"HTTP {e.code} on {path} (attempt {attempt + 1}/{retries}). {body}")
            except Exception as e:
                warn(f"{type(e).__name__} on {path} (attempt {attempt + 1}/{retries}): {e}")
                self.transport_fails += 1
                # Nothing has ever succeeded and the transport keeps failing:
                # the host is unreachable (egress policy, DNS, offline). Stop
                # rather than burning minutes of backoff on every later call.
                if self.transport_fails >= retries and self.calls <= retries:
                    self.dead = True
                    warn("host unreachable — skipping all remaining API calls")
                    return None
            time.sleep(2 ** attempt)
        return None

    def paged(self, path: str, cap: int = 1000, **params):
        """Follow limit/offset pagination, returning a flat list."""
        out, offset, limit = [], 0, min(200, cap)
        while len(out) < cap:
            data = self.get(path, limit=limit, offset=offset, **params)
            if data is None:
                break
            rows = data.get("results", data) if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows:
                break
            out.extend(rows)
            if isinstance(data, dict) and not data.get("next"):
                break
            if len(rows) < limit:
                break
            offset += limit
        return out[:cap]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

REPORT: list[str] = []


def say(line: str = ""):
    print(line)
    REPORT.append(line)


def warn(line: str):
    print(f"  ! {line}", file=sys.stderr)


def norm(s: str) -> str:
    """Fold accents and punctuation. Deliberately conservative — see
    mapping.resolve_team_name for why team names get a lookup table instead."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def surname(name: str) -> str:
    n = norm(name)
    return n.split()[-1] if n else ""


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        if v in ("", None, "None"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def dump(out: Path, name: str, obj):
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(obj, indent=1)[:4_000_000], encoding="utf-8")


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

def probe_leagues(api: Api, out: Path):
    say("## 1. Competition coverage\n")
    leagues = api.paged("/api/v2/leagues/", cap=1000)
    if not leagues:
        say("No leagues returned — auth failed or the endpoint is unreachable.\n")
        return {}
    dump(out, "leagues.json", leagues)
    say(f"`/api/v2/leagues/` returned **{len(leagues)}** competitions.\n")

    found = {}
    say("| Competition we publish | Found | Match |")
    say("|---|---|---|")
    for label, pattern in COMPETITIONS_OF_INTEREST.items():
        hits = [lg for lg in leagues if re.search(pattern, str(lg.get("name", "")), re.I)]
        # Prefer English competitions where the name is ambiguous.
        eng = [h for h in hits if str(h.get("country_code", "")).upper() in ("GB", "EN", "")]
        pick = (eng or hits)[0] if hits else None
        if pick:
            found[label] = pick
            say(f"| {label} | yes | `{pick['id']}` {pick.get('name')} "
                f"({pick.get('country') or '-'}) |")
        else:
            say(f"| {label} | **no** | - |")
    say("")
    return found


def probe_seasons(api: Api, league: dict, out: Path):
    say("## 2. Historical depth (Premier League)\n")
    seasons = api.get(f"/api/v2/leagues/{league['id']}/seasons/")
    rows = seasons.get("results", seasons) if isinstance(seasons, dict) else seasons
    if not rows:
        say("No seasons returned.\n")
        return {}
    dump(out, "pl_seasons.json", rows)
    say(f"**{len(rows)}** seasons on file. Newest 8:\n")
    say("| season_id | name | year | current |")
    say("|---|---|---|---|")
    for s in rows[:8]:
        say(f"| `{s.get('id')}` | {s.get('name')} | {s.get('year')} | "
            f"{s.get('is_current')} |")
    say("")

    wanted = {}
    for s in rows:
        tag = f"{s.get('year') or ''} {s.get('name') or ''}"
        if re.search(r"25.?26|2025", tag):
            wanted.setdefault("2025-2026", s)
        if re.search(r"26.?27|2026", tag):
            wanted.setdefault("2026-2027", s)
    for k, v in wanted.items():
        say(f"- Resolved **{k}** -> season_id `{v.get('id')}` ({v.get('name')})")
    if not wanted:
        say("- Could not auto-resolve 2025/26 or 2026/27 — check `pl_seasons.json`.")
    say("")
    return wanted


def probe_last_season(api: Api, league: dict, season: dict, gameweek: int, out: Path):
    """Match one repo gameweek against the API, field by field."""
    say(f"## 3. Last season cross-check (2025/26, GW{gameweek})\n")

    repo_dir = REPO_ROOT / "data/2025-2026/By Tournament/Premier League" / f"GW{gameweek}"
    repo_matches = read_csv(repo_dir / "matches.csv")
    repo_pms = read_csv(repo_dir / "playermatchstats.csv")
    repo_players = {r["player_id"]: r for r in read_csv(REPO_ROOT / "data/2025-2026/players.csv")}
    repo_teams = {r["id"]: r for r in read_csv(REPO_ROOT / "data/2025-2026/teams.csv")}
    if not repo_matches:
        say(f"No repo data at `{repo_dir}` — pick another `--gameweek`.\n")
        return
    say(f"Repo GW{gameweek}: **{len(repo_matches)}** matches, "
        f"**{len(repo_pms)}** player rows.\n")

    events = api.paged("/api/v2/events/", cap=1000,
                       league_id=league["id"], season_id=season.get("id"))
    if not events:
        say("No events returned for that season.\n")
        return
    dump(out, "pl_2025_26_events.json", events[:50])
    say(f"API returned **{len(events)}** events for season `{season.get('id')}`.\n")

    # --- fixture matching: resolve both sides onto FPL team names first
    by_pair, unresolved_provider = {}, set()
    for e in events:
        raw_h = e.get("home_team") or (e.get("home_team_obj") or {}).get("name", "")
        raw_a = e.get("away_team") or (e.get("away_team_obj") or {}).get("name", "")
        h = mapping.resolve_team_name(raw_h)
        a = mapping.resolve_team_name(raw_a)
        if h and a:
            by_pair[(h, a)] = e
        else:
            unresolved_provider.update(x for x, y in ((raw_h, h), (raw_a, a)) if not y)

    if unresolved_provider:
        say(f"_Provider team names not in the alias table: "
            f"{', '.join(sorted(unresolved_provider)[:10])}_\n")

    paired, unpaired = [], []
    for m in repo_matches:
        ht = repo_teams.get(str(m.get("home_team", "")).split(".")[0], {})
        at = repo_teams.get(str(m.get("away_team", "")).split(".")[0], {})
        key = (ht.get("name"), at.get("name"))
        if key in by_pair:
            paired.append((m, by_pair[key]))
        else:
            unpaired.append((m, ht.get("name"), at.get("name")))

    say(f"**Fixture mapping:** {len(paired)}/{len(repo_matches)} matched on team names.")
    for m, h, a in unpaired[:5]:
        say(f"  - unmatched: {h} v {a}")
    say("")
    if not paired:
        return

    # --- score agreement, a cheap sanity check that we paired the right games
    score_ok = sum(
        1 for m, e in paired
        if num(m.get("home_score")) == num(e.get("home_score"))
        and num(m.get("away_score")) == num(e.get("away_score"))
    )
    say(f"**Score agreement:** {score_ok}/{len(paired)} paired fixtures agree "
        f"on the final score.\n")

    # --- player stats for a sample of those fixtures
    sample = paired[:5]
    repo_by_match = defaultdict(list)
    for r in repo_pms:
        repo_by_match[r["match_id"]].append(r)

    name_hits = name_total = 0
    deltas = defaultdict(list)
    compare_cols = [("expected_goals", "xg"), ("expected_assists", "xa"),
                    ("minutes_played", "minutes_played"), ("total_shots", "total_shots"),
                    ("touches", "touches"), ("goals", "goals")]

    for m, e in sample:
        stats = api.get(f"/api/v2/events/{e['id']}/player-stats/")
        rows = stats.get("results", stats) if isinstance(stats, dict) else stats
        if not rows:
            continue
        dump(out, f"player_stats_event_{e['id']}.json", rows[:5])

        ours = repo_by_match.get(m["match_id"], [])
        ours_by_surname = {}
        for r in ours:
            p = repo_players.get(r["player_id"], {})
            full = f"{p.get('first_name', '')} {p.get('second_name', '')}"
            ours_by_surname.setdefault(surname(full) or surname(p.get("web_name", "")), r)

        for row in rows:
            pl = row.get("player") or {}
            name_total += 1
            mine = ours_by_surname.get(surname(pl.get("name", "")))
            if not mine:
                continue
            name_hits += 1
            for bz, repo_col in compare_cols:
                a, b = num(row.get(bz)), num(mine.get(repo_col))
                if a is not None and b is not None:
                    deltas[repo_col].append(abs(a - b))

    say(f"**Player mapping:** {name_hits}/{name_total} of their player rows "
        f"resolved to an FPL player_id by surname across {len(sample)} matches.")
    say("_(Surname-only matching — a production mapper needs first name + team + "
        "a hand-checked override table.)_\n")

    if deltas:
        say("**Stat agreement on shared columns** (mean absolute difference):\n")
        say("| column | n | mean abs diff | exact |")
        say("|---|---|---|---|")
        for col, vals in deltas.items():
            exact = sum(1 for v in vals if v < 1e-6)
            say(f"| {col} | {len(vals)} | {sum(vals) / len(vals):.3f} | "
                f"{exact}/{len(vals)} |")
        say("")
        say("A large gap on `xg`/`xa` means a different model upstream — the two "
            "sources would not be safely interchangeable per-column.\n")

    # --- shotmap, the thing we have no table for
    ev = sample[0][1]
    full = api.get(f"/api/v2/events/{ev['id']}/stats/")
    shots = (full or {}).get("shotmap")
    if shots:
        dump(out, f"shotmap_event_{ev['id']}.json", shots)
        say(f"**Shot map:** {len(shots)} shots on event `{ev['id']}`. "
            f"Sample shot:\n")
        say("```json")
        say(json.dumps(shots[0], indent=1))
        say("```\n")
        with_pid = sum(1 for s in shots if s.get("pid"))
        say(f"{with_pid}/{len(shots)} shots carry a player id (`pid`) — that is "
            f"what makes shot-level data joinable to FPL players.\n")
    else:
        say("**Shot map:** not returned by `/events/{id}/stats/` for the sampled "
            "event (the spec notes it may need `?full=true` on some routes).\n")


def probe_friendlies(api: Api, out: Path, friendly_league: dict | None):
    """The strongest case for a second source: our friendlies are near-empty."""
    say("## 4. Pre-season friendlies (2026/27 GW0)\n")

    gw0 = REPO_ROOT / "data/2026-2027/By Tournament/Friendlies/GW0"
    repo_matches = read_csv(gw0 / "matches.csv")
    repo_pms = read_csv(gw0 / "playermatchstats.csv")
    with_stats = {r["match_id"] for r in repo_pms}
    with_xg = [m for m in repo_matches
               if num(m.get("home_expected_goals_xg")) is not None]
    say(f"Repo today: **{len(repo_matches)}** friendlies, "
        f"**{len(with_stats)}** with any player stats, "
        f"**{len(with_xg)}** with team xG.")
    say(f"That is the gap worth closing — {len(repo_matches) - len(with_stats)} "
        f"matches carry no player data at all.\n")

    teams = read_csv(REPO_ROOT / "data/2026-2027/teams.csv")
    say(f"Resolving {len(teams)} PL teams to API team ids...\n")

    resolved, unresolved = {}, []
    for t in teams:
        # Search on the longest alias — "Manchester United" finds more than
        # "Man Utd" does on a provider using full club names.
        aliases = mapping.TEAM_ALIASES.get(t["name"], [t["name"]])
        pick = None
        for query in sorted(aliases, key=len, reverse=True):
            hits = api.get("/api/v2/teams/", name=query, limit=20)
            rows = hits.get("results", hits) if isinstance(hits, dict) else (hits or [])
            pick = next((r for r in rows
                         if mapping.resolve_team_name(r.get("name", "")) == t["name"]),
                        None)
            if pick:
                break
        if pick:
            resolved[t["id"]] = pick
        else:
            unresolved.append(t["name"])
    say(f"**Team mapping:** {len(resolved)}/{len(teams)} resolved.")
    if unresolved:
        say(f"Unresolved: {', '.join(unresolved)}")
    say("")

    # Pre-season window: friendlies cluster from mid-June to mid-August.
    found, with_player_stats, samples = [], 0, []
    for repo_id, team in list(resolved.items()):
        fx = api.get(f"/api/v2/teams/{team['id']}/fixtures/",
                     date_from="2026-06-15T00:00:00Z",
                     date_to="2026-08-16T00:00:00Z", limit=100)
        rows = fx.get("results", fx) if isinstance(fx, dict) else (fx or [])
        for e in rows:
            lg = str(e.get("league") or e.get("league_name") or "")
            if friendly_league and str(e.get("league_id")) == str(friendly_league["id"]):
                found.append(e)
            elif re.search(r"friendl", lg, re.I):
                found.append(e)

    uniq = {e["id"]: e for e in found}
    say(f"**Friendlies found via `/teams/{{id}}/fixtures/`:** {len(uniq)} unique "
        f"matches involving PL clubs in the 15 Jun - 16 Aug window.\n")

    for e in list(uniq.values())[:12]:
        stats = api.get(f"/api/v2/events/{e['id']}/player-stats/")
        rows = stats.get("results", stats) if isinstance(stats, dict) else stats
        if rows:
            with_player_stats += 1
            samples.append({"event": e.get("id"),
                            "home": e.get("home_team"), "away": e.get("away_team"),
                            "date": e.get("event_date"), "player_rows": len(rows)})
    dump(out, "friendlies_sample.json",
         {"found": list(uniq.values())[:40], "with_stats": samples})

    checked = min(12, len(uniq))
    say(f"**Player-stat coverage:** {with_player_stats}/{checked} sampled "
        f"friendlies returned player rows.\n")
    if samples:
        say("| event | fixture | date | player rows |")
        say("|---|---|---|---|")
        for s in samples:
            say(f"| `{s['event']}` | {s['home']} v {s['away']} | "
                f"{(s['date'] or '')[:10]} | {s['player_rows']} |")
        say("")


def report_static_coverage():
    """Schema-level verdict — true regardless of what the network says."""
    say("## 5. Column coverage vs our schema (from the OpenAPI spec)\n")
    c = mapping.coverage_summary()
    say(f"- `playermatchstats.csv` has **{c['repo_columns']}** columns.")
    say(f"- Bzzoiro fills **{c['direct']}** directly and **{c['derived']}** by "
        f"derivation = **{c['covered']}**.")
    say(f"- **{c['unavailable']}** of our columns have no equivalent.")
    say(f"- **{c['new_fields']}** of their fields are new to us.\n")
    say("Missing, grouped by why it hurts:\n")
    say("- **`blocks`** — FPL defensive contribution is CBIT (def) / CBIRT "
        "(mid+fwd); blocks is a term in both, so DefCon cannot be rebuilt "
        "from this source.")
    say("- **Goalkeeping** — `xgot_faced`, `goals_prevented`, `sweeper_actions`, "
        "`high_claim`, `saves_inside_box`, `gk_accurate_passes`, "
        "`gk_accurate_long_balls` (7 of our 8 GK columns).")
    say("- **Physical** — `top_speed`, `distance_covered`, "
        "`walking/running/sprinting_distance`, `number_of_sprints`.")
    say("- **Attacking detail** — `xgot`, `big_chances_missed`, "
        "`touches_opposition_box`, `final_third_passes`, `offsides`, `corners`.\n")
    say("New tables it could support that we have nothing for:\n")
    for k, v in mapping.EVENT_NEW_TABLES.items():
        say(f"- `{k}` — {v}")
    say("")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default=os.environ.get("BZZOIRO_API_KEY"),
                    help="API key (default: $BZZOIRO_API_KEY)")
    ap.add_argument("--out", default="bzzoiro_probe_out",
                    help="output directory for the report and JSON samples")
    ap.add_argument("--gameweek", type=int, default=10,
                    help="2025/26 gameweek to cross-check (default 10)")
    ap.add_argument("--skip-friendlies", action="store_true")
    ap.add_argument("--skip-last-season", action="store_true")
    args = ap.parse_args()

    if not args.key:
        sys.exit("No API key. Pass --key or set BZZOIRO_API_KEY.")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    api = Api(args.key)

    say("# Bzzoiro Sports Data — integration probe\n")
    say(f"Base: `{BASE_URL}` · repo: `{REPO_ROOT.name}`\n")

    report_static_coverage()

    leagues = probe_leagues(api, out)
    pl = leagues.get("Premier League")
    if pl:
        seasons = probe_seasons(api, pl, out)
        if seasons.get("2025-2026") and not args.skip_last_season:
            probe_last_season(api, pl, seasons["2025-2026"], args.gameweek, out)
    else:
        say("Premier League not found — skipping season and cross-check probes.\n")

    if not args.skip_friendlies:
        probe_friendlies(api, out, leagues.get("Friendlies"))

    say(f"---\n\n{api.ok_calls}/{api.calls} API calls succeeded. "
        f"Samples in `{out}/`.")
    (out / "REPORT.md").write_text("\n".join(REPORT), encoding="utf-8")
    print(f"\nReport written to {out / 'REPORT.md'}", file=sys.stderr)

    # Nothing came back at all — bad key, or the host is unreachable. Exit
    # non-zero so CI shows this as a failure rather than a green empty report.
    if api.ok_calls == 0:
        print("No API call succeeded — check the key and network reachability.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
